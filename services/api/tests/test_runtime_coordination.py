"""Durable RecoveryCheckpoint and WorkerHeartbeat coordination tests."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from sqlalchemy import delete, select

from app.core.precision import utc_now
from app.core.recovery import RecoveryManager, check_manual_kill_switch_state
from app.core.runtime_state import runtime_state
from app.db.session import SessionLocal
from app.models.entities import KillSwitchState, Order, TradingAccount
from app.models.runtime import RecoveryCheckpoint, WorkerHeartbeat


def ready_status():
    return {
        "state": "READY",
        "new_entries_blocked_due_to": [],
        "steps": [{"state": "READY", "ok": True, "detail": "test checkpoint", "at": utc_now().isoformat()}],
        "counts": {"mismatches": 0, "unknown_orders": 0, "unprotected_positions": 0},
        "started_at": utc_now().isoformat(),
        "completed_at": utc_now().isoformat(),
    }


def clear_runtime(db):
    db.execute(delete(RecoveryCheckpoint))
    db.execute(delete(WorkerHeartbeat))
    db.execute(delete(KillSwitchState))
    db.commit()


def test_new_manager_reads_ready_checkpoint_not_process_memory(seeded):
    db = seeded
    clear_runtime(db)
    runtime_state.save_recovery(db, ready_status())

    fresh_process_manager = RecoveryManager()
    with SessionLocal() as other:
        status = fresh_process_manager.get_status(other)
        assert status["state"] == "READY"
        assert fresh_process_manager.is_ready(other) is True
        assert status["revision"] >= 1


def test_missing_checkpoint_is_fail_closed(seeded):
    db = seeded
    clear_runtime(db)
    fresh_process_manager = RecoveryManager()
    with SessionLocal() as other:
        assert fresh_process_manager.is_ready(other) is False


def test_force_block_is_shared_and_reason_is_preserved(seeded):
    db = seeded
    clear_runtime(db)
    runtime_state.save_recovery(db, ready_status())
    manager = RecoveryManager()
    manager.force_block("Operator manual checkpoint block", db=db)

    fresh_process_manager = RecoveryManager()
    with SessionLocal() as other:
        status = fresh_process_manager.get_status(other)
        assert status["state"] == "RECOVERY_MODE"
        assert any("Operator manual checkpoint block" in reason for reason in status["new_entries_blocked_due_to"])
        assert fresh_process_manager.is_ready(other) is False


def test_kill_switch_dynamically_overrides_stale_ready_checkpoint(seeded):
    db = seeded
    clear_runtime(db)
    runtime_state.save_recovery(db, ready_status())
    db.add(KillSwitchState(scope="global", enabled=True, reason="another process stopped trading"))
    db.commit()

    fresh_process_manager = RecoveryManager()
    with SessionLocal() as other:
        status = fresh_process_manager.get_status(other)
        assert status["ready"] is False
        assert status["state"] == "RECOVERY_MODE"
        assert any("another process stopped trading" in reason for reason in status["new_entries_blocked_due_to"])


def test_worker_heartbeat_upsert_is_shared(seeded):
    db = seeded
    clear_runtime(db)
    runtime_state.heartbeat(db, role="execution-worker", worker_id="worker-a", status="OK", details={"claimed": 1})
    runtime_state.heartbeat(db, role="execution-worker", worker_id="worker-a", status="FAILED", error="simulated")

    with SessionLocal() as other:
        rows = runtime_state.list_workers(other)
    assert len(rows) == 1
    assert rows[0]["worker_id"] == "worker-a"
    assert rows[0]["status"] == "FAILED"
    assert rows[0]["last_error"] == "simulated"
    assert rows[0]["consecutive_failures"] == 1


def test_stale_ready_checkpoint_is_not_trusted(seeded):
    db = seeded
    clear_runtime(db)
    runtime_state.save_recovery(db, ready_status())
    checkpoint = runtime_state.load_recovery(db)
    checkpoint.updated_at = utc_now() - timedelta(seconds=120)
    db.commit()

    fresh_process_manager = RecoveryManager()
    with SessionLocal() as other:
        status = fresh_process_manager.get_status(other)
        assert status["ready"] is False
        assert status["state"] == "RECOVERY_MODE"
        assert any("stale" in reason for reason in status["new_entries_blocked_due_to"])
        assert fresh_process_manager.is_ready(other) is False


def test_unknown_order_invalidates_ready_checkpoint(seeded):
    db = seeded
    clear_runtime(db)
    runtime_state.save_recovery(db, ready_status())
    account = db.scalar(select(TradingAccount))
    order = Order(
        account_id=account.id,
        exchange="binance",
        environment="demo",
        market_type="usdt_perp",
        symbol="BTC/USDT:USDT",
        client_order_id=f"unknown-{utc_now().timestamp()}",
        order_type="LIMIT",
        side="BUY",
        qty=Decimal("0.001"),
        price=Decimal("68000"),
        status="UNKNOWN",
    )
    db.add(order)
    db.commit()
    try:
        fresh_process_manager = RecoveryManager()
        with SessionLocal() as other:
            status = fresh_process_manager.get_status(other)
            assert status["ready"] is False
            assert any("UNKNOWN" in reason for reason in status["new_entries_blocked_due_to"])
            assert fresh_process_manager.is_ready(other) is False
    finally:
        db.delete(order)
        db.commit()


def test_account_scope_does_not_fall_through_to_another_account(seeded):
    db = seeded
    clear_runtime(db)
    accounts = list(db.scalars(select(TradingAccount)).all())
    assert len(accounts) >= 2
    for account in accounts:
        account.trading_locked = False
        account.lock_reason = None
    first, second = accounts[0], accounts[1]
    db.add(KillSwitchState(scope=f"account:{first.id}", enabled=True, reason="only first account"))
    db.commit()
    try:
        blocked_first, _ = check_manual_kill_switch_state(db, scope=f"account:{first.id}")
        blocked_second, reasons_second = check_manual_kill_switch_state(db, scope=f"account:{second.id}")
        assert blocked_first is True
        assert blocked_second is False, reasons_second
    finally:
        for kill in db.scalars(select(KillSwitchState)).all():
            kill.enabled = False
        db.commit()


def test_manual_block_database_error_fails_closed():
    class BrokenSession:
        def scalars(self, *args, **kwargs):
            raise RuntimeError("simulated database outage")

    blocked, reasons = check_manual_kill_switch_state(BrokenSession())
    assert blocked is True
    assert any("failing closed" in reason for reason in reasons)
