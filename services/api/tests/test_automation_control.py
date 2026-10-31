from __future__ import annotations

from pathlib import Path
import shutil

import pytest
from alembic import command
from alembic.config import Config
import sqlalchemy as sa
from sqlalchemy import select

from app.automation.control import (
    can_create_orders,
    clear_manual_block,
    get_control_row,
    get_status,
    pause,
    resume,
    set_manual_block,
    start,
    stop,
)
from app.core.config import settings
from app.db.session import SessionLocal
from app.models.automation import AutomationControl
from app.models.entities import AuditLog, KillSwitchState


@pytest.fixture(autouse=True)
def _reset_automation_control():
    """Ensure clean automation control table before and after each test."""
    session = SessionLocal()
    try:
        session.query(AuditLog).filter(AuditLog.resource == "automation:global").delete()
        session.query(AutomationControl).delete()
        session.commit()
    finally:
        session.close()

    yield

    session = SessionLocal()
    try:
        session.query(AuditLog).filter(AuditLog.resource == "automation:global").delete()
        session.query(AutomationControl).delete()
        session.commit()
    finally:
        session.close()


def test_default_row_fail_closed(db):
    """
    Test that an empty database is auto-initialized on first read,
    and the default row is strictly Fail-Closed (running=False, paused=True, can_create_orders=False).
    """
    status = get_status(db)
    assert status.id == "global"
    assert status.running is False
    assert status.paused is True
    assert status.environment == "demo"
    assert status.revision == 1
    assert status.updated_by == "system"
    assert status.pause_reason == "Default initialized closed"
    assert status.manual_block_reason is None
    assert status.can_create_orders is False

    # Even with AUTO_TRADING_ENABLED=True, running=False + paused=True keeps orders blocked
    assert can_create_orders(db) is False


def test_auto_trading_disabled_gates(db, monkeypatch):
    """
    When AUTO_TRADING_ENABLED is False:
    1. can_create_orders must return False unconditionally.
    2. start() must refuse and raise ValueError.
    3. resume() must refuse and raise ValueError.
    """
    monkeypatch.setattr(settings, "AUTO_TRADING_ENABLED", False)

    assert can_create_orders(db) is False

    with pytest.raises(ValueError, match="AUTO_TRADING_ENABLED is False"):
        start(db, environment="demo", actor="tester")

    with pytest.raises(ValueError, match="AUTO_TRADING_ENABLED is False"):
        resume(db, actor="tester")


def test_start_pause_lifecycle_and_orders(db, monkeypatch):
    """
    Test full state transition: start -> can_create_orders -> pause -> resume -> stop.
    """
    monkeypatch.setattr(settings, "AUTO_TRADING_ENABLED", True)

    # 1. Start automation
    status = start(db, environment="testnet", actor="trader_alice")
    assert status.running is True
    assert status.paused is False
    assert status.environment == "testnet"
    assert status.updated_by == "trader_alice"
    assert status.pause_reason is None
    assert status.can_create_orders is True
    assert can_create_orders(db) is True
    rev_after_start = status.revision
    assert rev_after_start >= 2

    # 2. Pause automation
    paused_status = pause(db, reason="High market volatility", actor="risk_bot")
    assert paused_status.running is True
    assert paused_status.paused is True
    assert paused_status.pause_reason == "High market volatility"
    assert paused_status.updated_by == "risk_bot"
    assert paused_status.revision == rev_after_start + 1
    assert paused_status.can_create_orders is False
    assert can_create_orders(db) is False

    # 3. Resume automation
    resumed_status = resume(db, actor="trader_alice")
    assert resumed_status.running is True
    assert resumed_status.paused is False
    assert resumed_status.pause_reason is None
    assert resumed_status.updated_by == "trader_alice"
    assert resumed_status.revision == paused_status.revision + 1
    assert resumed_status.can_create_orders is True
    assert can_create_orders(db) is True

    # 4. Stop automation
    stopped_status = stop(db, reason="Market close", actor="trader_alice")
    assert stopped_status.running is False
    assert stopped_status.can_create_orders is False
    assert can_create_orders(db) is False


def test_cross_session_immediate_visibility(monkeypatch):
    """
    Verify that commits from session_a are immediately visible to session_b
    without relying on process memory or manual caching.
    """
    monkeypatch.setattr(settings, "AUTO_TRADING_ENABLED", True)

    session_a = SessionLocal()
    session_b = SessionLocal()

    try:
        # A starts automation
        start(session_a, environment="demo", actor="alice")

        # B reads status immediately
        b_status = get_status(session_b)
        assert b_status.running is True
        assert b_status.paused is False
        assert b_status.updated_by == "alice"
        assert b_status.can_create_orders is True

        # B pauses automation
        pause(session_b, reason="Paused by B", actor="bob")

        # A reads status immediately
        a_status = get_status(session_a)
        assert a_status.paused is True
        assert a_status.pause_reason == "Paused by B"
        assert a_status.updated_by == "bob"
        assert a_status.can_create_orders is False
    finally:
        session_a.close()
        session_b.close()


