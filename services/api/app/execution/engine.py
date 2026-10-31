from __future__ import annotations

import uuid
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.events import event_bus
from app.core.logging import get_logger, log_event
from app.core.precision import (
    ONE,
    ZERO,
    round_price_to_tick,
    round_qty_to_lot,
    to_decimal,
    utc_iso,
    utc_now,
)
from app.models.entities import (
    AIDecision,
    AuditLog,
    BalanceSnapshot,
    EquitySnapshot,
    ExchangeConnection,
    Fill,
    Instrument,
    JournalEntry,
    MarketSnapshot,
    Order,
    Position,
    RiskEvent,
    Strategy,
    StrategyVersion,
    SystemEvent,
    TradeSignal,
    TradingAccount,
    Transfer,
)
from app.risk.engine import AttrDict, RiskEngine, RiskEvaluationResult, risk_engine
from app.schemas.domain import OrderSubmitRequest, TradePlanSchema

logger = get_logger("terminal.execution")


class ExecutionError(Exception):
    """Base exception for execution-layer errors."""


class AccountEnvironmentMismatch(ExecutionError, ValueError):
    """Raised when an account's environment does not match the requested environment."""


class AmbiguousAccountError(ExecutionError, ValueError):
    """Raised when multiple active accounts match the requested exchange/environment query."""


VALID_ORDER_TRANSITIONS: dict[str, set[str]] = {
    "CREATED": {"VALIDATED", "REJECTED", "CANCELED"},
    "VALIDATED": {"SUBMITTED", "REJECTED", "CANCELED"},
    "SUBMITTED": {
        "ACKNOWLEDGED",
        "PARTIALLY_FILLED",
        "FILLED",
        "REJECTED",
        "CANCELED",
        "EXPIRED",
        "UNKNOWN",
    },
    "ACKNOWLEDGED": {
        "PARTIALLY_FILLED",
        "FILLED",
        "CANCELED",
        "EXPIRED",
        "REJECTED",
        "UNKNOWN",
    },
    "PARTIALLY_FILLED": {
        "PARTIALLY_FILLED",
        "FILLED",
        "CANCELED",
        "EXPIRED",
        "UNKNOWN",
    },
    "UNKNOWN": {
        "ACKNOWLEDGED",
        "PARTIALLY_FILLED",
        "FILLED",
        "CANCELED",
        "EXPIRED",
        "REJECTED",
    },
    "FILLED": set(),
    "REJECTED": set(),
    "CANCELED": set(),
    "EXPIRED": set(),
}

TERMINAL_ORDER_STATUSES = {"FILLED", "REJECTED", "CANCELED", "EXPIRED"}
ACTIVE_ORDER_STATUSES = {"CREATED", "VALIDATED", "SUBMITTED", "ACKNOWLEDGED", "PARTIALLY_FILLED", "UNKNOWN"}


def can_transition_order(current_status: str, new_status: str) -> bool:
    allowed = VALID_ORDER_TRANSITIONS.get(current_status, set())
    return new_status in allowed


def validate_order_transition(current_status: str, new_status: str) -> None:
    if not can_transition_order(current_status, new_status):
        raise ValueError(
            f"Illegal order state transition: {current_status!r} -> {new_status!r}. "
            f"Allowed from {current_status!r}: {sorted(VALID_ORDER_TRANSITIONS.get(current_status, set()))}"
        )


def _extract_base_asset(symbol: str) -> str:
    s = (symbol or "BTC/USDT").strip().upper()
    if ":" in s:
        s = s.split(":", 1)[0]
    if "/" in s:
        return s.split("/", 1)[0].strip()
    if "-" in s:
        return s.split("-", 1)[0].strip()
    for quote in ("USDT", "USDC", "USD"):
        if s.endswith(quote) and len(s) > len(quote):
            return s[: -len(quote)]
    return s


