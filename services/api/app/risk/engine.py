from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.events import event_bus
from app.core.logging import get_logger, log_event
from app.core.precision import (
    ONE,
    ZERO,
    calc_position_sizing,
    calc_spread_bps,
    round_price_to_tick,
    round_qty_to_lot,
    to_decimal,
    utc_iso,
    utc_now,
)
from app.models.entities import (
    AuditLog,
    BalanceSnapshot,
    EquitySnapshot,
    ExchangeConnection,
    Fill,
    Instrument,
    JournalEntry,
    KillSwitchState,
    MarketSnapshot,
    Order,
    Position,
    RiskEvent,
    RiskProfile,
    SystemEvent,
    TradingAccount,
)
from app.schemas.domain import RiskLimitsSchema, TradePlanSchema

logger = get_logger("terminal.risk")


class AttrDict(dict):
    """Dict subclass supporting both key indexing and attribute access."""

    def __getattr__(self, item: str) -> Any:
        try:
            return self[item]
        except KeyError as exc:
            raise AttributeError(item) from exc

    def __setattr__(self, key: str, value: Any) -> None:
        self[key] = value


@dataclass
class RiskEvaluationResult:
    approved: bool
    rule_code: str
    reason: str
    sizing: dict[str, Decimal] = field(default_factory=dict)
    risk_event_id: str | None = None

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def to_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "rule_code": self.rule_code,
            "reason": self.reason,
            "sizing": {k: v for k, v in self.sizing.items()},
            "risk_event_id": self.risk_event_id,
        }

    def model_dump(self) -> dict[str, Any]:
        return self.to_dict()


# Alias per ARCHITECTURE.md
RiskCheckResult = RiskEvaluationResult


def default_risk_limits_dict() -> dict[str, Any]:
    return {
        "max_risk_per_trade_pct": str(settings.DEFAULT_MAX_RISK_PER_TRADE_PCT),
        "max_daily_loss_pct": str(settings.DEFAULT_MAX_DAILY_LOSS_PCT),
        "max_total_exposure_pct": str(settings.DEFAULT_MAX_TOTAL_EXPOSURE_PCT),
        "max_symbol_exposure_pct": str(settings.DEFAULT_MAX_SYMBOL_EXPOSURE_PCT),
        "max_open_positions": int(settings.DEFAULT_MAX_OPEN_POSITIONS),
        "max_leverage": str(settings.DEFAULT_MAX_LEVERAGE),
        "max_slippage_bps": str(settings.DEFAULT_MAX_SLIPPAGE_BPS),
        "max_spread_bps": str(settings.DEFAULT_MAX_SPREAD_BPS),
        "max_data_stale_seconds": int(settings.DEFAULT_MAX_DATA_STALE_SECONDS),
        "max_consecutive_api_failures": int(settings.DEFAULT_MAX_CONSECUTIVE_API_FAILURES),
        "max_consecutive_losses": int(settings.DEFAULT_MAX_CONSECUTIVE_LOSSES),
        "max_daily_trades": int(settings.DEFAULT_MAX_DAILY_TRADES),
        "account_paused": False,
    }


