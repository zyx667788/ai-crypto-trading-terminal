"""
Tests for Recovery state machine preserving manual blocks (V1.2 Stage 1).

Coverage:
1. Operator kill switch prevents run_recovery from auto-clearing blockers or entering READY.
2. Blocking reasons (KillSwitch reason, manual block reason, force_block reason) are preserved.
3. Exchange-scoped manual locks are not overwritten by global READY.
4. Normal flow without manual blocks can still acknowledge_and_ready.
5. Repeated run_recovery is idempotent and does not accumulate duplicate blockers.
6. Acknowledge and ready is rejected if persistent manual blocks remain active in DB.
7. Resuming trading via proper confirmation allows recovery to reach READY.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.recovery import (
    RecoveryState,
    check_manual_kill_switch_state,
    is_manual_kill_switch_blocked,
    recovery_manager,
)
from app.models.automation import AutomationControl
from app.models.entities import KillSwitchState, TradingAccount
from app.risk.engine import risk_engine


@pytest.fixture(autouse=True)
def _cleanup_recovery_state(seeded):
    """Ensure clean recovery state and clean DB kill switch/account locks before and after tests."""
    recovery_manager.clear_blocker()
    # Reset any DB kill switch states
    for ks in seeded.scalars(select(KillSwitchState)).all():
        seeded.delete(ks)
    for acc in seeded.scalars(select(TradingAccount)).all():
        acc.trading_locked = False
        acc.lock_reason = None
    for ctrl in seeded.scalars(select(AutomationControl)).all():
        seeded.delete(ctrl)
    seeded.commit()

    yield

    recovery_manager.clear_blocker()
    for ks in seeded.scalars(select(KillSwitchState)).all():
        seeded.delete(ks)
    for acc in seeded.scalars(select(TradingAccount)).all():
        acc.trading_locked = False
        acc.lock_reason = None
    for ctrl in seeded.scalars(select(AutomationControl)).all():
        seeded.delete(ctrl)
    seeded.commit()


def test_manual_kill_switch_blocks_recovery_and_preserves_reason(seeded):
    """
    Test requirement 1 & 2:
    After manual kill switch is enabled, run_recovery still blocks and preserves reason.
    """
    db = seeded

    # Clean run reaches READY
    initial_status = recovery_manager.run_recovery(db, force=True)
    assert initial_status["state"] == RecoveryState.READY.value
    assert initial_status["ready"] is True
    assert initial_status["new_entries_blocked"] is False

    # Trigger global kill switch
    risk_engine.trigger_kill_switch(
        db, scope="global", reason="Operator manual emergency shutdown"
    )
    db.commit()

    # Recovery runs (e.g. background watchdog or restart)
    status = recovery_manager.run_recovery(db, force=True)

    # Must stay blocked in RECOVERY_MODE and NOT transition to READY
    assert status["ready"] is False
    assert status["new_entries_blocked"] is True
    assert status["state"] == RecoveryState.RECOVERY_MODE.value
    assert recovery_manager.is_ready(db) is False
    assert recovery_manager.is_ready() is False

    # Blocker reason must be preserved
    blockers = status["new_entries_blocked_due_to"]
    assert any("Operator manual emergency shutdown" in b for b in blockers)
    assert any("Global kill switch active" in b for b in blockers)


def test_explicit_blocked_reason_preserved_across_recovery_runs(seeded):
    """
    Test requirement 2:
    Explicit blocked reasons from memory/operator are not wiped by run_recovery.
    """
    db = seeded

    # Operator sets explicit blocker
    explicit_reason = "Manual maintenance in progress: firmware update"
    recovery_manager.force_block(explicit_reason, manual=True)

    status_before = recovery_manager.get_status()
    assert status_before["ready"] is False
    assert explicit_reason in status_before["new_entries_blocked_due_to"]

    # Background watchdog / recovery execution
    status_after = recovery_manager.run_recovery(db, force=True)

    assert status_after["ready"] is False
    assert status_after["new_entries_blocked"] is True
    assert status_after["state"] == RecoveryState.RECOVERY_MODE.value
    assert any(explicit_reason in b for b in status_after["new_entries_blocked_due_to"])


def test_exchange_scoped_manual_lock_not_overwritten_by_global_ready(seeded):
    """
    Test requirement 3:
    Exchange-scoped manual lock is not overwritten by global READY,
    and scope/exchange boundaries are respected.
    """
    db = seeded

    # Setup: Binance-scoped kill switch
    binance_reason = "Binance custody maintenance by operator"
    ks = KillSwitchState(scope="exchange:binance", enabled=True, reason=binance_reason)
    db.add(ks)
    db.commit()

    # Recovery execution
    status = recovery_manager.run_recovery(db, force=True)

    # Global recovery must NOT be READY
    assert status["ready"] is False
    assert status["new_entries_blocked"] is True
    assert status["state"] == RecoveryState.RECOVERY_MODE.value
    assert recovery_manager.is_ready(db) is False

    # Reason preserves exchange scope
    blockers = status["new_entries_blocked_due_to"]
    assert any("binance" in b.lower() and binance_reason in b for b in blockers)

    # Conservative helper test: maintains scope/exchange limits
    blocked_global, reasons_global = check_manual_kill_switch_state(db)
    assert blocked_global is True
    assert any("binance" in r.lower() for r in reasons_global)

    blocked_binance, reasons_binance = check_manual_kill_switch_state(db, exchange="binance")
    assert blocked_binance is True
    assert any("binance" in r.lower() for r in reasons_binance)

    # OKX exchange must NOT be blocked by Binance-specific kill switch
    blocked_okx, reasons_okx = check_manual_kill_switch_state(db, exchange="okx")
    assert blocked_okx is False
    assert reasons_okx == []

    # RecoveryManager helpers
    assert recovery_manager.is_exchange_blocked(db, "binance") is True
    assert recovery_manager.is_exchange_blocked(db, "okx") is False


def test_trading_account_manual_lock_blocks_global_ready(seeded):
    """
    Test requirement 3 (account lock):
    Account-level manual trading lock on Binance prevents global READY.
    """
    db = seeded

    binance_account = db.scalar(select(TradingAccount).where(TradingAccount.exchange == "binance"))
    assert binance_account is not None
    binance_account.trading_locked = True
    binance_account.lock_reason = "Manual audit lock on Binance demo account"
    db.commit()

    status = recovery_manager.run_recovery(db, force=True)
    assert status["ready"] is False
    assert status["new_entries_blocked"] is True
    assert status["state"] == RecoveryState.RECOVERY_MODE.value

    blockers = status["new_entries_blocked_due_to"]
    assert any("Manual audit lock on Binance demo account" in b for b in blockers)
    assert any("binance" in b.lower() for b in blockers)

    # Scope limits: Binance is blocked, OKX is not blocked
    blocked_binance, _ = check_manual_kill_switch_state(db, exchange="binance")
    assert blocked_binance is True
    blocked_okx, _ = check_manual_kill_switch_state(db, exchange="okx")
    assert blocked_okx is False


def test_normal_flow_without_manual_block_can_acknowledge_and_ready(seeded):
    """
    Test requirement 4:
    When there is no manual block, normal flow can still acknowledge_and_ready.
    """
    db = seeded

    # Simulate a transient recovery failure
    recovery_manager.force_block("Transient network jitter during recovery", manual=False)
    status_blocked = recovery_manager.get_status()
    assert status_blocked["ready"] is False
    assert status_blocked["new_entries_blocked"] is True

    # Operator acknowledges and transitions to READY
    status_ready = recovery_manager.acknowledge_and_ready(
        "Operator inspected connectivity and confirmed ok", db=db
    )
    assert status_ready["ready"] is True
    assert status_ready["new_entries_blocked"] is False
    assert status_ready["state"] == RecoveryState.READY.value
    assert recovery_manager.is_ready(db) is True


def test_acknowledge_and_ready_rejected_while_manual_block_active_in_db(seeded):
    """
    Verify acknowledge_and_ready does NOT clear active persistent KillSwitch in DB.
    """
    db = seeded

    risk_engine.trigger_kill_switch(
        db, scope="global", reason="Emergency stop pending investigation"
    )
    db.commit()

    # Attempting to acknowledge and force READY must fail closed
    status = recovery_manager.acknowledge_and_ready("Operator trying to force resume", db=db)
    assert status["ready"] is False
    assert status["new_entries_blocked"] is True
    assert status["state"] == RecoveryState.RECOVERY_MODE.value
    assert any("Emergency stop pending investigation" in b for b in status["new_entries_blocked_due_to"])


def test_repeated_run_recovery_is_idempotent(seeded):
    """
    Test requirement 5:
    Repeated run_recovery calls are idempotent: state remains stable,
    blockers are deduplicated, and manual block is not lost or duplicated.
    """
    db = seeded

    risk_engine.trigger_kill_switch(
        db, scope="global", reason="Idempotency test manual lock"
    )
    db.commit()

    status1 = recovery_manager.run_recovery(db, force=True)
    status2 = recovery_manager.run_recovery(db, force=True)
    status3 = recovery_manager.run_recovery(db, force=False)

    assert status1["state"] == status2["state"] == status3["state"] == RecoveryState.RECOVERY_MODE.value
    assert status1["ready"] == status2["ready"] == status3["ready"] is False
    assert status1["new_entries_blocked"] == status2["new_entries_blocked"] == status3["new_entries_blocked"] is True

    # Blockers list is identical and deduplicated
    assert status1["new_entries_blocked_due_to"] == status2["new_entries_blocked_due_to"] == status3["new_entries_blocked_due_to"]
    assert len(status2["new_entries_blocked_due_to"]) == len(status1["new_entries_blocked_due_to"])


def test_resuming_kill_switch_allows_recovery_to_reach_ready(seeded):
    """
    Lifecycle test:
    1. Kill switch blocks recovery.
    2. Proper confirmation code resume lifts the kill switch in DB.
    3. Subsequent run_recovery reaches READY.
    """
    db = seeded

    risk_engine.trigger_kill_switch(db, scope="global", reason="Temporary risk halt")
    db.commit()

    status_blocked = recovery_manager.run_recovery(db, force=True)
    assert status_blocked["ready"] is False

    # Resume trading via risk_engine with explicit confirmation code
    risk_engine.resume_trading(
        db, scope="global", confirmation_code="CONFIRM_RESUME", reason="Maintenance complete"
    )
    db.commit()

    status_resumed = recovery_manager.run_recovery(db, force=True)
    assert status_resumed["ready"] is True
    assert status_resumed["new_entries_blocked"] is False
    assert status_resumed["state"] == RecoveryState.READY.value
    assert recovery_manager.is_ready(db) is True


def test_automation_control_manual_block_blocks_recovery(seeded):
    """
    Verify AutomationControl manual block prevents recovery from entering READY.
    """
    db = seeded

    ctrl = AutomationControl(
        id="global",
        running=True,
        paused=True,
        pause_reason="Security operator paused automation",
        manual_block_reason="Suspicious whale activity detected",
    )
    db.add(ctrl)
    db.commit()

    status = recovery_manager.run_recovery(db, force=True)
    assert status["ready"] is False
    assert status["state"] == RecoveryState.RECOVERY_MODE.value
    assert any("Suspicious whale activity detected" in b for b in status["new_entries_blocked_due_to"])


def test_ordinary_automation_pause_does_not_block_recovery(seeded):
    """
    Verify ordinary AutomationControl.paused (without manual_block_reason) does NOT block
    Recovery from being READY, avoiding Pause <-> Recovery deadlocks while new_order_gate blocks trading.
    """
    db = seeded

    ctrl = AutomationControl(
        id="global",
        running=True,
        paused=True,
        pause_reason="Routine operator pause",
        manual_block_reason=None,
    )
    db.add(ctrl)
    db.commit()

    status = recovery_manager.run_recovery(db, force=True)
    assert status["ready"] is True
    assert status["new_entries_blocked"] is False
    assert status["state"] == RecoveryState.READY.value
    assert recovery_manager.is_ready(db) is True
