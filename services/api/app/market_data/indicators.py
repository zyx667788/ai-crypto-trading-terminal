from __future__ import annotations

import math
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Sequence

from app.core.precision import ONE, ZERO, to_decimal


def _is_candle_like(item: Any) -> bool:
    if isinstance(item, dict):
        return "close" in item or "open" in item
    return hasattr(item, "close") and hasattr(item, "open")


def filter_closed_candles(candles: Sequence[Any], closed_only: bool = True) -> list[Any]:
    if not closed_only:
        return list(candles)
    result: list[Any] = []
    for c in candles:
        if isinstance(c, dict):
            if c.get("is_closed", True):
                result.append(c)
        else:
            if getattr(c, "is_closed", True):
                result.append(c)
    return result


def _extract_decimal_series(values: Sequence[Any], field: str = "close", closed_only: bool = True) -> list[Decimal]:
    if not values:
        return []
    if _is_candle_like(values[0]):
        filtered = filter_closed_candles(values, closed_only=closed_only)
        out: list[Decimal] = []
        for c in filtered:
            raw = c.get(field) if isinstance(c, dict) else getattr(c, field)
            out.append(to_decimal(raw, default=ZERO))
        return out
    return [to_decimal(v, default=ZERO) for v in values]


def _ema_series(nums: list[Decimal], period: int) -> list[Decimal]:
    if not nums:
        return []
    p = max(1, int(period))
    k = Decimal("2") / Decimal(p + 1)
    ema = nums[0]
    series: list[Decimal] = [ema]
    for val in nums[1:]:
        ema = (val * k) + (ema * (ONE - k))
        series.append(ema)
    return series


def calculate_ema(values: Sequence[Any], period: int = 9, closed_only: bool = True) -> Decimal:
    nums = _extract_decimal_series(values, field="close", closed_only=closed_only)
    if not nums:
        return ZERO
    series = _ema_series(nums, period)
    return series[-1].quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def calculate_rsi(values: Sequence[Any], period: int = 14, closed_only: bool = True) -> Decimal:
    nums = _extract_decimal_series(values, field="close", closed_only=closed_only)
    p = max(1, int(period))
    if len(nums) < 2:
        return Decimal("50.00")

    deltas = [nums[i] - nums[i - 1] for i in range(1, len(nums))]
    init_len = min(p, len(deltas))
    gains = [max(ZERO, d) for d in deltas[:init_len]]
    losses = [max(ZERO, -d) for d in deltas[:init_len]]

    avg_gain = sum(gains, ZERO) / Decimal(init_len)
    avg_loss = sum(losses, ZERO) / Decimal(init_len)

    p_dec = Decimal(p)
    for d in deltas[init_len:]:
        gain = max(ZERO, d)
        loss = max(ZERO, -d)
        avg_gain = ((avg_gain * (p_dec - ONE)) + gain) / p_dec
        avg_loss = ((avg_loss * (p_dec - ONE)) + loss) / p_dec

    if avg_loss <= ZERO:
        if avg_gain <= ZERO:
            return Decimal("50.00")
        return Decimal("100.00")

    rs = avg_gain / avg_loss
    rsi = Decimal("100") - (Decimal("100") / (ONE + rs))
    return rsi.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def calculate_macd(
    values: Sequence[Any],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
    closed_only: bool = True,
) -> dict[str, Decimal]:
    nums = _extract_decimal_series(values, field="close", closed_only=closed_only)
    if not nums:
        return {
            "macd": ZERO,
            "macd_signal": ZERO,
            "macd_hist": ZERO,
            "signal": ZERO,
            "histogram": ZERO,
        }

    fast_series = _ema_series(nums, fast)
    slow_series = _ema_series(nums, slow)
    macd_line = [f - s for f, s in zip(fast_series, slow_series)]
    signal_line = _ema_series(macd_line, signal)

    macd_val = macd_line[-1].quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    sig_val = signal_line[-1].quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    hist_val = (macd_val - sig_val).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    return {
        "macd": macd_val,
        "macd_signal": sig_val,
        "macd_hist": hist_val,
        "signal": sig_val,
        "histogram": hist_val,
    }


