"""Execution Worker: the only background consumer allowed to create opening orders."""
from __future__ import annotations

import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.core.precision import utc_now
from app.core.runtime_state import runtime_state
from app.execution.engine import execution_engine
from app.models.entities import Order
from app.models.outbox import OutboxMessage, OutboxStatus
from app.outbox.service import InvalidLeaseError, outbox_service

logger = get_logger("terminal.workers.execution")

ORDER_TOPIC = "execution.order"


def enqueue_order_task(
    db: Session,
    *,
    trade_plan: dict[str, Any],
    decision_id: str | None,
    signal_id: str | None,
    client_order_id: str,
    simulate_timeout: bool = False,
    account_id: str | None = None,
    exchange: str | None = None,
    environment: str | None = None,
):
    """Producer helper; persistence is committed by the caller with its business state."""
    eff_exchange = exchange or trade_plan.get("exchange")
    eff_environment = environment or trade_plan.get("environment")
    eff_account_id = account_id or trade_plan.get("account_id")

    # If absent, resolve once at enqueue time and persist them
    if eff_environment is None:
        try:
            from app.automation.control import get_control_row
            ctrl = get_control_row(db)
            if ctrl and ctrl.environment:
                eff_environment = ctrl.environment.lower()
        except Exception:
            pass

    if eff_account_id is None:
        acc = execution_engine._resolve_account(
            db,
            account_id=None,
            exchange=eff_exchange,
            environment=eff_environment,
        )
        if acc is not None:
            eff_account_id = acc.id
            if eff_environment is None:
                eff_environment = acc.environment

    return outbox_service.enqueue(
        db,
        topic=ORDER_TOPIC,
        aggregate_id=decision_id or signal_id or client_order_id,
        dedup_key=f"execution:{client_order_id}",
        payload={
            "trade_plan": trade_plan,
            "decision_id": decision_id,
            "signal_id": signal_id,
            "client_order_id": client_order_id,
            "simulate_timeout": bool(simulate_timeout),
            "account_id": eff_account_id,
            "exchange": eff_exchange,
            "environment": eff_environment,
        },
        max_attempts=5,
    )


def _settle_committed_tasks(db: Session, *, limit: int = 200) -> list[dict[str, Any]]:
    """
    Close the dual-commit gap using proof of a committed terminal Order.

    If a worker crashed after submit committed but before Outbox ack, a later Pause or
    Kill Switch must not turn that real execution into DEAD. A terminal Order with the
    same client_order_id is stronger evidence than a lease, so this path deliberately
    runs before the new-order gate and never submits anything to the exchange.
    """
    candidates = db.scalars(
        select(OutboxMessage)
        .where(
            OutboxMessage.topic == ORDER_TOPIC,
            OutboxMessage.status.in_([
                OutboxStatus.PENDING.value,
                OutboxStatus.CLAIMED.value,
                OutboxStatus.FAILED.value,
            ]),
        )
        .order_by(OutboxMessage.created_at.asc())
        .limit(limit)
    ).all()
    settled: list[dict[str, Any]] = []
    for message in candidates:
        client_order_id = str((message.payload_json or {}).get("client_order_id") or "")
        if not client_order_id:
            continue
        order = db.scalar(
            select(Order).where(
                Order.client_order_id == client_order_id,
                Order.status.in_(["FILLED", "REJECTED", "UNKNOWN"]),
            )
        )
        if order is None:
            continue
        now = utc_now()
        message.status = OutboxStatus.DONE.value
        message.processed_at = now
        message.claimed_by = None
        message.claimed_at = None
        message.lease_expires_at = None
        message.last_error = None
        settled.append(
            {
                "outbox_id": message.id,
                "order_id": order.id,
                "status": order.status,
                "client_order_id": client_order_id,
                "catch_up_ack": True,
            }
        )
    if settled:
        db.commit()
    return settled


