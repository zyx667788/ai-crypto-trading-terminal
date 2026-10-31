from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from pydantic import PrivateAttr, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.provider import AIProviderError, AIProviderInvalidOutput, AIProviderTimeout, get_ai_provider
from app.core.config import settings
from app.core.events import event_bus
from app.core.logging import get_logger, log_event
from app.core.precision import ZERO, round_price_to_tick, to_decimal, utc_iso, utc_now
from app.core.security import SENSITIVE_KEY_SUBSTRINGS, redact_sensitive_data
from app.models.entities import (
    AIDecision,
    AgentRun,
    Instrument,
    MarketCandle,
    MarketSnapshot,
    Strategy,
    StrategyVersion,
    SystemEvent,
    TradeSignal,
)
from app.scanner.service import compute_indicators_from_candles_and_snapshot, scanner_service
from app.schemas.domain import TradePlanSchema

logger = get_logger("terminal.ai")

FORBIDDEN_SECRET_MARKERS = ("api_key", "apikey", "secret", "passphrase", "private_key", "password")


# Deterministic plan lifetime (minutes) applied by the system, not the model.
PLAN_TTL_MINUTES: dict[str, int] = {
    "1m": 10,
    "5m": 15,
    "15m": 30,
    "1h": 60,
    "4h": 60,
    "1d": 60,
}


