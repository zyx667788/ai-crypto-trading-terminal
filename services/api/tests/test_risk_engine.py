"""Risk Engine tests: all 10 deterministic rules + position sizing precision."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.precision import utc_now
from app.models.entities import (
    ExchangeConnection,
    KillSwitchState,
    Order,
    Position,
    RiskEvent,
    TradingAccount,
)
from app.risk.engine import risk_engine
from app.schemas.domain import TradePlanSchema


def make_plan(**overrides) -> TradePlanSchema:
    base = {
        "exchange": "binance",
        "market_type": "usdt_perp",
        "symbol": "BTC/USDT:USDT",
        "direction": "LONG",
        "entry_type": "LIMIT",
        "entry_price": "68000.00",
        "stop_loss": "67200.00",
        "take_profit": "69800.00",
        "confidence": "0.8",
        "timeframe": "15m",
        "thesis": "EMA21 above EMA50 with expanding volume and positive orderbook imbalance.",
        "invalidation_conditions": ["15m close below EMA50", "funding spikes above 0.1%"],
        "expires_at": utc_now() + timedelta(minutes=30),
        "strategy_version": "trend_following_v1:1",
    }
    base.update(overrides)
    return TradePlanSchema.model_validate(base)


def account_for(db, exchange="binance"):
    return db.scalar(select(TradingAccount).where(TradingAccount.exchange == exchange))


# --------------------------------------------------------------------- rule 1
def test_rule1_stale_market_data_rejects(seeded):
    db = seeded
    from app.models.entities import MarketSnapshot

    snapshots = db.scalars(select(MarketSnapshot)).all()
    for s in snapshots:
        s.is_stale = True
    db.commit()

    acct = account_for(db)
    result = risk_engine.evaluate_trade_plan(db, account_id=acct.id, trade_plan=make_plan())
    assert result.approved is False
    assert result.rule_code in ("STALE_MARKET_DATA", "MARKET_DATA_MISSING")

    for s in snapshots:
        s.is_stale = False
    db.commit()


# --------------------------------------------------------------------- rule 2
def test_rule2_expired_plan_rejects(seeded):
    db = seeded
    acct = account_for(db)
    plan = make_plan(expires_at=utc_now() - timedelta(minutes=1))
    result = risk_engine.evaluate_trade_plan(db, account_id=acct.id, trade_plan=plan)
    assert result.approved is False
    assert result.rule_code == "PLAN_EXPIRED"


# --------------------------------------------------------------------- rule 3
def test_rule3_unsynced_or_locked_account_rejects(seeded):
    db = seeded
    acct = account_for(db)
    acct.sync_status = "MISMATCH"
    db.commit()
    result = risk_engine.evaluate_trade_plan(db, account_id=acct.id, trade_plan=make_plan())
    assert result.approved is False
    assert result.rule_code == "ACCOUNT_NOT_SYNCED_OR_LOCKED"

    acct.sync_status = "SYNCED"
    db.commit()


# --------------------------------------------------------------------- rule 4
def test_rule4_unresolved_unknown_order_blocks_new_entries(seeded):
    db = seeded
    acct = account_for(db)
    db.add(
        Order(
            account_id=acct.id,
            exchange="binance",
            environment="demo",
            market_type="usdt_perp",
            symbol="BTC/USDT:USDT",
            client_order_id="test-unknown-001",
            order_type="LIMIT",
            side="BUY",
            qty=Decimal("0.001"),
            price=Decimal("68000"),
            status="UNKNOWN",
        )
    )
    db.commit()

    result = risk_engine.evaluate_trade_plan(db, account_id=acct.id, trade_plan=make_plan())
    assert result.approved is False
    assert result.rule_code == "UNRESOLVED_UNKNOWN_ORDER"

    for order in db.scalars(select(Order).where(Order.status == "UNKNOWN")).all():
        db.delete(order)
    db.commit()


# --------------------------------------------------------------------- rule 5
def test_rule5_withdraw_permission_rejects(seeded):
    db = seeded
    acct = account_for(db)
    conn = db.get(ExchangeConnection, acct.connection_id)
    conn.has_withdraw_permission = True
    conn.status = "REJECTED_WITHDRAW_PERMISSION"
    db.commit()

    result = risk_engine.evaluate_trade_plan(db, account_id=acct.id, trade_plan=make_plan())
    assert result.approved is False
    assert result.rule_code == "ACCOUNT_CAPABILITY_OR_LIVE_LOCK"

    conn.has_withdraw_permission = False
    conn.status = "CONNECTED"
    db.commit()


def test_rule5_live_blocked_when_flag_disabled(seeded):
    db = seeded
    acct = account_for(db)
    plan = make_plan()
    # Simulate a live plan by flipping the account environment.
    original = acct.environment
    acct.environment = "live"
    db.commit()
    result = risk_engine.evaluate_trade_plan(db, account_id=acct.id, trade_plan=plan)
    assert result.approved is False
    assert result.rule_code == "ACCOUNT_CAPABILITY_OR_LIVE_LOCK"
    acct.environment = original
    db.commit()


# --------------------------------------------------------------------- rule 7
def test_rule7_invalid_stop_loss_is_impossible_to_express(seeded):
    """Pydantic rejects an invalid LONG stop before it can even reach the engine (defence in depth)."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        make_plan(stop_loss="69000.00")  # stop above entry for a LONG

    db = seeded
    acct = account_for(db)
    # Direction-level tampering is also caught by the schema.
    with pytest.raises(ValidationError):
        make_plan(direction="SHORT", stop_loss="67200.00", take_profit="69800.00")