def test_manual_block_persistence_and_no_clear_on_read(db, monkeypatch):
    """
    Verify:
    1. Manual block prevents order creation and pauses automation.
    2. Repeated get_status calls NEVER clear or corrupt manual_block_reason.
    3. start() and resume() are blocked while manual block is active.
    4. clear_manual_block() safely lifts the block.
    """
    monkeypatch.setattr(settings, "AUTO_TRADING_ENABLED", True)

    start(db, environment="demo", actor="operator")
    assert can_create_orders(db) is True

    # Set manual block
    status = set_manual_block(db, reason="Suspicious liquidity drop", actor="sec_officer")
    assert status.manual_block_reason == "Suspicious liquidity drop"
    assert status.paused is True
    assert status.can_create_orders is False
    assert can_create_orders(db) is False

    # Repeated reads across new sessions must preserve manual_block_reason
    session_2 = SessionLocal()
    try:
        read_status = get_status(session_2)
        assert read_status.manual_block_reason == "Suspicious liquidity drop"
        assert read_status.can_create_orders is False
    finally:
        session_2.close()

    # Attempting to start or resume while manually blocked must fail closed
    with pytest.raises(ValueError, match="manual block is active"):
        start(db, environment="demo", actor="operator")

    with pytest.raises(ValueError, match="manual block is active"):
        resume(db, actor="operator")

    # Clear manual block
    cleared_status = clear_manual_block(db, actor="sec_officer")
    assert cleared_status.manual_block_reason is None

    # Now resume is allowed
    resumed = resume(db, actor="operator")
    assert resumed.paused is False
    assert resumed.can_create_orders is True


def test_revision_and_audit_log_tracking(db, monkeypatch):
    """
    Verify revision increments monotonically on every mutation,
    and corresponding AuditLog rows are written with before/after state.
    """
    monkeypatch.setattr(settings, "AUTO_TRADING_ENABLED", True)

    init_status = get_status(db)
    rev0 = init_status.revision

    # Start -> rev + 1
    s1 = start(db, environment="demo", actor="alice")
    assert s1.revision == rev0 + 1

    # Pause -> rev + 1
    s2 = pause(db, reason="Routine check", actor="bob")
    assert s2.revision == s1.revision + 1

    # Check audit logs
    audits = (
        db.execute(
            select(AuditLog)
            .where(AuditLog.resource == "automation:global")
            .order_by(AuditLog.created_at.asc())
        )
        .scalars()
        .all()
    )

    actions = [a.action for a in audits]
    assert "automation.start" in actions
    assert "automation.pause" in actions

    start_audit = next(a for a in audits if a.action == "automation.start")
    assert start_audit.actor == "alice"
    assert start_audit.before_json["running"] is False
    assert start_audit.after_json["running"] is True

    pause_audit = next(a for a in audits if a.action == "automation.pause")
    assert pause_audit.actor == "bob"
    assert pause_audit.before_json["paused"] is False
    assert pause_audit.after_json["paused"] is True


def test_concurrency_revision_conflict(db, monkeypatch):
    """
    Verify optimistic concurrency conflict detection when expected_revision does not match.
    """
    monkeypatch.setattr(settings, "AUTO_TRADING_ENABLED", True)

    status = start(db, environment="demo", actor="alice")
    current_rev = status.revision

    # Using stale revision should raise conflict
    with pytest.raises(ValueError, match="Concurrency conflict"):
        pause(db, reason="Stale update", actor="bob", expected_revision=current_rev - 1)

    # Using matching revision succeeds
    updated = pause(db, reason="Valid update", actor="bob", expected_revision=current_rev)
    assert updated.revision == current_rev + 1


