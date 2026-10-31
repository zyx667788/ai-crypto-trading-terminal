"""
Exchange rate-limit governance (§3.4: "Scanner 不应对每个交易对高频发起 REST 请求…
遵守交易所限频规则").

A thread-safe weighted token bucket per (exchange, bucket). Real HTTP adapters call
`acquire()` before every request; the simulated adapters call it too so the accounting
is exercised and observable in tests and via `/api/v1/system/rate-limits`.

Weights follow the public documentation conventions:
- Binance: each endpoint carries a REQUEST_WEIGHT; the spot limit is 6000 weight/minute.
- OKX: 60 requests / 2s for most market endpoints.
These are conservative defaults and are configurable per environment.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any


class RateLimitExceeded(RuntimeError):
    """Raised when a request would exceed the exchange budget and cannot wait."""


@dataclass
class BucketConfig:
    capacity: float           # max tokens (weight) in the window
    refill_per_second: float  # tokens restored per second
    name: str = "default"


# Conservative defaults; override per deployment if the exchange changes its limits.
DEFAULT_BUCKETS: dict[str, BucketConfig] = {
    "binance:spot": BucketConfig(capacity=6000, refill_per_second=100, name="binance spot 6000/min"),
    "binance:usdt_perp": BucketConfig(capacity=2400, refill_per_second=40, name="binance usdt-m 2400/min"),
    "okx:spot": BucketConfig(capacity=60, refill_per_second=30, name="okx 60/2s"),
    "okx:usdt_perp": BucketConfig(capacity=60, refill_per_second=30, name="okx swap 60/2s"),
}

# Typical per-endpoint weights.
ENDPOINT_WEIGHTS: dict[str, float] = {
    "get_instruments": 10.0,
    "get_balance": 10.0,
    "get_balances": 10.0,
    "get_ticker": 2.0,
    "get_klines": 2.0,
    "get_orderbook": 5.0,
    "get_funding_rate": 2.0,
    "get_positions": 10.0,
    "get_fills": 10.0,
    "get_order": 2.0,
    "place_order": 1.0,
    "cancel_order": 1.0,
    "reconcile": 20.0,
    "subscribe_market": 1.0,
    "subscribe_private": 1.0,
    "connect": 1.0,
    "get_capabilities": 1.0,
}


@dataclass
class BucketState:
    tokens: float
    updated_at: float
    acquired_total: float = 0.0
    rejected_total: int = 0
    waited_seconds: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)


class ExchangeRateLimiter:
    """Weighted token bucket shared by every adapter of a given exchange+market."""

    def __init__(self, buckets: dict[str, BucketConfig] | None = None) -> None:
        self._configs = dict(buckets or DEFAULT_BUCKETS)
        self._states: dict[str, BucketState] = {}
        self._registry_lock = threading.Lock()

    # ------------------------------------------------------------- internals
    def _key(self, exchange: str, market_type: str | None) -> str:
        market = (market_type or "spot").lower()
        key = f"{exchange.lower()}:{market}"
        if key not in self._configs:
            # Unknown market: fall back to the exchange's spot budget rather than unlimited.
            key = f"{exchange.lower()}:spot"
        return key if key in self._configs else "binance:spot"

    def _state(self, key: str, now: float) -> BucketState:
        with self._registry_lock:
            state = self._states.get(key)
            if state is None:
                capacity = self._configs[key].capacity
                state = BucketState(tokens=capacity, updated_at=now)
                self._states[key] = state
            return state

    def _refill(self, key: str, state: BucketState, now: float) -> None:
        cfg = self._configs[key]
        elapsed = max(0.0, now - state.updated_at)
        if elapsed > 0:
            state.tokens = min(cfg.capacity, state.tokens + elapsed * cfg.refill_per_second)
            state.updated_at = now

    # ------------------------------------------------------------------- api
    def weight_for(self, method: str) -> float:
        return ENDPOINT_WEIGHTS.get(method, 1.0)

    def acquire(
        self,
        exchange: str,
        market_type: str | None,
        method: str,
        *,
        weight: float | None = None,
        block: bool = True,
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        """
        Consume `weight` tokens.

        `block=True` waits (bounded by `timeout`) until budget is available; otherwise the
        call fails fast with `RateLimitExceeded`. Either way the caller must treat a
        rejection as "do not send this request", never as "retry immediately".
        """
        key = self._key(exchange, market_type)
        need = float(weight if weight is not None else self.weight_for(method))
        cfg = self._configs[key]
        if need > cfg.capacity:
            raise ValueError(f"Request weight {need} exceeds bucket capacity {cfg.capacity} for {key}")

        deadline = time.monotonic() + max(0.0, timeout)
        waited = 0.0

        while True:
            now = time.monotonic()
            state = self._state(key, now)
            with state.lock:
                self._refill(key, state, now)
                if state.tokens >= need:
                    state.tokens -= need
                    state.acquired_total += need
                    state.waited_seconds += waited
                    return {
                        "bucket": key,
                        "weight": need,
                        "remaining": round(state.tokens, 3),
                        "capacity": cfg.capacity,
                        "waited_seconds": round(waited, 3),
                    }
                missing = need - state.tokens

            if not block:
                with state.lock:
                    state.rejected_total += 1
                raise RateLimitExceeded(
                    f"Rate limit exceeded for {key}: need {need}, available {round(state.tokens, 3)}"
                )

            wait_for = missing / cfg.refill_per_second
            if time.monotonic() + wait_for > deadline:
                with state.lock:
                    state.rejected_total += 1
                raise RateLimitExceeded(
                    f"Rate limit wait for {key} would exceed timeout ({wait_for:.2f}s > {timeout}s)"
                )
            time.sleep(min(wait_for, 0.25))
            waited += min(wait_for, 0.25)

    def peek(self, exchange: str, market_type: str | None) -> dict[str, Any]:
        key = self._key(exchange, market_type)
        now = time.monotonic()
        state = self._state(key, now)
        cfg = self._configs[key]
        with state.lock:
            self._refill(key, state, now)
            return {
                "bucket": key,
                "policy": cfg.name,
                "capacity": cfg.capacity,
                "refill_per_second": cfg.refill_per_second,
                "available": round(state.tokens, 3),
                "utilization_pct": round((1 - state.tokens / cfg.capacity) * 100, 2) if cfg.capacity else 0.0,
                "acquired_total": round(state.acquired_total, 2),
                "rejected_total": state.rejected_total,
                "waited_seconds": round(state.waited_seconds, 3),
            }

    def snapshot(self) -> list[dict[str, Any]]:
        out = []
        for key in self._configs:
            exchange, market = key.split(":", 1)
            out.append(self.peek(exchange, market))
        return out

    def reset(self) -> None:
        """Test helper: restore every bucket to full capacity."""
        with self._registry_lock:
            self._states.clear()


rate_limiter = ExchangeRateLimiter()
