"""One gate, one answer.

Review finding P1-10: `automation.control.get_status()` computed `can_create_orders`
from the AUTO flag, running, paused and manual-block only, while the real Worker gate
in `scheduler.new_order_gate()` also checked the global kill switch and recovery
readiness. The dashboard could therefore report "orders allowed" while the Execution
Worker refused every task.

Review finding P1-4: the live pre-flight checklist hard-coded `passed: True` for items
the system never measured.
"""
from __future__ import annotations

from sqlalchemy import select

from app.automation.gate import evaluate_order_gate, order_gate_summary
from app.core.config import settings
from app.core.recovery import recovery_manager
from app.models.entities import KillSwitchState
from app.workers.scheduler import worker_manager


def _force_automation_open(db) -> None:
    """Make the control row look healthy so gate failures isolate the other checks."""
    from app.automation import control as automation_control

    row = automation_control.get_control_row(db)
    row.running = True
    row.paused = False
    row.pause_reason = None
    row.manual_block_reason = None
    db.commit()


def test_gate_blocks_on_global_kill_switch(seeded, monkeypatch):
    db = seeded
    monkeypatch.setattr(settings, "AUTO_TRADING_ENABLED", True)
    _force_automation_open(db)
    recovery_manager.run_recovery(db, force=True)

    db.add(
        KillSwitchState(
            scope="global",
            enabled=True,
            reason="Emergency stop under investigation",
        )
    )
    db.commit()

    try:
        decision = evaluate_order_gate(db)
        assert decision.allowed is False
        assert "KILL_SWITCH_ACTIVE" in decision.blocked_by
        assert "AUTO_TRADING_DISABLED" not in decision.blocked_by
    finally:
        # A global kill switch is process-wide state: leaving it behind would silently
        # block every later suite in the same session.
        for row in db.execute(select(KillSwitchState)).scalars().all():
            db.delete(row)
        db.commit()


def test_dashboard_status_uses_the_same_gate(seeded, monkeypatch):
    db = seeded
    monkeypatch.setattr(settings, "AUTO_TRADING_ENABLED", True)
    _force_automation_open(db)
    recovery_manager.run_recovery(db, force=True)

    status = worker_manager.get_automation_status(db)
    gate = status["order_gate"]

    assert gate is not None
    assert status["can_create_orders"] == gate["allowed"]
    # And it must agree with what a worker would be told.
    allowed, reason = worker_manager.new_order_gate(db)
    assert allowed == gate["allowed"]
    assert reason == gate["reason"]


def test_gate_blocks_when_recovery_is_not_ready(seeded, monkeypatch):
    """A technical blocker (UNKNOWN order) must reach the gate whatever the checkpoint says.

    Using an UNKNOWN order instead of an in-memory `force_block` keeps this test
    independent of whether a checkpoint write succeeded in an earlier suite: the
    UNKNOWN check is recomputed from the database on every `get_status(db)` call.
    """
    db = seeded
    monkeypatch.setattr(settings, "AUTO_TRADING_ENABLED", True)
    _force_automation_open(db)
    recovery_manager.run_recovery(db, force=True)

    from app.models.entities import Order, TradingAccount

    account = db.scalar(select(TradingAccount).where(TradingAccount.exchange == "binance"))
    assert account is not None
    order = Order(
        account_id=account.id,
        exchange="binance",
        environment="demo",
        market_type="usdt_perp",
        symbol="BTC/USDT:USDT",
        side="BUY",
        order_type="MARKET",
        qty="0.01",
        price="60000",
        filled_qty="0",
        avg_fill_price="0",
        status="UNKNOWN",
        client_order_id="gate-unknown-1",
    )
    db.add(order)
    db.commit()

    try:
        decision = evaluate_order_gate(db)
        assert decision.allowed is False
        assert "RECOVERY_NOT_READY" in decision.blocked_by

        summary = order_gate_summary(db)
        assert summary["explanation"]
    finally:
        db.delete(order)
        db.commit()


def test_live_preflight_does_not_claim_unverified_items(seeded):
    db = seeded
    checklist = worker_manager.live_preflight_checklist(db)

    assert checklist["ready"] is False
    assert checklist["unverified_manual_items"]
    for item in checklist["items"]:
        if item["source"] == "manual_attestation":
            assert item["passed"] is False
            assert item["verified"] is False


def test_live_preflight_measures_withdraw_permission(seeded):
    db = seeded
    from app.models.entities import ExchangeConnection

    connections = list(db.scalars(select(ExchangeConnection)).all())
    assert connections, "seeded fixture must create demo connections"
    for connection in connections:
        connection.has_withdraw_permission = True
        connection.ip_whitelist_configured = True
    db.commit()

    checklist = worker_manager.live_preflight_checklist(db)
    withdraw = next(i for i in checklist["items"] if "withdraw" in i["item"].lower())

    assert withdraw["verified"] is True
    assert withdraw["passed"] is False
