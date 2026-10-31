"""Exchange rate-limit governance tests: weighting, exhaustion, blocking, re-entrancy."""
from __future__ import annotations

import threading
import time

import pytest

from app.exchanges.rate_limit import (
    BucketConfig,
    ExchangeRateLimiter,
    RateLimitExceeded,
)
from app.exchanges.binance.adapter import BinanceAdapter


def make_limiter(capacity: float = 10, refill: float = 1.0) -> ExchangeRateLimiter:
    return ExchangeRateLimiter(
        {"binance:spot": BucketConfig(capacity=capacity, refill_per_second=refill, name="test")}
    )


def test_default_weights_match_documented_conventions():
    limiter = make_limiter()
    assert limiter.weight_for("get_ticker") == 2.0
    assert limiter.weight_for("get_orderbook") == 5.0
    assert limiter.weight_for("place_order") == 1.0
    assert limiter.weight_for("unknown_method") == 1.0


def test_acquire_decrements_weight():
    limiter = make_limiter(capacity=100)
    limiter.acquire("binance", "spot", "get_ticker")
    est = limiter.peek("binance", "spot")
    assert est["available"] == pytest.approx(98.0, abs=0.5)
    assert est["acquired_total"] == 2.0


def test_non_blocking_acquire_raises_when_exhausted():
    limiter = make_limiter(capacity=5, refill=0.01)
    limiter.acquire("binance", "spot", "get_ticker", weight=5)
    with pytest.raises(RateLimitExceeded):
        limiter.acquire("binance", "spot", "get_ticker", block=False)
    assert limiter.peek("binance", "spot")["rejected_total"] == 1


def test_blocking_acquire_waits_then_succeeds():
    limiter = make_limiter(capacity=2, refill=50.0)  # refills very fast
    limiter.acquire("binance", "spot", "get_ticker", weight=2)
    started = time.perf_counter()
    limiter.acquire("binance", "spot", "get_ticker", weight=2, block=True, timeout=2.0)
    elapsed = time.perf_counter() - started
    assert elapsed < 1.0, "must wait only for the refill, not the whole timeout"


def test_blocking_acquire_times_out_when_refill_too_slow():
    limiter = make_limiter(capacity=5, refill=0.1)
    limiter.acquire("binance", "spot", "get_ticker", weight=5)
    with pytest.raises(RateLimitExceeded):
        limiter.acquire("binance", "spot", "get_ticker", weight=5, block=True, timeout=0.2)


def test_weight_above_capacity_is_rejected_immediately():
    limiter = make_limiter(capacity=1)
    with pytest.raises(ValueError):
        limiter.acquire("binance", "spot", "get_orderbook", weight=5)


def test_unknown_market_falls_back_to_spot_bucket():
    limiter = make_limiter(capacity=100)
    limiter.acquire("binance", "some_new_market", "get_ticker")
    assert limiter.peek("binance", "some_new_market")["bucket"] == "binance:spot"


def test_snapshot_covers_every_configured_bucket():
    from app.exchanges.rate_limit import DEFAULT_BUCKETS

    limiter = ExchangeRateLimiter()
    snapshot = limiter.snapshot()
    assert len(snapshot) == len(DEFAULT_BUCKETS)
    assert {b["bucket"] for b in snapshot} == set(DEFAULT_BUCKETS)


def test_limiter_is_thread_safe():
    limiter = make_limiter(capacity=1000, refill=1000)

    def worker():
        for _ in range(50):
            limiter.acquire("binance", "spot", "get_ticker", block=False)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    est = limiter.peek("binance", "spot")
    assert est["acquired_total"] == pytest.approx(8 * 50 * 2.0, abs=1.0)


def test_adapter_charges_one_weight_per_logical_call():
    """Nested public calls inside one adapter method must not be double-billed."""
    from app.exchanges.rate_limit import rate_limiter

    rate_limiter.reset()
    adapter = BinanceAdapter(environment="demo")
    adapter.get_ticker("BTC/USDT:USDT", market_type="usdt_perp")
    assert rate_limiter.peek("binance", "usdt_perp")["acquired_total"] == pytest.approx(2.0)

    adapter.get_orderbook("BTC/USDT:USDT", market_type="usdt_perp")
    assert rate_limiter.peek("binance", "usdt_perp")["acquired_total"] == pytest.approx(7.0)
    rate_limiter.reset()