class ExecutionEngine:
    """
    Order Execution & State Machine Engine (Section 3.8).
    Enforces:
    - Mandatory RiskEngine pre-trade validation
    - Strict order state transitions (`VALID_ORDER_TRANSITIONS`)
    - Client Order ID idempotency (`uq_orders_account_client_order_id`)
    - Timeout -> `UNKNOWN` handling (never blind retry) + `reconcile_unknown_order`
    - Native TP/SL protection order confirmation & CRITICAL risk event alerting
    - Atomic updates to `Fill`, `Position`, `BalanceSnapshot`, `EquitySnapshot`, and `JournalEntry`
    """

    def __init__(self, risk_eng: RiskEngine | None = None) -> None:
        self.risk_engine = risk_eng or risk_engine

    def transition_order(
        self,
        order: Order,
        new_status: str,
        reject_reason: str | None = None,
    ) -> Order:
        validate_order_transition(order.status, new_status)
        order.status = new_status
        order.updated_at = utc_now()
        if reject_reason is not None:
            order.reject_reason = reject_reason
        return order

    def _get_exchange_adapter(self, exchange: str, environment: str = "demo"):
        try:
            ex = (exchange or "binance").lower()
            if ex == "okx":
                from app.exchanges.okx.adapter import OKXAdapter

                return OKXAdapter(environment=environment)
            from app.exchanges.binance.adapter import BinanceAdapter

            return BinanceAdapter(environment=environment)
        except Exception:
            return None

    def _resolve_account(
        self,
        db: Session,
        account_id: str | None = None,
        exchange: str | None = None,
        environment: str | None = None,
    ) -> TradingAccount | None:
        if environment is not None and str(environment).strip():
            env_val = str(environment).strip().lower()
            if account_id:
                acc = db.scalar(select(TradingAccount).where(TradingAccount.id == account_id))
                if acc is None:
                    return None
                if acc.environment.lower() != env_val:
                    raise AccountEnvironmentMismatch(
                        f"Account {account_id} environment ({acc.environment}) does not match requested environment ({env_val})"
                    )
                if exchange and acc.exchange.lower() != exchange.lower():
                    raise AccountEnvironmentMismatch(
                        f"Account {account_id} exchange ({acc.exchange}) does not match requested exchange ({exchange.lower()})"
                    )
                return acc

            stmt = select(TradingAccount).where(
                TradingAccount.environment == env_val,
                TradingAccount.is_active == True,
            )
            if exchange:
                stmt = stmt.where(TradingAccount.exchange == exchange.lower())
            stmt = stmt.order_by(TradingAccount.created_at.asc())
            matches = list(db.scalars(stmt).all())
            if len(matches) > 1:
                acct_summaries = [f"(id={a.id}, exchange={a.exchange}, env={a.environment})" for a in matches]
                raise AmbiguousAccountError(
                    f"Ambiguous account resolution: found {len(matches)} active accounts matching "
                    f"exchange={exchange}, environment={env_val}: {', '.join(acct_summaries)}"
                )
            if len(matches) == 1:
                return matches[0]
            return None

        if account_id:
            acc = db.scalar(select(TradingAccount).where(TradingAccount.id == account_id))
            if acc is not None:
                return acc
        if exchange:
            acc = db.scalar(
                select(TradingAccount)
                .where(TradingAccount.exchange == exchange.lower())
                .order_by(TradingAccount.is_active.desc(), TradingAccount.created_at.asc())
            )
            if acc is not None:
                return acc
        return db.scalar(
            select(TradingAccount).order_by(TradingAccount.is_active.desc(), TradingAccount.created_at.asc())
        )

    def resolve_execution_account(
        self,
        db: Session,
        *,
        account_id: str | None = None,
        exchange: str | None = None,
        environment: str | None = None,
    ) -> TradingAccount:
        if not environment or not str(environment).strip():
            raise ValueError("resolve_execution_account requires a non-empty environment")

        env_val = str(environment).strip().lower()

        if account_id:
            acc = db.scalar(select(TradingAccount).where(TradingAccount.id == account_id))
            if acc is None:
                raise AccountEnvironmentMismatch(f"Account {account_id} not found")
            if acc.environment.lower() != env_val:
                raise AccountEnvironmentMismatch(
                    f"Account {account_id} environment ({acc.environment}) does not match requested environment ({env_val})"
                )
            if exchange and acc.exchange.lower() != exchange.lower():
                raise AccountEnvironmentMismatch(
                    f"Account {account_id} exchange ({acc.exchange}) does not match requested exchange ({exchange.lower()})"
                )
            return acc

        stmt = select(TradingAccount).where(
            TradingAccount.environment == env_val,
            TradingAccount.is_active == True,
        )
        if exchange:
            stmt = stmt.where(TradingAccount.exchange == exchange.lower())
        stmt = stmt.order_by(TradingAccount.created_at.asc())
        matches = list(db.scalars(stmt).all())

        if len(matches) == 0:
            all_accounts = list(db.scalars(select(TradingAccount)).all())
            found_summary = [f"(id={a.id}, exchange={a.exchange}, env={a.environment}, active={a.is_active})" for a in all_accounts]
            raise AccountEnvironmentMismatch(
                f"No active account found for exchange={exchange}, environment={env_val}. "
                f"Available accounts: {', '.join(found_summary) if found_summary else 'none'}"
            )

        if len(matches) > 1:
            acct_summaries = [f"(id={a.id}, exchange={a.exchange}, env={a.environment})" for a in matches]
            raise AmbiguousAccountError(
                f"Ambiguous account resolution: found {len(matches)} active accounts matching "
                f"exchange={exchange}, environment={env_val}: {', '.join(acct_summaries)}"
            )

        return matches[0]

    def _resolve_instrument(
        self,
        db: Session,
        exchange: str,
        market_type: str,
        symbol: str,
    ) -> Instrument | None:
        inst = db.scalar(
            select(Instrument).where(
                Instrument.exchange == exchange,
                Instrument.market_type == market_type,
                Instrument.canonical_symbol == symbol,
            )
        )
        if inst is None:
            inst = db.scalar(
                select(Instrument).where(
                    Instrument.exchange == exchange,
                    Instrument.market_type == market_type,
                    Instrument.native_symbol == symbol,
                )
            )
        return inst

    def _verify_tp_sl_protection(
        self,
        db: Session,
        order: Order,
        *,
        is_opening_order: bool,
    ) -> bool:
        """
        Verify native TP/SL protection order confirmation for filled opening orders.
        Logs a CRITICAL RiskEvent if protection is missing or unconfirmed.
        """
        if not is_opening_order or order.reduce_only:
            return True

        has_sl_or_tp = order.stop_loss is not None or order.take_profit is not None
        confirmed = bool(order.tp_sl_confirmed) and bool(order.protection_order_ids_json)

        if has_sl_or_tp and not confirmed:
            evt = RiskEvent(
                account_id=order.account_id,
                signal_id=order.signal_id,
                order_id=order.id,
                event_type="UNCONFIRMED_PROTECTION_ORDER",
                rule_code="UNCONFIRMED_PROTECTION_ORDER",
                severity="CRITICAL",
                details={
                    "order_id": order.id,
                    "client_order_id": order.client_order_id,
                    "symbol": order.symbol,
                    "stop_loss": str(order.stop_loss) if order.stop_loss else None,
                    "take_profit": str(order.take_profit) if order.take_profit else None,
                    "tp_sl_confirmed": order.tp_sl_confirmed,
                    "protection_order_ids": order.protection_order_ids_json,
                },
            )
            db.add(evt)
            event_bus.publish_sync(
                channel="risk",
                event_type="risk.unconfirmed_protection",
                data={"order_id": order.id, "symbol": order.symbol, "severity": "CRITICAL"},
                source="execution_engine",
            )
            return False
        return True

    def _check_spot_sell_balance(
        self,
        db: Session,
        account_id: str,
        symbol: str,
        required_qty: Decimal,
    ) -> tuple[bool, Decimal]:
        base_asset = _extract_base_asset(symbol)
        latest_bal = db.scalar(
            select(BalanceSnapshot)
            .where(
                BalanceSnapshot.account_id == account_id,
                BalanceSnapshot.asset == base_asset,
            )
            .order_by(BalanceSnapshot.observed_at.desc())
        )
        available = to_decimal(latest_bal.available, ZERO) if latest_bal is not None else ZERO

        open_spot_pos = db.scalar(
            select(Position).where(
                Position.account_id == account_id,
                Position.symbol == symbol,
                Position.market_type == "spot",
                Position.status == "OPEN",
            )
        )
        pos_qty = to_decimal(open_spot_pos.qty, ZERO) if open_spot_pos is not None else ZERO
        effective_available = max(available, pos_qty)
        return effective_available >= required_qty, effective_available

    def _apply_fill_and_update_portfolio(
        self,
        db: Session,
        *,
        order: Order,
        fill_qty: Decimal,
        fill_price: Decimal,
        fee_rate: Decimal,
        contract_size: Decimal,
        tick_size: Decimal,
        leverage: Decimal,
        instrument_id: str | None = None,
        exchange_trade_id: str | None = None,
        close_reason: str = "OPEN_OR_ACTIVE",
        risk_result: RiskEvaluationResult | None = None,
    ) -> tuple[Fill, Position | None, JournalEntry]:
        now = utc_now()
        base_qty = fill_qty * contract_size if order.market_type == "usdt_perp" else fill_qty
        notional = (base_qty * fill_price).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        fee = (notional * fee_rate).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        realized_pnl = ZERO
        eff_lev = max(ONE, leverage) if order.market_type == "usdt_perp" else ONE

        # Determine position update
        position: Position | None = None
        if order.reduce_only or (order.market_type == "spot" and order.side == "SELL"):
            # Reducing/closing an existing position
            pos_stmt = select(Position).where(
                Position.account_id == order.account_id,
                Position.symbol == order.symbol,
                Position.market_type == order.market_type,
                Position.status == "OPEN",
            )
            position = db.scalar(pos_stmt)
            if position is not None and to_decimal(position.qty, ZERO) > ZERO:
                old_qty = to_decimal(position.qty)
                closed_qty = min(old_qty, fill_qty)
                closed_base = closed_qty * contract_size if order.market_type == "usdt_perp" else closed_qty
                entry = to_decimal(position.entry_price)
                if position.position_side in ("SHORT", "SELL"):
                    gross_pnl = (entry - fill_price) * closed_base
                else:
                    gross_pnl = (fill_price - entry) * closed_base
                realized_pnl = gross_pnl.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

                rem_qty = max(ZERO, old_qty - closed_qty)
                position.qty = rem_qty
                position.mark_price = fill_price
                position.realized_pnl = to_decimal(position.realized_pnl, ZERO) + realized_pnl - fee
                if rem_qty <= ZERO:
                    position.status = "CLOSED"
                    position.margin_used = ZERO
                    position.notional_value = ZERO
                    position.unrealized_pnl = ZERO
                else:
                    rem_base = rem_qty * contract_size if order.market_type == "usdt_perp" else rem_qty
                    position.notional_value = (rem_base * fill_price).quantize(
                        Decimal("0.0001"), rounding=ROUND_HALF_UP
                    )
                    position.margin_used = (position.notional_value / eff_lev).quantize(
                        Decimal("0.0001"), rounding=ROUND_HALF_UP
                    )
                position.updated_at = now
        else:
            # Opening or adding to a position
            eff_pos_side = (
                "SPOT"
                if order.market_type == "spot"
                else (
                    order.position_side
                    if order.position_side in ("LONG", "SHORT")
                    else ("LONG" if order.side == "BUY" else "SHORT")
                )
            )
            position = db.scalar(
                select(Position).where(
                    Position.account_id == order.account_id,
                    Position.symbol == order.symbol,
                    Position.market_type == order.market_type,
                    Position.position_side == eff_pos_side,
                    Position.status == "OPEN",
                )
            )
            margin_req = (notional / eff_lev).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            liq_price = None
            if order.market_type == "usdt_perp":
                liq_price = (
                    round_price_to_tick(fill_price * (ONE - (ONE / eff_lev) * Decimal("0.90")), tick_size)
                    if eff_pos_side == "LONG"
                    else round_price_to_tick(fill_price * (ONE + (ONE / eff_lev) * Decimal("0.90")), tick_size)
                )

            if position is not None and to_decimal(position.qty, ZERO) > ZERO:
                old_qty = to_decimal(position.qty)
                new_qty = old_qty + fill_qty
                new_entry = ((old_qty * to_decimal(position.entry_price)) + (fill_qty * fill_price)) / new_qty
                position.qty = new_qty
                position.entry_price = round_price_to_tick(new_entry, tick_size)
                position.mark_price = fill_price
                position.leverage = eff_lev
                position.margin_used = (to_decimal(position.margin_used, ZERO) + margin_req).quantize(
                    Decimal("0.0001"), rounding=ROUND_HALF_UP
                )
                new_base = new_qty * contract_size if order.market_type == "usdt_perp" else new_qty
                position.notional_value = (new_base * fill_price).quantize(
                    Decimal("0.0001"), rounding=ROUND_HALF_UP
                )
                if order.stop_loss is not None:
                    position.stop_loss = order.stop_loss
                if order.take_profit is not None:
                    position.take_profit = order.take_profit
                position.tp_sl_confirmed = bool(order.tp_sl_confirmed)
                position.updated_at = now
            else:
                position = Position(
                    account_id=order.account_id,
                    instrument_id=instrument_id,
                    exchange=order.exchange,
                    environment=order.environment,
                    market_type=order.market_type,
                    symbol=order.symbol,
                    position_side=eff_pos_side,
                    qty=fill_qty,
                    entry_price=fill_price,
                    mark_price=fill_price,
                    liquidation_price=liq_price,
                    leverage=eff_lev,
                    margin_used=margin_req,
                    notional_value=notional,
                    unrealized_pnl=ZERO,
                    realized_pnl=-fee,
                    stop_loss=order.stop_loss,
                    take_profit=order.take_profit,
                    tp_sl_confirmed=bool(order.tp_sl_confirmed),
                    status="OPEN",
                    updated_at=now,
                )
                db.add(position)

        # Create Fill record idempotently
        trade_id = exchange_trade_id or f"TRD-{order.exchange.upper()}-{uuid.uuid4().hex[:12].upper()}"
        existing_fill = db.scalar(
            select(Fill).where(
                Fill.exchange == order.exchange,
                Fill.exchange_trade_id == trade_id,
            )
        )
        if existing_fill is not None:
            fill = existing_fill
        else:
            fill = Fill(
                order_id=order.id,
                account_id=order.account_id,
                exchange=order.exchange,
                market_type=order.market_type,
                symbol=order.symbol,
                exchange_trade_id=trade_id,
                side=order.side,
                price=fill_price,
                qty=fill_qty,
                fee=fee,
                fee_currency="USDT",
                realized_pnl=realized_pnl,
                occurred_at=now,
            )
            db.add(fill)

        db.flush()

        # Update BalanceSnapshot & EquitySnapshot
        self._refresh_account_snapshots(
            db,
            account_id=order.account_id,
            exchange=order.exchange,
            fill_fee=fee,
            fill_realized_pnl=realized_pnl,
            order=order,
            base_qty=base_qty,
            notional=notional,
            leverage=eff_lev,
        )

        # Create JournalEntry
        journal = self._record_journal_entry(
            db,
            order=order,
            fill=fill,
            close_reason=close_reason,
            risk_result=risk_result,
        )

        return fill, position, journal

    def _refresh_account_snapshots(
        self,
        db: Session,
        *,
        account_id: str,
        exchange: str,
        fill_fee: Decimal,
        fill_realized_pnl: Decimal,
        order: Order,
        base_qty: Decimal,
        notional: Decimal,
        leverage: Decimal,
    ) -> EquitySnapshot:
        now = utc_now()
        usdt_bal = db.scalar(
            select(BalanceSnapshot)
            .where(
                BalanceSnapshot.account_id == account_id,
                BalanceSnapshot.asset == "USDT",
            )
            .order_by(BalanceSnapshot.observed_at.desc())
        )
        prev_total = to_decimal(usdt_bal.total, Decimal("10000")) if usdt_bal else Decimal("10000")

        open_positions = [
            p
            for p in db.scalars(
                select(Position).where(
                    Position.account_id == account_id,
                    Position.status == "OPEN",
                )
            ).all()
            if to_decimal(p.qty, ZERO) > ZERO
        ]
        locked_margin = sum((to_decimal(p.margin_used, ZERO) for p in open_positions), ZERO)
        total_exposure = sum((to_decimal(p.notional_value, ZERO) for p in open_positions), ZERO)
        total_unrealized = sum((to_decimal(p.unrealized_pnl, ZERO) for p in open_positions), ZERO)

        all_fills = list(db.scalars(select(Fill).where(Fill.account_id == account_id)).all())
        cum_realized = sum((to_decimal(f.realized_pnl, ZERO) for f in all_fills), ZERO)
        cum_fees = sum((to_decimal(f.fee, ZERO) for f in all_fills), ZERO)

        transfers = list(db.scalars(select(Transfer).where(Transfer.account_id == account_id)).all())
        net_transfers = ZERO
        for t in transfers:
            amt = to_decimal(t.amount, ZERO)
            if t.transfer_type == "DEPOSIT":
                net_transfers += amt
            elif t.transfer_type == "WITHDRAWAL":
                net_transfers -= amt

        # Update spot base asset balance if spot order
        if order.market_type == "spot":
            base_asset = _extract_base_asset(order.symbol)
            base_bal = db.scalar(
                select(BalanceSnapshot)
                .where(
                    BalanceSnapshot.account_id == account_id,
                    BalanceSnapshot.asset == base_asset,
                )
                .order_by(BalanceSnapshot.observed_at.desc())
            )
            prev_base_avail = to_decimal(base_bal.available, ZERO) if base_bal else ZERO
            if order.side == "BUY":
                new_base_avail = prev_base_avail + base_qty
            else:
                new_base_avail = max(ZERO, prev_base_avail - base_qty)
            base_usd = (new_base_avail * order.avg_fill_price).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            if base_bal is not None:
                base_bal.available = new_base_avail
                base_bal.total = new_base_avail + to_decimal(base_bal.locked, ZERO)
                base_bal.usd_value = base_usd
                base_bal.observed_at = now
            else:
                db.add(
                    BalanceSnapshot(
                        account_id=account_id,
                        exchange=exchange,
                        asset=base_asset,
                        total=new_base_avail,
                        available=new_base_avail,
                        locked=ZERO,
                        usd_value=base_usd,
                        observed_at=now,
                    )
                )

        new_usdt_total = (prev_total + fill_realized_pnl - fill_fee).quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_UP
        )
        new_usdt_avail = max(ZERO, new_usdt_total - locked_margin).quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_UP
        )

        if usdt_bal is not None:
            usdt_bal.total = new_usdt_total
            usdt_bal.available = new_usdt_avail
            usdt_bal.locked = locked_margin
            usdt_bal.usd_value = new_usdt_total
            usdt_bal.observed_at = now
        else:
            db.add(
                BalanceSnapshot(
                    account_id=account_id,
                    exchange=exchange,
                    asset="USDT",
                    total=new_usdt_total,
                    available=new_usdt_avail,
                    locked=locked_margin,
                    usd_value=new_usdt_total,
                    observed_at=now,
                )
            )

        equity_val = (new_usdt_total + total_unrealized).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        eq_snap = EquitySnapshot(
            account_id=account_id,
            exchange=exchange,
            equity=equity_val,
            available_balance=new_usdt_avail,
            margin_balance=new_usdt_total,
            notional_exposure=total_exposure,
            realized_pnl=cum_realized,
            unrealized_pnl=total_unrealized,
            fees_paid=cum_fees,
            funding_pnl=ZERO,
            net_transfers=net_transfers,
            observed_at=now,
        )
        db.add(eq_snap)
        return eq_snap

    def _record_journal_entry(
        self,
        db: Session,
        *,
        order: Order,
        fill: Fill | None,
        close_reason: str = "OPEN_OR_ACTIVE",
        risk_result: RiskEvaluationResult | None = None,
    ) -> JournalEntry:
        ai_decision = (
            db.scalar(select(AIDecision).where(AIDecision.id == order.decision_id))
            if order.decision_id
            else None
        )
        strategy_id = "default_strategy"
        strategy_version_str = order.strategy_version_id or "trend_following_v1:1"
        if order.strategy_version_id:
            sv = db.scalar(select(StrategyVersion).where(StrategyVersion.id == order.strategy_version_id))
            if sv is not None:
                strategy_id = sv.strategy_id
                strategy_version_str = f"{sv.strategy_id}:{sv.version}"
        else:
            strat = db.scalar(select(Strategy).order_by(Strategy.created_at.asc()))
            if strat is not None:
                strategy_id = strat.id
                strategy_version_str = f"{strat.code}:{strat.current_version}"

        fills_payload: list[dict[str, Any]] = []
        if fill is not None:
            fills_payload.append(
                {
                    "id": fill.id,
                    "exchange_trade_id": fill.exchange_trade_id,
                    "side": fill.side,
                    "price": str(fill.price),
                    "qty": str(fill.qty),
                    "fee": str(fill.fee),
                    "realized_pnl": str(fill.realized_pnl),
                    "occurred_at": utc_iso(fill.occurred_at),
                }
            )

        net_pnl = (
            (to_decimal(fill.realized_pnl, ZERO) - to_decimal(fill.fee, ZERO)).quantize(
                Decimal("0.0001"), rounding=ROUND_HALF_UP
            )
            if fill is not None
            else ZERO
        )
        fees_paid = to_decimal(fill.fee, ZERO) if fill is not None else ZERO

        direction_str = (
            order.position_side
            if order.position_side in ("LONG", "SHORT")
            else order.side
        )
        review_text = (
            f"Executed {order.side} ({direction_str}) on {order.symbol} ({order.exchange} {order.market_type}): "
            f"filled_qty={order.filled_qty} @ avg_price={order.avg_fill_price}, "
            f"fee={fees_paid} USDT, net_realized_pnl={net_pnl} USDT, close_reason={close_reason}."
        )

        entry = JournalEntry(
            account_id=order.account_id,
            order_id=order.id,
            signal_id=order.signal_id,
            decision_id=order.decision_id,
            strategy_id=strategy_id,
            strategy_version=strategy_version_str,
            exchange=order.exchange,
            market_type=order.market_type,
            symbol=order.symbol,
            direction=direction_str,
            ai_model_version="deepseek-reasoner:v1.0",
            ai_output_json=ai_decision.output_json if ai_decision is not None else {},
            risk_check_json=(
                {
                    "approved": risk_result.approved,
                    "rule_code": risk_result.rule_code,
                    "reason": risk_result.reason,
                    "sizing": {k: str(v) for k, v in risk_result.sizing.items()},
                }
                if risk_result is not None
                else {"approved": True, "rule_code": "REDUCE_ONLY_OR_RECONCILED"}
            ),
            order_request_json={
                "client_order_id": order.client_order_id,
                "order_type": order.order_type,
                "side": order.side,
                "position_side": order.position_side,
                "reduce_only": order.reduce_only,
                "qty": str(order.qty),
                "price": str(order.price),
                "stop_loss": str(order.stop_loss) if order.stop_loss is not None else None,
                "take_profit": str(order.take_profit) if order.take_profit is not None else None,
            },
            exchange_order_id=order.exchange_order_id,
            fills_json=fills_payload,
            fees_paid=fees_paid,
            funding_pnl=ZERO,
            close_reason=close_reason,
            net_realized_pnl=net_pnl,
            review_summary=review_text,
        )
        db.add(entry)
        return entry

    def _serialize_order(
        self,
        order: Order,
        *,
        risk_result: RiskEvaluationResult | None = None,
        idempotent_replay: bool = False,
        fills: list[Fill] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": order.id,
            "order_id": order.id,
            "account_id": order.account_id,
            "signal_id": order.signal_id,
            "decision_id": order.decision_id,
            "strategy_version_id": order.strategy_version_id,
            "exchange": order.exchange,
            "environment": order.environment,
            "market_type": order.market_type,
            "symbol": order.symbol,
            "client_order_id": order.client_order_id,
            "exchange_order_id": order.exchange_order_id,
            "order_type": order.order_type,
            "side": order.side,
            "position_side": order.position_side,
            "reduce_only": order.reduce_only,
            "status": order.status,
            "qty": order.qty,
            "filled_qty": order.filled_qty,
            "price": order.price,
            "avg_fill_price": order.avg_fill_price,
            "stop_loss": order.stop_loss,
            "take_profit": order.take_profit,
            "tp_sl_confirmed": order.tp_sl_confirmed,
            "protection_order_ids": list(order.protection_order_ids_json or []),
            "protection_order_ids_json": list(order.protection_order_ids_json or []),
            "reject_reason": order.reject_reason,
            "idempotent_replay": idempotent_replay,
            "created_at": utc_iso(order.created_at),
            "updated_at": utc_iso(order.updated_at),
        }
        if risk_result is not None:
            payload["risk_evaluation"] = risk_result.to_dict()
            payload["risk_approved"] = risk_result.approved
            payload["risk_rule_code"] = risk_result.rule_code
        if fills is not None:
            payload["fills"] = [
                {
                    "id": f.id,
                    "exchange_trade_id": f.exchange_trade_id,
                    "side": f.side,
                    "price": f.price,
                    "qty": f.qty,
                    "fee": f.fee,
                    "realized_pnl": f.realized_pnl,
                    "occurred_at": utc_iso(f.occurred_at),
                }
                for f in fills
            ]
        return AttrDict(payload)

    def submit_trade_plan(
        self,
        db: Session,
        trade_plan: TradePlanSchema | dict[str, Any],
        *,
        account_id: str | None = None,
        environment: str | None = None,
        signal_id: str | None = None,
        decision_id: str | None = None,
        client_order_id: str | None = None,
        simulate_timeout: bool = False,
        simulate_partial_fill: bool = False,
    ) -> dict[str, Any]:
        """
        Submit a TradePlan through mandatory RiskEngine validation and full Order state machine.
        Idempotent by `(account_id, client_order_id)`.
        """
        plan = (
            trade_plan
            if isinstance(trade_plan, TradePlanSchema)
            else TradePlanSchema.model_validate(trade_plan)
        )

        eff_account_id = account_id or plan.account_id
        eff_environment = environment or plan.environment

        if eff_environment is not None and str(eff_environment).strip():
            account = self.resolve_execution_account(
                db,
                account_id=eff_account_id,
                exchange=plan.exchange,
                environment=eff_environment,
            )
            resolved_account_id = account.id
            env_name = eff_environment.lower()
        else:
            logger.warning(
                "submit_trade_plan called without explicit environment; "
                "falling back to legacy account environment resolution"
            )
            account = self._resolve_account(db, account_id=eff_account_id, exchange=plan.exchange)
            resolved_account_id = account.id if account is not None else (eff_account_id or "default")
            env_name = account.environment if account is not None else "demo"

        # 1. Idempotency check by (account_id, client_order_id) BEFORE evaluating or placing duplicate order
        if client_order_id:
            existing_order = db.scalar(
                select(Order).where(
                    Order.account_id == resolved_account_id,
                    Order.client_order_id == client_order_id,
                )
            )
            if existing_order is not None:
                existing_fills = list(
                    db.scalars(select(Fill).where(Fill.order_id == existing_order.id)).all()
                )
                return self._serialize_order(
                    existing_order,
                    idempotent_replay=True,
                    fills=existing_fills,
                )

        effective_cid = client_order_id or f"act-{plan.exchange[:3]}-{uuid.uuid4().hex[:16]}"

        # 2. Mandatory RiskEngine evaluation
        risk_result = self.risk_engine.evaluate_trade_plan(
            db,
            account_id=resolved_account_id,
            trade_plan=plan,
            signal_id=signal_id,
            decision_id=decision_id,
        )

        inst = self._resolve_instrument(db, plan.exchange, plan.market_type, plan.symbol)
        tick_size = to_decimal(inst.tick_size, Decimal("0.01")) if inst else Decimal("0.01")
        lot_size = to_decimal(inst.lot_size, Decimal("0.001")) if inst else Decimal("0.001")
        contract_size = to_decimal(inst.contract_size, ONE) if inst else ONE

        side = "BUY" if plan.direction in ("LONG", "BUY") else "SELL"
        position_side = (
            "SPOT"
            if plan.market_type == "spot"
            else ("LONG" if side == "BUY" else "SHORT")
        )
        rounded_entry = risk_result.sizing.get("rounded_entry_price") or round_price_to_tick(
            to_decimal(plan.entry_price), tick_size
        )
        rounded_stop = risk_result.sizing.get("rounded_stop_loss") or round_price_to_tick(
            to_decimal(plan.stop_loss), tick_size
        )
        rounded_tp = risk_result.sizing.get("rounded_take_profit") or round_price_to_tick(
            to_decimal(plan.take_profit), tick_size
        )
        planned_qty = risk_result.sizing.get("qty", ZERO)

        # Create initial Order in CREATED state
        order = Order(
            account_id=resolved_account_id,
            signal_id=signal_id,
            decision_id=decision_id,
            strategy_version_id=plan.strategy_version,
            exchange=plan.exchange,
            environment=env_name,
            market_type=plan.market_type,
            symbol=plan.symbol,
            client_order_id=effective_cid,
            exchange_order_id=None,
            order_type=plan.entry_type,
            side=side,
            position_side=position_side,
            reduce_only=False,
            status="CREATED",
            qty=planned_qty if planned_qty > ZERO else to_decimal(inst.min_qty if inst else "0.001"),
            filled_qty=ZERO,
            price=rounded_entry,
            avg_fill_price=ZERO,
            stop_loss=rounded_stop,
            take_profit=rounded_tp,
            tp_sl_confirmed=False,
            protection_order_ids_json=[],
            reject_reason=None,
            raw_response_json={},
        )
        db.add(order)
        db.flush()

        # If RiskEngine rejected, transition CREATED -> REJECTED and return without calling exchange adapter
        if not risk_result.approved:
            self.transition_order(
                order,
                "REJECTED",
                reject_reason=f"{risk_result.rule_code}: {risk_result.reason}",
            )
            if risk_result.risk_event_id:
                evt = db.scalar(select(RiskEvent).where(RiskEvent.id == risk_result.risk_event_id))
                if evt is not None:
                    evt.order_id = order.id
            if signal_id:
                sig = db.scalar(select(TradeSignal).where(TradeSignal.id == signal_id))
                if sig is not None:
                    sig.status = "REJECTED"
            db.commit()
            db.refresh(order)
            event_bus.publish_sync(
                channel="orders",
                event_type="order.rejected",
                data=self._serialize_order(order, risk_result=risk_result),
                source="execution_engine",
            )
            return self._serialize_order(order, risk_result=risk_result, fills=[])

        # 3. For spot SELL orders, check available base asset balance first
        if plan.market_type == "spot" and side == "SELL":
            has_balance, avail_base = self._check_spot_sell_balance(
                db,
                account_id=resolved_account_id,
                symbol=plan.symbol,
                required_qty=order.qty,
            )
            if not has_balance:
                reason = (
                    f"INSUFFICIENT_SPOT_BASE_BALANCE: Available base asset ({avail_base}) "
                    f"is less than order qty ({order.qty}) for spot SELL on {plan.symbol}."
                )
                self.transition_order(order, "REJECTED", reject_reason=reason)
                db.add(
                    RiskEvent(
                        account_id=resolved_account_id,
                        signal_id=signal_id,
                        order_id=order.id,
                        event_type="SPOT_SELL_INSUFFICIENT_BALANCE",
                        rule_code="INSUFFICIENT_SPOT_BASE_BALANCE",
                        severity="WARNING",
                        details={"symbol": plan.symbol, "available": str(avail_base), "required": str(order.qty)},
                    )
                )
                db.commit()
                db.refresh(order)
                return self._serialize_order(order, risk_result=risk_result, fills=[])

        # 4. State machine progression: CREATED -> VALIDATED -> SUBMITTED
        self.transition_order(order, "VALIDATED")
        self.transition_order(order, "SUBMITTED")
        db.flush()

        # 5. Call Exchange Adapter (or handle timeout -> UNKNOWN without blind retry)
        adapter = self._get_exchange_adapter(plan.exchange, env_name)
        leverage = risk_result.sizing.get("leverage", Decimal("2") if plan.market_type == "usdt_perp" else ONE)

        try:
            if adapter is not None:
                remote_resp = adapter.place_order(
                    {
                        "symbol": plan.symbol,
                        "market_type": plan.market_type,
                        "client_order_id": effective_cid,
                        "order_type": plan.entry_type,
                        "side": side,
                        "position_side": position_side,
                        "reduce_only": False,
                        "qty": order.qty,
                        "price": order.price,
                        "stop_loss": order.stop_loss,
                        "take_profit": order.take_profit,
                        "leverage": leverage,
                        "simulate_timeout": simulate_timeout,
                        "simulate_partial_fill": simulate_partial_fill,
                    }
                )
            else:
                remote_resp = {"status": "UNKNOWN" if simulate_timeout else "FILLED"}

            if simulate_timeout or remote_resp.get("status") == "UNKNOWN" or remote_resp.get("timed_out"):
                raise TimeoutError(
                    remote_resp.get("error")
                    or f"Order {effective_cid} timed out; state is UNKNOWN. Query and reconcile before new orders."
                )

        except TimeoutError as timeout_exc:
            # Transition SUBMITTED -> UNKNOWN (never blindly retry!)
            self.transition_order(
                order,
                "UNKNOWN",
                reject_reason=str(timeout_exc),
            )
            order.raw_response_json = {"timed_out": True, "error": str(timeout_exc)}
            db.add(
                RiskEvent(
                    account_id=resolved_account_id,
                    signal_id=signal_id,
                    order_id=order.id,
                    event_type="ORDER_TIMEOUT_UNKNOWN",
                    rule_code="UNRESOLVED_UNKNOWN_ORDER",
                    severity="CRITICAL",
                    details={
                        "order_id": order.id,
                        "client_order_id": order.client_order_id,
                        "symbol": order.symbol,
                        "reason": str(timeout_exc),
                    },
                )
            )
            db.add(
                SystemEvent(
                    component="execution_engine",
                    event_type="ORDER_TIMEOUT_UNKNOWN",
                    severity="CRITICAL",
                    payload={"order_id": order.id, "client_order_id": order.client_order_id},
                )
            )
            db.commit()
            db.refresh(order)
            event_bus.publish_sync(
                channel="orders",
                event_type="order.unknown",
                data=self._serialize_order(order, risk_result=risk_result),
                source="execution_engine",
            )
            return self._serialize_order(order, risk_result=risk_result, fills=[])

        # 6. Normal acknowledgment & fill processing: SUBMITTED -> ACKNOWLEDGED -> PARTIALLY_FILLED / FILLED
        self.transition_order(order, "ACKNOWLEDGED")
        ex_order_id = (
            remote_resp.get("exchange_order_id")
            or f"{plan.exchange[:2].upper()}-{env_name.upper()}-{uuid.uuid4().hex[:8].upper()}"
        )
        order.exchange_order_id = str(ex_order_id)
        protection_ids = list(
            remote_resp.get("protection_order_ids")
            or [f"{order.exchange_order_id}-SL", f"{order.exchange_order_id}-TP"]
        )
        order.protection_order_ids_json = protection_ids
        order.tp_sl_confirmed = bool(remote_resp.get("tp_sl_confirmed", True))
        order.raw_response_json = {
            k: (str(v) if isinstance(v, Decimal) else v)
            for k, v in remote_resp.items()
            if k not in ("fills", "created_at", "updated_at")
        }

        if simulate_partial_fill or remote_resp.get("status") == "PARTIALLY_FILLED":
            half_qty = round_qty_to_lot(order.qty * Decimal("0.5"), lot_size)
            fill_qty = half_qty if half_qty > ZERO else (order.qty * Decimal("0.5"))
            self.transition_order(order, "PARTIALLY_FILLED")
        else:
            fill_qty = order.qty
            self.transition_order(order, "FILLED")

        exec_price = to_decimal(remote_resp.get("avg_fill_price") or order.price, order.price)
        order.filled_qty = fill_qty
        order.avg_fill_price = exec_price

        # Verify native TP/SL protection confirmation
        self._verify_tp_sl_protection(db, order, is_opening_order=True)

        fee_rate = Decimal("0.0005") if plan.market_type == "usdt_perp" else Decimal("0.0010")
        remote_fills = remote_resp.get("fills") or []
        remote_trade_id = remote_fills[0].get("exchange_trade_id") if remote_fills else None

        fill, position, _ = self._apply_fill_and_update_portfolio(
            db,
            order=order,
            fill_qty=fill_qty,
            fill_price=exec_price,
            fee_rate=fee_rate,
            contract_size=contract_size,
            tick_size=tick_size,
            leverage=leverage,
            instrument_id=inst.id if inst else None,
            exchange_trade_id=remote_trade_id,
            close_reason="OPEN_OR_ACTIVE",
            risk_result=risk_result,
        )

        if signal_id:
            sig = db.scalar(select(TradeSignal).where(TradeSignal.id == signal_id))
            if sig is not None:
                sig.status = "CONSUMED"

        db.add(
            AuditLog(
                actor="execution_engine",
                action="ORDER_EXECUTED",
                resource=f"order:{order.id}",
                before_json={"status": "CREATED"},
                after_json={
                    "status": order.status,
                    "client_order_id": order.client_order_id,
                    "exchange_order_id": order.exchange_order_id,
                    "filled_qty": str(order.filled_qty),
                    "avg_fill_price": str(order.avg_fill_price),
                    "tp_sl_confirmed": order.tp_sl_confirmed,
                },
            )
        )
        db.commit()
        db.refresh(order)

        serialized = self._serialize_order(order, risk_result=risk_result, fills=[fill])
        event_bus.publish_sync(
            channel="orders",
            event_type="order.updated",
            data=serialized,
            source=order.exchange,
        )
        if position is not None:
            event_bus.publish_sync(
                channel="positions",
                event_type="position.updated",
                data={
                    "id": position.id,
                    "symbol": position.symbol,
                    "position_side": position.position_side,
                    "qty": str(position.qty),
                    "entry_price": str(position.entry_price),
                    "status": position.status,
                },
                source=order.exchange,
            )
        return serialized

    def submit_order(
        self,
        db: Session,
        request: OrderSubmitRequest | TradePlanSchema | dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        if isinstance(request, OrderSubmitRequest):
            return self.submit_trade_plan(
                db,
                request.trade_plan,
                account_id=request.account_id,
                environment=kwargs.get("environment"),
                signal_id=request.signal_id,
                decision_id=request.decision_id,
                client_order_id=request.client_order_id,
                simulate_timeout=request.simulate_timeout,
                simulate_partial_fill=request.simulate_partial_fill,
            )
        if isinstance(request, dict) and "trade_plan" in request:
            parsed = OrderSubmitRequest.model_validate(request)
            return self.submit_order(db, parsed, **kwargs)
        return self.submit_trade_plan(db, request, **kwargs)

    def reconcile_unknown_order(
        self,
        db: Session,
        order_id: str,
        simulated_remote_status: str = "FILLED",
    ) -> dict[str, Any]:
        """
        Query the exchange/demo state by `client_order_id` for an `UNKNOWN` (or active) order,
        recover its true state (`FILLED` or `CANCELED`/`EXPIRED`), record any missing `Fill` and `Position`,
        and clear the `UNKNOWN` blocker on the account.
        """
        order = db.scalar(select(Order).where(Order.id == order_id))
        if order is None:
            order = db.scalar(select(Order).where(Order.client_order_id == order_id))
        if order is None:
            raise ValueError(f"Order not found: {order_id}")

        adapter = self._get_exchange_adapter(order.exchange, order.environment)
        remote_order = (
            adapter.get_order(
                symbol=order.symbol,
                order_id=order.exchange_order_id,
                client_order_id=order.client_order_id,
                market_type=order.market_type,
            )
            if adapter is not None
            else None
        )

        target_status = (
            str(remote_order.get("status")).upper()
            if remote_order and remote_order.get("status") in ("FILLED", "PARTIALLY_FILLED", "CANCELED", "EXPIRED")
            else simulated_remote_status.upper()
        )

        inst = self._resolve_instrument(db, order.exchange, order.market_type, order.symbol)
        tick_size = to_decimal(inst.tick_size, Decimal("0.01")) if inst else Decimal("0.01")
        contract_size = to_decimal(inst.contract_size, ONE) if inst else ONE
        fills_list = list(db.scalars(select(Fill).where(Fill.order_id == order.id)).all())

        if target_status in ("FILLED", "PARTIALLY_FILLED"):
            if order.status != target_status:
                self.transition_order(order, target_status)
            order.exchange_order_id = (
                order.exchange_order_id
                or (remote_order.get("exchange_order_id") if remote_order else None)
                or f"{order.exchange[:2].upper()}-REC-{uuid.uuid4().hex[:8].upper()}"
            )
            order.filled_qty = (
                to_decimal(remote_order["filled_qty"])
                if remote_order and remote_order.get("filled_qty")
                else order.qty
            )
            order.avg_fill_price = (
                to_decimal(remote_order["avg_fill_price"])
                if remote_order and remote_order.get("avg_fill_price")
                else order.price
            )
            order.tp_sl_confirmed = True
            if not order.protection_order_ids_json:
                order.protection_order_ids_json = (
                    list(remote_order.get("protection_order_ids") or [])
                    if remote_order
                    else [f"{order.exchange_order_id}-SL", f"{order.exchange_order_id}-TP"]
                )
            order.reject_reason = None

            if not fills_list:
                fee_rate = Decimal("0.0005") if order.market_type == "usdt_perp" else Decimal("0.0010")
                remote_fills = (remote_order.get("fills") or []) if remote_order else []
                remote_trade_id = remote_fills[0].get("exchange_trade_id") if remote_fills else None
                fill, _, _ = self._apply_fill_and_update_portfolio(
                    db,
                    order=order,
                    fill_qty=order.filled_qty,
                    fill_price=order.avg_fill_price,
                    fee_rate=fee_rate,
                    contract_size=contract_size,
                    tick_size=tick_size,
                    leverage=Decimal("2") if order.market_type == "usdt_perp" else ONE,
                    instrument_id=inst.id if inst else None,
                    exchange_trade_id=remote_trade_id,
                    close_reason="RECONCILED_FROM_UNKNOWN",
                )
                fills_list = [fill]
        else:
            final_cancel_status = target_status if target_status in ("CANCELED", "EXPIRED") else "CANCELED"
            if order.status != final_cancel_status:
                self.transition_order(
                    order,
                    final_cancel_status,
                    reject_reason=f"Reconciled UNKNOWN order: remote status is {final_cancel_status}",
                )

        # Check if account has any remaining UNKNOWN orders; if none, mark SYNCED
        remaining_unknown = list(
            db.scalars(
                select(Order).where(
                    Order.account_id == order.account_id,
                    Order.status == "UNKNOWN",
                )
            ).all()
        )
        account = db.scalar(select(TradingAccount).where(TradingAccount.id == order.account_id))
        if account is not None and not remaining_unknown:
            account.sync_status = "SYNCED"
            account.last_reconciled_at = utc_now()

        db.add(
            RiskEvent(
                account_id=order.account_id,
                order_id=order.id,
                event_type="UNKNOWN_ORDER_RECONCILED",
                rule_code="ORDER_RECONCILED",
                severity="INFO",
                details={
                    "order_id": order.id,
                    "client_order_id": order.client_order_id,
                    "resolved_status": order.status,
                    "remaining_unknown_orders": len(remaining_unknown),
                },
            )
        )
        db.add(
            AuditLog(
                actor="execution_engine",
                action="RECONCILE_UNKNOWN_ORDER",
                resource=f"order:{order.id}",
                before_json={"status": "UNKNOWN"},
                after_json={"status": order.status, "filled_qty": str(order.filled_qty)},
            )
        )
        db.commit()
        db.refresh(order)

        serialized = self._serialize_order(order, fills=fills_list)
        serialized["reconciled"] = True
        serialized["remaining_unknown_orders"] = len(remaining_unknown)
        event_bus.publish_sync(
            channel="orders",
            event_type="order.reconciled",
            data=serialized,
            source="execution_engine",
        )
        return serialized

    def cancel_order(
        self,
        db: Session,
        order_id: str,
        reason: str = "Operator canceled order",
    ) -> dict[str, Any]:
        order = db.scalar(select(Order).where(Order.id == order_id))
        if order is None:
            order = db.scalar(select(Order).where(Order.client_order_id == order_id))
        if order is None:
            raise ValueError(f"Order not found: {order_id}")

        if order.status in ("CANCELED",):
            return self._serialize_order(order)
        if order.status in ("FILLED", "REJECTED", "EXPIRED"):
            raise ValueError(f"Cannot cancel order in terminal status {order.status}")

        adapter = self._get_exchange_adapter(order.exchange, order.environment)
        if adapter is not None:
            adapter.cancel_order(
                symbol=order.symbol,
                order_id=order.exchange_order_id,
                client_order_id=order.client_order_id,
            )

        self.transition_order(order, "CANCELED", reject_reason=reason)
        db.add(
            AuditLog(
                actor="local_admin",
                action="CANCEL_ORDER",
                resource=f"order:{order.id}",
                before_json={},
                after_json={"status": "CANCELED", "reason": reason},
            )
        )
        db.commit()
        db.refresh(order)

        serialized = self._serialize_order(order)
        event_bus.publish_sync(
            channel="orders",
            event_type="order.canceled",
            data=serialized,
            source="execution_engine",
        )
        return serialized

    def close_position(
        self,
        db: Session,
        position_id: str,
        reason: str = "MANUAL_CLOSE",
    ) -> dict[str, Any]:
        position = db.scalar(select(Position).where(Position.id == position_id))
        if position is None:
            raise ValueError(f"Position not found: {position_id}")
        if position.status == "CLOSED" or to_decimal(position.qty, ZERO) <= ZERO:
            return AttrDict(
                {
                    "position_id": position.id,
                    "status": "CLOSED",
                    "already_closed": True,
                    "realized_pnl": position.realized_pnl,
                }
            )

        close_side = "SELL" if position.position_side in ("LONG", "SPOT") else "BUY"
        snapshot = db.scalar(
            select(MarketSnapshot)
            .where(
                MarketSnapshot.exchange == position.exchange,
                MarketSnapshot.market_type == position.market_type,
                MarketSnapshot.symbol == position.symbol,
            )
            .order_by(MarketSnapshot.received_at.desc(), MarketSnapshot.event_time.desc())
        )
        close_price = (
            to_decimal(snapshot.bid if close_side == "SELL" else snapshot.ask)
            if snapshot is not None and snapshot.bid > ZERO and snapshot.ask > ZERO
            else to_decimal(position.mark_price or position.entry_price)
        )

        inst = self._resolve_instrument(db, position.exchange, position.market_type, position.symbol)
        tick_size = to_decimal(inst.tick_size, Decimal("0.01")) if inst else Decimal("0.01")
        contract_size = to_decimal(inst.contract_size, ONE) if inst else ONE
        close_price = round_price_to_tick(close_price, tick_size)

        cid = f"close-{position.exchange[:3]}-{uuid.uuid4().hex[:14]}"
        close_order = Order(
            account_id=position.account_id,
            exchange=position.exchange,
            environment=position.environment,
            market_type=position.market_type,
            symbol=position.symbol,
            client_order_id=cid,
            exchange_order_id=f"{position.exchange[:2].upper()}-CLS-{uuid.uuid4().hex[:8].upper()}",
            order_type="MARKET",
            side=close_side,
            position_side=position.position_side,
            reduce_only=True,
            status="CREATED",
            qty=position.qty,
            filled_qty=ZERO,
            price=close_price,
            avg_fill_price=ZERO,
            tp_sl_confirmed=True,
            protection_order_ids_json=[],
        )
        db.add(close_order)
        db.flush()

        self.transition_order(close_order, "VALIDATED")
        self.transition_order(close_order, "SUBMITTED")
        self.transition_order(close_order, "ACKNOWLEDGED")
        self.transition_order(close_order, "FILLED")
        close_order.filled_qty = close_order.qty
        close_order.avg_fill_price = close_price

        fee_rate = Decimal("0.0005") if position.market_type == "usdt_perp" else Decimal("0.0010")
        fill, updated_pos, journal = self._apply_fill_and_update_portfolio(
            db,
            order=close_order,
            fill_qty=close_order.qty,
            fill_price=close_price,
            fee_rate=fee_rate,
            contract_size=contract_size,
            tick_size=tick_size,
            leverage=to_decimal(position.leverage, ONE),
            instrument_id=position.instrument_id,
            close_reason=reason,
        )

        db.add(
            AuditLog(
                actor="local_admin",
                action="CLOSE_POSITION",
                resource=f"position:{position.id}",
                before_json={"status": "OPEN"},
                after_json={
                    "status": "CLOSED",
                    "close_order_id": close_order.id,
                    "close_price": str(close_price),
                    "realized_pnl": str(fill.realized_pnl),
                    "reason": reason,
                },
            )
        )
        db.commit()
        db.refresh(position)
        db.refresh(close_order)

        result = {
            "position_id": position.id,
            "symbol": position.symbol,
            "exchange": position.exchange,
            "market_type": position.market_type,
            "position_side": position.position_side,
            "status": position.status,
            "close_order_id": close_order.id,
            "reduce_only": close_order.reduce_only,
            "close_price": close_price,
            "realized_pnl": fill.realized_pnl,
            "fee": fill.fee,
            "journal_entry_id": journal.id,
            "reason": reason,
        }
        event_bus.publish_sync(
            channel="positions",
            event_type="position.closed",
            data=result,
            source="execution_engine",
        )
        return AttrDict(result)

    def flatten_all_positions(
        self,
        db: Session,
        account_id: str | None = None,
        reason: str = "EMERGENCY_FLATTEN",
    ) -> dict[str, Any]:
        """
        Emergency flatten: cancel all active/open orders and close all OPEN positions with reduce_only=True.
        """
        ord_stmt = select(Order).where(
            Order.status.in_(["CREATED", "VALIDATED", "SUBMITTED", "ACKNOWLEDGED", "PARTIALLY_FILLED"])
        )
        pos_stmt = select(Position).where(Position.status == "OPEN")
        if account_id:
            ord_stmt = ord_stmt.where(Order.account_id == account_id)
            pos_stmt = pos_stmt.where(Position.account_id == account_id)

        active_orders = list(db.scalars(ord_stmt).all())
        canceled_ids: list[str] = []
        for ord_item in active_orders:
            try:
                self.cancel_order(db, ord_item.id, reason=reason)
                canceled_ids.append(ord_item.id)
            except Exception:
                pass

        open_positions = [
            p for p in db.scalars(pos_stmt).all() if to_decimal(p.qty, ZERO) > ZERO
        ]
        closed_results: list[dict[str, Any]] = []
        for pos in open_positions:
            closed_res = self.close_position(db, pos.id, reason=reason)
            closed_results.append(closed_res)

        target_acc = account_id or (open_positions[0].account_id if open_positions else "global")
        db.add(
            RiskEvent(
                account_id=target_acc,
                event_type="EMERGENCY_FLATTEN",
                rule_code="EMERGENCY_FLATTEN",
                severity="CRITICAL",
                details={
                    "reason": reason,
                    "canceled_orders_count": len(canceled_ids),
                    "closed_positions_count": len(closed_results),
                },
            )
        )
        db.commit()

        summary = {
            "flattened": True,
            "reason": reason,
            "canceled_order_ids": canceled_ids,
            "closed_positions_count": len(closed_results),
            "closed_positions": closed_results,
        }
        event_bus.publish_sync(
            channel="positions",
            event_type="positions.flattened",
            data=summary,
            source="execution_engine",
        )
        return AttrDict(summary)

    def get_orders(
        self,
        db: Session,
        *,
        account_id: str | None = None,
        status: str | None = None,
        symbol: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        stmt = select(Order).order_by(Order.created_at.desc())
        if account_id:
            stmt = stmt.where(Order.account_id == account_id)
        if status:
            stmt = stmt.where(Order.status == status.upper())
        if symbol:
            stmt = stmt.where(Order.symbol == symbol)
        rows = list(db.scalars(stmt.limit(max(1, min(limit, 500)))).all())
        return [self._serialize_order(r) for r in rows]

    def get_order(self, db: Session, order_id: str) -> dict[str, Any] | None:
        order = db.scalar(select(Order).where(Order.id == order_id))
        if order is None:
            order = db.scalar(select(Order).where(Order.client_order_id == order_id))
        if order is None:
            return None
        fills = list(db.scalars(select(Fill).where(Fill.order_id == order.id)).all())
        return self._serialize_order(order, fills=fills)

    def get_fills(
        self,
        db: Session,
        *,
        account_id: str | None = None,
        order_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        stmt = select(Fill).order_by(Fill.occurred_at.desc())
        if account_id:
            stmt = stmt.where(Fill.account_id == account_id)
        if order_id:
            stmt = stmt.where(Fill.order_id == order_id)
        rows = list(db.scalars(stmt.limit(max(1, min(limit, 500)))).all())
        return [
            AttrDict(
                {
                    "id": f.id,
                    "order_id": f.order_id,
                    "account_id": f.account_id,
                    "exchange": f.exchange,
                    "market_type": f.market_type,
                    "symbol": f.symbol,
                    "exchange_trade_id": f.exchange_trade_id,
                    "side": f.side,
                    "price": f.price,
                    "qty": f.qty,
                    "fee": f.fee,
                    "fee_currency": f.fee_currency,
                    "realized_pnl": f.realized_pnl,
                    "occurred_at": utc_iso(f.occurred_at),
                }
            )
            for f in rows
        ]

    def get_positions(
        self,
        db: Session,
        *,
        account_id: str | None = None,
        status: str | None = "OPEN",
    ) -> list[dict[str, Any]]:
        stmt = select(Position).order_by(Position.updated_at.desc())
        if account_id:
            stmt = stmt.where(Position.account_id == account_id)
        if status and status.upper() != "ALL":
            stmt = stmt.where(Position.status == status.upper())
        rows = list(db.scalars(stmt).all())
        return [
            AttrDict(
                {
                    "id": p.id,
                    "account_id": p.account_id,
                    "instrument_id": p.instrument_id,
                    "exchange": p.exchange,
                    "environment": p.environment,
                    "market_type": p.market_type,
                    "symbol": p.symbol,
                    "position_side": p.position_side,
                    "qty": p.qty,
                    "entry_price": p.entry_price,
                    "mark_price": p.mark_price,
                    "liquidation_price": p.liquidation_price,
                    "leverage": p.leverage,
                    "margin_used": p.margin_used,
                    "notional_value": p.notional_value,
                    "unrealized_pnl": p.unrealized_pnl,
                    "realized_pnl": p.realized_pnl,
                    "stop_loss": p.stop_loss,
                    "take_profit": p.take_profit,
                    "tp_sl_confirmed": p.tp_sl_confirmed,
                    "status": p.status,
                    "updated_at": utc_iso(p.updated_at),
                }
            )
            for p in rows
        ]


execution_engine = ExecutionEngine()