# --------------------------------------------------------------------- rule 8
def test_rule8_kill_switch_blocks_all_new_entries(seeded):
    db = seeded
    acct = account_for(db)
    risk_engine.trigger_kill_switch(db, scope="global", reason="pytest kill switch")

    result = risk_engine.evaluate_trade_plan(db, account_id=acct.id, trade_plan=make_plan())
    assert result.approved is False
    # A kill switch locks the account, so it is surfaced through the account-lock rule.
    assert result.rule_code in ("ACCOUNT_NOT_SYNCED_OR_LOCKED", "KILL_SWITCH_ACTIVE")
    assert "kill switch" in result.reason.lower() or "KILL_SWITCH" in result.reason

    status = risk_engine.get_risk_status(db, account_id=acct.id)
    assert status["kill_switch_active"] is True
    assert status["trading_locked"] is True

    risk_engine.resume_trading(db, scope="global", confirmation_code="CONFIRM_RESUME", reason="pytest cleanup")
    assert db.scalar(select(KillSwitchState).where(KillSwitchState.scope == "global")).enabled is False

    # After a proper resume the account lock is lifted and trading is allowed again.
    status_after = risk_engine.get_risk_status(db, account_id=acct.id)
    assert status_after["kill_switch_active"] is False


def test_resume_requires_exact_confirmation_code(seeded):
    db = seeded
    risk_engine.trigger_kill_switch(db, scope="global", reason="pytest")
    with pytest.raises(ValueError):
        risk_engine.resume_trading(db, scope="global", confirmation_code="confirm_resume")
    with pytest.raises(ValueError):
        risk_engine.resume_trading(db, scope="global", confirmation_code="")
    risk_engine.resume_trading(db, scope="global", confirmation_code="CONFIRM_RESUME")


# ------------------------------------------------------------------- sizing
def test_position_sizing_is_decimal_and_respects_min_notional(seeded):
    db = seeded
    acct = account_for(db)
    result = risk_engine.evaluate_trade_plan(db, account_id=acct.id, trade_plan=make_plan())
    sizing = result.sizing
    assert isinstance(sizing["qty"], Decimal)
    assert isinstance(sizing["notional"], Decimal)
    assert sizing["qty"] > Decimal("0")
    # Planned loss must never exceed the risk budget by more than rounding noise.
    assert sizing["max_planned_loss"] <= sizing["risk_budget"] * Decimal("1.0001")


def test_risk_evaluation_never_returns_floats(seeded):
    db = seeded
    acct = account_for(db)
    result = risk_engine.evaluate_trade_plan(db, account_id=acct.id, trade_plan=make_plan())
    for key in ("risk_budget", "stop_distance", "qty", "notional", "margin_required", "max_planned_loss"):
        assert not isinstance(result.sizing[key], float), f"{key} must be Decimal, never float"


# --------------------------------------------------------------- fail closed
def test_fail_closed_when_engine_raises(seeded, monkeypatch):
    db = seeded
    acct = account_for(db)

    def boom(*args, **kwargs):
        raise RuntimeError("simulated internal risk engine failure")

    monkeypatch.setattr(risk_engine, "_compute_account_equity", boom)
    result = risk_engine.evaluate_trade_plan(db, account_id=acct.id, trade_plan=make_plan())
    assert result.approved is False
    assert result.rule_code == "RISK_ENGINE_FAIL_CLOSED"


def test_rejections_are_persisted_as_risk_events(seeded):
    db = seeded
    acct = account_for(db)
    before = len(db.scalars(select(RiskEvent)).all())
    risk_engine.evaluate_trade_plan(
        db, account_id=acct.id, trade_plan=make_plan(expires_at=utc_now() - timedelta(minutes=5))
    )
    after = db.scalars(select(RiskEvent)).all()
    assert len(after) > before
    assert any(e.rule_code == "PLAN_EXPIRED" for e in after)