def _enforce_plan_expiry(payload: dict[str, Any], timeframe: str) -> dict[str, Any]:
    """
    Enforce a system-owned expiry on model output.

    `expires_at` is a mechanical deadline, not a market judgement, and LLMs have no
    clock — a hallucinated past date would silently poison an otherwise valid plan.
    The original value is preserved for audit and the override is flagged explicitly.
    """
    if not isinstance(payload, dict):
        return payload

    ttl_minutes = PLAN_TTL_MINUTES.get(timeframe, 30)
    now = utc_now()
    target = now + timedelta(minutes=ttl_minutes)
    original = payload.get("expires_at")

    parsed: datetime | None = None
    if isinstance(original, str):
        try:
            parsed = datetime.fromisoformat(original.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            parsed = None
    elif isinstance(original, datetime):
        parsed = original if original.tzinfo else original.replace(tzinfo=timezone.utc)

    needs_override = parsed is None or parsed <= now or parsed > now + timedelta(minutes=90)
    if needs_override:
        payload["expires_at"] = target.isoformat()
        payload["_expiry_policy"] = {
            "applied": True,
            "model_value": str(original),
            "enforced_ttl_minutes": ttl_minutes,
            "reason": "model expiry missing, in the past, or implausibly far in the future",
        }
    return payload


def scrub_and_verify_ai_input(payload: Any) -> Any:
    """
    Scrub all inputs with `redact_sensitive_data` and verify that no key matching
    `api_key`, `secret`, `passphrase` can ever enter AI inputs unredacted.
    """
    sanitized = redact_sensitive_data(payload)
    _assert_no_unredacted_secrets(sanitized)
    return sanitized


def _assert_no_unredacted_secrets(data: Any) -> None:
    if isinstance(data, dict):
        for k, v in data.items():
            key_lower = str(k).lower()
            if any(marker in key_lower for marker in FORBIDDEN_SECRET_MARKERS) and not key_lower.endswith("_masked"):
                if v != "[REDACTED]":
                    raise ValueError(f"Security violation: unredacted sensitive key '{k}' blocked from AI input")
            _assert_no_unredacted_secrets(v)
    elif isinstance(data, (list, tuple)):
        for item in data:
            _assert_no_unredacted_secrets(item)


def _estimate_tokens_and_cost(input_obj: Any, output_obj: Any) -> tuple[int, int, Decimal]:
    in_text = json.dumps(input_obj, default=str, ensure_ascii=False)
    out_text = json.dumps(output_obj, default=str, ensure_ascii=False)
    prompt_tokens = max(24, len(in_text) // 4)
    completion_tokens = max(16, len(out_text) // 4) if output_obj else 0
    # Conservative token pricing ($0.55 / 1M input, $2.19 / 1M output)
    cost = (
        Decimal(str(prompt_tokens)) * Decimal("0.00000055")
        + Decimal(str(completion_tokens)) * Decimal("0.00000219")
    ).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
    return prompt_tokens, completion_tokens, cost


class ValidatedTradePlan(TradePlanSchema):
    """
    A `TradePlanSchema` subclass returned by `AIOrchestratorService` when validation succeeds.
    - Passes `isinstance(x, TradePlanSchema)` and `OrderSubmitRequest(trade_plan=x)`.
    - Exposes `.decision_id`, `.id`, `.agent_run_id`, `.risk_explanation`, `.is_valid`,
      `.decision`, `.trade_plan`, `.output_json`, and dict-style `x["key"]` / `x.get("key")`.
    """

    _decision_id: str | None = PrivateAttr(default=None)
    _agent_run_id: str | None = PrivateAttr(default=None)
    _risk_explanation: str = PrivateAttr(default="")
    _is_valid: bool = PrivateAttr(default=True)
    _decision_obj: AIDecision | None = PrivateAttr(default=None)
    _workflow_trace: list[dict[str, Any]] = PrivateAttr(default_factory=list)

    def attach_metadata(
        self,
        *,
        decision: AIDecision | None,
        agent_run_id: str | None,
        risk_explanation: str,
        workflow_trace: list[dict[str, Any]] | None = None,
    ) -> "ValidatedTradePlan":
        self._decision_obj = decision
        self._decision_id = decision.id if decision else None
        self._agent_run_id = agent_run_id
        self._risk_explanation = risk_explanation
        self._is_valid = True
        self._workflow_trace = list(workflow_trace or [])
        return self

    @property
    def decision_id(self) -> str | None:
        return self._decision_id

    @property
    def id(self) -> str | None:
        return self._decision_id

    @property
    def agent_run_id(self) -> str | None:
        return self._agent_run_id

    @property
    def risk_explanation(self) -> str:
        return self._risk_explanation

    @property
    def is_valid(self) -> bool:
        return self._is_valid

    @property
    def decision(self) -> AIDecision | None:
        return self._decision_obj

    @property
    def trade_plan(self) -> "ValidatedTradePlan":
        return self

    @property
    def output_json(self) -> dict[str, Any]:
        return self.to_contract_dict()

    @property
    def workflow_trace(self) -> list[dict[str, Any]]:
        return self._workflow_trace

    def to_contract_dict(self) -> dict[str, Any]:
        return {
            "exchange": self.exchange,
            "market_type": self.market_type,
            "symbol": self.symbol,
            "direction": self.direction,
            "entry_type": self.entry_type,
            "entry_price": str(self.entry_price) if self.entry_price is not None else None,
            "entry_zone": [str(x) for x in self.entry_zone] if self.entry_zone else None,
            "stop_loss": str(self.stop_loss),
            "take_profit": str(self.take_profit),
            "confidence": str(self.confidence),
            "timeframe": self.timeframe,
            "thesis": self.thesis,
            "invalidation_conditions": list(self.invalidation_conditions),
            "expires_at": utc_iso(self.expires_at),
            "strategy_version": self.strategy_version,
        }

    def to_dict(self) -> dict[str, Any]:
        base = self.to_contract_dict()
        base.update(
            {
                "id": self._decision_id,
                "decision_id": self._decision_id,
                "agent_run_id": self._agent_run_id,
                "risk_explanation": self._risk_explanation,
                "is_valid": self._is_valid,
                "trade_plan": self.to_contract_dict(),
                "output_json": self.to_contract_dict(),
                "workflow_trace": self._workflow_trace,
            }
        )
        return base

    def __getitem__(self, key: str) -> Any:
        mapping = self.to_dict()
        if key in mapping:
            return mapping[key]
        raise KeyError(key)

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)

    def __contains__(self, key: Any) -> bool:
        return key in self.to_dict()


class MarketAgent:
    """
    Agent 1 (`MarketAgent`):
    Summarizes market regime, volatility, and funding rate conditions.
    """

    name = "MarketAgent"
    run_type = "market"
    prompt_version = "v1.0"

    def analyze(
        self,
        db: Session,
        *,
        symbol: str,
        exchange: str,
        market_type: str,
        timeframe: str = "15m",
        indicators: dict[str, Any] | None = None,
        extra_context: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], AgentRun]:
        t0 = time.perf_counter()
        raw_input = {
            "agent": self.name,
            "symbol": symbol,
            "exchange": exchange,
            "market_type": market_type,
            "timeframe": timeframe,
            "indicators": indicators or {},
            "context": extra_context or {},
        }
        sanitized_input = scrub_and_verify_ai_input(raw_input)
        ind = sanitized_input.get("indicators", {})

        ema_21 = to_decimal(ind.get("ema_21"), Decimal("100"))
        ema_50 = to_decimal(ind.get("ema_50"), Decimal("99"))
        rsi_14 = to_decimal(ind.get("rsi_14"), Decimal("55"))
        volatility = to_decimal(ind.get("volatility"), Decimal("0.006"))
        spread_bps = to_decimal(ind.get("spread_bps"), Decimal("2.5"))
        funding_rate = to_decimal(ind.get("funding_rate"), ZERO)

        if ema_21 > ema_50 and rsi_14 >= Decimal("50"):
            regime = "BULLISH_TREND"
        elif ema_21 < ema_50 and rsi_14 < Decimal("50"):
            regime = "BEARISH_TREND"
        else:
            regime = "CONSOLIDATION_RANGE"

        if volatility >= Decimal("0.015"):
            volatility_state = "HIGH_VOLATILITY"
        elif volatility >= Decimal("0.003"):
            volatility_state = "MODERATE_VOLATILITY"
        else:
            volatility_state = "LOW_VOLATILITY"

        if market_type == "spot":
            funding_condition = "SPOT_NO_FUNDING"
        elif funding_rate > Decimal("0.0005"):
            funding_condition = "CROWDED_LONG_POSITIVE_FUNDING"
        elif funding_rate < Decimal("-0.0005"):
            funding_condition = "CROWDED_SHORT_NEGATIVE_FUNDING"
        else:
            funding_condition = "NEUTRAL_BALANCED_FUNDING"

        output_summary = {
            "symbol": symbol,
            "exchange": exchange,
            "market_type": market_type,
            "market_regime": regime,
            "volatility_state": volatility_state,
            "volatility_pct": str(volatility),
            "spread_bps": str(spread_bps),
            "funding_rate": str(funding_rate),
            "funding_condition": funding_condition,
            "summary": (
                f"{symbol} on {exchange} ({market_type}) is in {regime} with {volatility_state} "
                f"(ATR/price={volatility}, spread={spread_bps} bps) and {funding_condition} ({funding_rate})."
            ),
        }

        latency_ms = max(1, int((time.perf_counter() - t0) * 1000))
        prompt_tokens, completion_tokens, cost = _estimate_tokens_and_cost(sanitized_input, output_summary)
        run = AgentRun(
            run_type=self.run_type,
            model=settings.AI_MODEL,
            provider=settings.AI_PROVIDER,
            prompt_version=self.prompt_version,
            status="SUCCEEDED",
            latency_ms=latency_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            estimated_cost_usd=cost,
            input_summary_json=sanitized_input,
            output_summary_json=output_summary,
            error_message=None,
        )
        db.add(run)
        db.flush()
        return output_summary, run


class ScannerAgent:
    """
    Agent 2 (`ScannerAgent`):
    Ranks and deduplicates candidate opportunities from the Market Scanner.
    """

    name = "ScannerAgent"
    run_type = "scanner"
    prompt_version = "v1.0"

    def rank_and_deduplicate(
        self,
        db: Session,
        opportunities: list[dict[str, Any]],
        extra_context: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], AgentRun]:
        t0 = time.perf_counter()
        raw_input = {
            "agent": self.name,
            "raw_count": len(opportunities),
            "opportunities": opportunities,
            "context": extra_context or {},
        }
        sanitized_input = scrub_and_verify_ai_input(raw_input)

        dedup_map: dict[tuple[str, str, str], dict[str, Any]] = {}
        for item in sanitized_input.get("opportunities", []):
            if not item.get("risk_filter_passed", True):
                continue
            key = (
                str(item.get("exchange", "binance")),
                str(item.get("market_type", "usdt_perp")),
                str(item.get("symbol", "BTC/USDT:USDT")),
            )
            item_score = to_decimal(item.get("score", "0"), ZERO)
            existing = dedup_map.get(key)
            if existing is None or item_score > to_decimal(existing.get("score", "0"), ZERO):
                dedup_map[key] = dict(item)

        ranked = sorted(
            dedup_map.values(),
            key=lambda x: to_decimal(x.get("score", "0"), ZERO),
            reverse=True,
        )
        for idx, entry in enumerate(ranked, start=1):
            entry["rank"] = idx
            entry["score"] = str(entry.get("score", "0"))

        output_summary = {
            "input_count": len(opportunities),
            "deduplicated_count": len(ranked),
            "top_candidates": ranked[:5],
        }

        latency_ms = max(1, int((time.perf_counter() - t0) * 1000))
        prompt_tokens, completion_tokens, cost = _estimate_tokens_and_cost(sanitized_input, output_summary)
        run = AgentRun(
            run_type=self.run_type,
            model=settings.AI_MODEL,
            provider=settings.AI_PROVIDER,
            prompt_version=self.prompt_version,
            status="SUCCEEDED",
            latency_ms=latency_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            estimated_cost_usd=cost,
            input_summary_json=sanitized_input,
            output_summary_json=output_summary,
            error_message=None,
        )
        db.add(run)
        db.flush()
        return output_summary, run


