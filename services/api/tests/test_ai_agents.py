"""AI orchestration tests: schema enforcement, secret isolation, Fail-Closed behaviour, cost accounting."""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.ai.service import ai_service, scrub_and_verify_ai_input
from app.models.entities import AIDecision, AgentRun, ExchangeConnection, TradingAccount
from app.schemas.domain import TradePlanSchema


@pytest.fixture()
def ai_ready(seeded):
    db = seeded
    acct = db.scalar(select(TradingAccount).where(TradingAccount.exchange == "binance"))
    acct.trading_locked = False
    acct.sync_status = "SYNCED"
    db.commit()
    return db


# ------------------------------------------------------- schema enforcement
def test_valid_plan_is_produced_and_validated(ai_ready):
    db = ai_ready
    result = ai_service.generate_trade_plan(db, symbol="BTC/USDT:USDT", exchange="binance", market_type="usdt_perp")
    assert result is not None
    assert result.is_valid is True
    plan = result.trade_plan
    assert isinstance(plan, TradePlanSchema)
    # Risk/Reward and stop placement must satisfy the schema's semantic rules.
    assert plan.stop_loss < plan.entry_price < plan.take_profit
    assert plan.invalidation_conditions


def test_every_required_tradepan_field_is_present(ai_ready):
    db = ai_ready
    result = ai_service.generate_trade_plan(db, symbol="ETH/USDT:USDT")
    assert result is not None
    payload = result.output_json
    for field in (
        "exchange",
        "market_type",
        "symbol",
        "direction",
        "entry_type",
        "entry_price",
        "stop_loss",
        "take_profit",
        "confidence",
        "timeframe",
        "thesis",
        "invalidation_conditions",
        "expires_at",
        "strategy_version",
    ):
        assert field in payload, f"TradePlan missing required field: {field}"


# --------------------------------------------------------- FAIL-CLOSED paths
def test_invalid_json_output_is_blocked(ai_ready):
    db = ai_ready
    result = ai_service.generate_trade_plan(db, symbol="BTC/USDT:USDT", simulate_invalid_json=True)
    assert result is None, "invalid AI JSON must never yield an executable TradePlan"

    failed_runs = [r for r in db.scalars(select(AgentRun)).all() if r.status in ("FAILED", "TIMEOUT")]
    assert failed_runs, "a failed AgentRun must be recorded"
    invalid = [d for d in db.scalars(select(AIDecision)).all() if d.is_valid is False]
    assert invalid, "an invalid AIDecision must be recorded for audit"


def test_ai_timeout_is_blocked(ai_ready):
    db = ai_ready
    result = ai_service.generate_trade_plan(db, symbol="BTC/USDT:USDT", simulate_timeout=True)
    assert result is None
    timed_out = [r for r in db.scalars(select(AgentRun)).all() if r.status == "TIMEOUT"]
    assert timed_out, "timeout must be recorded as a TIMEOUT AgentRun"


# ----------------------------------------------------------- secret isolation
def test_secrets_never_enter_ai_input(ai_ready):
    db = ai_ready
    conn = db.scalar(select(ExchangeConnection))
    payload = {
        "symbol": "BTC/USDT:USDT",
        "api_key": conn.encrypted_key_ref,
        "api_secret": conn.encrypted_secret_ref,
        "passphrase": "super-secret-passphrase",
        "nested": {"secret": "another-secret", "note": "keep me"},
    }
    scrubbed = scrub_and_verify_ai_input(payload)
    assert scrubbed["api_key"] != conn.encrypted_key_ref
    assert "super-secret-passphrase" not in str(scrubbed)
    assert "another-secret" not in str(scrubbed)
    assert scrubbed["nested"]["note"] == "keep me"


def test_ai_input_redacts_raw_secrets_instead_of_forwarding_them():
    """
    A raw secret must be scrubbed, never forwarded. Redaction (rather than raising)
    is deliberate: a live pipeline must degrade safely rather than crash on a
    mislabelled field, while still guaranteeing the secret cannot reach the model.
    """
    raw = "raw_live_key_abc123456789"
    scrubbed = scrub_and_verify_ai_input({"api_key": raw, "symbol": "BTC/USDT"})
    assert raw not in str(scrubbed)
    assert scrubbed["symbol"] == "BTC/USDT"


def test_decrypted_secrets_are_never_stored_in_decision_output(ai_ready):
    db = ai_ready
    result = ai_service.generate_trade_plan(db, symbol="BTC/USDT:USDT")
    assert result is not None
    blob = str(result.output_json).lower()
    for marker in ("api_secret", "passphrase", "encrypted_secret"):
        assert marker not in blob


# ------------------------------------------------------------- cost tracking
def test_agent_runs_track_latency_tokens_and_cost(ai_ready):
    db = ai_ready
    ai_service.generate_trade_plan(db, symbol="BTC/USDT:USDT")
    runs = db.scalars(select(AgentRun).order_by(AgentRun.created_at.desc())).all()
    assert runs
    latest = runs[0]
    assert latest.latency_ms >= 0
    assert latest.prompt_tokens >= 0
    assert latest.completion_tokens >= 0
    assert isinstance(latest.estimated_cost_usd, Decimal)


def test_agents_status_exposes_all_five_roles(ai_ready):
    db = ai_ready
    status = ai_service.get_agents_status(db)
    names = " ".join(
        str(a.get("name") or a.get("role") or a.get("run_type") or "") for a in status.get("agents", [])
    ).lower()
    for role in ("market", "scanner", "strategy", "risk", "journal"):
        assert role in names, f"missing agent role: {role}"
