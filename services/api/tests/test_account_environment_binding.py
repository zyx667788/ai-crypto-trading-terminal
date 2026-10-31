"""
Test suite for Account & Environment Binding (Review finding P0-3).

Verifies:
1. Two accounts on the same exchange (one demo, one live): requesting environment="demo"
   selects the demo account and never the live one.
2. Requesting environment="live" while only a demo account exists -> deterministic rejection,
   no order created.
3. account_id given but environment mismatch -> AccountEnvironmentMismatch, no order created.
4. Two active demo accounts on the same exchange and no account_id -> AmbiguousAccountError,
   no order created.
5. Outbox payload contains account_id, exchange, and environment.
6. A task whose payload lacks environment is rejected (nack to DEAD) and does not create an Order row.
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
import pytest
from sqlalchemy import select

from app.execution.engine import (
    AccountEnvironmentMismatch,
    AmbiguousAccountError,
    execution_engine,
)
from app.models.entities import ExchangeConnection, Fill, Order, TradingAccount
from app.models.outbox import OutboxMessage, OutboxStatus
from app.schemas.domain import RiskLimitsSchema, TradePlanSchema
from app.workers.execution import enqueue_order_task, run_execution_worker_once
from app.core.precision import utc_now


def make_plan(**overrides):
    base = {
        "exchange": "binance",
        "market_type": "usdt_perp",
        "symbol": "BTC/USDT:USDT",
        "direction": "LONG",
        "entry_type": "LIMIT",
        "entry_price": "68000.00",
        "stop_loss": "67200.00",
        "take_profit": "69800.00",
        "confidence": "0.85",
        "timeframe": "15m",
        "thesis": "Trend continuation on 15m breakout with tight stop.",
        "invalidation_conditions": ["Price breaks below EMA 20"],
        "expires_at": (utc_now() + timedelta(minutes=30)).isoformat(),
        "strategy_version": "trend_following_v1:1",
    }
    base.update(overrides)
    return TradePlanSchema.model_validate(base)


@pytest.fixture()
def clean_orders(seeded, monkeypatch):
    from app.automation import control as automation_control
    from app.core.config import settings
    from app.core.runtime_state import runtime_state
    from app.models.entities import KillSwitchState, Position
    from app.risk.engine import risk_engine

    db = seeded
    monkeypatch.setattr(settings, "AUTO_TRADING_ENABLED", True)
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", False)
    risk_engine.recovery_mode = "READY"
    risk_engine.recovery_reason = None

    for account in db.scalars(select(TradingAccount)).all():
        account.is_active = True
        account.trading_locked = False
        account.sync_status = "SYNCED"
        account.lock_reason = None
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
            account_id=account.id,
        )
    for position in db.scalars(select(Position).where(Position.status == "OPEN")).all():
        position.status = "CLOSED"
        position.qty = Decimal("0")
    for kill in db.scalars(select(KillSwitchState)).all():
        kill.enabled = False
    for f in db.scalars(select(Fill)).all():
        db.delete(f)
    for o in db.scalars(select(Order)).all():
        db.delete(o)
    for m in db.scalars(select(OutboxMessage)).all():
        db.delete(m)
    db.commit()

    automation_control.clear_manual_block(db, actor="pytest")
    automation_control.start(db, environment="demo", actor="pytest")
    runtime_state.save_recovery(
        db,
        {
            "state": "READY",
            "new_entries_blocked_due_to": [],
            "steps": [],
            "counts": {},
            "started_at": utc_now().isoformat(),
            "completed_at": utc_now().isoformat(),
        },
    )
    return db


def test_two_accounts_same_exchange_selects_demo_never_live(clean_orders):
    """
    Two accounts on the same exchange (one demo, one live):
    requesting environment="demo" selects the demo account and never the live one.
    """
    db = clean_orders
    # Existing binance account is demo
    demo_acct = db.scalar(
        select(TradingAccount).where(
            TradingAccount.exchange == "binance",
            TradingAccount.environment == "demo",
        )
    )
    assert demo_acct is not None

    # Create a live account on binance
    live_conn = ExchangeConnection(
        id=f"conn-live-{uuid.uuid4().hex[:8]}",
        name="Binance Live Connection",
        exchange="binance",
        environment="live",
        api_key_masked="fake_live_key_masked",
        encrypted_key_ref="fake_key_ref",
        encrypted_secret_ref="fake_secret_ref",
        status="CONNECTED",
        has_withdraw_permission=False,
        ip_whitelist_configured=True,
        permissions_json={},
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    db.add(live_conn)
    db.flush()

    live_acct = TradingAccount(
        id=f"acct-live-{uuid.uuid4().hex[:8]}",
        connection_id=live_conn.id,
        exchange="binance",
        environment="live",
        account_type="unified",
        region="GLOBAL",
        capabilities={},
        is_active=True,
        trading_locked=False,
        sync_status="SYNCED",
        created_at=utc_now(),
    )
    db.add(live_acct)
    db.commit()

    # Resolve execution account directly
    resolved = execution_engine.resolve_execution_account(
        db,
        exchange="binance",
        environment="demo",
    )
    assert resolved.id == demo_acct.id
    assert resolved.environment == "demo"
    assert resolved.id != live_acct.id

    # Submit trade plan with environment="demo"
    plan = make_plan(exchange="binance")
    result = execution_engine.submit_trade_plan(
        db,
        plan,
        environment="demo",
        client_order_id=f"demo-bind-{uuid.uuid4().hex[:8]}",
    )
    assert result["status"] == "FILLED"
    order = db.scalar(select(Order).where(Order.client_order_id == result["client_order_id"]))
    assert order is not None
    assert order.account_id == demo_acct.id
    assert order.environment == "demo"
    assert order.account_id != live_acct.id


def test_requesting_live_with_only_demo_account_rejects_no_order(clean_orders):
    """
    Requesting environment="live" while only a demo account exists ->
    deterministic rejection, no order created.
    """
    db = clean_orders
    # Ensure no live account exists on binance
    live_accts = list(
        db.scalars(
            select(TradingAccount).where(
                TradingAccount.exchange == "binance",
                TradingAccount.environment == "live",
            )
        ).all()
    )
    for a in live_accts:
        db.delete(a)
    db.commit()

    before_orders = len(db.scalars(select(Order)).all())

    # Direct resolve raises AccountEnvironmentMismatch
    with pytest.raises(AccountEnvironmentMismatch) as exc_info:
        execution_engine.resolve_execution_account(
            db,
            exchange="binance",
            environment="live",
        )
    assert "No active account found for exchange=binance, environment=live" in str(exc_info.value)

    # Submit trade plan with environment="live" also raises and creates no order
    with pytest.raises(AccountEnvironmentMismatch):
        execution_engine.submit_trade_plan(
            db,
            make_plan(exchange="binance"),
            environment="live",
            client_order_id=f"live-no-acct-{uuid.uuid4().hex[:8]}",
        )

    after_orders = len(db.scalars(select(Order)).all())
    assert after_orders == before_orders


def test_account_id_given_but_environment_mismatch_raises(clean_orders):
    """
    account_id given but environment mismatch -> AccountEnvironmentMismatch, no order created.
    """
    db = clean_orders
    demo_acct = db.scalar(
        select(TradingAccount).where(
            TradingAccount.exchange == "binance",
            TradingAccount.environment == "demo",
        )
    )
    assert demo_acct is not None
    before_orders = len(db.scalars(select(Order)).all())

    # Given demo account_id, but requesting "live"
    with pytest.raises(AccountEnvironmentMismatch) as exc_info:
        execution_engine.resolve_execution_account(
            db,
            account_id=demo_acct.id,
            exchange="binance",
            environment="live",
        )
    assert f"Account {demo_acct.id} environment (demo) does not match requested environment (live)" in str(exc_info.value)

    with pytest.raises(AccountEnvironmentMismatch):
        execution_engine.submit_trade_plan(
            db,
            make_plan(exchange="binance"),
            account_id=demo_acct.id,
            environment="live",
            client_order_id=f"mismatch-{uuid.uuid4().hex[:8]}",
        )

    after_orders = len(db.scalars(select(Order)).all())
    assert after_orders == before_orders


def test_two_active_demo_accounts_raises_ambiguous_account_error(clean_orders):
    """
    Two active demo accounts on the same exchange and no account_id ->
    AmbiguousAccountError, no order created.
    """
    db = clean_orders
    existing_demo = db.scalar(
        select(TradingAccount).where(
            TradingAccount.exchange == "binance",
            TradingAccount.environment == "demo",
        )
    )
    assert existing_demo is not None

    # Add a second active demo account on binance
    conn2 = ExchangeConnection(
        id=f"conn-demo2-{uuid.uuid4().hex[:8]}",
        name="Binance Demo 2 Connection",
        exchange="binance",
        environment="demo",
        api_key_masked="fake_demo2_masked",
        encrypted_key_ref="fake_key_ref_2",
        encrypted_secret_ref="fake_secret_ref_2",
        status="CONNECTED",
        has_withdraw_permission=False,
        ip_whitelist_configured=True,
        permissions_json={},
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    db.add(conn2)
    db.flush()

    demo_acct_2 = TradingAccount(
        id=f"acct-demo2-{uuid.uuid4().hex[:8]}",
        connection_id=conn2.id,
        exchange="binance",
        environment="demo",
        account_type="unified",
        region="GLOBAL",
        capabilities={},
        is_active=True,
        trading_locked=False,
        sync_status="SYNCED",
        created_at=utc_now(),
    )
    db.add(demo_acct_2)
    db.commit()

    before_orders = len(db.scalars(select(Order)).all())

    # Resolve without account_id raises AmbiguousAccountError
    with pytest.raises(AmbiguousAccountError) as exc_info:
        execution_engine.resolve_execution_account(
            db,
            exchange="binance",
            environment="demo",
        )
    assert "Ambiguous account resolution: found 2 active accounts" in str(exc_info.value)

    # Submit without account_id raises AmbiguousAccountError and creates no order
    with pytest.raises(AmbiguousAccountError):
        execution_engine.submit_trade_plan(
            db,
            make_plan(exchange="binance"),
            environment="demo",
            client_order_id=f"ambig-{uuid.uuid4().hex[:8]}",
        )

    after_orders = len(db.scalars(select(Order)).all())
    assert after_orders == before_orders

    # Clean up the extra account so subsequent fixtures are clean
    from sqlalchemy import delete
    db.execute(delete(TradingAccount).where(TradingAccount.id == demo_acct_2.id))
    db.execute(delete(ExchangeConnection).where(ExchangeConnection.id == conn2.id))
    db.commit()


def test_outbox_payload_contains_account_exchange_environment(clean_orders):
    """
    Outbox payload contains account_id, exchange, and environment.
    """
    db = clean_orders
    plan_dict = make_plan(exchange="binance").model_dump(mode="json")
    cid = f"test-payload-{uuid.uuid4().hex[:8]}"

    msg = enqueue_order_task(
        db,
        trade_plan=plan_dict,
        decision_id="dec-123",
        signal_id="sig-456",
        client_order_id=cid,
        environment="demo",
    )
    db.commit()

    assert msg.payload_json is not None
    payload = msg.payload_json
    assert payload.get("exchange") == "binance"
    assert payload.get("environment") == "demo"
    assert payload.get("account_id") is not None
    assert payload.get("client_order_id") == cid


def test_task_lacking_environment_is_rejected_no_order(clean_orders, monkeypatch):
    """
    A task whose payload lacks environment is rejected (nack with terminal status)
    and does not create an Order row.
    """
    from app.core.config import settings
    from app.outbox.service import outbox_service

    db = clean_orders
    monkeypatch.setattr(settings, "AUTO_TRADING_ENABLED", True)

    plan_dict = make_plan(exchange="binance").model_dump(mode="json")
    cid = f"no-env-{uuid.uuid4().hex[:8]}"

    # Enqueue a legacy message explicitly lacking environment
    msg = outbox_service.enqueue(
        db,
        topic="execution.order",
        aggregate_id=cid,
        dedup_key=f"execution:{cid}",
        payload={
            "trade_plan": plan_dict,
            "decision_id": "dec-no-env",
            "signal_id": "sig-no-env",
            "client_order_id": cid,
            # no "environment"
        },
        max_attempts=1,
    )
    db.commit()

    before_orders = len(db.scalars(select(Order)).all())

    worker_result = run_execution_worker_once(db, worker_id="test-env-checker")

    # Order count unchanged
    after_orders = len(db.scalars(select(Order)).all())
    assert after_orders == before_orders

    # Message must be nacked / DEAD
    db.refresh(msg)
    assert msg.status == OutboxStatus.DEAD.value
    assert "lacks required environment" in (msg.last_error or "")