def test_fail_closed_combinations(db, monkeypatch):
    """
    Ensure can_create_orders strictly returns False under any unsafe combination:
    - running=False, paused=False
    - running=True, paused=True
    - running=True, paused=False, manual_block active
    - running=True, paused=False, AUTO_TRADING_ENABLED=False
    """
    monkeypatch.setattr(settings, "AUTO_TRADING_ENABLED", True)
    ctrl = get_control_row(db, for_update=True)

    # 1. running=False, paused=False
    ctrl.running = False
    ctrl.paused = False
    ctrl.manual_block_reason = None
    db.commit()
    assert can_create_orders(db) is False

    # 2. running=True, paused=True
    ctrl.running = True
    ctrl.paused = True
    ctrl.manual_block_reason = None
    db.commit()
    assert can_create_orders(db) is False

    # 3. running=True, paused=False, manual block active
    ctrl.running = True
    ctrl.paused = False
    ctrl.manual_block_reason = "Manual block active"
    db.commit()
    assert can_create_orders(db) is False

    # 4. running=True, paused=False, manual_block=None, but AUTO_TRADING_ENABLED=False
    ctrl.manual_block_reason = None
    db.commit()
    assert can_create_orders(db) is True
    monkeypatch.setattr(settings, "AUTO_TRADING_ENABLED", False)
    assert can_create_orders(db) is False


def test_status_dict_and_attribute_access(db):
    """
    Verify AutomationStatus supports both dictionary key access and attribute access.
    """
    status = get_status(db)
    assert status["id"] == status.id == "global"
    assert status["running"] == status.running is False
    assert status["paused"] == status.paused is True
    assert status["revision"] == status.revision
    assert "auto_trading_enabled" in status
    assert "can_create_orders" in status


def test_kill_switch_state_not_modified(db, monkeypatch):
    """
    Verify automation control operations NEVER alter KillSwitchState table.
    """
    monkeypatch.setattr(settings, "AUTO_TRADING_ENABLED", True)

    # Insert a pre-existing KillSwitchState row
    ks = KillSwitchState(scope="global", enabled=False, reason="Pre-existing test state")
    db.add(ks)
    db.commit()

    # Perform automation operations
    start(db, environment="demo", actor="alice")
    pause(db, reason="Maintenance", actor="bob")
    set_manual_block(db, reason="Safety check", actor="charlie")
    clear_manual_block(db, actor="charlie")

    # Verify KillSwitchState remains identical
    ks_after = db.execute(select(KillSwitchState).where(KillSwitchState.scope == "global")).scalar_one()
    assert ks_after.enabled is False
    assert ks_after.reason == "Pre-existing test state"


def test_alembic_migration_upgrade_and_downgrade(tmp_path, monkeypatch):
    """
    Verify alembic migration 0003_automation_control upgrades to head and downgrades cleanly.
    Runs against an isolated test environment containing the 0001 -> 0002 -> 0003 migration chain.
    """
    api_dir = Path(__file__).resolve().parent.parent
    alembic_dir = api_dir / "alembic"

    temp_alembic = tmp_path / "alembic"
    temp_versions = temp_alembic / "versions"
    temp_versions.mkdir(parents=True)

    shutil.copy(alembic_dir / "env.py", temp_alembic / "env.py")
    if (alembic_dir / "script.py.mako").exists():
        shutil.copy(alembic_dir / "script.py.mako", temp_alembic / "script.py.mako")

    shutil.copy(alembic_dir / "versions" / "0001_initial_schema.py", temp_versions / "0001_initial_schema.py")
    shutil.copy(alembic_dir / "versions" / "0002_risk_lock_reset.py", temp_versions / "0002_risk_lock_reset.py")
    shutil.copy(alembic_dir / "versions" / "0003_automation_control.py", temp_versions / "0003_automation_control.py")

    test_db_path = tmp_path / "mig_test.db"
    test_db_url = f"sqlite:///{test_db_path.as_posix()}"
    monkeypatch.setattr(settings, "DATABASE_URL", test_db_url)

    alembic_cfg = Config()
    alembic_cfg.set_main_option("script_location", str(temp_alembic))
    alembic_cfg.set_main_option("sqlalchemy.url", test_db_url)

    # 1. Upgrade from empty database to head (0003_automation_control)
    command.upgrade(alembic_cfg, "head")

    test_engine = sa.create_engine(test_db_url)
    try:
        inspector = sa.inspect(test_engine)
        assert "automation_controls" in inspector.get_table_names()
        columns = {c["name"] for c in inspector.get_columns("automation_controls")}
        assert {"id", "running", "paused", "environment", "revision", "updated_by", "pause_reason", "manual_block_reason"}.issubset(columns)

        # 2. Downgrade to 0002_risk_lock_reset
        command.downgrade(alembic_cfg, "0002_risk_lock_reset")
        inspector = sa.inspect(test_engine)
        assert "automation_controls" not in inspector.get_table_names()

        # 3. Upgrade back to head
        command.upgrade(alembic_cfg, "head")
        inspector = sa.inspect(test_engine)
        assert "automation_controls" in inspector.get_table_names()
    finally:
        test_engine.dispose()
