"""Watchdog, recovery state machine, backup, and system/API surface tests (V1.1 additions)."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.recovery import RecoveryState, recovery_manager
from app.core.watchdog import BACKUP_DIR, watchdog_service
from app.models.entities import KillSwitchState, Position, TradingAccount


# ------------------------------------------------------------- health probes
def test_liveness_reports_pid_and_uptime():
    live = watchdog_service.check_liveness()
    assert live["status"] == "alive"
    assert isinstance(live["pid"], int)
    assert live["process_uptime_seconds"] >= 0


def test_database_probe_reports_real_dialect(seeded):
    probe = watchdog_service.check_database()
    assert probe["status"] == "healthy"
    # Must not claim PostgreSQL when running on the SQLite fallback.
    assert probe["dialect"] in ("sqlite", "postgresql")
    if probe["dialect"] == "sqlite":
        assert "sqlite" in probe["name"]


def test_cache_probe_degrades_without_redis():
    probe = watchdog_service.check_redis()
    assert probe["status"] in ("healthy", "degraded")
    if probe["status"] == "degraded":
        assert probe["backend"] == "in_process_event_bus"


def test_readiness_exposes_trading_allowed_flag(seeded):
    ready = watchdog_service.check_readiness()
    assert ready["status"] in ("ready", "not_ready")
    assert "trading_allowed" in ready
    assert isinstance(ready["checks"], dict)


def test_metrics_endpoint_is_prometheus_text(client):
    res = client.get("/metrics")
    assert res.status_code == 200
    body = res.text
    assert "terminal_memory_usage_percent" in body
    assert "terminal_disk_free_percent" in body


def test_resource_snapshot_reports_16gb_budget(seeded):
    snap = watchdog_service.resource_snapshot()
    assert snap["memory"]["unified_16gb_budget_mb"]
    assert snap["memory"]["pressure_level"] in ("GREEN", "YELLOW", "RED")
    assert snap["disk"]["free_pct"] >= 0


# ------------------------------------------------------- recovery state machine
def test_recovery_runs_full_sequence_and_reaches_ready(seeded):
    db = seeded
    status = recovery_manager.run_recovery(db, force=True)
    assert status["state"] == RecoveryState.READY.value
    states = [s["state"] for s in status["steps"]]
    # The documented order from Section 9.4.6 must be followed.
    expected_prefix = [
        "HOST_BOOT",
        "START_DATABASE_AND_REDIS",
        "START_API_AND_WORKERS",
        "RECOVERY_MODE",
        "CHECK_LOCAL_DATABASE",
        "CONNECT_BINANCE_AND_OKX",
        "FETCH_BALANCES_POSITIONS_OPEN_ORDERS_RECENT_FILLS",
        "RECONCILE_LOCAL_AND_EXCHANGE_STATE",
        "VERIFY_EXCHANGE_NATIVE_PROTECTIVE_ORDERS",
    ]
    assert states[: len(expected_prefix)] == expected_prefix


def test_recovery_blocks_entries_when_position_has_no_protective_order(seeded):
    db = seeded
    acct = db.scalar(select(TradingAccount).where(TradingAccount.exchange == "binance"))
    pos = Position(
        account_id=acct.id,
        exchange="binance",
        environment="demo",
        market_type="usdt_perp",
        symbol="BTC/USDT:USDT",
        position_side="LONG",
        qty="0.01",
        entry_price="68000",
        mark_price="68000",
        notional_value="680",
        tp_sl_confirmed=False,  # deliberately unprotected
        status="OPEN",
    )
    db.add(pos)
    db.commit()

    status = recovery_manager.run_recovery(db, force=True)
    assert status["ready"] is False
    assert status["new_entries_blocked"] is True
    assert any("stop-loss" in r.lower() for r in status["new_entries_blocked_due_to"])

    db.delete(pos)
    db.commit()
    recovery_manager.run_recovery(db, force=True)


def test_recovery_never_auto_flattens_positions(seeded):
    db = seeded
    acct = db.scalar(select(TradingAccount).where(TradingAccount.exchange == "binance"))
    pos = Position(
        account_id=acct.id,
        exchange="binance",
        environment="demo",
        market_type="usdt_perp",
        symbol="ETH/USDT:USDT",
        position_side="LONG",
        qty="0.1",
        entry_price="3500",
        mark_price="3500",
        notional_value="350",
        tp_sl_confirmed=True,
        status="OPEN",
    )
    db.add(pos)
    db.commit()

    recovery_manager.run_recovery(db, force=True)
    db.refresh(pos)
    assert pos.status == "OPEN", "recovery must never liquidate positions automatically"
    assert recovery_manager.get_status()["policy"]["auto_flatten_on_recovery"] is False

    db.delete(pos)
    db.commit()
    recovery_manager.run_recovery(db, force=True)


def test_operator_acknowledge_clears_blockers(seeded):
    db = seeded
    recovery_manager.force_block("simulated blocker")
    assert recovery_manager.get_status()["new_entries_blocked"] is True
    status = recovery_manager.acknowledge_and_ready("pytest operator acknowledgement")
    assert status["ready"] is True
    assert status["new_entries_blocked"] is False


# ------------------------------------------------------------------- backups
def test_encrypted_backup_contains_no_plaintext_secrets(seeded):
    from app.core.security import secret_vault

    manifest = watchdog_service.create_encrypted_backup(tag="daily")
    blob = (BACKUP_DIR / manifest["file"]).read_text(encoding="utf-8")
    assert manifest["contains_plaintext_secrets"] is False
    # The ciphertext must not be human-readable JSON.
    assert blob.strip().startswith("gAAAA")
    decrypted = secret_vault.decrypt(blob)
    assert decrypted is not None
    assert '"tables"' in decrypted


def test_backup_restore_drill_succeeds(seeded):
    watchdog_service.create_encrypted_backup(tag="daily")
    result = watchdog_service.verify_latest_backup_restore()
    assert result["ok"] is True
    assert result["restored_rows"] > 0
    assert "orders" in result["table_counts"]


# ------------------------------------------------------------ API smoke tests
API_GET_PATHS = [
    "/health",
    "/health/live",
    "/health/ready",
    "/api/v1/exchanges",
    "/api/v1/exchange-connections",
    "/api/v1/accounts",
    "/api/v1/markets/instruments",
    "/api/v1/markets/tickers",
    "/api/v1/markets/candles?limit=20",
    "/api/v1/markets/orderbook",
    "/api/v1/markets/funding-rate",
    "/api/v1/scanner/status",
    "/api/v1/scanner/opportunities",
    "/api/v1/scanner/rankings",
    "/api/v1/strategies",
    "/api/v1/trade-signals",
    "/api/v1/agents/status",
    "/api/v1/agents/runs",
    "/api/v1/decisions",
    "/api/v1/risk/profile",
    "/api/v1/risk/status",
    "/api/v1/risk/events",
    "/api/v1/orders",
    "/api/v1/fills",
    "/api/v1/positions",
    "/api/v1/portfolio/summary",
    "/api/v1/automation/status",
    "/api/v1/analytics/summary",
    "/api/v1/analytics/equity-curve",
    "/api/v1/analytics/strategy-performance",
    "/api/v1/journal",
    "/api/v1/system/status",
    "/api/v1/system/recovery",
    "/api/v1/system/config",
    "/api/v1/system/events",
    "/api/v1/system/backups",
    "/api/v1/system/rate-limits",
    "/api/v1/agents/provider",
]


@pytest.mark.parametrize("path", API_GET_PATHS)
def test_api_get_endpoints_return_200(client, path):
    res = client.get(path)
    assert res.status_code == 200, f"{path} -> {res.status_code}: {res.text[:300]}"


def test_health_never_leaks_secrets(client):
    body = client.get("/api/v1/system/status").text.lower()
    for marker in ("api_secret", "passphrase", "encrypted_secret", "encrypted_key"):
        assert marker not in body


def test_live_automation_is_rejected_while_flag_disabled(client):
    res = client.post("/api/v1/automation/start", json={"environment": "live"})
    assert res.status_code == 403
    assert "LIVE_TRADING_ENABLED" in res.text or "disabled" in res.text.lower()


def test_resume_requires_confirmation_code(client):
    res = client.post("/api/v1/risk/resume", json={"scope": "global", "confirmation_code": "nope"})
    assert res.status_code == 400


def test_websocket_channels_all_exist(client):
    from app.core.events import CHANNELS

    for ch in CHANNELS:
        with client.websocket_connect(f"/ws/{ch}") as ws:
            ack = ws.receive_json()
            assert ack["event_type"].endswith(".connected")
            assert ack["event_id"]
            assert ack["occurred_at"]


def test_end_to_end_automation_cycle_completes(client):
    res = client.post("/api/v1/automation/run-cycle")
    assert res.status_code == 200
    payload = res.json()
    assert payload["blocked_reason"] is None
    steps = {s["step"] for s in payload["steps"]}
    for required in (
        "market_data",
        "scanner",
        "strategy_signal",
        "ai_trade_plan",
        "risk_engine",
        "execution_engine",
        "portfolio_reconciliation",
        "journal",
    ):
        assert required in steps
    assert all(s["ok"] for s in payload["steps"]), payload["steps"]
