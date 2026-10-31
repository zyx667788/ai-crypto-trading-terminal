"""
AI Provider Adapter — OpenAI-compatible chat completions.

Design constraints (V1.0 §3.6 / AI_RULES.md):
- The exchange API secrets are NEVER part of any prompt. Only already-scrubbed market
  features, indicators and risk summaries are sent.
- Every response must be JSON; anything else raises `AIProviderInvalidOutput` so the
  orchestrator can Fail-Closed instead of guessing.
- Timeouts and transport errors are typed (`AIProviderTimeout`, `AIProviderError`) and
  never retried blindly more than `AI_MAX_RETRIES` times.
- The API key is held only in memory and never logged, returned, or persisted.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger
from app.core.precision import to_decimal
from app.core.security import redact_sensitive_data

logger = get_logger("terminal.ai.provider")

# Rough per-1M-token pricing used only for local cost reporting, never for billing.
INPUT_USD_PER_MTOK = 0.30
OUTPUT_USD_PER_MTOK = 2.50


class AIProviderError(Exception):
    """Transport / HTTP / configuration failure."""


class AIProviderTimeout(AIProviderError):
    """The provider did not answer within the configured timeout."""


class AIProviderInvalidOutput(AIProviderError):
    """The provider answered, but the payload was not a usable JSON object."""


@dataclass
class AICompletion:
    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    finish_reason: str | None = None
    attempts: int = 1
    raw_usage: dict[str, Any] = field(default_factory=dict)

    @property
    def estimated_cost_usd(self):
        return (
            to_decimal(self.prompt_tokens) / to_decimal(1_000_000) * to_decimal(str(INPUT_USD_PER_MTOK))
            + to_decimal(self.completion_tokens) / to_decimal(1_000_000) * to_decimal(str(OUTPUT_USD_PER_MTOK))
        ).quantize(to_decimal("0.000001"))

    def as_json(self) -> dict[str, Any]:
        text = self.text.strip()
        # Some OpenAI-compatible gateways wrap JSON in markdown fences even when
        # response_format=json_object is requested. Strip a single outer fence.
        if text.startswith("```"):
            first_newline = text.find("\n")
            if first_newline != -1:
                text = text[first_newline + 1 :]
            if text.rstrip().endswith("```"):
                text = text.rstrip()[:-3]
            text = text.strip()
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, TypeError) as exc:
            raise AIProviderInvalidOutput(f"Provider returned non-JSON content: {exc}") from exc
        if not isinstance(payload, dict):
            raise AIProviderInvalidOutput("Provider returned JSON that is not an object")
        return payload


class OpenAICompatibleProvider:
    """Thin, dependency-light adapter for any OpenAI-compatible `/v1/chat/completions` API."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout_seconds: int | None = None,
        max_retries: int | None = None,
    ) -> None:
        self._base_url = (base_url if base_url is not None else settings.AI_BASE_URL or "").rstrip("/")
        self._api_key = api_key if api_key is not None else (settings.AI_API_KEY or "")
        self.model = model or settings.AI_MODEL
        self.timeout_seconds = timeout_seconds if timeout_seconds is not None else settings.AI_TIMEOUT_SECONDS
        self.max_retries = max_retries if max_retries is not None else settings.AI_MAX_RETRIES

    # ------------------------------------------------------------------ state
    @property
    def is_configured(self) -> bool:
        return bool(self._api_key) and bool(self._base_url) and bool(self.model)

    @property
    def describe(self) -> dict[str, Any]:
        """Safe, non-secret description for status endpoints."""
        return {
            "provider": "openai_compatible",
            "configured": self.is_configured,
            "model": self.model,
            "base_url_masked": self._masked_base(),
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
        }

    def _masked_base(self) -> str:
        if not self._base_url:
            return ""
        parts = self._base_url.split("//", 1)
        return parts[0] + "//" + (parts[1][:12] + "…" if len(parts) > 1 else "")

    # ---------------------------------------------------------------- requests
    def complete_json(
        self,
        *,
        system_prompt: str,
        user_payload: dict[str, Any],
        temperature: float = 0.2,
        max_tokens: int = 1200,
        expect_json: bool = True,
    ) -> AICompletion:
        if not self.is_configured:
            raise AIProviderError("AI provider is not configured (missing AI_API_KEY / AI_BASE_URL / AI_MODEL)")

        # Defence in depth: scrub once more right before leaving the process.
        safe_payload = redact_sensitive_data(user_payload)

        body = {
            "model": self.model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(safe_payload, ensure_ascii=False, default=str)},
            ],
        }

        last_error: Exception | None = None
        attempts = max(1, int(self.max_retries) + 1)

        for attempt in range(1, attempts + 1):
            started = time.perf_counter()
            try:
                raw = self._post(body)
            except AIProviderTimeout as exc:
                last_error = exc
                logger.warning("ai provider timeout", extra={"structured_data": {"attempt": attempt, "model": self.model}})
                continue
            except AIProviderError as exc:
                last_error = exc
                logger.warning("ai provider error", extra={"structured_data": {"attempt": attempt, "error": str(exc)[:200]}})
                continue

            latency_ms = int((time.perf_counter() - started) * 1000)
            choices = raw.get("choices") or []
            if not choices:
                last_error = AIProviderInvalidOutput("Provider response contained no choices")
                continue

            message = choices[0].get("message") or {}
            content = message.get("content")
            if isinstance(content, list):  # some gateways return content parts
                content = "".join(part.get("text", "") for part in content if isinstance(part, dict))

            usage = raw.get("usage") or {}
            completion = AICompletion(
                text=(content or "").strip(),
                model=raw.get("model") or self.model,
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                latency_ms=latency_ms,
                finish_reason=choices[0].get("finish_reason"),
                attempts=attempt,
                raw_usage=usage,
            )
            if not completion.text:
                last_error = AIProviderInvalidOutput("Provider returned an empty message")
                continue
            if completion.finish_reason == "length":
                # A truncated payload can never be valid JSON — surface it explicitly
                # instead of letting a JSON parse error masquerade as bad formatting.
                raise AIProviderInvalidOutput(
                    f"Provider response was truncated at max_tokens={max_tokens} (finish_reason=length). "
                    "Increase AI max_tokens or shorten the requested output."
                )
            if expect_json:
                # Fail here rather than handing unparseable prose to the caller.
                completion.as_json()
            return completion

        raise last_error or AIProviderError("AI provider call failed")

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        import httpx

        url = f"{self._base_url}/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            # trust_env=False so a stray IDE/proxy variable cannot hijack a localhost-adjacent API call.
            with httpx.Client(timeout=float(self.timeout_seconds), trust_env=False) as client:
                resp = client.post(url, json=body, headers=headers)
        except httpx.TimeoutException as exc:
            raise AIProviderTimeout(f"AI provider timed out after {self.timeout_seconds}s") from exc
        except httpx.HTTPError as exc:
            raise AIProviderError(f"AI provider transport error: {type(exc).__name__}") from exc

        if resp.status_code == 429:
            raise AIProviderError("AI provider rate-limited the request (HTTP 429)")
        if resp.status_code >= 400:
            # Surface a short upstream reason for diagnosis, but never the credentials.
            detail = ""
            try:
                body = resp.json()
                message = (body.get("error") or {}).get("message") if isinstance(body.get("error"), dict) else None
                if not message:
                    message = body.get("message") if isinstance(body, dict) else None
                if message:
                    detail = f": {str(message)[:240]}"
            except ValueError:
                detail = f": {resp.text[:160]}"
            raise AIProviderError(f"AI provider returned HTTP {resp.status_code}{detail}")
        try:
            return resp.json()
        except ValueError as exc:
            raise AIProviderInvalidOutput("AI provider returned a non-JSON HTTP body") from exc


# Process-wide default instance; re-created lazily if settings change between runs.
_provider_singleton: OpenAICompatibleProvider | None = None


def get_ai_provider(refresh: bool = False) -> OpenAICompatibleProvider:
    global _provider_singleton
    if _provider_singleton is None or refresh:
        _provider_singleton = OpenAICompatibleProvider()
    return _provider_singleton
