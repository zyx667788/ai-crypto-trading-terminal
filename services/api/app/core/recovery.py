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
from datetime import datetime, timedelta, timezone
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


def _is_operator_account_lock(lock_reason: str | None) -> bool:
    if not lock_reason:
        return True
    reason_lower = lock_reason.lower()
    # Automated reconciliation mismatch flag is not an operator manual lock
    if "local and exchange state mismatch" in reason_lower:
        return False
    return True


def check_manual_kill_switch_state(
    db: Any,
    *,
    scope: str | None = None,
    exchange: str | None = None,
) -> tuple[bool, list[str]]:
    """
    Conservative helper to inspect manual KillSwitchState, TradingAccount locks,
    and automation control pause / manual block states.
    Fails closed: if any applicable manual block is active, returns (True, [reasons]).
    Preserves scope and exchange boundaries.
    """
    if db is None:
        try:
            from app.db.session import SessionLocal

            with SessionLocal() as session:
                return check_manual_kill_switch_state(session, scope=scope, exchange=exchange)
        except Exception as exc:
            return True, [f"Database manual-block check failed; failing closed: {exc}"]

    from sqlalchemy import select
    from app.models.entities import KillSwitchState, TradingAccount

    reasons: list[str] = []

    norm_target_scope = scope.strip().lower() if scope else None
    norm_target_exchange = exchange.strip().lower() if exchange else None

    # 1. KillSwitchState in DB
    try:
        active_ks = list(
            db.scalars(select(KillSwitchState).where(KillSwitchState.enabled.is_(True))).all()
        )
    except Exception as exc:
        logger.warning("kill_switch_query_failed", extra={"structured_data": {"error": str(exc)}})
        return True, [f"Kill switch state query failed; failing closed: {exc}"]

    for ks in active_ks:
        raw_scope = (ks.scope or "global").strip()
        ks_scope = raw_scope.lower()
        ks_reason = ks.reason or "Kill switch active"

        # A. Global kill switch always blocks everything
        if ks_scope == "global":
            reasons.append(f"Global kill switch active: {ks_reason}")
            continue

        # B. Exchange-scoped kill switch: "exchange:<name>" or known exchange name
        ks_exchange: str | None = None
        if ks_scope.startswith("exchange:"):
            ks_exchange = ks_scope.split("exchange:", 1)[1].strip().lower()
        elif ks_scope in ("binance", "okx"):
            ks_exchange = ks_scope

        if ks_exchange:
            if norm_target_exchange is None or norm_target_exchange == ks_exchange:
                reasons.append(f"Exchange kill switch active ({ks_exchange}): {ks_reason}")
            continue

        # C. Account-scoped kill switch: "account:<id>" or "<id>"
        ks_account_id = (
            raw_scope.split("account:", 1)[1].strip()
            if ks_scope.startswith("account:")
            else raw_scope
        )

        if norm_target_scope is not None:
            if norm_target_scope in (ks_scope, ks_account_id.lower(), f"account:{ks_account_id.lower()}"):
                reasons.append(f"Account kill switch active ({raw_scope}): {ks_reason}")
            # An explicit account scope must never fall through to another account's
            # exchange/global interpretation.
            continue

        # Check which exchange this account belongs to
        try:
            acc = db.scalar(select(TradingAccount).where(TradingAccount.id == ks_account_id))
            if acc:
                acc_exchange = (acc.exchange or "").strip().lower()
                if norm_target_exchange is None or norm_target_exchange == acc_exchange:
                    reasons.append(f"Account kill switch active on {acc.exchange} ({raw_scope}): {ks_reason}")
            else:
                if norm_target_exchange is None:
                    reasons.append(f"Account kill switch active ({raw_scope}): {ks_reason}")
        except Exception:
            if norm_target_exchange is None:
                reasons.append(f"Account kill switch active ({raw_scope}): {ks_reason}")

    # 2. TradingAccount manual locks in DB (only operator manual locks, not automated reconciliation locks)
    try:
        locked_accounts = list(
            db.scalars(select(TradingAccount).where(TradingAccount.trading_locked.is_(True))).all()
        )
        for acc in locked_accounts:
            if norm_target_scope is not None:
                target_account = norm_target_scope.split("account:", 1)[-1]
                if str(acc.id).lower() != target_account:
                    continue
            if not _is_operator_account_lock(acc.lock_reason):
                continue
            acc_exchange = (acc.exchange or "").strip().lower()
            if norm_target_exchange is None or norm_target_exchange == acc_exchange:
                lock_reason = acc.lock_reason or "Account trading manually locked"
                reasons.append(f"Trading account {acc.id} ({acc.exchange}) locked: {lock_reason}")
    except Exception as exc:
        logger.warning("trading_account_lock_query_failed", extra={"structured_data": {"error": str(exc)}})
        reasons.append(f"Trading-account lock query failed; failing closed: {exc}")

    # 3. AutomationControl manual_block_reason in DB (deferred import)
    # Ordinary 'paused' is handled independently by new_order_gate to avoid Pause <-> Recovery deadlock
    try:
        from app.models.automation import AutomationControl

        ctrls = list(db.scalars(select(AutomationControl)).all())
        for ctrl in ctrls:
            if ctrl.manual_block_reason and str(ctrl.manual_block_reason).strip():
                reasons.append(f"Automation manual block ({ctrl.id}): {ctrl.manual_block_reason.strip()}")
    except Exception as exc:
        reasons.append(f"Automation manual-block query failed; failing closed: {exc}")

    reasons = list(dict.fromkeys(reasons))
    return bool(reasons), reasons


