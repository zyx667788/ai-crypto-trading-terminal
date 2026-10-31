"""Worker execution gates and Outbox hand-off tests (V1.2 phase 1)."""
from __future__ import annotations

import inspect
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from app.automation import control as automation_control
from app.core.config import settings
from app.core.precision import utc_now
from app.core.runtime_state import runtime_state
from app.db.session import SessionLocal
from app.models.entities import KillSwitchState, Order, Position, TradingAccount
from app.models.outbox import OutboxMessage, OutboxStatus
from app.schemas.domain import RiskLimitsSchema
from app.workers.execution import enqueue_order_task, run_execution_worker_once
from app.workers.scheduler import worker_manager


def make_plan(**overrides):
    return {
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
        "thesis": "Closed-candle EMA trend with valid volatility-based stop.",
        "invalidation_conditions": ["15m close below EMA50"],
        "expires_at": (utc_now() + timedelta(minutes=30)).isoformat(),
        "strategy_version": "trend_following_v1:1",
        **overrides,
    }


@pytest.fixture()
def worker_db(seeded, monkeypatch):
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
    db.execute(delete(OutboxMessage))
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
    yield db
    db.rollback()


def enqueue(db, key: str):
    message = enqueue_order_task(
        db,
        trade_plan=make_plan(),
        decision_id=f"decision-{key}",
        signal_id=None,
        client_order_id=f"pytest-{key}",
    )
    db.commit()
    return message


def order_count(db) -> int:
    return len(db.scalars(select(Order)).all())


def test_auto_disabled_worker_never_creates_order(worker_db, monkeypatch):
    monkeypatch.setattr(settings, "AUTO_TRADING_ENABLED", False)
    message = enqueue(worker_db, "auto-off")
    before = order_count(worker_db)

    result = run_execution_worker_once(worker_db, worker_id="execution-off")

    assert result["blocked_reason"] == "AUTO_TRADING_DISABLED"
    assert result["claimed"] == 0
    assert order_count(worker_db) == before
    assert worker_db.get(OutboxMessage, message.id).status == OutboxStatus.PENDING.value


def test_pause_from_another_session_blocks_worker(worker_db):
    message = enqueue(worker_db, "paused")
    before = order_count(worker_db)
    with SessionLocal() as other:
        automation_control.pause(other, reason="cross-process pause", actor="other-process")

    result = run_execution_worker_once(worker_db, worker_id="execution-paused")

    assert "AUTOMATION_PAUSED" in result["blocked_reason"]
    assert order_count(worker_db) == before
    assert worker_db.get(OutboxMessage, message.id).status == OutboxStatus.PENDING.value


def test_kill_switch_from_another_session_blocks_worker(worker_db):
    from app.risk.engine import risk_engine

    message = enqueue(worker_db, "killed")
    before = order_count(worker_db)
    with SessionLocal() as other:
        risk_engine.trigger_kill_switch(other, reason="cross-process kill")

    result = run_execution_worker_once(worker_db, worker_id="execution-killed")

    assert "KILL_SWITCH_ACTIVE" in result["blocked_reason"]
    assert order_count(worker_db) == before
    assert worker_db.get(OutboxMessage, message.id).status == OutboxStatus.PENDING.value

    for kill in worker_db.scalars(select(KillSwitchState)).all():
        kill.enabled = False
    worker_db.commit()


def test_execution_worker_really_consumes_order_task(worker_db):
    message = enqueue(worker_db, "consume")
    before = order_count(worker_db)

    result = run_execution_worker_once(worker_db, worker_id="execution-real")

    assert result["claimed"] == 1
    assert result["completed"] == 1
    assert result["orders"][0]["status"] == "FILLED"
    assert order_count(worker_db) == before + 1
    assert worker_db.get(OutboxMessage, message.id).status == OutboxStatus.DONE.value


def test_run_cycle_enqueues_but_does_not_execute_inline(worker_db):
    before = order_count(worker_db)
    summary = worker_manager.run_automation_cycle(worker_db)

    assert summary["blocked_reason"] is None
    if summary.get("order") is not None:
        assert summary["order"]["status"] == "QUEUED"
        assert order_count(worker_db) == before
        result = run_execution_worker_once(worker_db, worker_id="execution-after-cycle")
        assert result["completed"] >= 1


def test_market_worker_auto_off_refreshes_without_orders(worker_db, monkeypatch):
    monkeypatch.setattr(settings, "AUTO_TRADING_ENABLED", False)
    before = order_count(worker_db)
    result = worker_manager.run_worker_once("market-worker", worker_db, worker_id="market-off")
    assert result["blocked_reason"] == "AUTO_TRADING_DISABLED"
    assert result["market_data"]["snapshots_synced"] > 0
    assert order_count(worker_db) == before


def test_committed_task_is_caught_up_not_nacked_during_pause(worker_db):
    from app.outbox.service import outbox_service

    message = enqueue(worker_db, "crash-pause")
    claimed = outbox_service.claim(
        worker_db,
        worker_id="crashed-owner",
        topics=["execution.order"],
        lease_seconds=60,
        auto_commit=True,
    )
    assert len(claimed) == 1
    from app.execution.engine import execution_engine

    order = execution_engine.submit_trade_plan(
        worker_db,
        make_plan(),
        client_order_id="pytest-crash-pause",
        decision_id="decision-crash-pause",
    )
    assert order["status"] == "FILLED"
    automation_control.pause(worker_db, reason="pause after business commit", actor="pytest")
    message = worker_db.get(OutboxMessage, message.id)
    message.lease_expires_at = utc_now() - timedelta(seconds=1)
    worker_db.commit()

    result = run_execution_worker_once(worker_db, worker_id="recovery-during-pause")

    assert result["settled"] == 1
    assert result["orders"][0]["catch_up_ack"] is True
    assert worker_db.get(OutboxMessage, message.id).status == OutboxStatus.DONE.value
    assert order_count(worker_db) >= 1


def test_invalid_lease_does_not_cascade_into_nack(worker_db, monkeypatch):
    from app.outbox.service import InvalidLeaseError, outbox_service

    enqueue(worker_db, "lease-expired")

    def expired_ack(*args, **kwargs):
        raise InvalidLeaseError("simulated expired lease")

    def forbidden_nack(*args, **kwargs):
        raise AssertionError("nack must not be called after InvalidLeaseError")

    monkeypatch.setattr(outbox_service, "ack", expired_ack)
    monkeypatch.setattr(outbox_service, "nack", forbidden_nack)
    result = run_execution_worker_once(worker_db, worker_id="lease-worker")
    assert result["claimed"] == 1
    assert result["orders"][-1]["status"] == "LEASE_EXPIRED"


def test_force_live_bypass_is_not_part_of_worker_or_api_contract():
    assert "force_live" not in inspect.signature(worker_manager.run_automation_cycle).parameters
    from app.api.v1.automation import run_automation_cycle

    assert "force_live" not in inspect.signature(run_automation_cycle).parameters


def test_worker_heartbeat_is_visible_from_another_session(worker_db):
    run_execution_worker_once(worker_db, worker_id="execution-heartbeat")
    with SessionLocal() as other:
        rows = runtime_state.list_workers(other)
    row = next(row for row in rows if row["worker_id"] == "execution-heartbeat")
    assert row["role"] == "execution-worker"
    assert row["status"] == "OK"
    assert row["pid"] > 0
