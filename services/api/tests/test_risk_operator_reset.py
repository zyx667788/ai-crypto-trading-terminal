"""Operator reset path for the daily-loss / loss-streak lock (spec §3.7 rule 8)."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.precision import utc_now
from app.models.entities import AuditLog, Fill, Order, RiskEvent, RiskProfile, TradingAccount
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
        "thesis": "EMA21 above EMA50 with expanding volume.",
        "invalidation_conditions": ["close below EMA50"],
        "expires_at": utc_now() + timedelta(minutes=30),
        "strategy_version": "trend_following_v1:1",
    }
    base.update(overrides)
    return TradePlanSchema.model_validate(base)


@pytest.fixture()
def loss_streak(seeded):
    """Create three consecutive losing fills so Rule 8 trips, then hand back the account."""
    db = seeded
    acct = db.scalar(select(TradingAccount).where(TradingAccount.exchange == "binance"))
    acct.trading_locked = False
    acct.sync_status = "SYNCED"

    # Pin the limits this scenario depends on, so the test is order-independent
    # (other suites widen limits on the same shared account).
    from app.schemas.domain import RiskLimitsSchema

    risk_engine.update_profile(
        db,
        RiskLimitsSchema(
            max_risk_per_trade_pct=Decimal("0.0025"),
            max_daily_loss_pct=Decimal("0.20"),
            max_total_exposure_pct=Decimal("2.00"),
            max_symbol_exposure_pct=Decimal("1.00"),
            max_open_positions=20,
            max_leverage=Decimal("10"),
            max_slippage_bps=Decimal("200"),
            max_spread_bps=Decimal("200"),
            max_data_stale_seconds=600,
            max_consecutive_api_failures=20,
            max_consecutive_losses=3,
            max_daily_trades=200,
        ),
        account_id=acct.id,
    )

    for i in range(3):
        order = Order(
            account_id=acct.id,
            exchange="binance",
            environment="demo",
            market_type="usdt_perp",
            symbol="BTC/USDT:USDT",
            client_order_id=f"loss-{i}-{utc_now().timestamp()}",
            order_type="LIMIT",
            side="SELL",
            qty=Decimal("0.001"),
            price=Decimal("68000"),
            status="FILLED",
            filled_qty=Decimal("0.001"),
        )
        db.add(order)
        db.flush()
        db.add(
            Fill(
                order_id=order.id,
                account_id=acct.id,
                exchange="binance",
                market_type="usdt_perp",
                symbol="BTC/USDT:USDT",
                exchange_trade_id=f"trade-loss-{i}-{utc_now().timestamp()}",
                side="SELL",
                price=Decimal("68000"),
                qty=Decimal("0.001"),
                fee=Decimal("0.04"),
                realized_pnl=Decimal("-12.50"),
                occurred_at=utc_now() - timedelta(minutes=3 - i),
            )
        )
    db.commit()
    return db, acct


def test_loss_streak_triggers_rule8_lock(loss_streak):
    db, acct = loss_streak
    result = risk_engine.evaluate_trade_plan(db, account_id=acct.id, trade_plan=make_plan())
    assert result.approved is False
    assert result.rule_code == "DAILY_LOSS_OR_TRADE_LIMIT_LOCK"
    assert "consecutive_losses" in result.reason


def test_reset_requires_exact_confirmation_code(loss_streak):
    db, acct = loss_streak
    for bad_code in ("", "confirm_reset", "RESET", "CONFIRM_RESUME"):
        with pytest.raises(ValueError):
            risk_engine.reset_daily_locks(db, account_id=acct.id, confirmation_code=bad_code)


def test_reset_clears_the_lock_and_unblocks_new_entries(loss_streak):
    db, acct = loss_streak
    blocked = risk_engine.evaluate_trade_plan(db, account_id=acct.id, trade_plan=make_plan())
    assert blocked.approved is False

    result = risk_engine.reset_daily_locks(
        db, account_id=acct.id, confirmation_code="CONFIRM_RESET", reason="pytest acknowledgement"
    )
    assert result["status"] == "RESET"

    after = risk_engine.evaluate_trade_plan(db, account_id=acct.id, trade_plan=make_plan())
    assert after.rule_code != "DAILY_LOSS_OR_TRADE_LIMIT_LOCK", "reset must clear the loss-streak lock"


def test_reset_is_append_only_and_audited(loss_streak):
    db, acct = loss_streak
    fills_before = len(db.scalars(select(Fill).where(Fill.account_id == acct.id)).all())

    risk_engine.reset_daily_locks(db, account_id=acct.id, confirmation_code="CONFIRM_RESET", reason="audit check")

    fills_after = len(db.scalars(select(Fill).where(Fill.account_id == acct.id)).all())
    assert fills_after == fills_before, "reset must never delete trading history"

    audits = [a for a in db.scalars(select(AuditLog)).all() if a.action == "RESET_DAILY_LOCKS"]
    assert audits, "reset must write an audit log entry"

    events = [e for e in db.scalars(select(RiskEvent)).all() if e.event_type == "DAILY_LOCKS_RESET"]
    assert events, "reset must write a risk event"

    profile = db.scalar(select(RiskProfile).where(RiskProfile.account_id == acct.id))
    assert profile.lock_reset_at is not None
    assert profile.lock_reset_reason == "audit check"


def test_reset_watermark_is_exposed_in_risk_status(loss_streak):
    db, acct = loss_streak
    risk_engine.reset_daily_locks(db, account_id=acct.id, confirmation_code="CONFIRM_RESET")
    status = risk_engine.get_risk_status(db, account_id=acct.id)
    assert status["daily_trades_count"] == 0, "orders created before the watermark must not count"
    assert status["consecutive_losses"] == 0