def run_execution_worker_once(
    db: Session,
    *,
    worker_id: str,
    limit: int = 10,
    lease_seconds: int = 60,
) -> dict[str, Any]:
    """
    Claim and execute a bounded batch.

    Safety properties:
    - the durable gate is checked before claiming and again immediately before execution;
    - no task is executed when AUTO_TRADING_ENABLED=false, paused, killed, or recovering;
    - submit_trade_plan commits using the same deterministic client_order_id before ack,
      so a crash between those two steps is recovered by an idempotent replay, not a duplicate.
    """
    from app.workers.scheduler import worker_manager

    started = time.perf_counter()
    # Crash cleanup and catch-up acknowledgement are safe even while new orders are paused.
    reclaimed = outbox_service.reclaim_expired(db, topic=ORDER_TOPIC, auto_commit=True)
    settled = _settle_committed_tasks(db)

    allowed, reason = worker_manager.new_order_gate(db)
    if not allowed:
        result = {
            "claimed": 0,
            "completed": 0,
            "settled": len(settled),
            "reclaimed": len(reclaimed),
            "blocked_reason": reason,
            "orders": settled,
        }
        runtime_state.heartbeat(
            db,
            role="execution-worker",
            worker_id=worker_id,
            status="SKIPPED",
            duration_ms=int((time.perf_counter() - started) * 1000),
            details=result,
        )
        return result

    messages = outbox_service.claim(
        db,
        worker_id=worker_id,
        topics=[ORDER_TOPIC],
        lease_seconds=lease_seconds,
        limit=limit,
        auto_commit=True,
    )

    newly_settled = _settle_committed_tasks(db)
    settled.extend(newly_settled)
    settled_ids = {item["outbox_id"] for item in newly_settled}

    results: list[dict[str, Any]] = []
    completed = 0
    for message in messages:
        if message.id in settled_ids:
            completed += 1
            results.extend(item for item in newly_settled if item["outbox_id"] == message.id)
            continue
        allowed, reason = worker_manager.new_order_gate(db)
        if not allowed:
            outbox_service.nack(
                db,
                message.id,
                worker_id,
                error=reason,
                retry_delay=5,
                auto_commit=True,
            )
            results.append({"outbox_id": message.id, "status": "BLOCKED", "reason": reason})
            continue

        payload = dict(message.payload_json or {})
        environment = payload.get("environment")
        if not environment:
            err_msg = "Task payload lacks required environment; rejected deterministically without submitting order."
            logger.error(err_msg, extra={"structured_data": {"outbox_id": message.id}})
            message.attempts = message.max_attempts  # Terminal error -> transition to DEAD on nack
            outbox_service.nack(
                db,
                message.id,
                worker_id,
                error=err_msg,
                retry_delay=0,
                auto_commit=True,
            )
            results.append({"outbox_id": message.id, "status": "REJECTED_NO_ENVIRONMENT", "error": err_msg})
            continue

        try:
            order = execution_engine.submit_trade_plan(
                db,
                payload["trade_plan"],
                account_id=payload.get("account_id"),
                environment=environment,
                decision_id=payload.get("decision_id"),
                signal_id=payload.get("signal_id"),
                client_order_id=payload["client_order_id"],
                simulate_timeout=bool(payload.get("simulate_timeout", False)),
            )
            # REJECTED and UNKNOWN are valid terminal outcomes for this task. UNKNOWN
            # must never be blindly retried; reconciliation owns the next action.
            outbox_service.ack(db, message.id, worker_id, auto_commit=True)
            completed += 1
            results.append(
                {
                    "outbox_id": message.id,
                    "status": order.get("status"),
                    "order_id": order.get("id"),
                    "client_order_id": payload["client_order_id"],
                }
            )
        except InvalidLeaseError as exc:
            # The lease expired between business commit and ack. Another worker will
            # recover it; calling nack here would only raise the same exception again.
            db.rollback()
            logger.warning(
                "execution lease expired; leaving task for recovery",
                extra={"structured_data": {"outbox_id": message.id, "error": str(exc)}},
            )
            results.append({"outbox_id": message.id, "status": "LEASE_EXPIRED", "error": str(exc)})
            continue
        except Exception as exc:
            db.rollback()
            logger.exception("execution task failed", extra={"structured_data": {"outbox_id": message.id, "error": str(exc)}})
            outbox_service.nack(
                db,
                message.id,
                worker_id,
                error=str(exc),
                retry_delay=min(60, 2 ** max(0, message.attempts - 1)),
                auto_commit=True,
            )
            results.append({"outbox_id": message.id, "status": "FAILED", "error": str(exc)})

    result = {
        "claimed": len(messages),
        "completed": completed,
        "settled": len(settled),
        "reclaimed": len(reclaimed),
        "blocked_reason": None,
        "orders": settled + [item for item in results if not item.get("catch_up_ack")],
    }
    runtime_state.heartbeat(
        db,
        role="execution-worker",
        worker_id=worker_id,
        status="OK",
        duration_ms=int((time.perf_counter() - started) * 1000),
        details={"claimed": len(messages), "completed": completed, "settled": len(settled), "reclaimed": len(reclaimed)},
    )
    return result