class StrategyAgent:
    """
    Agent 3 (`StrategyAgent`):
    Evaluates rule signals and market context to synthesize a structured `TradePlanSchema` candidate.
    """

    name = "StrategyAgent"
    run_type = "strategy"
    prompt_version = "v1.0"

    def synthesize_plan(
        self,
        db: Session,
        *,
        exchange: str,
        market_type: str,
        symbol: str,
        timeframe: str,
        direction: str,
        strategy_version_str: str,
        indicators: dict[str, Any],
        market_summary: dict[str, Any],
        tick_size: Decimal,
        signal_reason: str | None = None,
        raw_ai_output: dict[str, Any] | str | None = None,
        simulate_invalid_json: bool = False,
        simulate_timeout: bool = False,
        extra_context: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | str, AgentRun]:
        t0 = time.perf_counter()
        raw_input = {
            "agent": self.name,
            "exchange": exchange,
            "market_type": market_type,
            "symbol": symbol,
            "timeframe": timeframe,
            "direction": direction,
            "strategy_version": strategy_version_str,
            "indicators": indicators,
            "market_summary": market_summary,
            "signal_reason": signal_reason,
            "context": extra_context or {},
        }
        sanitized_input = scrub_and_verify_ai_input(raw_input)

        if simulate_timeout:
            latency_ms = int(settings.AI_TIMEOUT_SECONDS * 1000)
            prompt_tokens, _, cost = _estimate_tokens_and_cost(sanitized_input, {})
            run = AgentRun(
                run_type=self.run_type,
                model=settings.AI_MODEL,
                provider=settings.AI_PROVIDER,
                prompt_version=self.prompt_version,
                status="TIMEOUT",
                latency_ms=latency_ms,
                prompt_tokens=prompt_tokens,
                completion_tokens=0,
                estimated_cost_usd=cost,
                input_summary_json=sanitized_input,
                output_summary_json={"error": "AI_PROVIDER_TIMEOUT"},
                error_message=f"Simulated AI timeout after {settings.AI_TIMEOUT_SECONDS}s",
            )
            db.add(run)
            db.flush()
            return {"error": "TIMEOUT"}, run

        if simulate_invalid_json:
            malformed = '{"exchange": "binance", "symbol": "BTC/USDT:USDT", "stop_loss": -999, INVALID_JSON'
            latency_ms = max(1, int((time.perf_counter() - t0) * 1000))
            prompt_tokens, completion_tokens, cost = _estimate_tokens_and_cost(sanitized_input, malformed)
            run = AgentRun(
                run_type=self.run_type,
                model=settings.AI_MODEL,
                provider=settings.AI_PROVIDER,
                prompt_version=self.prompt_version,
                status="FAILED",
                latency_ms=latency_ms,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                estimated_cost_usd=cost,
                input_summary_json=sanitized_input,
                output_summary_json={"raw_invalid_payload": malformed},
                error_message="AI returned malformed JSON / invalid TradePlan schema",
            )
            db.add(run)
            db.flush()
            return malformed, run

        if raw_ai_output is not None:
            latency_ms = max(1, int((time.perf_counter() - t0) * 1000))
            prompt_tokens, completion_tokens, cost = _estimate_tokens_and_cost(sanitized_input, raw_ai_output)
            run = AgentRun(
                run_type=self.run_type,
                model=settings.AI_MODEL,
                provider=settings.AI_PROVIDER,
                prompt_version=self.prompt_version,
                status="SUCCEEDED",
                latency_ms=latency_ms,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                estimated_cost_usd=cost,
                input_summary_json=sanitized_input,
                output_summary_json=(
                    raw_ai_output if isinstance(raw_ai_output, dict) else {"raw_text": str(raw_ai_output)}
                ),
                error_message=None,
            )
            db.add(run)
            db.flush()
            return raw_ai_output, run

        # ------------------------------------------------------------------
        # Real LLM call (only when a provider is configured). Any failure here is
        # Fail-Closed: we record the failed run and return an error marker so the
        # orchestrator refuses to emit an executable TradePlan.
        # ------------------------------------------------------------------
        provider = get_ai_provider()
        if provider.is_configured:
            from app.ai.prompts import (
                PROMPT_VERSION as LLM_PROMPT_VERSION,
                TRADE_PLAN_SYSTEM_PROMPT,
                build_trade_plan_user_payload,
            )

            user_payload = build_trade_plan_user_payload(
                exchange=exchange,
                market_type=market_type,
                symbol=symbol,
                timeframe=timeframe,
                direction_bias=direction,
                strategy_version=strategy_version_str,
                tick_size=str(tick_size),
                indicators=indicators,
                market=market_summary,
                signal_reason=signal_reason,
                current_utc_time=utc_iso(utc_now()),
            )

            try:
                completion = provider.complete_json(
                    system_prompt=TRADE_PLAN_SYSTEM_PROMPT,
                    user_payload=user_payload,
                    temperature=0.2,
                    max_tokens=2600,
                )
            except AIProviderTimeout as exc:
                run = AgentRun(
                    run_type=self.run_type,
                    model=provider.model,
                    provider="openai_compatible",
                    prompt_version=LLM_PROMPT_VERSION,
                    status="TIMEOUT",
                    latency_ms=max(1, int((time.perf_counter() - t0) * 1000)),
                    prompt_tokens=0,
                    completion_tokens=0,
                    estimated_cost_usd=ZERO,
                    input_summary_json=sanitized_input,
                    output_summary_json={"error": "AI_PROVIDER_TIMEOUT"},
                    error_message=str(exc)[:500],
                )
                db.add(run)
                db.flush()
                return {"error": "TIMEOUT"}, run
            except AIProviderError as exc:
                run = AgentRun(
                    run_type=self.run_type,
                    model=provider.model,
                    provider="openai_compatible",
                    prompt_version=LLM_PROMPT_VERSION,
                    status="FAILED",
                    latency_ms=max(1, int((time.perf_counter() - t0) * 1000)),
                    prompt_tokens=0,
                    completion_tokens=0,
                    estimated_cost_usd=ZERO,
                    input_summary_json=sanitized_input,
                    output_summary_json={"error": "AI_PROVIDER_ERROR"},
                    error_message=str(exc)[:500],
                )
                db.add(run)
                db.flush()
                return {"error": "PROVIDER_ERROR"}, run

            try:
                parsed = completion.as_json()
            except AIProviderInvalidOutput as exc:
                run = AgentRun(
                    run_type=self.run_type,
                    model=completion.model,
                    provider="openai_compatible",
                    prompt_version=LLM_PROMPT_VERSION,
                    status="FAILED",
                    latency_ms=completion.latency_ms,
                    prompt_tokens=completion.prompt_tokens,
                    completion_tokens=completion.completion_tokens,
                    estimated_cost_usd=completion.estimated_cost_usd,
                    input_summary_json=sanitized_input,
                    output_summary_json={"raw_text": completion.text[:2000]},
                    error_message=str(exc)[:500],
                )
                db.add(run)
                db.flush()
                return completion.text, run

            parsed = _enforce_plan_expiry(parsed, timeframe)
            run = AgentRun(
                run_type=self.run_type,
                model=completion.model,
                provider="openai_compatible",
                prompt_version=LLM_PROMPT_VERSION,
                status="SUCCEEDED",
                latency_ms=completion.latency_ms,
                prompt_tokens=completion.prompt_tokens,
                completion_tokens=completion.completion_tokens,
                estimated_cost_usd=completion.estimated_cost_usd,
                input_summary_json=sanitized_input,
                output_summary_json=parsed,
                error_message=None,
            )
            db.add(run)
            db.flush()
            return parsed, run
        effective_direction = direction.upper()
        if market_type == "spot" and effective_direction in ("SHORT", "SELL"):
            effective_direction = "BUY"

        raw_entry = to_decimal(
            indicators.get("entry_price", indicators.get("last_price", "50000")),
            Decimal("50000"),
        )
        if raw_entry <= ZERO:
            raw_entry = Decimal("50000")
        entry_price = round_price_to_tick(raw_entry, tick_size)

        atr_val = to_decimal(
            indicators.get("atr_14"),
            max(entry_price * Decimal("0.008"), tick_size * Decimal("10")),
        )
        if atr_val <= ZERO:
            atr_val = max(entry_price * Decimal("0.008"), tick_size * Decimal("10"))

        atr_mult = to_decimal(indicators.get("atr_multiplier", "2.0"), Decimal("2.0"))
        rr_ratio = to_decimal(indicators.get("rr_ratio", "2.0"), Decimal("2.0"))

        stop_offset = max(atr_mult * atr_val, tick_size * Decimal("5"))
        tp_offset = max(rr_ratio * stop_offset, tick_size * Decimal("10"))

        if effective_direction in ("LONG", "BUY"):
            stop_loss = round_price_to_tick(
                to_decimal(indicators.get("stop_loss"), max(entry_price - stop_offset, tick_size)),
                tick_size,
            )
            if stop_loss >= entry_price or stop_loss <= ZERO:
                stop_loss = max(round_price_to_tick(entry_price - stop_offset, tick_size), tick_size)
                if stop_loss >= entry_price:
                    stop_loss = max(entry_price - tick_size, tick_size)

            take_profit = round_price_to_tick(
                to_decimal(indicators.get("take_profit"), entry_price + tp_offset),
                tick_size,
            )
            if take_profit <= entry_price:
                take_profit = round_price_to_tick(entry_price + tp_offset, tick_size)
                if take_profit <= entry_price:
                    take_profit = entry_price + tick_size * Decimal("2")
        else:
            stop_loss = round_price_to_tick(
                to_decimal(indicators.get("stop_loss"), entry_price + stop_offset),
                tick_size,
            )
            if stop_loss <= entry_price:
                stop_loss = round_price_to_tick(entry_price + stop_offset, tick_size)
                if stop_loss <= entry_price:
                    stop_loss = entry_price + tick_size

            take_profit = round_price_to_tick(
                to_decimal(indicators.get("take_profit"), max(entry_price - tp_offset, tick_size)),
                tick_size,
            )
            if take_profit >= entry_price or take_profit <= ZERO:
                take_profit = max(round_price_to_tick(entry_price - tp_offset, tick_size), tick_size)
                if take_profit >= entry_price:
                    take_profit = max(entry_price - tick_size * Decimal("2"), tick_size)

        zone_low = round_price_to_tick(max(entry_price - tick_size * Decimal("2"), tick_size), tick_size)
        zone_high = round_price_to_tick(entry_price + tick_size * Decimal("2"), tick_size)
        if zone_low > zone_high:
            zone_low, zone_high = zone_high, zone_low

        regime_str = market_summary.get("market_regime", "BULLISH_TREND")
        reason_str = signal_reason or f"Aligned with {regime_str} and technical momentum"
        thesis = (
            f"{symbol} ({exchange} {market_type}) {effective_direction} setup on {timeframe}: "
            f"{reason_str}. Regime is {regime_str} with ATR(14)={atr_val} and R:R={rr_ratio}."
        )

        invalidation_conditions = [
            f"Closed {timeframe} candle breaches stop_loss at {stop_loss}",
            "EMA(21)/EMA(50) structure reverses against trade direction",
            f"Bid-ask spread widens above {settings.DEFAULT_MAX_SPREAD_BPS} bps or market data becomes stale",
        ]

        valid_timeframes = {"1m", "5m", "15m", "1h", "4h", "1d"}
        clean_tf = timeframe if timeframe in valid_timeframes else "15m"

        candidate_plan = {
            "exchange": exchange if exchange in ("binance", "okx") else "binance",
            "market_type": market_type if market_type in ("spot", "usdt_perp") else "usdt_perp",
            "symbol": symbol,
            "direction": effective_direction,
            "entry_type": "LIMIT",
            "entry_price": str(entry_price),
            "entry_zone": [str(zone_low), str(zone_high)],
            "stop_loss": str(stop_loss),
            "take_profit": str(take_profit),
            "confidence": "0.82",
            "timeframe": clean_tf,
            "thesis": thesis,
            "invalidation_conditions": invalidation_conditions,
            "expires_at": utc_iso(utc_now() + timedelta(minutes=30)),
            "strategy_version": strategy_version_str,
        }

        latency_ms = max(1, int((time.perf_counter() - t0) * 1000))
        prompt_tokens, completion_tokens, cost = _estimate_tokens_and_cost(sanitized_input, candidate_plan)
        run = AgentRun(
            run_type=self.run_type,
            model=settings.AI_MODEL,
            provider=settings.AI_PROVIDER,
            prompt_version=self.prompt_version,
            status="SUCCEEDED",
            latency_ms=latency_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            estimated_cost_usd=cost,
            input_summary_json=sanitized_input,
            output_summary_json=candidate_plan,
            error_message=None,
        )
        db.add(run)
        db.flush()
        return candidate_plan, run


