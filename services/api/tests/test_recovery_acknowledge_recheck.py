"""Operator acknowledgement must not be a shortcut to READY.

Review finding P1-3: `acknowledge_and_ready()` used to clear every blocker and set
READY after checking only the database manual kill switches. That let an operator wave
through an UNKNOWN order, an unprotected position or an unreconciled account.

Review finding P0-4 (partial): the connectivity step used to default to
`["binance", "okx"]` when no connection record existed, so an unconfigured install
reported a successful exchange check.

These tests pin both behaviours.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.recovery import RecoveryState, compute_readiness_tier, recovery_manager
from app.models.entities import (
    ExchangeConnection,
    Order,
    Position,
    TradingAccount,
)


def _make_position(db, *, confirmed: bool) -> Position:
    account = db.scalar(select(TradingAccount).where(TradingAccount.exchange == "binance"))
    assert account is not None
    position = Position(
        account_id=account.id,
        exchange="binance",
        environment="demo",
        market_type="usdt_perp",
        symbol="SOL/USDT:USDT",
        position_side="LONG",
        qty="1",
        entry_price="100",
        mark_price="100",
        notional_value="100",
        tp_sl_confirmed=confirmed,
        status="OPEN",
    )
    db.add(position)
    db.commit()
    return position


def _make_unknown_order(db) -> Order:
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
        client_order_id="recheck-unknown-1",
    )
    db.add(order)
    db.commit()
    return order


def test_acknowledge_requires_a_meaningful_reason(seeded):
    with pytest.raises(ValueError):
        recovery_manager.acknowledge_and_ready("ok", db=seeded)


def test_acknowledge_is_rejected_while_an_unknown_order_exists(seeded):
    db = seeded
    order = _make_unknown_order(db)
    try:
        status = recovery_manager.acknowledge_and_ready(
            "Operator reviewed the incident log and retried", db=db
        )
        assert status["ready"] is False
        assert status["state"] == RecoveryState.RECOVERY_MODE.value
        assert any("UNKNOWN" in b for b in status["new_entries_blocked_due_to"])
    finally:
        db.delete(order)
        db.commit()


def test_acknowledge_is_rejected_while_a_position_is_unprotected(seeded):
    db = seeded
    position = _make_position(db, confirmed=False)
    try:
        status = recovery_manager.acknowledge_and_ready(
            "Operator confirmed protective order restoration manually", db=db
        )
        assert status["ready"] is False
        assert any("protective order" in b.lower() for b in status["new_entries_blocked_due_to"])
    finally:
        db.delete(position)
        db.commit()


def test_acknowledge_reaches_ready_only_when_technical_checks_pass(seeded):
    db = seeded
    recovery_manager.force_block("Operator declared maintenance window", manual=True)
    assert recovery_manager.get_status()["ready"] is False

    status = recovery_manager.acknowledge_and_ready(
        "Maintenance window finished and checks were re-run", db=db
    )

    assert status["ready"] is True
    assert status["state"] == RecoveryState.READY.value
    assert status["new_entries_blocked"] is False
    assert recovery_manager.is_ready(db) is True


def test_acknowledge_rejects_a_short_reason_even_after_a_manual_block(seeded):
    recovery_manager.force_block("Operator declared maintenance window", manual=True)
    with pytest.raises(ValueError):
        recovery_manager.acknowledge_and_ready("short", db=seeded)


def test_recovery_does_not_assume_exchanges_are_reachable(seeded):
    db = seeded
    for position in db.execute(select(Position)).scalars().all():
        db.delete(position)
    for account in db.execute(select(TradingAccount)).scalars().all():
        db.delete(account)
    for connection in db.execute(select(ExchangeConnection)).scalars().all():
        db.delete(connection)
    db.commit()

    status = recovery_manager.run_recovery(db, force=True)

    connect_steps = [
        step for step in status["steps"] if step["state"] == "CONNECT_BINANCE_AND_OKX"
    ]
    assert connect_steps, "connectivity step must be recorded"
    assert connect_steps[-1]["ok"] is False
    assert "No verified exchange connection" in connect_steps[-1]["detail"]


def test_readiness_tier_is_simulated_even_for_a_live_account(seeded):
    db = seeded
    account = db.scalar(select(TradingAccount).where(TradingAccount.exchange == "binance"))
    assert account is not None
    original = account.environment
    account.environment = "live"
    db.commit()
    try:
        readiness = compute_readiness_tier(db)
        assert readiness["adapters_simulated"] is True
        assert readiness["tier"] == "SIMULATED_READY"
        assert readiness["live_ready"] is False
        assert any("simulator" in reason.lower() for reason in readiness["downgrades"])
    finally:
        account.environment = original
        db.commit()


def test_status_exposes_readiness_block(seeded):
    status = recovery_manager.get_status(seeded)
    assert "readiness" in status
    assert status["readiness"]["tier"] in {
        "SIMULATED_READY",
        "DEMO_READY",
        "TESTNET_READY",
        "LIVE_READY",
    }
    assert status["readiness"]["live_ready"] is False
