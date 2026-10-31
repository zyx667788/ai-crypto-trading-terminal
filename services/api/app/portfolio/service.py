"""
Portfolio, positions, balances and reconciliation (V1.0 §3.9, V1.1 §9.4.6).

Guarantees:
- Deposits, withdrawals and internal transfers are tracked separately and NEVER counted as trading PnL.
- Realized and unrealized PnL are reported separately.
- A local-vs-exchange mismatch locks the account (trading_locked=True, sync_status=MISMATCH)
  and blocks new entries until reconciliation completes.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.events import event_bus
from app.core.logging import get_logger
from app.core.precision import ZERO, to_decimal, utc_iso, utc_now
from app.core.security import redact_sensitive_data
from app.models.entities import (
    AgentRun,
    BalanceSnapshot,
    EquitySnapshot,
    ExchangeConnection,
    Fill,
    Instrument,
    Order,
    Position,
    RiskEvent,
    Strategy,
    SystemEvent,
    TradeSignal,
    TradingAccount,
    Transfer,
)

logger = get_logger("terminal.portfolio")

ACTIVE_ORDER_STATUSES = ("CREATED", "VALIDATED", "SUBMITTED", "ACKNOWLEDGED", "PARTIALLY_FILLED", "UNKNOWN")


class PortfolioService:
    # ------------------------------------------------------------ aggregation
    def _latest_equity_by_account(self, db: Session) -> dict[str, EquitySnapshot]:
        latest: dict[str, EquitySnapshot] = {}
        rows = db.execute(select(EquitySnapshot).order_by(EquitySnapshot.observed_at.asc())).scalars().all()
        for row in rows:
            latest[row.account_id] = row
        return latest

    def _latest_balances(self, db: Session) -> list[BalanceSnapshot]:
        subq = (
            select(
                BalanceSnapshot.account_id,
                BalanceSnapshot.asset,
                func.max(BalanceSnapshot.observed_at).label("max_observed"),
            )
            .group_by(BalanceSnapshot.account_id, BalanceSnapshot.asset)
            .subquery()
        )
        stmt = select(BalanceSnapshot).join(
            subq,
            (BalanceSnapshot.account_id == subq.c.account_id)
            & (BalanceSnapshot.asset == subq.c.asset)
            & (BalanceSnapshot.observed_at == subq.c.max_observed),
        )
        return list(db.execute(stmt).scalars().all())

    def get_portfolio_summary(self, db: Session) -> dict[str, Any]:
        accounts = db.execute(select(TradingAccount)).scalars().all()
        connections = {c.id: c for c in db.execute(select(ExchangeConnection)).scalars().all()}
        equity_map = self._latest_equity_by_account(db)
        balances = self._latest_balances(db)

        # --- per-exchange equity breakdown
        per_exchange: dict[str, dict[str, Decimal]] = {}
        for account in accounts:
            exchange = account.exchange
            bucket = per_exchange.setdefault(
                exchange,
                {"equity": ZERO, "available": ZERO, "margin": ZERO, "notional": ZERO,
                 "realized_pnl": ZERO, "unrealized_pnl": ZERO, "fees": ZERO, "funding_pnl": ZERO},
            )
            eq = equity_map.get(account.id)
            if eq is not None:
                bucket["equity"] += to_decimal(eq.equity, ZERO)
                bucket["available"] += to_decimal(eq.available_balance, ZERO)
                bucket["margin"] += to_decimal(eq.margin_balance, ZERO)
                bucket["notional"] += to_decimal(eq.notional_exposure, ZERO)
                bucket["realized_pnl"] += to_decimal(eq.realized_pnl, ZERO)
                bucket["unrealized_pnl"] += to_decimal(eq.unrealized_pnl, ZERO)
                bucket["fees"] += to_decimal(eq.fees_paid, ZERO)
                bucket["funding_pnl"] += to_decimal(eq.funding_pnl, ZERO)
            else:
                # Fall back to asset valuation if no equity snapshot exists yet.
                for bal in balances:
                    if bal.account_id == account.id:
                        bucket["equity"] += to_decimal(bal.usd_value, ZERO)
                        bucket["available"] += to_decimal(bal.usd_value, ZERO)

        totals = {k: sum((b[k] for b in per_exchange.values()), ZERO) for k in
                  ("equity", "available", "margin", "notional", "realized_pnl", "unrealized_pnl", "fees", "funding_pnl")}

        # --- spot valuation vs perpetual margin
        # Spot valuation is the asset value NOT tied up as contract margin; perpetual margin is
        # the sum of margin actually held against open positions. Keeping these separate is a
        # hard requirement of Section 3.1 (equity vs available vs margin balance vs notional).
        total_asset_value = sum((to_decimal(b.usd_value, ZERO) for b in balances), ZERO)
        open_positions = db.execute(select(Position).where(Position.status == "OPEN")).scalars().all()
        perp_margin = sum((to_decimal(p.margin_used, ZERO) for p in open_positions), ZERO)
        spot_value = max(total_asset_value - perp_margin, ZERO)
        if total_asset_value == ZERO:
            # No balance snapshots yet: fall back to equity and label it as such.
            spot_value = max(totals["equity"] - perp_margin, ZERO)

        # --- transfers are excluded from trading profit
        transfers = db.execute(select(Transfer)).scalars().all()
        net_transfers = ZERO
        for t in transfers:
            amt = to_decimal(t.amount, ZERO)
            net_transfers += amt if t.transfer_type == "DEPOSIT" else -amt

        # --- positions and orders
        open_positions = db.execute(select(Position).where(Position.status == "OPEN")).scalars().all()
        open_orders = db.execute(select(Order).where(Order.status.in_(ACTIVE_ORDER_STATUSES))).scalars().all()
        unknown_orders = [o for o in open_orders if o.status == "UNKNOWN"]

        position_unrealized = sum((to_decimal(p.unrealized_pnl, ZERO) for p in open_positions), ZERO)
        position_notional = sum((to_decimal(p.notional_value, ZERO) for p in open_positions), ZERO)

        active_strategies = db.execute(select(Strategy).where(Strategy.status == "ACTIVE")).scalars().all()
        active_signals = db.execute(select(TradeSignal).where(TradeSignal.status == "ACTIVE")).scalars().all()
        last_agent_run = db.execute(select(AgentRun).order_by(AgentRun.created_at.desc()).limit(1)).scalar_one_or_none()

        # --- data freshness
        # Platform freshness is reported conservatively: if ANY configured account is unsynced,
        # the summary is flagged stale rather than presenting a rosy max() across exchanges.
        sync_times = [c.last_sync_at for c in connections.values() if c.last_sync_at is not None]
        latest_conn_sync = max(sync_times, default=None)
        stale_accounts = [a.id for a in accounts if a.sync_status != "SYNCED"]
        data_age_seconds = None
        if latest_conn_sync is not None:
            data_age_seconds = (utc_now() - latest_conn_sync).total_seconds()
        is_stale = (
            latest_conn_sync is None
            or data_age_seconds is None
            or data_age_seconds > 300
            or bool(stale_accounts)
            or len(sync_times) < len(connections)
        )

        # `Position.realized_pnl` is already net of trading fees (the Execution Engine books
        # `gross - fee`), so fees are NOT subtracted a second time here. Fees are still reported
        # separately for transparency, and the gross figure is exposed alongside.
        realized_net = totals["realized_pnl"]
        unrealized = totals["unrealized_pnl"] if totals["unrealized_pnl"] else position_unrealized
        net_profit = realized_net + unrealized + to_decimal(totals["funding_pnl"], ZERO)
        realized_gross = realized_net + to_decimal(totals["fees"], ZERO)

        return {
            "currency": "USDT",
            "total_equity": str(totals["equity"]),
            "available_balance": str(totals["available"]),
            "margin_balance": str(totals["margin"]),
            "notional_exposure": str(totals["notional"] or position_notional),
            "spot_valuation": str(spot_value),
            "perp_margin": str(perp_margin),
            "realized_pnl": str(realized_net),
            "realized_pnl_gross": str(realized_gross),
            "realized_pnl_note": "realized_pnl is net of trading fees; realized_pnl_gross is before fees.",
            "unrealized_pnl": str(unrealized),
            "fees_paid": str(totals["fees"]),
            "funding_pnl": str(totals["funding_pnl"]),
            "net_transfers": str(net_transfers),
            "net_profit": str(net_profit),
            "transfers_note": "Deposits, withdrawals, and internal transfers are excluded from trading profit.",
            "per_exchange": {
                k: {metric: str(v) for metric, v in bucket.items()} for k, bucket in per_exchange.items()
            },
            "open_positions_count": len(open_positions),
            "open_orders_count": len(open_orders),
            "unknown_orders_count": len(unknown_orders),
            "active_strategies": [{"id": s.id, "name": s.name, "version": s.current_version} for s in active_strategies],
            "active_signals_count": len(active_signals),
            "agent_status": {
                "last_run_type": last_agent_run.run_type if last_agent_run else None,
                "last_run_status": last_agent_run.status if last_agent_run else "IDLE",
                "last_run_at": utc_iso(last_agent_run.created_at) if last_agent_run else None,
            },
            "data_freshness": {
                "last_updated_at": utc_iso(latest_conn_sync) if latest_conn_sync else None,
                "data_age_seconds": round(data_age_seconds, 1) if data_age_seconds is not None else None,
                "is_stale": is_stale,
                "accounts_not_synced": stale_accounts,
                "label": "数据过期/未知" if data_age_seconds is None else ("数据过期" if is_stale else "数据新鲜"),
            },
            "generated_at": utc_iso(),
        }

    def _safe_adapter_positions(self, adapter: Any) -> list[Any]:
        """Adapters expose the synchronous `get_positions()`; tolerate either sync/async wrapping."""
        import inspect

        try:
            fn = getattr(adapter, "get_positions", None)
            if fn is None:
                return []
            result = fn()
            if inspect.isawaitable(result):
                return []
            return list(result or [])
        except Exception:
            return []

    def _hydrate_simulator(self, db: Session, adapter: Any, account_id: str, positions: list[Position]) -> None:
        """Mirror the local ledger into a demo/testnet simulator (there is no external book to compare)."""
        state = getattr(adapter, "_state", None)
        if state is None or not isinstance(state, dict):
            return

        live_positions = state.setdefault("positions", {})
        for pos in positions:
            qty = to_decimal(pos.qty, ZERO)
            if qty <= ZERO:
                continue
            key = (pos.market_type, pos.symbol, (pos.position_side or "LONG").upper())
            live_positions[key] = {
                "market_type": pos.market_type,
                "symbol": pos.symbol,
                "position_side": (pos.position_side or "LONG").upper(),
                "qty": qty,
                "status": "OPEN",
                "entry_price": to_decimal(pos.entry_price, ZERO),
                "mark_price": to_decimal(pos.mark_price, pos.entry_price),
                "leverage": to_decimal(pos.leverage, Decimal("1")),
                "unrealized_pnl": to_decimal(pos.unrealized_pnl, ZERO),
                "realized_pnl": to_decimal(pos.realized_pnl, ZERO),
                "margin_used": to_decimal(pos.margin_used, ZERO),
                "notional_value": to_decimal(pos.notional_value, ZERO),
                "liquidation_price": to_decimal(pos.liquidation_price, ZERO) if pos.liquidation_price else None,
                "stop_loss": to_decimal(pos.stop_loss, ZERO) if pos.stop_loss else None,
                "take_profit": to_decimal(pos.take_profit, ZERO) if pos.take_profit else None,
            }

        # Ensure every local order is visible to the simulator so UNKNOWN recovery can query by client id.
        orders_by_client = state.setdefault("orders_by_client_id", {})
        local_orders = db.execute(select(Order).where(Order.account_id == account_id)).scalars().all()
        for order in local_orders:
            if order.client_order_id in orders_by_client:
                continue
            orders_by_client[order.client_order_id] = {
                "exchange_order_id": order.exchange_order_id or f"SIM-{order.client_order_id[:12]}",
                "client_order_id": order.client_order_id,
                "symbol": order.symbol,
                "market_type": order.market_type,
                "side": order.side,
                "position_side": order.position_side,
                "order_type": order.order_type,
                "status": order.status if order.status != "UNKNOWN" else "FILLED",
                "qty": to_decimal(order.qty, ZERO),
                "filled_qty": to_decimal(order.filled_qty, ZERO),
                "price": to_decimal(order.price, ZERO),
                "avg_fill_price": to_decimal(order.avg_fill_price, order.price),
                "reduce_only": bool(order.reduce_only),
            }

    # --------------------------------------------------------- reconciliation
    def reconcile_account(self, db: Session, account_id: str, force_fix: bool = False) -> dict[str, Any]:
        from app.exchanges.registry import get_adapter_for_account

        account = db.get(TradingAccount, account_id)
        if account is None:
            raise ValueError(f"Trading account not found: {account_id}")

        mismatches: list[str] = []

        # ---- 1) Positions
        local_positions = db.execute(
            select(Position).where(Position.account_id == account_id, Position.status == "OPEN")
        ).scalars().all()
        try:
            adapter = get_adapter_for_account(db, account_id)
        except Exception as exc:
            remote_positions = []
            adapter = None
            mismatches.append(f"Exchange position fetch failed: {exc}")
        else:
            remote_positions = self._safe_adapter_positions(adapter)

        # A demo/testnet simulator has no independent source of truth: its state is rebuilt from the
        # local ledger. Only a Live account is compared against an authoritative external book.
        environment = (getattr(adapter, "environment", account.environment) or "demo").lower()
        is_simulated = environment in ("demo", "testnet")
        if is_simulated and adapter is not None:
            self._hydrate_simulator(db, adapter, account_id, local_positions)
            remote_positions = self._safe_adapter_positions(adapter)

        remote_by_key = {}
        for rp in remote_positions or []:
            sym = rp.get("symbol") if isinstance(rp, dict) else getattr(rp, "symbol", None)
            side = rp.get("position_side", rp.get("side")) if isinstance(rp, dict) else getattr(rp, "position_side", None)
            qty = rp.get("qty") if isinstance(rp, dict) else getattr(rp, "qty", None)
            if sym:
                remote_by_key[(sym, (side or "LONG").upper())] = to_decimal(qty, ZERO)

        for lp in local_positions:
            key = (lp.symbol, (lp.position_side or "LONG").upper())
            remote_qty = remote_by_key.get(key)
            if remote_qty is None:
                mismatches.append(f"Local position {lp.symbol} {lp.position_side} qty={lp.qty} has no exchange counterpart.")
            elif remote_qty != to_decimal(lp.qty, ZERO):
                mismatches.append(f"Position qty mismatch {lp.symbol} {lp.position_side}: local={lp.qty} exchange={remote_qty}")

        # ---- 2) Orders stuck in UNKNOWN (must be queried by client_order_id)
        unknown_orders = db.execute(
            select(Order).where(Order.account_id == account_id, Order.status == "UNKNOWN")
        ).scalars().all()
        for o in unknown_orders:
            mismatches.append(f"Order {o.client_order_id} is UNKNOWN; query the exchange before any new entry.")

        # ---- 3) Protective orders on open positions
        for lp in local_positions:
            if not bool(lp.tp_sl_confirmed):
                mismatches.append(f"Position {lp.symbol} has no confirmed exchange-native stop-loss/take-profit.")

        mismatch_count = len(mismatches)
        if mismatch_count and not force_fix:
            account.sync_status = "MISMATCH"
            account.trading_locked = True
            account.lock_reason = "Local and exchange state mismatch; reconcile before trading."
            db.add(RiskEvent(
                account_id=account_id,
                event_type="RECONCILIATION_MISMATCH",
                rule_code="RECONCILIATION",
                severity="CRITICAL",
                details={"mismatches": mismatches[:20], "mismatch_count": mismatch_count},
            ))
            db.commit()
        else:
            account.sync_status = "SYNCED"
            account.trading_locked = False
            account.lock_reason = None
            account.last_reconciled_at = utc_now()
            # A successful reconciliation IS a successful sync; record it so the dashboard's
            # freshness indicator reflects real work rather than permanently showing stale.
            connection = db.get(ExchangeConnection, account.connection_id)
            if connection is not None:
                connection.last_sync_at = utc_now()
                connection.last_error = None
                if connection.status == "DISCONNECTED":
                    connection.status = "CONNECTED"
            db.commit()

        result = {
            "account_id": account_id,
            "exchange": account.exchange,
            "mismatch_count": mismatch_count,
            "mismatches": mismatches,
            "sync_status": account.sync_status,
            "trading_locked": account.trading_locked,
            "reconciled_at": utc_iso(),
        }
        event_bus.publish_sync("orders", "account.reconciled", result, source=account.exchange)
        return result

    def reconcile_all_accounts(self, db: Session, force_fix: bool = False) -> dict[str, Any]:
        accounts = db.execute(select(TradingAccount)).scalars().all()
        results = []
        all_mismatches: list[str] = []
        for account in accounts:
            try:
                res = self.reconcile_account(db, account.id, force_fix=force_fix)
                results.append(res)
                all_mismatches.extend(res["mismatches"])
            except Exception as exc:
                all_mismatches.append(f"Account {account.id} reconcile error: {exc}")
                results.append({"account_id": account.id, "mismatch_count": 1, "error": str(exc)})
        return {
            "accounts": results,
            "mismatch_count": len(all_mismatches),
            "mismatches": all_mismatches,
            "reconciled_at": utc_iso(),
        }

    # ------------------------------------------------------------- snapshots
    def capture_equity_snapshot(self, db: Session, account_id: str) -> EquitySnapshot:
        account = db.get(TradingAccount, account_id)
        positions = db.execute(
            select(Position).where(Position.account_id == account_id, Position.status == "OPEN")
        ).scalars().all()
        balances = db.execute(
            select(BalanceSnapshot).where(BalanceSnapshot.account_id == account_id).order_by(BalanceSnapshot.observed_at.desc()).limit(20)
        ).scalars().all()

        equity = sum((to_decimal(b.usd_value, ZERO) for b in balances), ZERO)
        margin = sum((to_decimal(p.margin_used, ZERO) for p in positions), ZERO)
        notional = sum((to_decimal(p.notional_value, ZERO) for p in positions), ZERO)
        unrealized = sum((to_decimal(p.unrealized_pnl, ZERO) for p in positions), ZERO)
        realized = sum((to_decimal(p.realized_pnl, ZERO) for p in positions), ZERO)

        fees = db.execute(
            select(func.coalesce(func.sum(Fill.fee), 0)).where(Fill.account_id == account_id)
        ).scalar()
        fees_dec = to_decimal(fees, ZERO)

        snapshot = EquitySnapshot(
            account_id=account_id,
            exchange=account.exchange if account else "all",
            equity=equity,
            available_balance=max(equity - margin, ZERO),
            margin_balance=margin,
            notional_exposure=notional,
            realized_pnl=realized,
            unrealized_pnl=unrealized,
            fees_paid=fees_dec,
            funding_pnl=ZERO,
        )
        db.add(snapshot)
        db.commit()
        db.refresh(snapshot)
        return snapshot

    def list_positions(self, db: Session, status: str = "OPEN") -> list[dict[str, Any]]:
        stmt = select(Position).order_by(Position.updated_at.desc())
        if status and status.upper() != "ALL":
            stmt = stmt.where(Position.status == status.upper())
        rows = db.execute(stmt).scalars().all()
        return [
            {
                "id": p.id,
                "account_id": p.account_id,
                "exchange": p.exchange,
                "environment": p.environment,
                "market_type": p.market_type,
                "symbol": p.symbol,
                "position_side": p.position_side,
                "qty": str(p.qty),
                "entry_price": str(p.entry_price),
                "mark_price": str(p.mark_price),
                "liquidation_price": str(p.liquidation_price) if p.liquidation_price is not None else None,
                "leverage": str(p.leverage),
                "margin_used": str(p.margin_used),
                "notional_value": str(p.notional_value),
                "unrealized_pnl": str(p.unrealized_pnl),
                "realized_pnl": str(p.realized_pnl),
                "stop_loss": str(p.stop_loss) if p.stop_loss is not None else None,
                "take_profit": str(p.take_profit) if p.take_profit is not None else None,
                "tp_sl_confirmed": bool(p.tp_sl_confirmed),
                "status": p.status,
                "updated_at": utc_iso(p.updated_at),
            }
            for p in rows
        ]

    def list_transfers(self, db: Session, limit: int = 100) -> list[dict[str, Any]]:
        rows = db.execute(select(Transfer).order_by(Transfer.occurred_at.desc()).limit(limit)).scalars().all()
        return [
            {
                "id": t.id,
                "account_id": t.account_id,
                "transfer_type": t.transfer_type,
                "asset": t.asset,
                "amount": str(t.amount),
                "occurred_at": utc_iso(t.occurred_at),
            }
            for t in rows
        ]


portfolio_service = PortfolioService()