def is_manual_kill_switch_blocked(
    db: Any,
    *,
    scope: str | None = None,
    exchange: str | None = None,
) -> bool:
    """Helper to conservatively check if manual kill switch or lock is active."""
    blocked, _ = check_manual_kill_switch_state(db, scope=scope, exchange=exchange)
    return blocked


class RecoveryManager:
    """Tracks and executes the recovery sequence. New entries stay blocked until READY."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.state: RecoveryState = RecoveryState.HOST_BOOT
        self.steps: list[RecoveryStep] = []
        self.started_at: datetime | None = None
        self.completed_at: datetime | None = None
        self.blockers: list[str] = ["Recovery has not run yet; new entries are blocked by default."]
        self._manual_blockers: list[str] = []
        self.last_mismatch_count = 0
        self.last_unknown_order_count = 0
        self.last_unprotected_position_count = 0
        self._reconcile_hook: Callable[[Any], dict[str, Any]] | None = None
        self._active_db: Any = None

    # ----------------------------------------------------------- plugin wiring
    def register_reconciliation_hook(self, hook: Callable[[Any], dict[str, Any]]) -> None:
        """Allows PortfolioService to provide the full account-by-account reconciliation."""
        self._reconcile_hook = hook

    # -------------------------------------------------------------- lifecycle
    def _persist(self, db: Any = None) -> bool:
        """Commit the local state as the shared checkpoint; readers never trust another process's memory."""
        target = db or self._active_db
        if target is None:
            return False
        try:
            from app.core.runtime_state import runtime_state

            runtime_state.save_recovery(target, self.get_status())
            return True
        except Exception as exc:
            try:
                target.rollback()
            except Exception:
                pass
            with self._lock:
                self.state = RecoveryState.RECOVERY_MODE
                reason = f"Recovery checkpoint persistence failed: {exc}"
                if reason not in self.blockers:
                    self.blockers.append(reason)
            logger.error("recovery_checkpoint_persist_failed", extra={"structured_data": {"error": str(exc)}})
            return False

    def _transition(self, state: RecoveryState, ok: bool, detail: str) -> None:
        with self._lock:
            self.state = state
            self.steps.append(RecoveryStep(state=state.value, ok=ok, detail=detail))
        logger.info("recovery_step", extra={"structured_data": {"state": state.value, "ok": ok, "detail": detail}})
        try:
            event_bus.publish_sync("system", "recovery.state_changed", {"state": state.value, "ok": ok, "detail": detail}, source="recovery")
        except Exception:
            pass

    def is_ready(self, db_or_exchange: Any = None, *, exchange: str | None = None) -> bool:
        target_exchange = exchange
        db = None
        if isinstance(db_or_exchange, str):
            target_exchange = db_or_exchange
        elif db_or_exchange is not None:
            db = db_or_exchange

        if db is not None:
            status = self.get_status(db)
            if not status["ready"] or status["new_entries_blocked_due_to"]:
                return False
            if target_exchange is not None:
                blocked, _ = check_manual_kill_switch_state(db, exchange=target_exchange)
                return not blocked
            return True

        with self._lock:
            return self.state is RecoveryState.READY and not self.blockers

    def get_status(self, db: Any = None) -> dict[str, Any]:
        with self._lock:
            blockers = list(self.blockers)
            state_val = self.state.value
            is_ready_val = self.state is RecoveryState.READY and not blockers
            steps_copy = [{"state": s.state, "ok": s.ok, "detail": s.detail, "at": s.at} for s in self.steps]
            started_at_str = utc_iso(self.started_at) if self.started_at else None
            completed_at_str = utc_iso(self.completed_at) if self.completed_at else None
            counts = {
                "mismatches": self.last_mismatch_count,
                "unknown_orders": self.last_unknown_order_count,
                "unprotected_positions": self.last_unprotected_position_count,
            }

        if db is not None:
            from app.core.runtime_state import runtime_state

            checkpoint = runtime_state.load_recovery(db)
            if checkpoint is not None:
                status = runtime_state.recovery_to_status(checkpoint)
                blockers = list(status["new_entries_blocked_due_to"])
                try:
                    from app.core.config import settings
                    from app.models.entities import Order
                    from sqlalchemy import select

                    updated_at = checkpoint.updated_at
                    if updated_at is not None:
                        if updated_at.tzinfo is None:
                            updated_at = updated_at.replace(tzinfo=timezone.utc)
                        age = (utc_now() - updated_at).total_seconds()
                        if age > settings.RECOVERY_CHECKPOINT_TTL_SECONDS:
                            status["ready"] = False
                            status["state"] = RecoveryState.RECOVERY_MODE.value
                            blockers.append(
                                f"Recovery checkpoint is stale ({age:.1f}s > {settings.RECOVERY_CHECKPOINT_TTL_SECONDS}s); watchdog reconciliation is required."
                            )
                    unknown_id = db.scalar(select(Order.id).where(Order.status == "UNKNOWN").limit(1))
                    if unknown_id is not None:
                        status["ready"] = False
                        status["state"] = RecoveryState.RECOVERY_MODE.value
                        blockers.append(f"Order {unknown_id} is UNKNOWN; reconcile it before any new entry.")
                except Exception as exc:
                    status["ready"] = False
                    status["state"] = RecoveryState.RECOVERY_MODE.value
                    blockers.append(f"Recovery runtime check failed; failing closed: {exc}")
                status["new_entries_blocked_due_to"] = list(dict.fromkeys(blockers))
                status["new_entries_blocked"] = bool(status["new_entries_blocked_due_to"])
                if status["new_entries_blocked"]:
                    status["ready"] = False
                blocked, db_reasons = check_manual_kill_switch_state(db)
                if blocked:
                    blockers = list(status["new_entries_blocked_due_to"])
                    for reason in db_reasons:
                        if reason not in blockers:
                            blockers.append(reason)
                    status["new_entries_blocked_due_to"] = blockers
                    status["new_entries_blocked"] = True
                    status["ready"] = False
                    if status["state"] == RecoveryState.READY.value:
                        status["state"] = RecoveryState.RECOVERY_MODE.value
                return status

        if db is not None:
            blocked, db_reasons = check_manual_kill_switch_state(db)
            if blocked:
                is_ready_val = False
                for r in db_reasons:
                    if r not in blockers:
                        blockers.append(r)
                if state_val == RecoveryState.READY.value:
                    state_val = RecoveryState.RECOVERY_MODE.value

        return {
            "state": state_val,
            "ready": is_ready_val,
            "new_entries_blocked": bool(blockers),
            "new_entries_blocked_due_to": blockers,
            "steps": steps_copy,
            "started_at": started_at_str,
            "completed_at": completed_at_str,
            "counts": counts,
            "policy": {
                "auto_flatten_on_recovery": False,
                "auto_resume_live_entries": False,
                "requires_operator_confirmation_for_live": True,
            },
        }

    def force_block(self, reason: str, *, manual: bool = True, db: Any = None) -> None:
        """Explicitly blocks recovery. External calls default to manual=True to prevent automatic recovery from auto-clearing them."""
        with self._lock:
            if reason not in self.blockers:
                self.blockers.append(reason)
            if manual and reason not in self._manual_blockers:
                self._manual_blockers.append(reason)
            self.state = RecoveryState.RECOVERY_MODE
        self._persist(db)
        try:
            event_bus.publish_sync("risk", "recovery.blocked", {"reason": reason}, source="recovery")
        except Exception:
            pass

    def _add_transient_blocker(self, reason: str) -> None:
        """Internal transient step blocker that does not register as a persistent manual block."""
        with self._lock:
            if reason not in self.blockers:
                self.blockers.append(reason)
            self.state = RecoveryState.RECOVERY_MODE
        try:
            event_bus.publish_sync("risk", "recovery.blocked", {"reason": reason}, source="recovery")
        except Exception:
            pass

    def clear_blocker(self, reason: str | None = None) -> None:
        with self._lock:
            if reason is None:
                self.blockers.clear()
                self._manual_blockers.clear()
            else:
                self.blockers = [b for b in self.blockers if reason not in b]
                self._manual_blockers = [b for b in self._manual_blockers if reason not in b]

    def check_manual_blocks(
        self,
        db: Any = None,
        *,
        scope: str | None = None,
        exchange: str | None = None,
    ) -> tuple[bool, list[str]]:
        """Conservative helper to inspect active manual blocks for this RecoveryManager."""
        is_blocked, reasons = check_manual_kill_switch_state(db, scope=scope, exchange=exchange)
        with self._lock:
            all_reasons = list(dict.fromkeys(reasons + self._manual_blockers))
        return bool(all_reasons), all_reasons

    def is_exchange_blocked(self, db: Any, exchange: str) -> bool:
        """Check if a specific exchange has an active manual kill switch or lock."""
        blocked, _ = self.check_manual_blocks(db, exchange=exchange)
        return blocked

    # ---------------------------------------------------------------- sequence
    def run_recovery(self, db: Any, *, force: bool = False) -> dict[str, Any]:
        """Run recovery and publish a durable checkpoint before any other process may rely on it."""
        with self._lock:
            will_run = not (self.state is RecoveryState.READY and not force)
            if will_run:
                self.state = RecoveryState.RECOVERY_MODE
                reason = "Recovery is in progress; new entries remain blocked."
                if reason not in self.blockers:
                    self.blockers.append(reason)
        self._active_db = db
        if will_run:
            self._persist(db)
        try:
            return self._run_recovery(db, force=force)
        finally:
            self._persist(db)
            self._active_db = None

    def _run_recovery(self, db: Any, *, force: bool = False) -> dict[str, Any]:
        """Executes the full recovery sequence. Idempotent and safe to re-run."""
        # Check persistent manual blocks in database
        is_blocked, db_manual_blocks = check_manual_kill_switch_state(db)

        with self._lock:
            # Combine persistent DB manual blocks with any explicit operator memory blockers
            active_manual_blocks = list(dict.fromkeys(db_manual_blocks + self._manual_blockers))

            if active_manual_blocks:
                self.state = RecoveryState.RECOVERY_MODE
                for b in active_manual_blocks:
                    if b not in self.blockers:
                        self.blockers.append(b)
            elif self.state is RecoveryState.READY and not force:
                return self.get_status()

            self.started_at = utc_now()
            self.completed_at = None
            self.steps.clear()
            # Retain active manual blockers; clear only transient check blockers
            self.blockers = list(active_manual_blocks)
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
            self._add_transient_blocker("Database unavailable; new entries are blocked (Fail Closed).")
            self._finish(False)
            return self.get_status()

        self._transition(RecoveryState.START_API_AND_WORKERS, True, "API layer is serving; workers supervise themselves via launchd KeepAlive.")

        # 2) Enter RECOVERY_MODE explicitly — trading is never resumed implicitly.
        self._add_transient_blocker("RECOVERY_MODE active: exchange reconciliation has not completed yet.")
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
            self._add_transient_blocker("Local database inspection failed.")
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

        # Pre-capture manual account locks so reconciliation does not wipe them out
        pre_locked_accounts = {
            acc.id: (acc.lock_reason or "Manual lock")
            for acc in db.scalars(select(TradingAccount).where(TradingAccount.trading_locked.is_(True))).all()
            if _is_operator_account_lock(acc.lock_reason)
        }

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
            self._add_transient_blocker("Reconciliation failed; new entries remain blocked.")
            self._finish(False)
            return self.get_status()

        # Restore any operator manual locks that portfolio_service reconciliation mistakenly auto-cleared
        if pre_locked_accounts:
            for acc_id, lock_reason in pre_locked_accounts.items():
                acc = db.get(TradingAccount, acc_id)
                if acc is not None and not acc.trading_locked:
                    acc.trading_locked = True
                    acc.lock_reason = lock_reason
            try:
                db.commit()
            except Exception:
                db.rollback()

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
                self._add_transient_blocker(detail)

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
            self._add_transient_blocker(
                f"{self.last_unprotected_position_count} open position(s) without confirmed exchange-native stop-loss. Position protection must be restored first."
            )

        # Clear transient reconciliation progress message now that reconciliation finished
        self.clear_blocker("exchange reconciliation has not completed yet")

        # Technical check vs manual block check
        technical_ok = ok and protect_ok
        overall_success = technical_ok and not active_manual_blocks
        self._finish(overall_success)
        return self.get_status()

    def _finish(self, success: bool) -> None:
        with self._lock:
            if success:
                self.blockers.clear()
                self._manual_blockers.clear()
                self.state = RecoveryState.READY
                self.completed_at = utc_now()
                self.steps.append(
                    RecoveryStep(
                        state="READY",
                        ok=True,
                        detail="Recovery complete; market monitoring resumed and new entries may proceed if all risk gates pass.",
                    )
                )
            else:
                self.state = RecoveryState.RECOVERY_MODE
                self.completed_at = utc_now()
                self.blockers = list(dict.fromkeys(self.blockers))
                detail = "Recovery incomplete; new entries remain blocked. Operator action required."
                manual_hints = [
                    b for b in self.blockers
                    if any(k in b.lower() for k in ("kill switch", "locked", "pause", "manual"))
                ]
                if manual_hints:
                    detail = f"Recovery blocked by manual / operator constraint: {manual_hints[0]}"
                self.steps.append(
                    RecoveryStep(state="RECOVERY_MODE", ok=False, detail=detail)
                )
        try:
            from app.portfolio.service import portfolio_service  # noqa: F401  (ensures module loads)
        except Exception:
            pass

    def acknowledge_and_ready(self, reason: str, db: Any = None) -> dict[str, Any]:
        """Operator-explicit resume of new entries after a blocked recovery (V1.1 default policy)."""
        db_blocked = False
        db_reasons: list[str] = []
        if db is not None:
            db_blocked, db_reasons = check_manual_kill_switch_state(db)
        else:
            try:
                from app.db.session import SessionLocal

                with SessionLocal() as session:
                    db_blocked, db_reasons = check_manual_kill_switch_state(session)
            except Exception as exc:
                db_blocked, db_reasons = True, [f"Database acknowledgement check failed; failing closed: {exc}"]

        with self._lock:
            if db_blocked:
                self.state = RecoveryState.RECOVERY_MODE
                for r in db_reasons:
                    if r not in self.blockers:
                        self.blockers.append(r)
                self.steps.append(
                    RecoveryStep(
                        state="RECOVERY_MODE",
                        ok=False,
                        detail=f"Operator acknowledge rejected: active manual block in database ({'; '.join(db_reasons)})",
                    )
                )
                logger.warning("recovery_acknowledge_rejected_manual_block", extra={"structured_data": {"reasons": db_reasons}})
                self._persist(db)
                return self.get_status()

            # No persistent manual blocks in DB
            self.blockers.clear()
            self._manual_blockers.clear()
            self.state = RecoveryState.READY
            self.completed_at = utc_now()
            self.steps.append(RecoveryStep(state="READY", ok=True, detail=f"Operator acknowledged recovery: {reason}"))
        try:
            event_bus.publish_sync("system", "recovery.operator_acknowledged", {"reason": reason}, source="recovery")
        except Exception:
            pass
        self._persist(db)
        return self.get_status()

    def record_system_event(self, db: Any, event_type: str, severity: str, payload: dict[str, Any]) -> None:
        from app.models.entities import SystemEvent

        db.add(SystemEvent(component="recovery", event_type=event_type, severity=severity, payload=redact_sensitive_data(payload)))
        db.commit()


recovery_manager = RecoveryManager()
