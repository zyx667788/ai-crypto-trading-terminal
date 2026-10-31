"""Single read-only order-entry gate.

Every component that can create or submit an order must ask the same question and
must get the same answer:

    - the scheduler before it enqueues an outbox task
    - the Execution Worker immediately before it submits to the exchange
    - the API when it reports "can create orders" to the dashboard

Before this module existed the answer was assembled in three different places, so
the dashboard could claim orders were allowed while the worker refused them (or the
other way round). The gate below is the only place that decides.

Rules, evaluated in a fixed order so the reported reason is deterministic:

    1. AUTO_TRADING_ENABLED            process-level master switch
    2. automation control row         running / paused / manual block
    3. global kill switch             operator emergency stop
    4. recovery readiness             checkpoint freshness, UNKNOWN orders, manual blocks

Any exception while evaluating a rule is treated as a refusal (fail closed): a gate
that cannot prove it is safe must not allow an order.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("terminal.automation.gate")

BLOCKING_SEVERITY = (
    "AUTO_TRADING_DISABLED",
    "KILL_SWITCH_ACTIVE",
    "MANUAL_BLOCK",
    "AUTOMATION_NOT_RUNNING",
    "AUTOMATION_PAUSED",
    "RECOVERY_NOT_READY",
    "ORDER_GATE_FAIL_CLOSED",
)


@dataclass(frozen=True)
class OrderGateDecision:
    """Immutable result of one gate evaluation."""

    allowed: bool
    reason: str
    blocked_by: list[str] = field(default_factory=list)
    checks: dict[str, Any] = field(default_factory=dict)
    environment: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "blocked_by": list(self.blocked_by),
            "checks": dict(self.checks),
            "environment": self.environment,
        }


def _evaluate(db: Session) -> OrderGateDecision:
    from app.automation import control as automation_control
    from app.core.recovery import recovery_manager
    from app.models.entities import KillSwitchState

    checks: dict[str, Any] = {}
    blocked: list[str] = []
    # The primary reason string keeps the historical contract used by the failure
    # reasons already stored on automation cycles: the bare code for conditions that
    # have no extra detail, "CODE: detail" where a human needs the operator text.
    reasons: list[str] = []

    def block(code: str, detail: str | None = None) -> None:
        blocked.append(code)
        reasons.append(code if not detail else f"{code}: {detail}")

    # 1. process master switch -------------------------------------------------
    auto_enabled = bool(getattr(settings, "AUTO_TRADING_ENABLED", False))
    checks["auto_trading_enabled"] = auto_enabled
    if not auto_enabled:
        block("AUTO_TRADING_DISABLED")

    # 2. durable automation control row ---------------------------------------
    status = dict(automation_control.get_status(db))
    environment = status.get("environment")
    checks["automation"] = {
        "running": bool(status.get("running")),
        "paused": bool(status.get("paused")),
        "pause_reason": status.get("pause_reason"),
        "manual_block_reason": status.get("manual_block_reason"),
        "revision": status.get("revision"),
        "environment": environment,
    }
    if status.get("manual_block_reason"):
        block("MANUAL_BLOCK", str(status["manual_block_reason"]))
    if not status.get("running"):
        block("AUTOMATION_NOT_RUNNING")
    if status.get("paused"):
        block(
            "AUTOMATION_PAUSED",
            str(status.get("pause_reason") or "operator pause"),
        )

    # 3. global kill switch ----------------------------------------------------
    global_kill = db.scalar(
        select(KillSwitchState).where(
            KillSwitchState.scope == "global",
            KillSwitchState.enabled.is_(True),
        )
    )
    checks["global_kill_switch"] = (
        None if global_kill is None else {"reason": global_kill.reason}
    )
    if global_kill is not None:
        block("KILL_SWITCH_ACTIVE", str(global_kill.reason or "global kill switch enabled"))

    # 4. recovery readiness ----------------------------------------------------
    try:
        recovery_status = recovery_manager.get_status(db)
        recovery_ready = bool(recovery_status.get("ready")) and not recovery_status.get(
            "new_entries_blocked_due_to"
        )
        checks["recovery"] = {
            "ready": recovery_ready,
            "state": recovery_status.get("state"),
            "blocked_due_to": list(recovery_status.get("new_entries_blocked_due_to") or []),
        }
        if not recovery_ready:
            block("RECOVERY_NOT_READY")
    except Exception as exc:  # pragma: no cover - defensive
        checks["recovery"] = {"ready": False, "error": str(exc)}
        block("RECOVERY_NOT_READY")

    # Result ------------------------------------------------------------------
    if blocked:
        primary = reasons[0]
        return OrderGateDecision(
            allowed=False,
            reason=primary,
            blocked_by=blocked,
            checks=checks,
            environment=environment,
        )
    return OrderGateDecision(
        allowed=True,
        reason="READY",
        blocked_by=[],
        checks=checks,
        environment=environment,
    )


def evaluate_order_gate(db: Session) -> OrderGateDecision:
    """Return the authoritative order-entry decision for this database state."""
    try:
        return _evaluate(db)
    except Exception as exc:
        logger.error(
            "order_gate_failed_closed",
            extra={"structured_data": {"error": str(exc)}},
        )
        try:
            db.rollback()
        except Exception:
            pass
        return OrderGateDecision(
            allowed=False,
            reason=f"ORDER_GATE_FAIL_CLOSED: {exc}",
            blocked_by=["ORDER_GATE_FAIL_CLOSED"],
            checks={"error": str(exc)},
        )


def order_gate_summary(db: Session) -> dict[str, Any]:
    """Dashboard-friendly view: the decision plus a compact explanation list."""
    decision = evaluate_order_gate(db)
    payload = decision.to_dict()
    payload["explanation"] = [
        f"{code}" for code in decision.blocked_by
    ] or ["All order-entry preconditions satisfied."]
    return payload
