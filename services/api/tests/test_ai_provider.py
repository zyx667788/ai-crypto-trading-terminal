"""AI provider adapter tests: JSON contract, timeouts, truncation, secret isolation, expiry policy."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.ai.provider import (
    AIProviderError,
    AIProviderInvalidOutput,
    AIProviderTimeout,
    OpenAICompatibleProvider,
)
from app.ai.service import _enforce_plan_expiry

VALID_PLAN = {
    "exchange": "binance",
    "market_type": "usdt_perp",
    "symbol": "BTC/USDT:USDT",
    "direction": "LONG",
    "entry_type": "LIMIT",
    "entry_price": 68600.0,
    "stop_loss": 67956.9,
    "take_profit": 69886.2,
    "confidence": 0.72,
    "timeframe": "15m",
    "thesis": "EMA21 above EMA50 with positive MACD histogram.",
    "invalidation_conditions": ["15m close below EMA50", "MACD histogram turns negative"],
    "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=25)).isoformat(),
    "strategy_version": "trend_following_v1:1",
}


def _provider(monkeypatch, handler) -> OpenAICompatibleProvider:
    provider = OpenAICompatibleProvider(
        base_url="https://example.invalid",
        api_key="sk-test-not-a-real-key",
        model="gemini-3.8-flash",
        timeout_seconds=5,
        max_retries=0,
    )
    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = transport
        kwargs["trust_env"] = False
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", fake_client)
    return provider


def _ok_response(content: str, finish_reason: str = "stop"):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"].startswith("Bearer ")
        return httpx.Response(
            200,
            json={
                "model": "gemini-3.8-flash",
                "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            },
        )

    return handler


# ------------------------------------------------------------------- happy path
def test_provider_returns_parsed_json_and_usage(monkeypatch):
    import json

    provider = _provider(monkeypatch, _ok_response(json.dumps(VALID_PLAN)))
    completion = provider.complete_json(system_prompt="sys", user_payload={"a": 1})
    assert completion.model == "gemini-3.8-flash"
    assert completion.prompt_tokens == 100
    assert completion.completion_tokens == 50
    assert completion.as_json()["symbol"] == "BTC/USDT:USDT"
    assert completion.estimated_cost_usd >= 0


def test_provider_sends_scrubbed_payload(monkeypatch):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        captured.update(_json.loads(request.content))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"ok":true}'}, "finish_reason": "stop"}], "usage": {}},
        )

    provider = _provider(monkeypatch, handler)
    provider.complete_json(
        system_prompt="sys",
        user_payload={"symbol": "BTC/USDT", "api_secret": "must-not-leak", "nested": {"passphrase": "nope"}},
    )
    user_msg = captured["messages"][1]["content"]
    assert "must-not-leak" not in user_msg
    assert "nope" not in user_msg
    assert "BTC/USDT" in user_msg


# ---------------------------------------------------------------- failure modes
def test_provider_timeout_raises_typed_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated timeout", request=request)

    provider = _provider(monkeypatch, handler)
    with pytest.raises(AIProviderTimeout):
        provider.complete_json(system_prompt="sys", user_payload={"a": 1})


def test_provider_http_error_raises_without_echoing_key(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid key"})

    provider = _provider(monkeypatch, handler)
    with pytest.raises(AIProviderError) as exc:
        provider.complete_json(system_prompt="sys", user_payload={"a": 1})
    assert "sk-test-not-a-real-key" not in str(exc.value)


def test_truncated_response_is_reported_as_truncation(monkeypatch):
    provider = _provider(monkeypatch, _ok_response('{"exchange": "binance", "symbol": "BTC', finish_reason="length"))
    with pytest.raises(AIProviderInvalidOutput) as exc:
        provider.complete_json(system_prompt="sys", user_payload={"a": 1})
    assert "truncated" in str(exc.value).lower()


def test_non_json_body_is_invalid_output(monkeypatch):
    provider = _provider(monkeypatch, _ok_response("Sure! Here is your plan: ..."))
    with pytest.raises(AIProviderInvalidOutput):
        provider.complete_json(system_prompt="sys", user_payload={"a": 1})


def test_unconfigured_provider_refuses_to_call():
    provider = OpenAICompatibleProvider(base_url="", api_key="", model="")
    assert provider.is_configured is False
    with pytest.raises(AIProviderError):
        provider.complete_json(system_prompt="sys", user_payload={})


def test_describe_never_exposes_the_key():
    provider = OpenAICompatibleProvider(
        base_url="https://a2a.example.com", api_key="sk-super-secret-value", model="m1"
    )
    description = str(provider.describe)
    assert "sk-super-secret-value" not in description
    assert description.count("configured") >= 1


# --------------------------------------------------------------- expiry policy
def test_expiry_policy_replaces_past_timestamp():
    payload = dict(VALID_PLAN, expires_at="2025-05-18T14:45:00+00:00")
    result = _enforce_plan_expiry(payload, "15m")
    parsed = datetime.fromisoformat(result["expires_at"])
    assert parsed > datetime.now(timezone.utc)
    assert result["_expiry_policy"]["applied"] is True
    assert "2025-05-18" in result["_expiry_policy"]["model_value"]


def test_expiry_policy_keeps_valid_future_timestamp():
    future = (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat()
    payload = dict(VALID_PLAN, expires_at=future)
    result = _enforce_plan_expiry(payload, "15m")
    assert "_expiry_policy" not in result
    assert result["expires_at"] == future


def test_expiry_policy_replaces_absurdly_distant_timestamp():
    far = (datetime.now(timezone.utc) + timedelta(days=400)).isoformat()
    result = _enforce_plan_expiry(dict(VALID_PLAN, expires_at=far), "1h")
    parsed = datetime.fromisoformat(result["expires_at"])
    assert parsed < datetime.now(timezone.utc) + timedelta(hours=3)
    assert result["_expiry_policy"]["applied"] is True


def test_expiry_policy_handles_missing_and_garbage_values():
    for bad in (None, "", "not-a-date", 12345):
        result = _enforce_plan_expiry(dict(VALID_PLAN, expires_at=bad), "5m")
        assert datetime.fromisoformat(result["expires_at"]) > datetime.now(timezone.utc)