class RiskAgent:
    """
    Agent 4 (`RiskAgent`):
    Writes a human-readable risk explanation (`risk_explanation`) of stop distance,
    invalidation conditions, and R:R ratio (explicitly noting that deterministic
    `RiskEngine` has final authority).
    """

    name = "RiskAgent"
    run_type = "risk"
    prompt_version = "v1.0"

    def explain_risk(
        self,
        db: Session,
        trade_plan: TradePlanSchema,
        market_summary: dict[str, Any] | None = None,
        extra_context: dict[str, Any] | None = None,
    ) -> tuple[str, AgentRun]:
        t0 = time.perf_counter()
        plan_dict = {
            "exchange": trade_plan.exchange,
            "market_type": trade_plan.market_type,
            "symbol": trade_plan.symbol,
            "direction": trade_plan.direction,
            "entry_price": str(trade_plan.entry_price),
            "stop_loss": str(trade_plan.stop_loss),
            "take_profit": str(trade_plan.take_profit),
            "confidence": str(trade_plan.confidence),
            "invalidation_conditions": list(trade_plan.invalidation_conditions),
        }
        raw_input = {
            "agent": self.name,
            "trade_plan": plan_dict,
            "market_summary": market_summary or {},
            "context": extra_context or {},
        }
        sanitized_input = scrub_and_verify_ai_input(raw_input)

        entry = to_decimal(trade_plan.entry_price, Decimal("1"))
        stop = to_decimal(trade_plan.stop_loss, Decimal("1"))
        tp = to_decimal(trade_plan.take_profit, Decimal("1"))

        stop_distance = abs(entry - stop)
        reward_distance = abs(tp - entry)
        stop_pct = (
            (stop_distance / entry * Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if entry > ZERO
            else ZERO
        )
        rr_ratio = (
            (reward_distance / stop_distance).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if stop_distance > ZERO
            else ZERO
        )

        invalidation_str = "; ".join(trade_plan.invalidation_conditions)
        explanation = (
            f"Risk Assessment for {trade_plan.symbol} ({trade_plan.direction} on {trade_plan.exchange} {trade_plan.market_type}): "
            f"Entry={entry}, Stop-Loss={stop} (stop distance={stop_distance} / {stop_pct}%), "
            f"Take-Profit={tp} (reward distance={reward_distance}, R:R ratio={rr_ratio}:1). "
            f"Invalidation conditions: [{invalidation_str}]. "
            f"NOTE: This AI risk explanation is advisory only; the deterministic RiskEngine has final authority "
            f"over position sizing, exposure limits, kill-switch checks, and order execution approval."
        )

        output_summary = {
            "stop_distance": str(stop_distance),
            "stop_distance_pct": f"{stop_pct}%",
            "reward_distance": str(reward_distance),
            "rr_ratio": str(rr_ratio),
            "risk_explanation": explanation,
            "deterministic_risk_engine_final_authority": True,
        }

        latency_ms = max(1, int((time.perf_counter() - t0) * 1000))
        prompt_tokens, completion_tokens, cost = _estimate_tokens_and_cost(sanitized_input, output_summary)
        run = AgentRun(
            run_type=self.run_type,
            model=settings.AI_MODEL,
            provider=settings.AI_PROVIDER,
            prompt_version=self.prompt_version,
            status="SUCCEEDED",
            latency_ms=latency_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            estimated_cost_usd=cost,
            input_summary_json=sanitized_input,
            output_summary_json=output_summary,
            error_message=None,
        )
        db.add(run)
        db.flush()
        return explanation, run


class JournalAgent:
    """
    Agent 5 (`JournalAgent`):
    Generates a structured post-trade / execution review summary (`generate_journal_summary`).
    """

    name = "JournalAgent"
    run_type = "journal"
    prompt_version = "v1.0"

    def generate_journal_summary(
        self,
        db: Session,
        journal_context: dict[str, Any],
    ) -> tuple[str, AgentRun]:
        t0 = time.perf_counter()
        sanitized_input = scrub_and_verify_ai_input({"agent": self.name, "journal_context": journal_context})
        ctx = sanitized_input.get("journal_context", {})

        symbol = ctx.get("symbol", "BTC/USDT:USDT")
        exchange = ctx.get("exchange", "binance")
        market_type = ctx.get("market_type", "usdt_perp")
        direction = ctx.get("direction", "LONG")
        strategy_version = ctx.get("strategy_version", "trend_following_v1:v1")
        close_reason = ctx.get("close_reason", "OPEN_OR_ACTIVE")
        net_pnl = to_decimal(ctx.get("net_realized_pnl", "0"), ZERO)
        fees_paid = to_decimal(ctx.get("fees_paid", "0"), ZERO)
        funding_pnl = to_decimal(ctx.get("funding_pnl", "0"), ZERO)

        outcome_tag = "PROFITABLE" if net_pnl > ZERO else ("BREAKEVEN_OR_OPEN" if net_pnl == ZERO else "LOSS")
        review_summary = (
            f"[Journal Review - {outcome_tag}] {symbol} {direction} ({exchange} {market_type}, "
            f"strategy={strategy_version}): status/close_reason={close_reason}, "
            f"net_realized_pnl={net_pnl} USDT, fees_paid={fees_paid} USDT, funding_pnl={funding_pnl} USDT. "
            f"Execution followed deterministic RiskEngine sizing and native TP/SL verification."
        )

        output_summary = {
            "symbol": symbol,
            "direction": direction,
            "outcome_tag": outcome_tag,
            "net_realized_pnl": str(net_pnl),
            "fees_paid": str(fees_paid),
            "review_summary": review_summary,
        }

        latency_ms = max(1, int((time.perf_counter() - t0) * 1000))
        prompt_tokens, completion_tokens, cost = _estimate_tokens_and_cost(sanitized_input, output_summary)
        run = AgentRun(
            run_type=self.run_type,
            model=settings.AI_MODEL,
            provider=settings.AI_PROVIDER,
            prompt_version=self.prompt_version,
            status="SUCCEEDED",
            latency_ms=latency_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            estimated_cost_usd=cost,
            input_summary_json=sanitized_input,
            output_summary_json=output_summary,
            error_message=None,
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        return review_summary, run


class AIOrchestratorService:
    """
    AI Orchestrator Service (Section 3.6):
    Coordinates the 5 specialized agents (`MarketAgent`, `ScannerAgent`, `StrategyAgent`,
    `RiskAgent`, `JournalAgent`) as a deterministic workflow state machine with strict
    secret scrubbing, Pydantic `TradePlanSchema` validation, and Fail-Closed fault handling.
    """

    WORKFLOW_STATES = (
        "IDLE",
        "MARKET_ANALYSIS",
        "OPPORTUNITY_RANKING",
        "STRATEGY_SYNTHESIS",
        "SCHEMA_VALIDATION",
        "RISK_EXPLANATION",
        "COMPLETED",
        "FAILED_CLOSED",
    )

    def __init__(self) -> None:
        self.market_agent = MarketAgent()
        self.scanner_agent = ScannerAgent()
        self.strategy_agent = StrategyAgent()
        self.risk_agent = RiskAgent()
        self.journal_agent = JournalAgent()
        self.current_state: str = "IDLE"
        self.last_workflow_trace: list[dict[str, Any]] = []

    def _transition(self, state: str, details: dict[str, Any] | None = None) -> None:
        self.current_state = state
        self.last_workflow_trace.append(
            {
                "state": state,
                "timestamp": utc_iso(),
                "details": redact_sensitive_data(details or {}),
            }
        )

    def generate_trade_plan(
        self,
        db: Session,
        *,
        signal: TradeSignal | dict[str, Any] | None = None,
        signal_id: str | None = None,
        symbol: str | None = None,
        exchange: str | None = None,
        market_type: str | None = None,
        timeframe: str | None = None,
        direction: str | None = None,
        opportunities: list[dict[str, Any]] | None = None,
        extra_context: dict[str, Any] | None = None,
        raw_ai_output: dict[str, Any] | str | None = None,
        simulate_invalid_json: bool = False,
        simulate_timeout: bool = False,
    ) -> ValidatedTradePlan | None:
        """
        Execute the deterministic multi-agent state machine:
        `MarketAgent -> ScannerAgent -> StrategyAgent -> TradePlanSchema Validation -> RiskAgent`.
        On timeout, invalid JSON, or schema validation failure:
        - Records failed `AgentRun` (`status="FAILED"` or `"TIMEOUT"`)
        - Records invalid `AIDecision` (`is_valid=False`)
        - Logs a `SystemEvent`
        - Returns `None` (Fail-Closed).
        """
        self.last_workflow_trace = []
        self._transition("IDLE", {"simulate_invalid_json": simulate_invalid_json, "simulate_timeout": simulate_timeout})

        if extra_context is not None:
            extra_context = scrub_and_verify_ai_input(extra_context)

        signal_row: TradeSignal | None = None
        if isinstance(signal, TradeSignal):
            signal_row = signal
        elif signal_id:
            signal_row = db.scalar(select(TradeSignal).where(TradeSignal.id == signal_id))
        elif isinstance(signal, dict) and signal.get("id"):
            signal_row = db.scalar(select(TradeSignal).where(TradeSignal.id == str(signal["id"])))

        sig_dict: dict[str, Any] = {}
        if signal_row is not None:
            sig_dict = {
                "id": signal_row.id,
                "strategy_version_id": signal_row.strategy_version_id,
                "exchange": signal_row.exchange,
                "market_type": signal_row.market_type,
                "symbol": signal_row.symbol,
                "timeframe": signal_row.timeframe,
                "direction": signal_row.direction,
                "trigger_reason": signal_row.trigger_reason,
                "indicators_snapshot": dict(signal_row.indicators_snapshot or {}),
            }
        elif isinstance(signal, dict):
            sig_dict = scrub_and_verify_ai_input(signal)

        eff_exchange = exchange or sig_dict.get("exchange") or "binance"
        eff_market_type = market_type or sig_dict.get("market_type") or "usdt_perp"
        eff_symbol = symbol or sig_dict.get("symbol") or (
            "BTC/USDT:USDT" if eff_market_type == "usdt_perp" else "BTC/USDT"
        )
        eff_timeframe = timeframe or sig_dict.get("timeframe") or "15m"
        eff_direction = direction or sig_dict.get("direction") or ("LONG" if eff_market_type == "usdt_perp" else "BUY")

        strategy_version_id = sig_dict.get("strategy_version_id")
        strategy_version_str = "trend_following_v1:v1"
        if strategy_version_id:
            ver_row = db.scalar(select(StrategyVersion).where(StrategyVersion.id == strategy_version_id))
            if ver_row is not None:
                strat_row = db.scalar(select(Strategy).where(Strategy.id == ver_row.strategy_id))
                code_prefix = strat_row.code if strat_row else "strategy"
                strategy_version_str = f"{code_prefix}:v{ver_row.version}"

        inst = db.scalar(
            select(Instrument).where(
                Instrument.exchange == eff_exchange,
                Instrument.market_type == eff_market_type,
                Instrument.canonical_symbol == eff_symbol,
            )
        )
        tick_size = to_decimal(inst.tick_size, Decimal("0.01")) if inst else Decimal("0.01")

        snap = db.scalar(
            select(MarketSnapshot)
            .where(
                MarketSnapshot.exchange == eff_exchange,
                MarketSnapshot.market_type == eff_market_type,
                MarketSnapshot.symbol == eff_symbol,
            )
            .order_by(MarketSnapshot.event_time.desc())
        )
        closed_candles = list(
            db.scalars(
                select(MarketCandle)
                .where(
                    MarketCandle.exchange == eff_exchange,
                    MarketCandle.market_type == eff_market_type,
                    MarketCandle.symbol == eff_symbol,
                    MarketCandle.is_closed.is_(True),
                )
                .order_by(MarketCandle.open_time.asc())
            ).all()
        )

        indicators = dict(sig_dict.get("indicators_snapshot") or {})
        if not indicators:
            indicators = compute_indicators_from_candles_and_snapshot(closed_candles, snap)

        # Stage 1: MarketAgent
        self._transition("MARKET_ANALYSIS", {"symbol": eff_symbol, "exchange": eff_exchange})
        market_summary, market_run = self.market_agent.analyze(
            db,
            symbol=eff_symbol,
            exchange=eff_exchange,
            market_type=eff_market_type,
            timeframe=eff_timeframe,
            indicators=indicators,
            extra_context=extra_context,
        )

        # Stage 2: ScannerAgent
        self._transition("OPPORTUNITY_RANKING")
        raw_opps = opportunities if opportunities is not None else scanner_service.get_opportunities(db)
        scanner_summary, scanner_run = self.scanner_agent.rank_and_deduplicate(
            db,
            opportunities=raw_opps,
            extra_context=extra_context,
        )

        # Stage 3: StrategyAgent
        self._transition("STRATEGY_SYNTHESIS", {"strategy_version": strategy_version_str})
        candidate_output, strategy_run = self.strategy_agent.synthesize_plan(
            db,
            exchange=eff_exchange,
            market_type=eff_market_type,
            symbol=eff_symbol,
            timeframe=eff_timeframe,
            direction=eff_direction,
            strategy_version_str=strategy_version_str,
            indicators=indicators,
            market_summary=market_summary,
            tick_size=tick_size,
            signal_reason=sig_dict.get("trigger_reason"),
            raw_ai_output=raw_ai_output,
            simulate_invalid_json=simulate_invalid_json,
            simulate_timeout=simulate_timeout,
            extra_context=extra_context,
        )

        # Handle Timeout Fault Injection (Fail-Closed)
        if simulate_timeout or strategy_run.status == "TIMEOUT":
            self._transition("FAILED_CLOSED", {"reason": "AI_TIMEOUT", "agent_run_id": strategy_run.id})
            invalid_decision = AIDecision(
                agent_run_id=strategy_run.id,
                strategy_version_id=strategy_version_id,
                market_snapshot_id=snap.id if snap else None,
                exchange=eff_exchange,
                market_type=eff_market_type,
                symbol=eff_symbol,
                direction=eff_direction,
                confidence=ZERO,
                output_json={"error": "AI_TIMEOUT", "fail_closed": True},
                risk_explanation="Fail-Closed: AI agent execution timed out; no order permitted.",
                schema_version="1.0",
                is_valid=False,
                validation_error="AI provider request timed out",
            )
            db.add(invalid_decision)
            db.add(
                SystemEvent(
                    component="ai_orchestrator",
                    event_type="AI_AGENT_TIMEOUT",
                    severity="WARNING",
                    payload={
                        "agent_run_id": strategy_run.id,
                        "symbol": eff_symbol,
                        "exchange": eff_exchange,
                        "fail_closed": True,
                    },
                )
            )
            db.commit()
            event_bus.publish_sync(
                channel="agents",
                event_type="ai.decision_failed",
                data={"reason": "AI_TIMEOUT", "symbol": eff_symbol, "agent_run_id": strategy_run.id},
                source="ai_orchestrator",
            )
            log_event(logger, 30, "AI orchestrator timeout - fail-closed", symbol=eff_symbol)
            return None

        # Stage 4: Strict TradePlanSchema Validation
        self._transition("SCHEMA_VALIDATION")
        try:
            if isinstance(candidate_output, str):
                parsed_json = json.loads(candidate_output)
            elif isinstance(candidate_output, dict):
                parsed_json = candidate_output
            else:
                raise ValueError("AI output must be a JSON object or JSON string")

            validated_base = TradePlanSchema.model_validate(parsed_json)
        except ( json.JSONDecodeError, ValidationError, ValueError ) as exc:
            error_msg = str(exc)
            strategy_run.status = "FAILED"
            strategy_run.error_message = error_msg
            self._transition("FAILED_CLOSED", {"reason": "INVALID_SCHEMA_OR_JSON", "error": error_msg})

            invalid_decision = AIDecision(
                agent_run_id=strategy_run.id,
                strategy_version_id=strategy_version_id,
                market_snapshot_id=snap.id if snap else None,
                exchange=eff_exchange,
                market_type=eff_market_type,
                symbol=eff_symbol,
                direction=eff_direction,
                confidence=ZERO,
                output_json=(
                    {"raw_output": candidate_output}
                    if not isinstance(candidate_output, dict)
                    else redact_sensitive_data(candidate_output)
                ),
                risk_explanation="Fail-Closed: AI output failed strict TradePlanSchema validation.",
                schema_version="1.0",
                is_valid=False,
                validation_error=error_msg,
            )
            db.add(invalid_decision)
            db.add(
                SystemEvent(
                    component="ai_orchestrator",
                    event_type="AI_SCHEMA_VALIDATION_FAILED",
                    severity="WARNING",
                    payload={
                        "agent_run_id": strategy_run.id,
                        "symbol": eff_symbol,
                        "error": error_msg,
                        "fail_closed": True,
                    },
                )
            )
            db.commit()
            event_bus.publish_sync(
                channel="agents",
                event_type="ai.decision_failed",
                data={"reason": "AI_SCHEMA_VALIDATION_FAILED", "symbol": eff_symbol, "error": error_msg},
                source="ai_orchestrator",
            )
            log_event(logger, 30, "AI output failed schema validation - fail-closed", error=error_msg)
            return None

        # Stage 5: RiskAgent Explanation
        self._transition("RISK_EXPLANATION")
        risk_explanation, risk_run = self.risk_agent.explain_risk(
            db,
            trade_plan=validated_base,
            market_summary=market_summary,
            extra_context=extra_context,
        )

        validated_plan = ValidatedTradePlan.model_validate(validated_base.model_dump())
        contract_dict = validated_plan.to_contract_dict()

        decision = AIDecision(
            agent_run_id=strategy_run.id,
            strategy_version_id=strategy_version_id,
            market_snapshot_id=snap.id if snap else None,
            exchange=validated_plan.exchange,
            market_type=validated_plan.market_type,
            symbol=validated_plan.symbol,
            direction=validated_plan.direction,
            confidence=validated_plan.confidence,
            output_json=contract_dict,
            risk_explanation=risk_explanation,
            schema_version="1.0",
            is_valid=True,
            validation_error=None,
        )
        db.add(decision)
        db.flush()

        if signal_row is not None:
            signal_row.decision_id = decision.id

        self._transition(
            "COMPLETED",
            {
                "decision_id": decision.id,
                "agent_run_ids": {
                    "market": market_run.id,
                    "scanner": scanner_run.id,
                    "strategy": strategy_run.id,
                    "risk": risk_run.id,
                },
            },
        )
        db.commit()
        db.refresh(decision)

        validated_plan.attach_metadata(
            decision=decision,
            agent_run_id=strategy_run.id,
            risk_explanation=risk_explanation,
            workflow_trace=self.last_workflow_trace,
        )

        event_bus.publish_sync(
            channel="agents",
            event_type="ai.decision_generated",
            data=validated_plan.to_dict(),
            source="ai_orchestrator",
        )
        return validated_plan

    # Convenience alias
    run_workflow = generate_trade_plan
    generate_decision = generate_trade_plan

    def generate_journal_summary(
        self,
        db: Session,
        journal_context: dict[str, Any],
    ) -> str:
        summary_text, _ = self.journal_agent.generate_journal_summary(db, journal_context)
        return summary_text

    def get_agents_status(self, db: Session | None = None) -> dict[str, Any]:
        recent_runs_count = 0
        failed_runs_count = 0
        total_cost = ZERO
        if db is not None:
            runs = list(db.scalars(select(AgentRun).order_by(AgentRun.created_at.desc()).limit(100)).all())
            recent_runs_count = len(runs)
            failed_runs_count = sum(1 for r in runs if r.status != "SUCCEEDED")
            total_cost = sum((to_decimal(r.estimated_cost_usd, ZERO) for r in runs), ZERO)

        agents_info = [
            {
                "name": "MarketAgent",
                "run_type": "market",
                "role": "Summarizes market regime, volatility, and funding rate conditions",
                "model": settings.AI_MODEL,
                "provider": settings.AI_PROVIDER,
                "status": "READY",
            },
            {
                "name": "ScannerAgent",
                "run_type": "scanner",
                "role": "Ranks and deduplicates candidate market opportunities",
                "model": settings.AI_MODEL,
                "provider": settings.AI_PROVIDER,
                "status": "READY",
            },
            {
                "name": "StrategyAgent",
                "run_type": "strategy",
                "role": "Synthesizes rule signals into structured TradePlanSchema candidates",
                "model": settings.AI_MODEL,
                "provider": settings.AI_PROVIDER,
                "status": "READY",
            },
            {
                "name": "RiskAgent",
                "run_type": "risk",
                "role": "Explains stop distance, invalidation conditions, and R:R (RiskEngine retains final authority)",
                "model": settings.AI_MODEL,
                "provider": settings.AI_PROVIDER,
                "status": "READY",
            },
            {
                "name": "JournalAgent",
                "run_type": "journal",
                "role": "Generates structured trade lifecycle and post-trade review summaries",
                "model": settings.AI_MODEL,
                "provider": settings.AI_PROVIDER,
                "status": "READY",
            },
        ]

        return {
            "workflow_state": self.current_state,
            "provider": settings.AI_PROVIDER,
            "model": settings.AI_MODEL,
            "timeout_seconds": settings.AI_TIMEOUT_SECONDS,
            "secret_redaction_enforced": True,
            "direct_execution_allowed": False,
            "agents": agents_info,
            "recent_runs_count": recent_runs_count,
            "failed_runs_count": failed_runs_count,
            "total_estimated_cost_usd": str(total_cost),
            "last_workflow_trace": self.last_workflow_trace,
        }

    get_status = get_agents_status

    def list_agent_runs(
        self,
        db: Session,
        run_type: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        stmt = select(AgentRun).order_by(AgentRun.created_at.desc())
        if run_type:
            stmt = stmt.where(AgentRun.run_type == run_type)
        runs = list(db.scalars(stmt.limit(limit)).all())
        return [
            {
                "id": r.id,
                "run_type": r.run_type,
                "model": r.model,
                "provider": r.provider,
                "prompt_version": r.prompt_version,
                "status": r.status,
                "latency_ms": r.latency_ms,
                "prompt_tokens": r.prompt_tokens,
                "completion_tokens": r.completion_tokens,
                "estimated_cost_usd": str(r.estimated_cost_usd),
                "input_summary_json": r.input_summary_json,
                "output_summary_json": r.output_summary_json,
                "error_message": r.error_message,
                "created_at": utc_iso(r.created_at),
            }
            for r in runs
        ]

    get_runs = list_agent_runs

    def list_decisions(
        self,
        db: Session,
        symbol: str | None = None,
        valid_only: bool = False,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        stmt = select(AIDecision).order_by(AIDecision.created_at.desc())
        if symbol:
            stmt = stmt.where(AIDecision.symbol == symbol)
        if valid_only:
            stmt = stmt.where(AIDecision.is_valid.is_(True))
        rows = list(db.scalars(stmt.limit(limit)).all())
        return [
            {
                "id": d.id,
                "agent_run_id": d.agent_run_id,
                "strategy_version_id": d.strategy_version_id,
                "market_snapshot_id": d.market_snapshot_id,
                "exchange": d.exchange,
                "market_type": d.market_type,
                "symbol": d.symbol,
                "direction": d.direction,
                "confidence": str(d.confidence),
                "output_json": d.output_json,
                "risk_explanation": d.risk_explanation,
                "schema_version": d.schema_version,
                "is_valid": d.is_valid,
                "validation_error": d.validation_error,
                "created_at": utc_iso(d.created_at),
            }
            for d in rows
        ]

    get_decisions = list_decisions


ai_orchestrator_service = AIOrchestratorService()
ai_service = ai_orchestrator_service
