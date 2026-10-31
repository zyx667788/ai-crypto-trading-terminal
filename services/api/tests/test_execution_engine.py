"""Execution Engine tests: order state machine, idempotency, UNKNOWN recovery, protective orders."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.precision import utc_now
from app.execution.engine import (
    TERMINAL_ORDER_STATUSES,
    VALID_ORDER_TRANSITIONS,
    can_transition_order,
    execution_engine,
    validate_order_transition,
)
from app.models.entities import Fill, KillSwitchState, Order, Position, RiskEvent, TradingAccount
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
        "invalidation_conditions": ["15m close below EMA50"],
        "expires_at": utc_now() + timedelta(minutes=30),
        "strategy_version": "trend_following_v1:1",
    }
    base.update(overrides)
    return TradePlanSchema.model_validate(base)


# ------------------------------------------------------------ state machine
def test_happy_path_state_machine_is_linear():
    chain = ["CREATED", "VALIDATED", "SUBMITTED", "ACKNOWLEDGED", "PARTIALLY_FILLED", "FILLED"]
    for current, nxt in zip(chain, chain[1:]):
        assert can_transition_order(current, nxt) is True
        validate_order_transition(current, nxt)  # must not raise


def test_terminal_states_cannot_transition():
    for terminal in TERMINAL_ORDER_STATUSES:
        for target in ("CREATED", "VALIDATED", "SUBMITTED", "FILLED"):
            assert can_transition_order(terminal, target) is False, f"{terminal} -> {target} must be illegal"


def test_illegal_transition_raises():
    with pytest.raises(ValueError):
        validate_order_transition("CREATED", "FILLED")  # cannot skip validation/submission


def test_unknown_is_a_first_class_state():
    assert "UNKNOWN" in VALID_ORDER_TRANSITIONS
    # UNKNOWN can only resolve *after* a query — never jump straight to a new submission.
    assert can_transition_order("UNKNOWN", "FILLED") is True
    assert can_transition_order("UNKNOWN", "CANCELED") is True
    assert can_transition_order("UNKNOWN", "CREATED") is False


# -------------------------------------------------------- happy-path submit
@pytest.fixture()
def tradable(seeded):
    """
    Fully reset the account's mutable trading state.

    These tests exercise Execution Engine semantics (state machine, idempotency, UNKNOWN
    recovery). The Risk Engine's accumulating counters (daily trade cap, consecutive
    losses) are covered by `test_risk_engine.py`, so they are reset here via generous
    per-test limits to keep each execution scenario isolated and deterministic.
    """
    db = seeded
    acct = db.scalar(select(TradingAccount).where(TradingAccount.exchange == "binance"))

    for o in db.scalars(select(Order).where(Order.account_id == acct.id)).all():
        db.delete(o)
    for f in db.scalars(select(Fill).where(Fill.account_id == acct.id)).all():
        db.delete(f)
    for p in db.scalars(select(Position).where(Position.account_id == acct.id)).all():
        db.delete(p)
    for ks in db.scalars(select(KillSwitchState)).all():
        db.delete(ks)
    db.flush()

    acct.trading_locked = False
    acct.sync_status = "SYNCED"
    acct.lock_reason = None
    db.commit()

    # Widen counters for this scenario only; every risk *rule* is verified in its own suite.
    from app.risk.engine import risk_engine
    from app.schemas.domain import RiskLimitsSchema

    risk_engine.update_profile(
        db,
        RiskLimitsSchema(
            max_risk_per_trade_pct=Decimal("0.05"),
            max_daily_loss_pct=Decimal("0.20"),
            max_total_exposure_pct=Decimal("2.00"),
            max_symbol_exposure_pct=Decimal("1.00"),
            max_open_positions=20,
            max_leverage=Decimal("10"),
            max_slippage_bps=Decimal("200"),
            max_spread_bps=Decimal("200"),
            max_data_stale_seconds=600,
            max_consecutive_api_failures=20,
            max_consecutive_losses=20,
            max_daily_trades=200,
        ),
        account_id=acct.id,
    )
    return db, acct


def test_submit_creates_filled_order_with_protective_orders(tradable):
    db, acct = tradable
    result = execution_engine.submit_trade_plan(db, make_plan())

    assert result["status"] == "FILLED"
    assert result["client_order_id"]
    assert result["exchange_order_id"]
    # Section 3.8: native TP/SL must be acknowledged by the exchange, not assumed.
    assert result["tp_sl_confirmed"] is True
    assert len(result["protection_order_ids"]) >= 1


def test_every_order_gets_a_unique_client_order_id(tradable):
    db, acct = tradable
    seen = set()
    for i in range(3):
        r = execution_engine.submit_trade_plan(db, make_plan())
        assert r["client_order_id"] not in seen
        seen.add(r["client_order_id"])
    assert len(seen) == 3


def test_client_order_id_idempotency_prevents_duplicate_submission(tradable):
    db, acct = tradable
    first = execution_engine.submit_trade_plan(db, make_plan(), client_order_id="idem-test-001")
    second = execution_engine.submit_trade_plan(db, make_plan(), client_order_id="idem-test-001")

    assert first["id"] == second["id"]
    assert second.get("idempotent_replay") is True
    orders = db.scalars(select(Order).where(Order.client_order_id == "idem-test-001")).all()
    assert len(orders) == 1


def test_fill_and_position_are_recorded(tradable):
    db, acct = tradable
    result = execution_engine.submit_trade_plan(db, make_plan())
    fills = db.scalars(select(Fill).where(Fill.order_id == result["id"])).all()
    assert len(fills) == 1
    assert fills[0].exchange_trade_id  # exchange trade id persisted for dedup

    positions = db.scalars(select(Position).where(Position.account_id == acct.id, Position.status == "OPEN")).all()
    assert len(positions) == 1
    assert positions[0].tp_sl_confirmed is True
    assert positions[0].stop_loss is not None and positions[0].take_profit is not None


# ------------------------------------------------------- UNKNOWN + recovery
def test_timeout_produces_unknown_and_does_not_retry(tradable):
    db, acct = tradable
    result = execution_engine.submit_trade_plan(db, make_plan(), simulate_timeout=True)

    assert result["status"] == "UNKNOWN"
    assert result["client_order_id"]
    # Exactly one order row exists — the engine must never resubmit on timeout.
    orders = db.scalars(select(Order).where(Order.account_id == acct.id)).all()
    assert len(orders) == 1


def test_risk_rule4_blocks_after_unknown_until_reconciled(tradable):
    db, acct = tradable
    from app.risk.engine import risk_engine

    unknown = execution_engine.submit_trade_plan(db, make_plan(), simulate_timeout=True)
    blocked = risk_engine.evaluate_trade_plan(db, account_id=acct.id, trade_plan=make_plan())
    assert blocked.approved is False
    assert blocked.rule_code == "UNRESOLVED_UNKNOWN_ORDER"

    recovered = execution_engine.reconcile_unknown_order(db, unknown["id"], simulated_remote_status="FILLED")
    assert recovered["status"] in ("FILLED", "CANCELED", "EXPIRED")

    # The UNKNOWN blocker itself must be cleared (a different rule may still apply afterwards).
    after = risk_engine.evaluate_trade_plan(db, account_id=acct.id, trade_plan=make_plan())
    assert after.rule_code != "UNRESOLVED_UNKNOWN_ORDER", "reconciliation must clear the UNKNOWN blocker"


def test_reconcile_trusts_exchange_over_operator_hint(tradable):
    """
    The exchange is the authoritative source of truth: if it still reports FILLED,
    the engine must adopt FILLED even when the operator requests CANCELED.
    """
    db, acct = tradable
    unknown = execution_engine.submit_trade_plan(db, make_plan(), simulate_timeout=True)
    recovered = execution_engine.reconcile_unknown_order(db, unknown["id"], simulated_remote_status="CANCELED")
    assert recovered["status"] == "FILLED"


def test_reconcile_uses_hint_when_exchange_has_no_record(tradable):
    """Only when the exchange has no record of the order does the query hint decide the outcome."""
    db, acct = tradable
    from app.exchanges.registry import get_adapter_for_account

    unknown = execution_engine.submit_trade_plan(db, make_plan(), simulate_timeout=True)
    get_adapter_for_account(db, acct.id).reset_state()  # exchange has no memory of this order

    recovered = execution_engine.reconcile_unknown_order(db, unknown["id"], simulated_remote_status="CANCELED")
    assert recovered["status"] == "CANCELED"


# ------------------------------------------------------------- risk gating
def test_rejected_plan_never_reaches_the_exchange(tradable):
    db, acct = tradable
    before = len(db.scalars(select(Order)).all())
    # Expired plan must be rejected by the Risk Engine before any exchange call.
    result = execution_engine.submit_trade_plan(db, make_plan(expires_at=utc_now() - timedelta(minutes=5)))
    orders = db.scalars(select(Order)).all()
    assert result["status"] == "REJECTED"
    assert len(orders) == before + 1  # the rejection is recorded, but nothing was submitted
    assert orders[-1].exchange_order_id is None
    assert orders[-1].reject_reason and "PLAN_EXPIRED" in orders[-1].reject_reason


def test_kill_switch_prevents_submission(tradable):
    db, acct = tradable
    from app.risk.engine import risk_engine

    risk_engine.trigger_kill_switch(db, scope="global", reason="pytest sub")
    result = execution_engine.submit_trade_plan(db, make_plan())
    assert result["status"] == "REJECTED"
    assert db.scalar(select(Order).where(Order.id == result["id"])).exchange_order_id is None
    risk_engine.resume_trading(db, scope="global", confirmation_code="CONFIRM_RESUME")


# ---------------------------------------------------------- close / flatten
def test_close_position_is_reduce_only(tradable):
    db, acct = tradable
    execution_engine.submit_trade_plan(db, make_plan())
    position = db.scalar(select(Position).where(Position.account_id == acct.id, Position.status == "OPEN"))
    assert position is not None

    closed = execution_engine.close_position(db, position.id)
    assert closed["status"] == "CLOSED"
    db.refresh(position)
    assert position.status == "CLOSED"
    assert to_dec(position.qty) == Decimal("0")


def test_flatten_all_cancels_and_closes(tradable):
    db, acct = tradable
    execution_engine.submit_trade_plan(db, make_plan())
    result = execution_engine.flatten_all_positions(db)
    assert result["flattened"] is True
    assert result["closed_positions_count"] >= 1
    remaining = db.scalars(select(Position).where(Position.account_id == acct.id, Position.status == "OPEN")).all()
    assert len(remaining) == 0


def to_dec(value) -> Decimal:
    return Decimal(str(value))