def calculate_atr(candles: Sequence[Any], period: int = 14, closed_only: bool = True) -> Decimal:
    closed = filter_closed_candles(candles, closed_only=closed_only)
    if not closed:
        return ZERO

    p = max(1, int(period))
    trs: list[Decimal] = []
    prev_close: Decimal | None = None

    for c in closed:
        high = to_decimal(c["high"] if isinstance(c, dict) else c.high, default=ZERO)
        low = to_decimal(c["low"] if isinstance(c, dict) else c.low, default=ZERO)
        close = to_decimal(c["close"] if isinstance(c, dict) else c.close, default=ZERO)

        if prev_close is None:
            tr = max(ZERO, high - low)
        else:
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
        prev_close = close

    if not trs:
        return ZERO

    init_len = min(p, len(trs))
    atr = sum(trs[:init_len], ZERO) / Decimal(init_len)
    p_dec = Decimal(p)
    for tr in trs[init_len:]:
        atr = ((atr * (p_dec - ONE)) + tr) / p_dec

    return atr.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def calculate_volatility(values: Sequence[Any], period: int = 20, closed_only: bool = True) -> Decimal:
    nums = _extract_decimal_series(values, field="close", closed_only=closed_only)
    p = max(2, int(period))
    window = nums[-(p + 1) :]
    if len(window) < 2:
        return ZERO

    returns: list[ float ] = []
    for i in range(1, len(window)):
        prev = window[i - 1]
        curr = window[i]
        if prev > ZERO:
            returns.append(float((curr - prev) / prev))

    if not returns:
        return ZERO

    mean_ret = sum(returns) / len(returns)
    variance = sum((r - mean_ret) ** 2 for r in returns) / len(returns)
    std_dev = math.sqrt(max(0.0, variance))
    return Decimal(str(round(std_dev, 6))).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def calculate_volume_ratio(candles: Sequence[Any], period: int = 20, closed_only: bool = True) -> Decimal:
    vols = _extract_decimal_series(candles, field="volume", closed_only=closed_only)
    if not vols:
        return ZERO
    if len(vols) == 1:
        return ONE

    p = max(1, int(period))
    latest_vol = vols[-1]
    lookback = vols[-(p + 1) : -1] or vols[:-1]
    avg_vol = sum(lookback, ZERO) / Decimal(len(lookback))
    if avg_vol <= ZERO:
        return ONE
    return (latest_vol / avg_vol).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def calculate_orderbook_imbalance(bids: Sequence[Any], asks: Sequence[Any]) -> Decimal:
    def _sum_qty(levels: Sequence[Any]) -> Decimal:
        total = ZERO
        for lvl in levels or []:
            if isinstance(lvl, (list, tuple)) and len(lvl) >= 2:
                total += to_decimal(lvl[1], default=ZERO)
            elif isinstance(lvl, dict):
                total += to_decimal(lvl.get("qty") or lvl.get("size") or lvl.get("amount"), default=ZERO)
        return total

    bid_vol = _sum_qty(bids)
    ask_vol = _sum_qty(asks)
    denom = bid_vol + ask_vol
    if denom <= ZERO:
        return ZERO
    return ((bid_vol - ask_vol) / denom).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def compute_indicator_snapshot(
    candles: Sequence[Any],
    orderbook: dict[str, Any] | None = None,
    closed_only: bool = True,
) -> dict[str, Any]:
    """
    Deterministic indicator snapshot computed strictly on closed candles (is_closed=True by default).
    """
    closed_candles = filter_closed_candles(candles, closed_only=closed_only)
    closes = _extract_decimal_series(closed_candles, field="close", closed_only=False)
    highs = _extract_decimal_series(closed_candles, field="high", closed_only=False)
    lows = _extract_decimal_series(closed_candles, field="low", closed_only=False)

    ema_9 = calculate_ema(closes, 9, closed_only=False)
    ema_21 = calculate_ema(closes, 21, closed_only=False)
    ema_50 = calculate_ema(closes, 50, closed_only=False)
    rsi_14 = calculate_rsi(closes, 14, closed_only=False)
    macd_res = calculate_macd(closes, 12, 26, 9, closed_only=False)
    atr_14 = calculate_atr(closed_candles, 14, closed_only=False)
    volatility_20 = calculate_volatility(closes, 20, closed_only=False)
    volume_ratio_20 = calculate_volume_ratio(closed_candles, 20, closed_only=False)

    if orderbook:
        if "bids" in orderbook or "asks" in orderbook:
            ob_imbalance = calculate_orderbook_imbalance(orderbook.get("bids", []), orderbook.get("asks", []))
        else:
            ob_imbalance = to_decimal(orderbook.get("imbalance"), default=ZERO).quantize(
                Decimal("0.0001"), rounding=ROUND_HALF_UP
            )
    else:
        ob_imbalance = ZERO

    if len(closes) >= 2:
        ref_idx = max(0, len(closes) - 20)
        ref_close = closes[ref_idx]
        price_change_pct = (
            ((closes[-1] - ref_close) / ref_close * Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if ref_close > ZERO
            else ZERO
        )
    else:
        price_change_pct = ZERO

    latest_close = closes[-1] if closes else ZERO
    if closes and ema_9 >= ema_21 and latest_close >= ema_21:
        trend_direction = "BULLISH"
    elif closes and ema_9 < ema_21 and latest_close < ema_21:
        trend_direction = "BEARISH"
    else:
        trend_direction = "NEUTRAL"

    if atr_14 > ZERO and latest_close > ZERO:
        raw_strength = abs(ema_9 - ema_50) / (atr_14 * Decimal("2.5"))
        trend_strength = min(Decimal("0.98"), max(Decimal("0.15"), raw_strength)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    else:
        trend_strength = Decimal("0.50") if trend_direction != "NEUTRAL" else Decimal("0.20")

    prior_highs = highs[-21:-1] if len(highs) > 1 else highs
    prior_lows = lows[-21:-1] if len(lows) > 1 else lows
    high_20 = max(prior_highs) if prior_highs else latest_close
    low_20 = min(prior_lows) if prior_lows else latest_close

    return {
        "ema_9": ema_9,
        "ema_21": ema_21,
        "ema_50": ema_50,
        "rsi_14": rsi_14,
        "macd": macd_res["macd"],
        "macd_signal": macd_res["macd_signal"],
        "macd_hist": macd_res["macd_hist"],
        "atr_14": atr_14,
        "volatility_20": volatility_20,
        "volume_ratio_20": volume_ratio_20,
        "trend_direction": trend_direction,
        "trend_strength": trend_strength,
        "orderbook_imbalance": ob_imbalance,
        "price_change_pct": price_change_pct,
        "latest_close": latest_close,
        "high_20": high_20,
        "low_20": low_20,
        "closed_bars_count": len(closed_candles),
    }
