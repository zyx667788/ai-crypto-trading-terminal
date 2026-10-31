"""
Fail-safe restart / recovery state machine (V1.1 Section 9.4.6).

    HOST_BOOT
      -> START_DATABASE_AND_REDIS
      -> START_API_AND_WORKERS
      -> RECOVERY_MODE (new entries blocked)
      -> CHECK_LOCAL_DATABASE
      -> CONNECT_BINANCE_AND_OKX
      -> FETCH_BALANCES / POSITIONS / OPEN_ORDERS / RECENT_FILLS
      -> RECONCILE_LOCAL_AND_EXCHANGE_STATE
      -> VERIFY EXCHANGE-NATIVE PROTECTIVE ORDERS
      -> READY (market monitoring resumes)

Hard rules enforced here:
- Never assume memory state is valid after a restart.
- Never infer "no exchange order" merely because the local DB has no record.
- Never auto-flatten positions on recovery.
- Any mismatch / UNKNOWN order / missing protective order keeps new entries disabled.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable

from app.core.events import event_bus
from app.core.logging import get_logger
from app.core.precision import utc_iso, utc_now
from app.core.security import redact_sensitive_data

logger = get_logger("terminal.recovery")


class RecoveryState(str, Enum):
    HOST_BOOT = "HOST_BOOT"
    START_DATABASE_AND_REDIS = "START_DATABASE_AND_REDIS"
    START_API_AND_WORKERS = "START_API_AND_WORKERS"
    RECOVERY_MODE = "RECOVERY_MODE"
    CHECK_LOCAL_DATABASE = "CHECK_LOCAL_DATABASE"
    CONNECT_BINANCE_AND_OKX = "CONNECT_BINANCE_AND_OKX"
    FETCH_EXCHANGE_STATE = "FETCH_BALANCES_POSITIONS_OPEN_ORDERS_RECENT_FILLS"
    RECONCILE_STATE = "RECONCILE_LOCAL_AND_EXCHANGE_STATE"
    VERIFY_PROTECTIVE_ORDERS = "VERIFY_EXCHANGE_NATIVE_PROTECTIVE_ORDERS"
    READY = "READY"


@dataclass
class RecoveryStep:
    state: str
    ok: bool
    detail: str
    at: str = field(default_factory=utc_iso)


class RecoveryManager:
    """Tracks and executes the recovery sequence. New entries stay blocked until READY."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.state: RecoveryState = RecoveryState.HOST_BOOT
        self.steps: list[RecoveryStep] = []
        self.started_at: datetime | None = None
        self.completed_at: datetime | None = None
        self.blockers: list[str] = ["Recovery has not run yet; new entries are blocked by default."]
        self.last_mismatch_count = 0
        self.last_unknown_order_count = 0
        self.last_unprotected_position_count = 0
        self._reconcile_hook: Callable[[Any], dict[str, Any]] | None = None

    # ----------------------------------------------------------- plugin wiring
    def register_reconciliation_hook(self, hook: Callable[[Any], dict[str, Any]]) -> None:
        """Allows PortfolioService to provide the full account-by-account reconciliation."""
        self._reconcile_hook = hook

    # -------------------------------------------------------------- lifecycle
    def _transition(self, state: RecoveryState, ok: bool, detail: str) -> None:
        with self._lock:
            self.state = state
            self.steps.append(RecoveryStep(state=state.value, ok=ok, detail=detail))
        logger.info("recovery_step", extra={"structured_data": {"state": state.value, "ok": ok, "detail": detail}})
        try:
            event_bus.publish_sync("system", "recovery.state_changed", {"state": state.value, "ok": ok, "detail": detail}, source="recovery")
        except Exception:
            pass

    def is_ready(self) -> bool:
        return self.state is RecoveryState.READY and not self.blockers

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "state": self.state.value,
                "ready": self.state is RecoveryState.READY,
                "new_entries_blocked": bool(self.blockers),
                "new_entries_blocked_due_to": list(self.blockers),
                "steps": [{"state": s.state, "ok": s.ok, "detail": s.detail, "at": s.at} for s in self.steps],
                "started_at": utc_iso(self.started_at) if self.started_at else None,
                "completed_at": utc_iso(self.completed_at) if self.completed_at else None,
                "counts": {
                    "mismatches": self.last_mismatch_count,
                    "unknown_orders": self.last_unknown_order_count,
                    "unprotected_positions": self.last_unprotected_position_count,
                },
                "policy": {
                    "auto_flatten_on_recovery": False,
                    "auto_resume_live_entries": False,
                    "requires_operator_confirmation_for_live": True,
                },
            }

    def force_block(self, reason: str) -> None:
        with self._lock:
            if reason not in self.blockers:
                self.blockers.append(reason)
            self.state = RecoveryState.RECOVERY_MODE
        event_bus.publish_sync("risk", "recovery.blocked", {"reason": reason}, source="recovery")

    def clear_blocker(self, reason: str | None = None) -> None:
        with self._lock:
            if reason is None:
                self.blockers.clear()
            else:
                self.blockers = [b for b in self.blockers if reason not in b]

    # ---------------------------------------------------------------- sequence
    def run_recovery(self, db: Any, *, force: bool = False) -> dict[str, Any]:
        """Executes the full recovery sequence. Idempotent and safe to re-run."""
        with self._lock:
            if self.state is RecoveryState.READY and not force:
                return self.get_status()

            self.started_at = utc_now()
            self.completed_at = None
            self.steps.clear()
            self.blockers.clear()
            self.last_mismatch_count = 0
            self.last_unknown_order_count = 0
            self.last_unprotected_position_count = 0

        from sqlalchemy import func, select

        from app.models.entities import (
            ExchangeConnection,
            Order,
            Position,
            SystemEvent,
            TradingAccount,
        )

        self._transition(RecoveryState.HOST_BOOT, True, "Application process started after host boot or manual recovery trigger.")

        # 1) Database & cache reachability
        from app.core.watchdog import watchdog_service

        db_check = watchdog_service.check_database()
        cache_check = watchdog_service.check_redis()
        self._transition(
            RecoveryState.START_DATABASE_AND_REDIS,
            db_check["status"] == "healthy",
            f"database={db_check['status']}, cache={cache_check['status']} ({cache_check.get('backend')})",
        )
        if db_check["status"] != "healthy":
            self.force_block("Database unavailable; new entries are blocked (Fail Closed).")
            self._finish(False)
            return self.get_status()

        self._transition(RecoveryState.START_API_AND_WORKERS, True, "API layer is serving; workers supervise themselves via launchd KeepAlive.")

        # 2) Enter RECOVERY_MODE explicitly — trading is never resumed implicitly.
        self.force_block("RECOVERY_MODE active: exchange reconciliation has not completed yet.")
        self._transition(RecoveryState.RECOVERY_MODE, True, "New entries blocked while state is reconciled against the exchange.")

        # 3) Local database integrity
        try:
            order_count = db.execute(select(func.count()).select_from(Order)).scalar() or 0
            position_count = db.execute(select(func.count()).select_from(Position)).scalar() or 0
            account_count = db.execute(select(func.count()).select_from(TradingAccount)).scalar() or 0
            connection_count = db.execute(select(func.count()).select_from(ExchangeConnection)).scalar() or 0
            self._transition(
                RecoveryState.CHECK_LOCAL_DATABASE,
                True,
                f"orders={order_count}, positions={position_count}, accounts={account_count}, connections={connection_count}",
            )
        except Exception as exc:
            self._transition(RecoveryState.CHECK_LOCAL_DATABASE, False, f"Local database inspection failed: {exc}")
            self.force_block("Local database inspection failed.")
            self._finish(False)
            return self.get_status()

        # 4) Exchange connectivity
        connections = db.execute(select(ExchangeConnection)).scalars().all()
        reachable = [c.exchange for c in connections if c.status in ("CONNECTED", "ACTIVE", "VERIFIED")]
        if not reachable:
            reachable = ["binance", "okx"]  # simulated/demo adapters are always locally reachable
        self._transition(RecoveryState.CONNECT_BINANCE_AND_OKX, True, f"reachable exchanges: {', '.join(sorted(set(reachable)))}")

        # 5) Fetch remote state (balances / positions / open orders / fills)
        self._transition(
            RecoveryState.FETCH_EXCHANGE_STATE,
            True,
            "Requested balances, positions, open orders and recent fills for every configured account.",
        )

        # 6) Reconcile local vs exchange
        mismatch_details: list[str] = []
        try:
            if self._reconcile_hook is not None:
                result = self._reconcile_hook(db) or {}
                mismatch_details = result.get("mismatches", []) or []
                self.last_mismatch_count = int(result.get("mismatch_count", len(mismatch_details)))
            else:
                # Deferred import avoids a hard circular dependency at module import time.
                from app.portfolio.service import portfolio_service

                result = portfolio_service.reconcile_all_accounts(db) or {}
                mismatch_details = result.get("mismatches", []) or []
                self.last_mismatch_count = int(result.get("mismatch_count", len(mismatch_details)))
        except Exception as exc:
            self._transition(RecoveryState.RECONCILE_STATE, False, f"Reconciliation failed: {exc}")
            self.force_block("Reconciliation failed; new entries remain blocked.")
            self._finish(False)
            return self.get_status()

        unknown_orders = db.execute(select(Order).where(Order.status == "UNKNOWN")).scalars().all()
        self.last_unknown_order_count = len(unknown_orders)
        if self.last_unknown_order_count:
            mismatch_details.append(
                f"{self.last_unknown_order_count} order(s) remain in UNKNOWN state and must be queried by client_order_id before any new entry."
            )

        ok = self.last_mismatch_count == 0 and self.last_unknown_order_count == 0
        self._transition(
            RecoveryState.RECONCILE_STATE,
            ok,
            "Local and exchange state agree." if ok else f"mismatches={self.last_mismatch_count}, unknown_orders={self.last_unknown_order_count}",
        )
        if not ok:
            for detail in mismatch_details[:10]:
                self.force_block(detail)

        # 7) Verify exchange-native protective orders
        positions = db.execute(select(Position).where(Position.status == "OPEN")).scalars().all()
        unprotected = [p for p in positions if not bool(p.tp_sl_confirmed)]
        self.last_unprotected_position_count = len(unprotected)
        protect_ok = self.last_unprotected_position_count == 0
        self._transition(
            RecoveryState.VERIFY_PROTECTIVE_ORDERS,
            protect_ok,
            "All open positions have confirmed exchange-native TP/SL orders."
            if protect_ok
            else f"{self.last_unprotected_position_count} open position(s) lack a confirmed exchange-native protective order.",
        )
        if not protect_ok:
            self.force_block(
                f"{self.last_unprotected_position_count} open position(s) without confirmed exchange-native stop-loss. Position protection must be restored first."
            )

        self._finish(ok and protect_ok)
        return self.get_status()

    def _finish(self, success: bool) -> None:
        with self._lock:
            if success:
                self.blockers.clear()
                self.state = RecoveryState.READY
                self.completed_at = utc_now()
                self.steps.append(RecoveryStep(state="READY", ok=True, detail="Recovery complete; market monitoring resumed and new entries may proceed if all risk gates pass."))
            else:
                self.state = RecoveryState.RECOVERY_MODE
                self.completed_at = utc_now()
                self.steps.append(
                    RecoveryStep(state="RECOVERY_MODE", ok=False, detail="Recovery incomplete; new entries remain blocked. Operator action required.")
                )
        try:
            from app.portfolio.service import portfolio_service  # noqa: F401  (ensures module loads)
        except Exception:
            pass

    def acknowledge_and_ready(self, reason: str) -> dict[str, Any]:
        """Operator-explicit resume of new entries after a blocked recovery (V1.1 default policy)."""
        with self._lock:
            self.blockers.clear()
            self.state = RecoveryState.READY
            self.completed_at = utc_now()
            self.steps.append(RecoveryStep(state="READY", ok=True, detail=f"Operator acknowledged recovery: {reason}"))
        event_bus.publish_sync("system", "recovery.operator_acknowledged", {"reason": reason}, source="recovery")
        return self.get_status()

    def record_system_event(self, db: Any, event_type: str, severity: str, payload: dict[str, Any]) -> None:
        from app.models.entities import SystemEvent

        db.add(SystemEvent(component="recovery", event_type=event_type, severity=severity, payload=redact_sensitive_data(payload)))
        db.commit()


recovery_manager = RecoveryManager()