class RiskEngine:
    """
    Independent Deterministic Risk Engine (Section 3.7).
    Enforces all 10 hard risk rules, manages RiskProfile and KillSwitchState,
    calculates Decimal position sizing, and fails closed on any unexpected error.
    """

    def __init__(self) -> None:
        self.recovery_mode: str = "READY"  # READY | RECOVERY_MODE | RECOVERING
        self.recovery_reason: str | None = None
        self.consecutive_api_failures: dict[str, int] = {}

    def set_recovery_mode(
        self,
        mode: str,
        reason: str | None = None,
        db: Session | None = None,
    ) -> dict[str, Any]:
        normalized = mode.strip().upper()
        if normalized not in ("READY", "RECOVERY_MODE", "RECOVERING"):
            raise ValueError(f"Invalid recovery mode: {mode}")
        self.recovery_mode = normalized
        self.recovery_reason = reason
        if db is not None:
            db.add(
                SystemEvent(
                    component="risk_engine",
                    event_type=f"RECOVERY_MODE_{normalized}",
                    severity="WARNING" if normalized != "READY" else "INFO",
                    payload={"mode": normalized, "reason": reason},
                )
            )
            db.commit()
        return {"recovery_mode": self.recovery_mode, "reason": self.recovery_reason}

    def record_api_failure(self, db: Session, account_id: str, reason: str = "Exchange API failure") -> int:
        count = self.consecutive_api_failures.get(account_id, 0) + 1
        self.consecutive_api_failures[account_id] = count
        limits = self.get_limits(db, account_id)
        if count >= limits.max_consecutive_api_failures:
            self.trigger_kill_switch(
                db,
                scope=f"account:{account_id}",
                reason=f"Consecutive API failures ({count}) reached limit ({limits.max_consecutive_api_failures}): {reason}",
            )
        return count

    def reset_api_failures(self, account_id: str) -> None:
        self.consecutive_api_failures[account_id] = 0

    def _resolve_account(
        self,
        db: Session,
        account_id: str | None = None,
        exchange: str | None = None,
    ) -> TradingAccount | None:
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

    def get_or_create_profile(self, db: Session, account_id: str | None = None) -> RiskProfile:
        acc = self._resolve_account(db, account_id=account_id)
        resolved_account_id = acc.id if acc is not None else (account_id or "default")
        profile = db.scalar(select(RiskProfile).where(RiskProfile.account_id == resolved_account_id))
        if profile is None:
            profile = RiskProfile(
                account_id=resolved_account_id,
                limits_json=default_risk_limits_dict(),
                enabled=True,
            )
            db.add(profile)
            db.commit()
            db.refresh(profile)
        return profile

    def get_limits(self, db: Session, account_id: str | None = None) -> RiskLimitsSchema:
        profile = self.get_or_create_profile(db, account_id=account_id)
        merged = default_risk_limits_dict()
        if profile.limits_json:
            merged.update(profile.limits_json)
        return RiskLimitsSchema.model_validate(merged)

    def get_profile(self, db: Session, account_id: str | None = None) -> dict[str, Any]:
        profile = self.get_or_create_profile(db, account_id=account_id)
        limits = self.get_limits(db, account_id=profile.account_id)
        limits_dump = {
            k: (str(v) if isinstance(v, Decimal) else v)
            for k, v in limits.model_dump().items()
        }
        return AttrDict(
            {
                "id": profile.id,
                "account_id": profile.account_id,
                "enabled": profile.enabled,
                "limits": limits_dump,
                "limits_json": limits_dump,
                "updated_at": utc_iso(profile.updated_at),
                **limits_dump,
            }
        )

    def update_profile(
        self,
        db: Session,
        limits_update: RiskLimitsSchema | dict[str, Any],
        account_id: str | None = None,
        actor: str = "local_admin",
    ) -> dict[str, Any]:
        profile = self.get_or_create_profile(db, account_id=account_id)
        before_json = dict(profile.limits_json or default_risk_limits_dict())
        current = default_risk_limits_dict()
        current.update(before_json)

        if isinstance(limits_update, RiskLimitsSchema):
            incoming = limits_update.model_dump()
        elif isinstance(limits_update, dict):
            incoming = dict(limits_update)
        else:
            incoming = dict(vars(limits_update))

        for k, v in incoming.items():
            if v is not None and k in current:
                current[k] = str(v) if isinstance(v, Decimal) else v

        validated = RiskLimitsSchema.model_validate(current)
        after_json = {
            k: (str(v) if isinstance(v, Decimal) else v)
            for k, v in validated.model_dump().items()
        }
        profile.limits_json = after_json
        profile.updated_at = utc_now()

        db.add(
            AuditLog(
                actor=actor,
                action="UPDATE_RISK_PROFILE",
                resource=f"risk_profile:{profile.account_id}",
                before_json=before_json,
                after_json=after_json,
            )
        )
        db.commit()
        db.refresh(profile)

        event_bus.publish_sync(
            channel="risk",
            event_type="risk.profile_updated",
            data={"account_id": profile.account_id, "limits": after_json},
            source="risk_engine",
        )
        return self.get_profile(db, account_id=profile.account_id)

    def _record_rejection(
        self,
        db: Session,
        *,
        account_id: str,
        rule_code: str,
        reason: str,
        severity: str = "WARNING",
        signal_id: str | None = None,
        decision_id: str | None = None,
        plan_summary: dict[str, Any] | None = None,
        sizing: dict[str, Decimal] | None = None,
    ) -> RiskEvaluationResult:
        sizing_dict = sizing or {}
        serialized_sizing = {k: str(v) for k, v in sizing_dict.items()}
        risk_event_id: str | None = None
        try:
            evt = RiskEvent(
                account_id=account_id,
                signal_id=signal_id,
                order_id=None,
                event_type="TRADE_PLAN_REJECTED",
                rule_code=rule_code,
                severity=severity,
                details={
                    "reason": reason,
                    "decision_id": decision_id,
                    "trade_plan": plan_summary or {},
                    "sizing": serialized_sizing,
                },
            )
            db.add(evt)
            db.add(
                AuditLog(
                    actor="risk_engine",
                    action="RISK_REJECT",
                    resource=f"account:{account_id}",
                    before_json=plan_summary or {},
                    after_json={"rule_code": rule_code, "reason": reason, "sizing": serialized_sizing},
                )
            )
            db.commit()
            db.refresh(evt)
            risk_event_id = evt.id
        except Exception as log_exc:
            try:
                db.rollback()
            except Exception:
                pass
            log_event(logger, 40, "Failed to persist RiskEvent during rejection", error=str(log_exc))

        event_bus.publish_sync(
            channel="risk",
            event_type="risk.rejected",
            data={
                "risk_event_id": risk_event_id,
                "account_id": account_id,
                "rule_code": rule_code,
                "reason": reason,
                "severity": severity,
                "signal_id": signal_id,
                "decision_id": decision_id,
            },
            source="risk_engine",
        )
        return RiskEvaluationResult(
            approved=False,
            rule_code=rule_code,
            reason=reason,
            sizing=sizing_dict,
            risk_event_id=risk_event_id,
        )

    def _compute_account_equity(self, db: Session, account_id: str) -> Decimal:
        latest_eq = db.scalar(
            select(EquitySnapshot)
            .where(EquitySnapshot.account_id == account_id)
            .order_by(EquitySnapshot.observed_at.desc())
        )
        if latest_eq is not None and to_decimal(latest_eq.equity, ZERO) > ZERO:
            return to_decimal(latest_eq.equity)

        balances = list(
            db.scalars(
                select(BalanceSnapshot).where(BalanceSnapshot.account_id == account_id)
            ).all()
        )
        if balances:
            latest_by_asset: dict[str, BalanceSnapshot] = {}
            for b in sorted(balances, key=lambda x: x.observed_at):
                latest_by_asset[b.asset] = b
            total_usd = sum(
                (
                    to_decimal(b.usd_value, ZERO)
                    if to_decimal(b.usd_value, ZERO) > ZERO
                    else (to_decimal(b.total, ZERO) if b.asset == "USDT" else ZERO)
                )
                for b in latest_by_asset.values()
            )
            if total_usd > ZERO:
                return total_usd

        return Decimal("10000.0000")

    def _compute_daily_loss_and_streaks(
        self,
        db: Session,
        account_id: str,
        now: datetime,
    ) -> tuple[Decimal, Decimal, int, int]:
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        # Operator acknowledgement watermark (§3.7 rule 8: the lock holds "until the user
        # explicitly resets it"). Only activity after the reset counts, and no history is
        # deleted — the ledger stays append-only.
        profile = db.scalar(select(RiskProfile).where(RiskProfile.account_id == account_id))
        reset_at = profile.lock_reset_at if profile is not None else None
        window_start = max(day_start, reset_at) if reset_at is not None else day_start

        today_fills = list(
            db.scalars(
                select(Fill).where(
                    Fill.account_id == account_id,
                    Fill.occurred_at >= window_start,
                )
            ).all()
        )
        fills_net_pnl = sum(
            (to_decimal(f.realized_pnl, ZERO) - to_decimal(f.fee, ZERO) for f in today_fills),
            ZERO,
        )

        latest_eq = db.scalar(
            select(EquitySnapshot)
            .where(EquitySnapshot.account_id == account_id)
            .order_by(EquitySnapshot.observed_at.desc())
        )
        snap_realized = to_decimal(latest_eq.realized_pnl, ZERO) if latest_eq is not None else ZERO
        snap_unrealized = to_decimal(latest_eq.unrealized_pnl, ZERO) if latest_eq is not None else ZERO

        open_positions = list(
            db.scalars(
                select(Position).where(
                    Position.account_id == account_id,
                    Position.status == "OPEN",
                )
            ).all()
        )
        pos_unrealized = sum((to_decimal(p.unrealized_pnl, ZERO) for p in open_positions), ZERO)

        worst_realized = min(fills_net_pnl, snap_realized)
        worst_unrealized = min(pos_unrealized, snap_unrealized)

        daily_realized_loss = max(ZERO, -worst_realized)
        daily_unrealized_loss = max(ZERO, -worst_unrealized)

        # Consecutive losses from closed fills or journal entries
        recent_fills = list(
            db.scalars(
                select(Fill)
                .where(
                    Fill.account_id == account_id,
                    Fill.occurred_at >= window_start,
                )
                .order_by(Fill.occurred_at.desc())
                .limit(50)
            ).all()
        )
        consecutive_losses = 0
        closed_fills = [f for f in recent_fills if to_decimal(f.realized_pnl, ZERO) != ZERO]
        if closed_fills:
            for f in closed_fills:
                if to_decimal(f.realized_pnl, ZERO) < ZERO:
                    consecutive_losses += 1
                else:
                    break
        else:
            recent_journals = list(
                db.scalars(
                    select(JournalEntry)
                    .where(
                        JournalEntry.account_id == account_id,
                        JournalEntry.created_at >= window_start,
                    )
                    .order_by(JournalEntry.created_at.desc())
                    .limit(50)
                ).all()
            )
            for j in [j for j in recent_journals if to_decimal(j.net_realized_pnl, ZERO) != ZERO]:
                if to_decimal(j.net_realized_pnl, ZERO) < ZERO:
                    consecutive_losses += 1
                else:
                    break

        today_orders = list(
            db.scalars(
                select(Order).where(
                    Order.account_id == account_id,
                    Order.created_at >= window_start,
                    Order.status.in_(["SUBMITTED", "ACKNOWLEDGED", "PARTIALLY_FILLED", "FILLED"]),
                )
            ).all()
        )
        daily_trades_count = len(today_orders)

        return daily_realized_loss, daily_unrealized_loss, consecutive_losses, daily_trades_count

    def _is_kill_switch_enabled(self, db: Session, account_id: str) -> tuple[bool, str | None]:
        global_ks = db.scalar(
            select(KillSwitchState).where(
                KillSwitchState.scope == "global",
                KillSwitchState.enabled.is_(True),
            )
        )
        if global_ks is not None:
            return True, f"Global kill switch enabled: {global_ks.reason}"

        acc_ks = db.scalar(
            select(KillSwitchState).where(
                KillSwitchState.scope.in_([f"account:{account_id}", account_id]),
                KillSwitchState.enabled.is_(True),
            )
        )
        if acc_ks is not None:
            return True, f"Account kill switch enabled ({acc_ks.scope}): {acc_ks.reason}"

        return False, None

    def evaluate_trade_plan(
        self,
        db: Session,
        account_id: str | None,
        trade_plan: TradePlanSchema | dict[str, Any],
        signal_id: str | None = None,
        decision_id: str | None = None,
        simulate_error: bool = False,
    ) -> RiskEvaluationResult:
        """
        Deterministically evaluate a TradePlan against all 10 hard risk rules (Section 3.7).
        Fails closed (`RISK_ENGINE_FAIL_CLOSED`) on any unexpected exception.
        """
        fallback_account_id = account_id or "default"
        try:
            if simulate_error:
                raise RuntimeError("Simulated internal risk engine fault")

            if trade_plan is None:
                raise ValueError("trade_plan cannot be None")

            now = utc_now()

            # Extract raw fields safely for pre-validation checks (handles dict or mutated TradePlanSchema)
            if isinstance(trade_plan, dict):
                raw_dict = dict(trade_plan)
            elif isinstance(trade_plan, TradePlanSchema):
                raw_dict = {
                    "exchange": trade_plan.exchange,
                    "market_type": trade_plan.market_type,
                    "symbol": trade_plan.symbol,
                    "direction": trade_plan.direction,
                    "entry_type": trade_plan.entry_type,
                    "entry_price": trade_plan.entry_price,
                    "entry_zone": trade_plan.entry_zone,
                    "stop_loss": trade_plan.stop_loss,
                    "take_profit": trade_plan.take_profit,
                    "confidence": trade_plan.confidence,
                    "timeframe": trade_plan.timeframe,
                    "thesis": trade_plan.thesis,
                    "invalidation_conditions": trade_plan.invalidation_conditions,
                    "expires_at": trade_plan.expires_at,
                    "strategy_version": trade_plan.strategy_version,
                }
                if hasattr(trade_plan, "leverage"):
                    raw_dict["leverage"] = getattr(trade_plan, "leverage")
            else:
                raise TypeError(f"Unsupported trade_plan type: {type(trade_plan)!r}")

            exchange = str(raw_dict.get("exchange") or "binance").lower()
            market_type = str(raw_dict.get("market_type") or "usdt_perp").lower()
            symbol = str(raw_dict.get("symbol") or "BTC/USDT:USDT")
            direction = str(raw_dict.get("direction") or "LONG").upper()

            account = self._resolve_account(db, account_id=account_id, exchange=exchange)
            resolved_account_id = account.id if account is not None else fallback_account_id

            plan_summary = {
                "exchange": exchange,
                "market_type": market_type,
                "symbol": symbol,
                "direction": direction,
                "entry_price": str(raw_dict.get("entry_price")),
                "stop_loss": str(raw_dict.get("stop_loss")),
                "take_profit": str(raw_dict.get("take_profit")),
                "expires_at": str(raw_dict.get("expires_at")),
            }

            profile = self.get_or_create_profile(db, account_id=resolved_account_id)
            limits = self.get_limits(db, account_id=resolved_account_id)

            # Query latest market snapshot for Rule 1 and Rule 6
            snapshot = db.scalar(
                select(MarketSnapshot)
                .where(
                    MarketSnapshot.exchange == exchange,
                    MarketSnapshot.market_type == market_type,
                    MarketSnapshot.symbol == symbol,
                )
                .order_by(MarketSnapshot.received_at.desc(), MarketSnapshot.event_time.desc())
            )
            if snapshot is None:
                snapshot = db.scalar(
                    select(MarketSnapshot)
                    .where(
                        MarketSnapshot.exchange == exchange,
                        MarketSnapshot.symbol == symbol,
                    )
                    .order_by(MarketSnapshot.received_at.desc(), MarketSnapshot.event_time.desc())
                )

            # ------------------------------------------------------------------
            # RULE 1: Market data missing, is_stale=True, or older than max_data_stale_seconds
            # ------------------------------------------------------------------
            if snapshot is None:
                return self._record_rejection(
                    db,
                    account_id=resolved_account_id,
                    rule_code="STALE_MARKET_DATA",
                    reason=f"Rule 1 violation: Market snapshot missing for {exchange} {market_type} {symbol}.",
                    severity="WARNING",
                    signal_id=signal_id,
                    decision_id=decision_id,
                    plan_summary=plan_summary,
                )

            ref_time = snapshot.received_at or snapshot.event_time
            if ref_time.tzinfo is None:
                ref_time = ref_time.replace(tzinfo=timezone.utc)
            data_age_seconds = (now - ref_time).total_seconds()
            if bool(snapshot.is_stale) or data_age_seconds > float(limits.max_data_stale_seconds):
                return self._record_rejection(
                    db,
                    account_id=resolved_account_id,
                    rule_code="STALE_MARKET_DATA",
                    reason=(
                        f"Rule 1 violation: Market data for {symbol} is stale "
                        f"(is_stale={snapshot.is_stale}, age={data_age_seconds:.1f}s > max={limits.max_data_stale_seconds}s)."
                    ),
                    severity="WARNING",
                    signal_id=signal_id,
                    decision_id=decision_id,
                    plan_summary=plan_summary,
                )

            # ------------------------------------------------------------------
            # RULE 2: TradePlan expired (expires_at <= utc_now()) or invalid
            # ------------------------------------------------------------------
            raw_expires = raw_dict.get("expires_at")
            if raw_expires is None:
                return self._record_rejection(
                    db,
                    account_id=resolved_account_id,
                    rule_code="PLAN_EXPIRED",
                    reason="Rule 2 violation: TradePlan expires_at is missing.",
                    severity="WARNING",
                    signal_id=signal_id,
                    decision_id=decision_id,
                    plan_summary=plan_summary,
                )
            if isinstance(raw_expires, str):
                try:
                    exp_dt = datetime.fromisoformat(raw_expires.replace("Z", "+00:00"))
                except Exception:
                    return self._record_rejection(
                        db,
                        account_id=resolved_account_id,
                        rule_code="PLAN_EXPIRED",
                        reason=f"Rule 2 violation: Invalid expires_at format: {raw_expires!r}",
                        severity="WARNING",
                        signal_id=signal_id,
                        decision_id=decision_id,
                        plan_summary=plan_summary,
                    )
            elif isinstance(raw_expires, datetime):
                exp_dt = raw_expires
            else:
                return self._record_rejection(
                    db,
                    account_id=resolved_account_id,
                    rule_code="PLAN_EXPIRED",
                    reason="Rule 2 violation: Invalid expires_at value.",
                    severity="WARNING",
                    signal_id=signal_id,
                    decision_id=decision_id,
                    plan_summary=plan_summary,
                )

            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=timezone.utc)
            if exp_dt <= now:
                return self._record_rejection(
                    db,
                    account_id=resolved_account_id,
                    rule_code="PLAN_EXPIRED",
                    reason=f"Rule 2 violation: TradePlan expired at {utc_iso(exp_dt)} (now={utc_iso(now)}).",
                    severity="WARNING",
                    signal_id=signal_id,
                    decision_id=decision_id,
                    plan_summary=plan_summary,
                )

            # ------------------------------------------------------------------
            # RULE 3: Account sync status != "SYNCED", trading_locked=True, account paused, or KillSwitch/Recovery
            # ------------------------------------------------------------------
            if self.recovery_mode != "READY":
                return self._record_rejection(
                    db,
                    account_id=resolved_account_id,
                    rule_code="ACCOUNT_NOT_SYNCED_OR_LOCKED",
                    reason=f"Rule 3 violation: System is in {self.recovery_mode} ({self.recovery_reason or 'awaiting recovery verification'}).",
                    severity="CRITICAL",
                    signal_id=signal_id,
                    decision_id=decision_id,
                    plan_summary=plan_summary,
                )

            ks_active, ks_reason = self._is_kill_switch_enabled(db, resolved_account_id)
            if ks_active:
                return self._record_rejection(
                    db,
                    account_id=resolved_account_id,
                    rule_code="ACCOUNT_NOT_SYNCED_OR_LOCKED",
                    reason=f"Rule 3 violation: {ks_reason}",
                    severity="CRITICAL",
                    signal_id=signal_id,
                    decision_id=decision_id,
                    plan_summary=plan_summary,
                )

            if account is None:
                return self._record_rejection(
                    db,
                    account_id=resolved_account_id,
                    rule_code="ACCOUNT_NOT_SYNCED_OR_LOCKED",
                    reason="Rule 3 violation: Trading account not found.",
                    severity="CRITICAL",
                    signal_id=signal_id,
                    decision_id=decision_id,
                    plan_summary=plan_summary,
                )

            if (
                not account.is_active
                or bool(account.trading_locked)
                or account.sync_status != "SYNCED"
                or bool(limits.account_paused)
                or not bool(profile.enabled)
            ):
                return self._record_rejection(
                    db,
                    account_id=resolved_account_id,
                    rule_code="ACCOUNT_NOT_SYNCED_OR_LOCKED",
                    reason=(
                        f"Rule 3 violation: Account cannot trade "
                        f"(sync_status={account.sync_status}, trading_locked={account.trading_locked}, "
                        f"account_paused={limits.account_paused}, is_active={account.is_active}, "
                        f"lock_reason={account.lock_reason})."
                    ),
                    severity="CRITICAL",
                    signal_id=signal_id,
                    decision_id=decision_id,
                    plan_summary=plan_summary,
                )

            # ------------------------------------------------------------------
            # RULE 4: Account has any unresolved UNKNOWN status order
            # ------------------------------------------------------------------
            unknown_order = db.scalar(
                select(Order).where(
                    Order.account_id == account.id,
                    Order.status == "UNKNOWN",
                )
            )
            if unknown_order is not None:
                return self._record_rejection(
                    db,
                    account_id=account.id,
                    rule_code="UNRESOLVED_UNKNOWN_ORDER",
                    reason=(
                        f"Rule 4 violation: Account has unresolved UNKNOWN order "
                        f"(id={unknown_order.id}, client_order_id={unknown_order.client_order_id}). "
                        "Query and reconcile before submitting new orders."
                    ),
                    severity="CRITICAL",
                    signal_id=signal_id,
                    decision_id=decision_id,
                    plan_summary=plan_summary,
                )

            # ------------------------------------------------------------------
            # RULE 5: Exchange connection withdraw permission, status != CONNECTED,
            #         market_type capability mismatch, or live trading lock
            # ------------------------------------------------------------------
            connection = db.scalar(
                select(ExchangeConnection).where(ExchangeConnection.id == account.connection_id)
            )
            has_withdraw = bool(connection.has_withdraw_permission) if connection is not None else False
            if connection is not None and isinstance(connection.permissions_json, dict):
                if connection.permissions_json.get("withdraw") or connection.permissions_json.get("can_withdraw"):
                    has_withdraw = True

            conn_status = connection.status if connection is not None else "DISCONNECTED"
            caps = account.capabilities or {}
            market_supported = True
            if market_type in caps and caps[market_type] is False:
                market_supported = False
            if f"{market_type}_enabled" in caps and caps[f"{market_type}_enabled"] is False:
                market_supported = False
            if "supported_market_types" in caps and isinstance(caps["supported_market_types"], list):
                if market_type not in caps["supported_market_types"]:
                    market_supported = False
            if "market_types" in caps and isinstance(caps["market_types"], list):
                if market_type not in caps["market_types"]:
                    market_supported = False

            env_name = (account.environment or (connection.environment if connection else "demo")).lower()
            live_locked = env_name == "live" and not bool(settings.LIVE_TRADING_ENABLED)

            if connection is None or conn_status != "CONNECTED" or has_withdraw or not market_supported or live_locked:
                return self._record_rejection(
                    db,
                    account_id=account.id,
                    rule_code="ACCOUNT_CAPABILITY_OR_LIVE_LOCK",
                    reason=(
                        f"Rule 5 violation: Connection status={conn_status}, "
                        f"has_withdraw_permission={has_withdraw}, market_supported={market_supported} ({market_type}), "
                        f"environment={env_name}, LIVE_TRADING_ENABLED={settings.LIVE_TRADING_ENABLED}."
                    ),
                    severity="CRITICAL",
                    signal_id=signal_id,
                    decision_id=decision_id,
                    plan_summary=plan_summary,
                )

            # ------------------------------------------------------------------
            # RULE 6: Spread exceeds max_spread_bps
            # ------------------------------------------------------------------
            spread_bps = to_decimal(snapshot.spread_bps, ZERO)
            if spread_bps <= ZERO and snapshot.bid > ZERO and snapshot.ask > ZERO:
                spread_bps = calc_spread_bps(snapshot.bid, snapshot.ask)
            if spread_bps > limits.max_spread_bps:
                return self._record_rejection(
                    db,
                    account_id=account.id,
                    rule_code="SPREAD_TOO_WIDE",
                    reason=(
                        f"Rule 6 violation: Current spread {spread_bps} bps exceeds "
                        f"max_spread_bps {limits.max_spread_bps} bps for {symbol}."
                    ),
                    severity="WARNING",
                    signal_id=signal_id,
                    decision_id=decision_id,
                    plan_summary=plan_summary,
                )

            # ------------------------------------------------------------------
            # RULE 7: Stop-loss distance is zero or stop-loss direction is invalid
            # ------------------------------------------------------------------
            raw_entry = raw_dict.get("entry_price")
            raw_zone = raw_dict.get("entry_zone")
            if raw_entry is None and isinstance(raw_zone, (list, tuple)) and len(raw_zone) == 2:
                entry_price = (to_decimal(raw_zone[0], ZERO) + to_decimal(raw_zone[1], ZERO)) / Decimal("2")
            else:
                entry_price = to_decimal(raw_entry, ZERO)
            stop_loss = to_decimal(raw_dict.get("stop_loss"), ZERO)
            take_profit = to_decimal(raw_dict.get("take_profit"), ZERO)

            is_long = direction in ("LONG", "BUY")
            stop_distance = abs(entry_price - stop_loss)
            invalid_sl = False
            if entry_price <= ZERO or stop_loss <= ZERO or take_profit <= ZERO or stop_distance <= ZERO:
                invalid_sl = True
            elif market_type == "spot" and direction == "SHORT":
                invalid_sl = True
            elif is_long and not (stop_loss < entry_price < take_profit):
                invalid_sl = True
            elif (not is_long) and not (take_profit < entry_price < stop_loss):
                invalid_sl = True

            if invalid_sl:
                return self._record_rejection(
                    db,
                    account_id=account.id,
                    rule_code="INVALID_STOP_LOSS",
                    reason=(
                        f"Rule 7 violation: Invalid stop-loss/take-profit geometry for {direction} {market_type} "
                        f"(entry={entry_price}, stop_loss={stop_loss}, take_profit={take_profit})."
                    ),
                    severity="WARNING",
                    signal_id=signal_id,
                    decision_id=decision_id,
                    plan_summary=plan_summary,
                )

            # Also validate full TradePlanSchema semantics
            try:
                validated_plan = (
                    trade_plan
                    if isinstance(trade_plan, TradePlanSchema)
                    else TradePlanSchema.model_validate(raw_dict)
                )
            except ValidationError as val_exc:
                err_text = str(val_exc)
                if "stop_loss" in err_text or "take_profit" in err_text or "SHORT" in err_text:
                    return self._record_rejection(
                        db,
                        account_id=account.id,
                        rule_code="INVALID_STOP_LOSS",
                        reason=f"Rule 7 violation: {err_text}",
                        severity="WARNING",
                        signal_id=signal_id,
                        decision_id=decision_id,
                        plan_summary=plan_summary,
                    )
                return self._record_rejection(
                    db,
                    account_id=account.id,
                    rule_code="PLAN_EXPIRED",
                    reason=f"Rule 2 violation: Invalid TradePlan schema: {err_text}",
                    severity="WARNING",
                    signal_id=signal_id,
                    decision_id=decision_id,
                    plan_summary=plan_summary,
                )

            # ------------------------------------------------------------------
            # RULE 8: Daily loss threshold reached, max consecutive losses, or max daily trades
            # ------------------------------------------------------------------
            equity = self._compute_account_equity(db, account.id)
            daily_realized_loss, daily_unrealized_loss, consecutive_losses, daily_trades_count = (
                self._compute_daily_loss_and_streaks(db, account.id, now)
            )
            total_daily_loss = daily_realized_loss + daily_unrealized_loss
            max_daily_loss_usd = (equity * limits.max_daily_loss_pct).quantize(
                Decimal("0.0001"), rounding=ROUND_HALF_UP
            )

            if (
                total_daily_loss >= max_daily_loss_usd
                or consecutive_losses >= limits.max_consecutive_losses
                or daily_trades_count >= limits.max_daily_trades
            ):
                return self._record_rejection(
                    db,
                    account_id=account.id,
                    rule_code="DAILY_LOSS_OR_TRADE_LIMIT_LOCK",
                    reason=(
                        f"Rule 8 violation: Daily loss={total_daily_loss} (limit={max_daily_loss_usd}), "
                        f"consecutive_losses={consecutive_losses} (limit={limits.max_consecutive_losses}), "
                        f"daily_trades={daily_trades_count} (limit={limits.max_daily_trades})."
                    ),
                    severity="CRITICAL",
                    signal_id=signal_id,
                    decision_id=decision_id,
                    plan_summary=plan_summary,
                )

            # ------------------------------------------------------------------
            # RULE 9 & RULE 10: Exposure, open positions, leverage, and precision / min notional
            # ------------------------------------------------------------------
            open_positions = list(
                db.scalars(
                    select(Position).where(
                        Position.account_id == account.id,
                        Position.status == "OPEN",
                    )
                ).all()
            )
            open_positions = [p for p in open_positions if to_decimal(p.qty, ZERO) > ZERO]

            if len(open_positions) >= limits.max_open_positions:
                return self._record_rejection(
                    db,
                    account_id=account.id,
                    rule_code="EXPOSURE_OR_POSITION_LIMIT",
                    reason=(
                        f"Rule 9 violation: Open positions count ({len(open_positions)}) "
                        f"reached max_open_positions ({limits.max_open_positions})."
                    ),
                    severity="WARNING",
                    signal_id=signal_id,
                    decision_id=decision_id,
                    plan_summary=plan_summary,
                )

            # Check requested or existing leverage against limits.max_leverage
            requested_leverage = to_decimal(raw_dict.get("leverage"), ZERO)
            if requested_leverage > limits.max_leverage:
                return self._record_rejection(
                    db,
                    account_id=account.id,
                    rule_code="EXPOSURE_OR_POSITION_LIMIT",
                    reason=(
                        f"Rule 9 violation: Requested leverage {requested_leverage}x "
                        f"exceeds max_leverage {limits.max_leverage}x."
                    ),
                    severity="WARNING",
                    signal_id=signal_id,
                    decision_id=decision_id,
                    plan_summary=plan_summary,
                )
            for pos in open_positions:
                if to_decimal(pos.leverage, ONE) > limits.max_leverage:
                    return self._record_rejection(
                        db,
                        account_id=account.id,
                        rule_code="EXPOSURE_OR_POSITION_LIMIT",
                        reason=(
                            f"Rule 9 violation: Open position {pos.symbol} leverage {pos.leverage}x "
                            f"exceeds max_leverage {limits.max_leverage}x."
                        ),
                        severity="WARNING",
                        signal_id=signal_id,
                        decision_id=decision_id,
                        plan_summary=plan_summary,
                    )

            # Lookup Instrument precision rules
            instrument = db.scalar(
                select(Instrument).where(
                    Instrument.exchange == exchange,
                    Instrument.market_type == market_type,
                    Instrument.canonical_symbol == symbol,
                )
            )
            if instrument is None:
                instrument = db.scalar(
                    select(Instrument).where(
                        Instrument.exchange == exchange,
                        Instrument.market_type == market_type,
                        Instrument.native_symbol == symbol,
                    )
                )

            tick_size = to_decimal(instrument.tick_size, Decimal("0.01")) if instrument else Decimal("0.01")
            lot_size = to_decimal(instrument.lot_size, Decimal("0.001")) if instrument else Decimal("0.001")
            min_qty = to_decimal(instrument.min_qty, Decimal("0.001")) if instrument else Decimal("0.001")
            min_notional = to_decimal(instrument.min_notional, Decimal("5.0")) if instrument else Decimal("5.0")
            contract_size = to_decimal(instrument.contract_size, ONE) if instrument else ONE
            inst_max_lev = to_decimal(instrument.max_leverage, Decimal("20")) if instrument else Decimal("20")

            if instrument is not None and instrument.status != "TRADING":
                return self._record_rejection(
                    db,
                    account_id=account.id,
                    rule_code="ACCOUNT_CAPABILITY_OR_LIVE_LOCK",
                    reason=f"Rule 5 violation: Instrument {symbol} status is {instrument.status} (not TRADING).",
                    severity="WARNING",
                    signal_id=signal_id,
                    decision_id=decision_id,
                    plan_summary=plan_summary,
                )

            rounded_entry = round_price_to_tick(entry_price, tick_size)
            rounded_stop = round_price_to_tick(stop_loss, tick_size)
            rounded_tp = round_price_to_tick(take_profit, tick_size)

            if rounded_entry <= ZERO or rounded_stop <= ZERO or abs(rounded_entry - rounded_stop) <= ZERO:
                return self._record_rejection(
                    db,
                    account_id=account.id,
                    rule_code="INVALID_STOP_LOSS",
                    reason=(
                        f"Rule 7 violation: After rounding to tick_size ({tick_size}), "
                        f"entry ({rounded_entry}) and stop_loss ({rounded_stop}) have zero distance."
                    ),
                    severity="WARNING",
                    signal_id=signal_id,
                    decision_id=decision_id,
                    plan_summary=plan_summary,
                )

            effective_lev = (
                min(limits.max_leverage, inst_max_lev)
                if market_type == "usdt_perp"
                else ONE
            )
            if requested_leverage > ZERO and market_type == "usdt_perp":
                effective_lev = min(effective_lev, requested_leverage)

            fee_rate = Decimal("0.0005") if market_type == "usdt_perp" else Decimal("0.0010")
            slippage_bps_used = min(limits.max_slippage_bps, Decimal("5"))

            sizing = calc_position_sizing(
                account_equity=equity,
                risk_rate=limits.max_risk_per_trade_pct,
                entry_price=rounded_entry,
                stop_price=rounded_stop,
                market_type=market_type,
                lot_size=lot_size,
                min_qty=min_qty,
                min_notional=min_notional,
                contract_size=contract_size,
                fee_rate=fee_rate,
                slippage_bps=slippage_bps_used,
                leverage=effective_lev,
            )

            current_total_exposure = sum(
                (
                    abs(to_decimal(p.notional_value, ZERO))
                    if to_decimal(p.notional_value, ZERO) > ZERO
                    else abs(to_decimal(p.qty, ZERO) * to_decimal(p.mark_price, rounded_entry))
                )
                for p in open_positions
            )
            current_symbol_exposure = sum(
                (
                    abs(to_decimal(p.notional_value, ZERO))
                    if to_decimal(p.notional_value, ZERO) > ZERO
                    else abs(to_decimal(p.qty, ZERO) * to_decimal(p.mark_price, rounded_entry))
                )
                for p in open_positions
                if p.symbol == symbol
            )

            max_total_notional = (equity * limits.max_total_exposure_pct).quantize(
                Decimal("0.0001"), rounding=ROUND_HALF_UP
            )
            max_symbol_notional = (equity * limits.max_symbol_exposure_pct).quantize(
                Decimal("0.0001"), rounding=ROUND_HALF_UP
            )

            # Exposure capping (Rule 9 sizing): risk-budget sizing alone can produce a notional that
            # breaches the per-symbol or total exposure ceiling. Rather than emitting an unusable
            # oversized order, cap the quantity down to the remaining allowed exposure and then
            # re-verify min_qty / min_notional. If the capped order is below exchange minimums the
            # order is still rejected with PRECISION_OR_MIN_NOTIONAL.
            remaining_total_room = max_total_notional - current_total_exposure
            remaining_symbol_room = max_symbol_notional - current_symbol_exposure
            allowed_notional = min(remaining_total_room, remaining_symbol_room)
            if sizing["notional"] > allowed_notional and allowed_notional > ZERO:
                max_base_qty = allowed_notional / rounded_entry
                if market_type == "usdt_perp":
                    capped_qty = round_qty_to_lot(max_base_qty / contract_size, lot_size)
                    capped_base_qty = capped_qty * contract_size
                else:
                    capped_qty = round_qty_to_lot(max_base_qty, lot_size)
                    capped_base_qty = capped_qty
                if capped_qty >= min_qty:
                    capped_notional = (capped_base_qty * rounded_entry).quantize(
                        Decimal("0.0001"), rounding=ROUND_HALF_UP
                    )
                    if capped_notional >= min_notional:
                        sizing["qty"] = capped_qty
                        sizing["base_qty"] = capped_base_qty
                        sizing["notional"] = capped_notional
                        sizing["margin_required"] = (capped_notional / effective_lev).quantize(
                            Decimal("0.0001"), rounding=ROUND_HALF_UP
                        )
                        sizing["estimated_fee"] = (capped_notional * fee_rate * Decimal("2")).quantize(
                            Decimal("0.0001"), rounding=ROUND_HALF_UP
                        )
                        sizing["capped_to_exposure_limit"] = True
                        sizing["exposure_cap_notional"] = str(allowed_notional)

            sizing.update(
                {
                    "rounded_entry_price": rounded_entry,
                    "rounded_stop_loss": rounded_stop,
                    "rounded_take_profit": rounded_tp,
                    "tick_size": tick_size,
                    "lot_size": lot_size,
                    "contract_size": contract_size,
                    "leverage": effective_lev,
                    "account_equity": equity,
                }
            )

            post_trade_total_exposure = current_total_exposure + sizing["notional"]
            post_trade_symbol_exposure = current_symbol_exposure + sizing["notional"]
            effective_portfolio_leverage = (
                (post_trade_total_exposure / equity).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
                if equity > ZERO
                else ZERO
            )

            # Check Rule 9 exposure & leverage limits
            if (
                post_trade_total_exposure > max_total_notional
                or post_trade_symbol_exposure > max_symbol_notional
                or effective_portfolio_leverage > limits.max_leverage
                or effective_lev > limits.max_leverage
            ):
                return self._record_rejection(
                    db,
                    account_id=account.id,
                    rule_code="EXPOSURE_OR_POSITION_LIMIT",
                    reason=(
                        f"Rule 9 violation: Post-trade exposure exceeds limits "
                        f"(post_total={post_trade_total_exposure} vs max_total={max_total_notional}, "
                        f"post_symbol={post_trade_symbol_exposure} vs max_symbol={max_symbol_notional}, "
                        f"portfolio_lev={effective_portfolio_leverage}x vs max_lev={limits.max_leverage}x)."
                    ),
                    severity="WARNING",
                    signal_id=signal_id,
                    decision_id=decision_id,
                    plan_summary=plan_summary,
                    sizing=sizing,
                )

            # ------------------------------------------------------------------
            # RULE 10: Rounded qty < min_qty, notional < min_notional, or live order cap
            # ------------------------------------------------------------------
            if sizing["qty"] <= ZERO or sizing["qty"] < min_qty or sizing["notional"] < min_notional:
                return self._record_rejection(
                    db,
                    account_id=account.id,
                    rule_code="PRECISION_OR_MIN_NOTIONAL",
                    reason=(
                        f"Rule 10 violation: Rounded qty ({sizing['qty']}) < min_qty ({min_qty}) "
                        f"or notional ({sizing['notional']}) < min_notional ({min_notional})."
                    ),
                    severity="WARNING",
                    signal_id=signal_id,
                    decision_id=decision_id,
                    plan_summary=plan_summary,
                    sizing=sizing,
                )

            if env_name == "live" and sizing["notional"] > settings.LIVE_MAX_ORDER_NOTIONAL_USDT:
                return self._record_rejection(
                    db,
                    account_id=account.id,
                    rule_code="PRECISION_OR_MIN_NOTIONAL",
                    reason=(
                        f"Rule 10 violation: Live order notional ({sizing['notional']} USDT) "
                        f"exceeds LIVE_MAX_ORDER_NOTIONAL_USDT ({settings.LIVE_MAX_ORDER_NOTIONAL_USDT} USDT)."
                    ),
                    severity="CRITICAL",
                    signal_id=signal_id,
                    decision_id=decision_id,
                    plan_summary=plan_summary,
                    sizing=sizing,
                )

            # All 10 deterministic risk rules passed
            event_bus.publish_sync(
                channel="risk",
                event_type="risk.approved",
                data={
                    "account_id": account.id,
                    "symbol": validated_plan.symbol,
                    "exchange": validated_plan.exchange,
                    "market_type": validated_plan.market_type,
                    "direction": validated_plan.direction,
                    "qty": str(sizing["qty"]),
                    "notional": str(sizing["notional"]),
                    "risk_budget": str(sizing["risk_budget"]),
                },
                source="risk_engine",
            )
            return RiskEvaluationResult(
                approved=True,
                rule_code="APPROVED",
                reason="All 10 deterministic risk rules passed.",
                sizing=sizing,
                risk_event_id=None,
            )

        except Exception as exc:
            log_event(
                logger,
                40,
                "RiskEngine unexpected exception; failing closed",
                error=str(exc),
                account_id=fallback_account_id,
            )
            return self._record_rejection(
                db,
                account_id=fallback_account_id,
                rule_code="RISK_ENGINE_FAIL_CLOSED",
                reason=f"Risk engine fail-closed due to unexpected error: {exc}",
                severity="CRITICAL",
                signal_id=signal_id,
                decision_id=decision_id,
            )

    def trigger_kill_switch(
        self,
        db: Session,
        scope: str = "global",
        reason: str = "Manual operator emergency stop",
    ) -> dict[str, Any]:
        normalized_scope = (scope or "global").strip()
        ks = db.scalar(select(KillSwitchState).where(KillSwitchState.scope == normalized_scope))
        now = utc_now()
        if ks is None:
            ks = KillSwitchState(
                scope=normalized_scope,
                enabled=True,
                reason=reason,
                changed_at=now,
            )
            db.add(ks)
        else:
            ks.enabled = True
            ks.reason = reason
            ks.changed_at = now

        # Lock affected trading accounts
        if normalized_scope == "global":
            accounts = list(db.scalars(select(TradingAccount)).all())
        else:
            target_id = (
                normalized_scope.split("account:", 1)[1]
                if normalized_scope.startswith("account:")
                else normalized_scope
            )
            accounts = list(
                db.scalars(select(TradingAccount).where(TradingAccount.id == target_id)).all()
            )

        for acc in accounts:
            acc.trading_locked = True
            acc.lock_reason = f"KILL_SWITCH ({normalized_scope}): {reason}"

        target_account_id = accounts[0].id if accounts else "global"
        risk_event = RiskEvent(
            account_id=target_account_id,
            event_type="KILL_SWITCH_TRIGGERED",
            rule_code="KILL_SWITCH_ACTIVE",
            severity="CRITICAL",
            details={"scope": normalized_scope, "reason": reason, "locked_accounts": [a.id for a in accounts]},
        )
        db.add(risk_event)
        db.add(
            AuditLog(
                actor="local_admin",
                action="TRIGGER_KILL_SWITCH",
                resource=f"kill_switch:{normalized_scope}",
                before_json={"enabled": False},
                after_json={"enabled": True, "reason": reason},
            )
        )
        db.commit()
        db.refresh(ks)
        db.refresh(risk_event)

        payload = {
            "id": ks.id,
            "scope": ks.scope,
            "enabled": ks.enabled,
            "reason": ks.reason,
            "changed_at": utc_iso(ks.changed_at),
            "risk_event_id": risk_event.id,
            "locked_accounts": [a.id for a in accounts],
        }
        event_bus.publish_sync(
            channel="risk",
            event_type="risk.kill_switch_triggered",
            data=payload,
            source="risk_engine",
        )
        event_bus.publish_sync(
            channel="system",
            event_type="system.kill_switch_triggered",
            data=payload,
            source="risk_engine",
        )
        return AttrDict(payload)

    def reset_daily_locks(
        self,
        db: Session,
        *,
        account_id: str | None = None,
        confirmation_code: str = "",
        reason: str = "Operator acknowledged the loss streak and reset daily counters",
        actor: str = "local_admin",
    ) -> dict[str, Any]:
        """
        §3.7 rule 8: the daily-loss / loss-streak lock must persist "until the user
        explicitly resets it".

        This records a watermark instead of deleting history: counters then only consider
        activity after this instant. Every order, fill and journal row is preserved.
        """
        if confirmation_code != "CONFIRM_RESET":
            raise ValueError("Invalid confirmation_code: must be exactly 'CONFIRM_RESET'")

        profile = self.get_or_create_profile(db, account_id=account_id)
        before = {
            "lock_reset_at": profile.lock_reset_at.isoformat() if profile.lock_reset_at else None,
            "lock_reset_reason": profile.lock_reset_reason,
        }
        now = utc_now()
        profile.lock_reset_at = now
        profile.lock_reset_reason = reason
        db.add(
            AuditLog(
                actor=actor,
                action="RESET_DAILY_LOCKS",
                resource=f"risk_profile:{profile.account_id}",
                before_json=before,
                after_json={"lock_reset_at": now.isoformat(), "lock_reset_reason": reason},
            )
        )
        db.add(
            RiskEvent(
                account_id=profile.account_id,
                event_type="DAILY_LOCKS_RESET",
                rule_code="OPERATOR_RESET",
                severity="INFO",
                details={"reason": reason, "reset_at": now.isoformat(), "actor": actor},
            )
        )
        db.commit()

        payload = {
            "account_id": profile.account_id,
            "lock_reset_at": utc_iso(now),
            "reason": reason,
            "status": "RESET",
        }
        event_bus.publish_sync(
            channel="system",
            event_type="system.daily_locks_reset",
            data=payload,
            source="risk_engine",
        )
        return AttrDict(payload)

    def resume_trading(
        self,
        db: Session,
        scope: str = "global",
        confirmation_code: str = "",
        reason: str = "Operator verified account reconciliation and risk status",
    ) -> dict[str, Any]:
        if confirmation_code != "CONFIRM_RESUME":
            raise ValueError("Invalid confirmation_code: must be exactly 'CONFIRM_RESUME'")

        normalized_scope = (scope or "global").strip()
        now = utc_now()
        ks = db.scalar(select(KillSwitchState).where(KillSwitchState.scope == normalized_scope))
        if ks is None:
            ks = KillSwitchState(
                scope=normalized_scope,
                enabled=False,
                reason=reason,
                changed_at=now,
            )
            db.add(ks)
        else:
            ks.enabled = False
            ks.reason = reason
            ks.changed_at = now

        if normalized_scope == "global":
            all_ks = list(db.scalars(select(KillSwitchState)).all())
            for item in all_ks:
                item.enabled = False
                item.reason = reason
                item.changed_at = now
            accounts = list(db.scalars(select(TradingAccount)).all())
        else:
            target_id = (
                normalized_scope.split("account:", 1)[1]
                if normalized_scope.startswith("account:")
                else normalized_scope
            )
            accounts = list(
                db.scalars(select(TradingAccount).where(TradingAccount.id == target_id)).all()
            )

        unlocked_accounts: list[str] = []
        still_locked_accounts: list[dict[str, str]] = []
        for acc in accounts:
            unknown_count = len(
                list(
                    db.scalars(
                        select(Order).where(
                            Order.account_id == acc.id,
                            Order.status == "UNKNOWN",
                        )
                    ).all()
                )
            )
            if unknown_count > 0:
                acc.trading_locked = True
                acc.lock_reason = f"Cannot unlock: {unknown_count} unresolved UNKNOWN order(s) remain"
                still_locked_accounts.append({"account_id": acc.id, "reason": acc.lock_reason})
            elif acc.sync_status != "SYNCED":
                acc.trading_locked = True
                acc.lock_reason = f"Cannot unlock: account sync_status is {acc.sync_status}"
                still_locked_accounts.append({"account_id": acc.id, "reason": acc.lock_reason})
            else:
                acc.trading_locked = False
                acc.lock_reason = None
                unlocked_accounts.append(acc.id)

        if not still_locked_accounts:
            self.recovery_mode = "READY"
            self.recovery_reason = None

        target_account_id = accounts[0].id if accounts else "global"
        risk_event = RiskEvent(
            account_id=target_account_id,
            event_type="KILL_SWITCH_RESUMED",
            rule_code="TRADING_RESUMED",
            severity="INFO",
            details={
                "scope": normalized_scope,
                "reason": reason,
                "unlocked_accounts": unlocked_accounts,
                "still_locked_accounts": still_locked_accounts,
            },
        )
        db.add(risk_event)
        db.add(
            AuditLog(
                actor="local_admin",
                action="RESUME_TRADING",
                resource=f"kill_switch:{normalized_scope}",
                before_json={"enabled": True},
                after_json={
                    "enabled": False,
                    "reason": reason,
                    "unlocked_accounts": unlocked_accounts,
                },
            )
        )
        db.commit()
        db.refresh(ks)

        payload = {
            "id": ks.id,
            "scope": ks.scope,
            "enabled": ks.enabled,
            "reason": ks.reason,
            "changed_at": utc_iso(ks.changed_at),
            "unlocked_accounts": unlocked_accounts,
            "still_locked_accounts": still_locked_accounts,
        }
        event_bus.publish_sync(
            channel="risk",
            event_type="risk.trading_resumed",
            data=payload,
            source="risk_engine",
        )
        return AttrDict(payload)

    def verify_protective_orders(
        self,
        db: Session,
        account_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Verify that all open positions and filled entry orders have confirmed native TP/SL protection.
        Emits a CRITICAL RiskEvent if any open position lacks confirmed TP/SL protection.
        """
        stmt = select(Position).where(Position.status == "OPEN")
        if account_id:
            stmt = stmt.where(Position.account_id == account_id)
        open_positions = [p for p in db.scalars(stmt).all() if to_decimal(p.qty, ZERO) > ZERO]

        unprotected: list[dict[str, Any]] = []
        for pos in open_positions:
            if not bool(pos.tp_sl_confirmed) or (pos.stop_loss is None and pos.take_profit is None):
                unprotected.append(
                    {
                        "position_id": pos.id,
                        "account_id": pos.account_id,
                        "symbol": pos.symbol,
                        "position_side": pos.position_side,
                        "tp_sl_confirmed": bool(pos.tp_sl_confirmed),
                        "stop_loss": str(pos.stop_loss) if pos.stop_loss is not None else None,
                        "take_profit": str(pos.take_profit) if pos.take_profit is not None else None,
                    }
                )
                db.add(
                    RiskEvent(
                        account_id=pos.account_id,
                        event_type="MISSING_TP_SL_PROTECTION",
                        rule_code="UNCONFIRMED_PROTECTION_ORDER",
                        severity="CRITICAL",
                        details={
                            "position_id": pos.id,
                            "symbol": pos.symbol,
                            "reason": "Open position does not have confirmed native TP/SL protection orders.",
                        },
                    )
                )
        if unprotected:
            db.commit()

        return {
            "verified": len(unprotected) == 0,
            "open_positions_checked": len(open_positions),
            "unprotected_positions": unprotected,
        }

    def get_risk_status(
        self,
        db: Session,
        account_id: str | None = None,
    ) -> dict[str, Any]:
        account = self._resolve_account(db, account_id=account_id)
        resolved_account_id = account.id if account is not None else (account_id or "default")
        limits = self.get_limits(db, account_id=resolved_account_id)
        now = utc_now()

        equity = self._compute_account_equity(db, resolved_account_id)
        daily_realized_loss, daily_unrealized_loss, consecutive_losses, daily_trades_count = (
            self._compute_daily_loss_and_streaks(db, resolved_account_id, now)
        )
        total_daily_loss = daily_realized_loss + daily_unrealized_loss
        daily_loss_pct = (
            (total_daily_loss / equity).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            if equity > ZERO
            else ZERO
        )

        open_positions = [
            p
            for p in db.scalars(
                select(Position).where(
                    Position.account_id == resolved_account_id,
                    Position.status == "OPEN",
                )
            ).all()
            if to_decimal(p.qty, ZERO) > ZERO
        ]
        total_exposure = sum(
            (
                abs(to_decimal(p.notional_value, ZERO))
                if to_decimal(p.notional_value, ZERO) > ZERO
                else abs(to_decimal(p.qty, ZERO) * to_decimal(p.mark_price, ZERO))
            )
            for p in open_positions
        )
        total_exposure_pct = (
            (total_exposure / equity).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            if equity > ZERO
            else ZERO
        )

        unknown_orders = list(
            db.scalars(
                select(Order).where(
                    Order.account_id == resolved_account_id,
                    Order.status == "UNKNOWN",
                )
            ).all()
        )

        ks_states = list(db.scalars(select(KillSwitchState)).all())
        global_ks_enabled = any(k.scope == "global" and k.enabled for k in ks_states)
        acc_ks_enabled = any(
            k.scope in (f"account:{resolved_account_id}", resolved_account_id) and k.enabled
            for k in ks_states
        )
        kill_switch_active = global_ks_enabled or acc_ks_enabled

        limits_dump = {
            k: (str(v) if isinstance(v, Decimal) else v)
            for k, v in limits.model_dump().items()
        }

        return AttrDict(
            {
                "account_id": resolved_account_id,
                "recovery_mode": self.recovery_mode,
                "recovery_reason": self.recovery_reason,
                "kill_switch_active": kill_switch_active,
                "kill_switch_global": global_ks_enabled,
                "kill_switches": [
                    {
                        "id": k.id,
                        "scope": k.scope,
                        "enabled": k.enabled,
                        "reason": k.reason,
                        "changed_at": utc_iso(k.changed_at),
                    }
                    for k in ks_states
                ],
                "trading_locked": bool(account.trading_locked) if account is not None else kill_switch_active,
                "lock_reason": account.lock_reason if account is not None else None,
                "sync_status": account.sync_status if account is not None else "SYNCED",
                "equity": equity,
                "daily_realized_loss": daily_realized_loss,
                "daily_unrealized_loss": daily_unrealized_loss,
                "daily_loss": total_daily_loss,
                "daily_loss_pct": daily_loss_pct,
                "max_daily_loss_pct": limits.max_daily_loss_pct,
                "total_exposure": total_exposure,
                "total_exposure_pct": total_exposure_pct,
                "max_total_exposure_pct": limits.max_total_exposure_pct,
                "max_symbol_exposure_pct": limits.max_symbol_exposure_pct,
                "open_positions_count": len(open_positions),
                "max_open_positions": limits.max_open_positions,
                "max_leverage": limits.max_leverage,
                "unresolved_unknown_orders": len(unknown_orders),
                "daily_trades_count": daily_trades_count,
                "max_daily_trades": limits.max_daily_trades,
                "consecutive_losses": consecutive_losses,
                "max_consecutive_losses": limits.max_consecutive_losses,
                "limits": limits_dump,
                "recent_events": self.get_risk_events(db, limit=10, account_id=resolved_account_id),
            }
        )

    def get_risk_events(
        self,
        db: Session,
        limit: int = 50,
        account_id: str | None = None,
    ) -> list[dict[str, Any]]:
        stmt = select(RiskEvent).order_by(RiskEvent.created_at.desc()).limit(max(1, min(limit, 500)))
        if account_id:
            stmt = (
                select(RiskEvent)
                .where(RiskEvent.account_id == account_id)
                .order_by(RiskEvent.created_at.desc())
                .limit(max(1, min(limit, 500)))
            )
        rows = list(db.scalars(stmt).all())
        return [
            AttrDict(
                {
                    "id": r.id,
                    "account_id": r.account_id,
                    "signal_id": r.signal_id,
                    "order_id": r.order_id,
                    "event_type": r.event_type,
                    "rule_code": r.rule_code,
                    "severity": r.severity,
                    "details": r.details or {},
                    "created_at": utc_iso(r.created_at),
                }
            )
            for r in rows
        ]


risk_engine = RiskEngine()
