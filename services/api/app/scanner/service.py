from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.events import event_bus
from app.core.logging import get_logger, log_event
from app.core.precision import ZERO, calc_spread_bps, to_decimal, utc_iso, utc_now
from app.models.entities import Instrument, MarketCandle, MarketSnapshot, SystemEvent

logger = get_logger("terminal.scanner")


def _ema_series(values: list[Decimal], period: int) -> list[Decimal]:
    if not values:
        return []
    k = Decimal("2") / Decimal(str(period + 1))
    one_minus_k = Decimal("1") - k
    ema_val = values[0]
    result = [ema_val]
    for val in values[1:]:
        ema_val = (val * k) + (ema_val * one_minus_k)
        result.append(ema_val)
    return result


def compute_rsi_14(closes: list[Decimal], period: int = 14) -> Decimal:
    if len(closes) < 2:
        return Decimal("55.00")
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    window = deltas[-period:] if len(deltas) >= period else deltas
    gains = [d for d in window if d > ZERO]
    losses = [-d for d in window if d < ZERO]
    avg_gain = sum(gains, ZERO) / Decimal(str(len(window)))
    avg_loss = sum(losses, ZERO) / Decimal(str(len(window)))
    if avg_loss == ZERO:
        return Decimal("70.00") if avg_gain > ZERO else Decimal("50.00")
    rs = avg_gain / avg_loss
    rsi = Decimal("100") - (Decimal("100") / (Decimal("1") + rs))
    return rsi.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def compute_macd_hist(closes: list[Decimal]) -> tuple[Decimal, Decimal, Decimal]:
    if len(closes) < 2:
        return ZERO, ZERO, ZERO
    ema12 = _ema_series(closes, 12)
    ema26 = _ema_series(closes, 26)
    macd_line_series = [a - b for a, b in zip(ema12, ema26)]
    signal_series = _ema_series(macd_line_series, 9)
    macd_val = macd_line_series[-1].quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    signal_val = signal_series[-1].quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    hist_val = (macd_val - signal_val).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    return macd_val, signal_val, hist_val


def compute_atr_14(candles: list[MarketCandle], period: int = 14) -> Decimal:
    if not candles:
        return Decimal("1.0")
    trs: list[Decimal] = []
    for idx, c in enumerate(candles):
        high = to_decimal(c.high, ZERO)
        low = to_decimal(c.low, ZERO)
        hl = max(high - low, ZERO)
        if idx == 0:
            trs.append(hl)
        else:
            prev_close = to_decimal(candles[idx - 1].close, ZERO)
            tr = max(hl, abs(high - prev_close), abs(low - prev_close))
            trs.append(tr)
    window = trs[-period:] if len(trs) >= period else trs
    if not window:
        return Decimal("1.0")
    atr = sum(window, ZERO) / Decimal(str(len(window)))
    if atr <= ZERO:
        last_close = to_decimal(candles[-1].close, Decimal("100"))
        atr = max(last_close * Decimal("0.005"), Decimal("0.01"))
    return atr.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def compute_indicators_from_candles_and_snapshot(
    candles: list[MarketCandle],
    snapshot: MarketSnapshot | None,
) -> dict[str, Any]:
    """
    Compute standardized technical & microstructure indicators from closed candles (`is_closed=True`)
    and latest `MarketSnapshot`.
    """
    closed_candles = [c for c in candles if bool(c.is_closed)]
    closed_candles.sort(key=lambda c: c.open_time)

    snap_ind = dict(snapshot.indicators_json or {}) if snapshot and snapshot.indicators_json else {}

    last_price = (
        to_decimal(snapshot.last, ZERO)
        if snapshot is not None and snapshot.last is not None
        else (to_decimal(closed_candles[-1].close, ZERO) if closed_candles else Decimal("100"))
    )

    if closed_candles:
        closes = [to_decimal(c.close, last_price) for c in closed_candles]
        volumes = [to_decimal(c.volume, ZERO) for c in closed_candles]

        ema_21_val = _ema_series(closes, 21)[-1].quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        ema_50_val = _ema_series(closes, 50)[-1].quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        rsi_14_val = compute_rsi_14(closes, 14)
        macd_line, macd_signal, macd_hist_val = compute_macd_hist(closes)
        atr_14_val = compute_atr_14(closed_candles, 14)

        if len(volumes) >= 2:
            prior_vols = volumes[-21:-1] if len(volumes) > 20 else volumes[:-1]
            avg_vol = sum(prior_vols, ZERO) / Decimal(str(len(prior_vols)))
            volume_ratio_20_val = (
                (volumes[-1] / avg_vol).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                if avg_vol > ZERO
                else Decimal("1.20")
            )
        else:
            volume_ratio_20_val = Decimal("1.20")

        prior_bars = closed_candles[-21:-1] if len(closed_candles) > 20 else closed_candles[:-1]
        if prior_bars:
            n_bar_high_20 = max(to_decimal(c.high, last_price) for c in prior_bars)
            n_bar_low_20 = min(to_decimal(c.low, last_price) for c in prior_bars)
        else:
            n_bar_high_20 = to_decimal(closed_candles[-1].high, last_price)
            n_bar_low_20 = to_decimal(closed_candles[-1].low, last_price)
    else:
        ema_21_val = to_decimal(snap_ind.get("ema_21"), last_price * Decimal("1.002")).quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_UP
        )
        ema_50_val = to_decimal(snap_ind.get("ema_50"), last_price * Decimal("0.998")).quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_UP
        )
        rsi_14_val = to_decimal(snap_ind.get("rsi_14"), Decimal("58.00")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        macd_hist_val = to_decimal(snap_ind.get("macd_hist"), Decimal("0.1500")).quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_UP
        )
        macd_line = to_decimal(snap_ind.get("macd"), macd_hist_val)
        macd_signal = (macd_line - macd_hist_val).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        atr_14_val = to_decimal(snap_ind.get("atr_14"), max(last_price * Decimal("0.008"), Decimal("0.01"))).quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_UP
        )
        volume_ratio_20_val = to_decimal(snap_ind.get("volume_ratio_20"), Decimal("1.35")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        n_bar_high_20 = to_decimal(snap_ind.get("n_bar_high_20"), last_price * Decimal("0.995"))
        n_bar_low_20 = to_decimal(snap_ind.get("n_bar_low_20"), last_price * Decimal("0.975"))

    # Allow explicit snapshot indicator overrides if provided in snapshot.indicators_json
    if "rsi_14" in snap_ind and not closed_candles:
        rsi_14_val = to_decimal(snap_ind["rsi_14"], rsi_14_val)
    if "macd_hist" in snap_ind and not closed_candles:
        macd_hist_val = to_decimal(snap_ind["macd_hist"], macd_hist_val)
    if "ema_21" in snap_ind and not closed_candles:
        ema_21_val = to_decimal(snap_ind["ema_21"], ema_21_val)
    if "ema_50" in snap_ind and not closed_candles:
        ema_50_val = to_decimal(snap_ind["ema_50"], ema_50_val)
    if "atr_14" in snap_ind and not closed_candles:
        atr_14_val = to_decimal(snap_ind["atr_14"], atr_14_val)
    if "volume_ratio_20" in snap_ind and not closed_candles:
        volume_ratio_20_val = to_decimal(snap_ind["volume_ratio_20"], volume_ratio_20_val)

    if snapshot is not None:
        spread_bps_val = to_decimal(snapshot.spread_bps, ZERO)
        if spread_bps_val <= ZERO and snapshot.bid > ZERO and snapshot.ask > ZERO:
            spread_bps_val = calc_spread_bps(snapshot.bid, snapshot.ask)
        ob_summary = snapshot.orderbook_summary or {}
        if "imbalance" in ob_summary:
            orderbook_imbalance_val = to_decimal(ob_summary["imbalance"], ZERO)
        elif "orderbook_imbalance" in snap_ind:
            orderbook_imbalance_val = to_decimal(snap_ind["orderbook_imbalance"], ZERO)
        else:
            bid_vol = to_decimal(ob_summary.get("bid_volume", ob_summary.get("bid_depth", "10")), Decimal("10"))
            ask_vol = to_decimal(ob_summary.get("ask_volume", ob_summary.get("ask_depth", "9")), Decimal("9"))
            total_vol = bid_vol + ask_vol
            orderbook_imbalance_val = (
                ((bid_vol - ask_vol) / total_vol).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
                if total_vol > ZERO
                else Decimal("0.0500")
            )
        funding_rate_val = (
            to_decimal(snapshot.funding_rate, ZERO)
            if snapshot.funding_rate is not None
            else to_decimal(snap_ind.get("funding_rate"), ZERO)
        )
        is_stale_val = bool(snapshot.is_stale)
        quote_volume_24h_val = to_decimal(snapshot.quote_volume_24h, ZERO)
    else:
        spread_bps_val = to_decimal(snap_ind.get("spread_bps"), Decimal("2.50"))
        orderbook_imbalance_val = to_decimal(snap_ind.get("orderbook_imbalance"), Decimal("0.0800"))
        funding_rate_val = to_decimal(snap_ind.get("funding_rate"), Decimal("0.0001"))
        is_stale_val = False
        quote_volume_24h_val = Decimal("50000000")

    volatility_val = (
        (atr_14_val / last_price).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        if last_price > ZERO
        else Decimal("0.0050")
    )

    return {
        "last_price": str(last_price),
        "rsi_14": str(rsi_14_val),
        "macd": str(macd_line),
        "macd_signal": str(macd_signal),
        "macd_hist": str(macd_hist_val),
        "ema_21": str(ema_21_val),
        "ema_50": str(ema_50_val),
        "atr_14": str(atr_14_val),
        "volume_ratio_20": str(volume_ratio_20_val),
        "spread_bps": str(spread_bps_val),
        "orderbook_imbalance": str(orderbook_imbalance_val),
        "funding_rate": str(funding_rate_val),
        "volatility": str(volatility_val),
        "quote_volume_24h": str(quote_volume_24h_val),
        "n_bar_high_20": str(n_bar_high_20),
        "n_bar_low_20": str(n_bar_low_20),
        "is_stale": is_stale_val,
        "closed_candles_used": len(closed_candles),
    }


class ScannerService:
    """
    Market Scanner Service (Section 3.4):
    - Maintains runtime state (RUNNING / PAUSED, last_scan_at, scan_interval_seconds, active filters)
    - Reads Instrument, MarketSnapshot, and closed MarketCandle (is_closed == True)
    - Computes technical & orderbook indicators
    - Evaluates deterministic risk filters (is_stale, spread_bps, instrument status)
    - Ranks opportunities and determines AI queue eligibility
    """

    def __init__(self) -> None:
        self.state: str = "RUNNING"
        self.last_scan_at: str | None = None
        self.scan_interval_seconds: int = 15
        self.exchanges: list[str] = ["binance", "okx"]
        self.market_types: list[str] = ["spot", "usdt_perp"]
        self.min_quote_volume_24h: Decimal = Decimal("100000")
        self.max_spread_bps: Decimal = settings.DEFAULT_MAX_SPREAD_BPS
        self.min_volatility: Decimal = Decimal("0.0005")
        self._latest_opportunities: list[dict[str, Any]] = []

    @property
    def active_filters(self) -> dict[str, Any]:
        return {
            "exchanges": list(self.exchanges),
            "market_types": list(self.market_types),
            "min_quote_volume_24h": str(self.min_quote_volume_24h),
            "max_spread_bps": str(self.max_spread_bps),
            "min_volatility": str(self.min_volatility),
        }

    def update_filters(
        self,
        *,
        exchanges: list[str] | None = None,
        market_types: list[str] | None = None,
        min_quote_volume_24h: Decimal | str | None = None,
        max_spread_bps: Decimal | str | None = None,
        min_volatility: Decimal | str | None = None,
        scan_interval_seconds: int | None = None,
    ) -> dict[str, Any]:
        if exchanges is not None:
            self.exchanges = list(exchanges)
        if market_types is not None:
            self.market_types = list(market_types)
        if min_quote_volume_24h is not None:
            self.min_quote_volume_24h = to_decimal(min_quote_volume_24h, self.min_quote_volume_24h)
        if max_spread_bps is not None:
            self.max_spread_bps = to_decimal(max_spread_bps, self.max_spread_bps)
        if min_volatility is not None:
            self.min_volatility = to_decimal(min_volatility, self.min_volatility)
        if scan_interval_seconds is not None and scan_interval_seconds > 0:
            self.scan_interval_seconds = int(scan_interval_seconds)
        return self.active_filters

    def run_scan(
        self,
        db: Session,
        exchange: str | None = None,
        market_type: str | None = None,
        timeframe: str = "15m",
    ) -> list[dict[str, Any]]:
        """
        Execute a market scan across matching instruments, market snapshots, and closed candles.
        """
        target_exchanges = [exchange] if exchange else self.exchanges
        target_market_types = [market_type] if market_type else self.market_types

        inst_stmt = select(Instrument).where(
            Instrument.exchange.in_(target_exchanges),
            Instrument.market_type.in_(target_market_types),
        )
        instruments = list(db.scalars(inst_stmt).all())

        snap_stmt = (
            select(MarketSnapshot)
            .where(
                MarketSnapshot.exchange.in_(target_exchanges),
                MarketSnapshot.market_type.in_(target_market_types),
            )
            .order_by(MarketSnapshot.event_time.desc(), MarketSnapshot.received_at.desc())
        )
        snapshots = list(db.scalars(snap_stmt).all())

        latest_snapshot_by_key: dict[tuple[str, str, str], MarketSnapshot] = {}
        for snap in snapshots:
            key = (snap.exchange, snap.market_type, snap.symbol)
            if key not in latest_snapshot_by_key:
                latest_snapshot_by_key[key] = snap

        instrument_by_key: dict[tuple[str, str, str], Instrument] = {}
        for inst in instruments:
            key = (inst.exchange, inst.market_type, inst.canonical_symbol)
            instrument_by_key[key] = inst

        all_keys: list[tuple[str, str, str]] = list(instrument_by_key.keys())
        for key in latest_snapshot_by_key:
            if key not in instrument_by_key:
                all_keys.append(key)

        scanned_at = utc_iso()
        opportunities: list[dict[str, Any]] = []

        for ex, m_type, symbol in all_keys:
            inst = instrument_by_key.get((ex, m_type, symbol))
            snap = latest_snapshot_by_key.get((ex, m_type, symbol))

            candle_stmt = (
                select(MarketCandle)
                .where(
                    MarketCandle.exchange == ex,
                    MarketCandle.market_type == m_type,
                    MarketCandle.symbol == symbol,
                    MarketCandle.is_closed.is_(True),
                )
                .order_by(MarketCandle.open_time.asc())
            )
            all_closed_candles = list(db.scalars(candle_stmt).all())
            tf_candles = [c for c in all_closed_candles if c.timeframe == timeframe]
            closed_candles = tf_candles if tf_candles else all_closed_candles
            effective_tf = closed_candles[-1].timeframe if closed_candles else timeframe

            indicators = compute_indicators_from_candles_and_snapshot(closed_candles, snap)

            if snap is not None:
                merged_ind = dict(snap.indicators_json or {})
                merged_ind.update(indicators)
                snap.indicators_json = merged_ind

            ema_21 = to_decimal(indicators["ema_21"], ZERO)
            ema_50 = to_decimal(indicators["ema_50"], ZERO)
            rsi_14 = to_decimal(indicators["rsi_14"], Decimal("50"))
            macd_hist = to_decimal(indicators["macd_hist"], ZERO)
            atr_14 = to_decimal(indicators["atr_14"], ZERO)
            volume_ratio_20 = to_decimal(indicators["volume_ratio_20"], Decimal("1.0"))
            spread_bps = to_decimal(indicators["spread_bps"], ZERO)
            ob_imbalance = to_decimal(indicators["orderbook_imbalance"], ZERO)
            funding_rate = to_decimal(indicators["funding_rate"], ZERO)
            volatility = to_decimal(indicators["volatility"], ZERO)
            quote_vol_24h = to_decimal(indicators["quote_volume_24h"], ZERO)
            is_stale = bool(indicators["is_stale"])

            # Determine direction_bias (Spot V1 only opens LONG; Perp supports LONG and SHORT)
            bullish_points = 0
            bearish_points = 0
            if ema_21 >= ema_50:
                bullish_points += 2
            else:
                bearish_points += 2
            if macd_hist >= ZERO:
                bullish_points += 1
            else:
                bearish_points += 1
            if ob_imbalance >= ZERO:
                bullish_points += 1
            else:
                bearish_points += 1

            if m_type == "spot":
                direction_bias = "LONG"
            else:
                direction_bias = "LONG" if bullish_points >= bearish_points else "SHORT"

            # Evaluate risk & liquidity filters
            rejection_reasons: list[str] = []
            inst_status = inst.status if inst is not None else "TRADING"
            if inst_status != "TRADING":
                rejection_reasons.append(f"Instrument status is {inst_status} (requires TRADING)")
            if is_stale:
                rejection_reasons.append("Market data snapshot is marked stale (is_stale=True)")
            if spread_bps > self.max_spread_bps:
                rejection_reasons.append(
                    f"Spread {spread_bps} bps exceeds max_spread_bps {self.max_spread_bps} bps"
                )
            if quote_vol_24h > ZERO and quote_vol_24h < self.min_quote_volume_24h:
                rejection_reasons.append(
                    f"24h quote volume {quote_vol_24h} below minimum {self.min_quote_volume_24h}"
                )

            risk_filter_passed = len(rejection_reasons) == 0

            # Compute composite opportunity score (Decimal 0 - 100)
            score = Decimal("55.00")
            trigger_reasons: list[str] = []

            if direction_bias == "LONG":
                if ema_21 >= ema_50:
                    score += Decimal("14.00")
                    trigger_reasons.append(f"EMA(21) {ema_21} >= EMA(50) {ema_50} bullish trend structure")
                if macd_hist > ZERO:
                    score += Decimal("10.00")
                    trigger_reasons.append(f"MACD histogram positive ({macd_hist})")
                if Decimal("45") <= rsi_14 <= Decimal("75"):
                    score += Decimal("8.00")
                    trigger_reasons.append(f"RSI(14) in bullish momentum zone ({rsi_14})")
                if ob_imbalance > ZERO:
                    score += Decimal("5.00")
                    trigger_reasons.append(f"Orderbook bid imbalance positive ({ob_imbalance})")
            else:
                if ema_21 < ema_50:
                    score += Decimal("14.00")
                    trigger_reasons.append(f"EMA(21) {ema_21} < EMA(50) {ema_50} bearish trend structure")
                if macd_hist < ZERO:
                    score += Decimal("10.00")
                    trigger_reasons.append(f"MACD histogram negative ({macd_hist})")
                if Decimal("25") <= rsi_14 <= Decimal("55"):
                    score += Decimal("8.00")
                    trigger_reasons.append(f"RSI(14) in bearish momentum zone ({rsi_14})")
                if ob_imbalance < ZERO:
                    score += Decimal("5.00")
                    trigger_reasons.append(f"Orderbook ask pressure dominant ({ob_imbalance})")

            if volume_ratio_20 >= Decimal("1.10"):
                score += Decimal("8.00")
                trigger_reasons.append(f"Volume expansion {volume_ratio_20}x vs 20-bar average")
            elif volume_ratio_20 >= Decimal("0.90"):
                score += Decimal("4.00")
                trigger_reasons.append(f"Steady volume participation ({volume_ratio_20}x)")

            if spread_bps <= Decimal("10.0"):
                score += Decimal("4.00")
                trigger_reasons.append(f"Tight bid-ask spread ({spread_bps} bps)")

            if volatility >= self.min_volatility:
                score += Decimal("3.00")

            if not trigger_reasons:
                trigger_reasons.append(f"Baseline market scan evaluated for {symbol} ({direction_bias})")

            if not risk_filter_passed:
                score = min(score, Decimal("35.00"))
                for r in rejection_reasons:
                    trigger_reasons.append(f"[FILTER_REJECTED] {r}")

            score = max(ZERO, min(Decimal("100.00"), score)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            enter_ai_queue = bool(risk_filter_passed and score >= Decimal("60.00"))

            opportunities.append(
                {
                    "symbol": symbol,
                    "exchange": ex,
                    "market_type": m_type,
                    "timeframe": effective_tf,
                    "score": score,
                    "direction_bias": direction_bias,
                    "trigger_reasons": trigger_reasons,
                    "rejection_reasons": rejection_reasons,
                    "indicators_snapshot": {
                        "rsi_14": indicators["rsi_14"],
                        "macd_hist": indicators["macd_hist"],
                        "ema_21": indicators["ema_21"],
                        "ema_50": indicators["ema_50"],
                        "atr_14": indicators["atr_14"],
                        "volume_ratio_20": indicators["volume_ratio_20"],
                        "spread_bps": indicators["spread_bps"],
                        "orderbook_imbalance": indicators["orderbook_imbalance"],
                        "funding_rate": indicators["funding_rate"],
                        "volatility": indicators["volatility"],
                        "last_price": indicators["last_price"],
                        "n_bar_high_20": indicators["n_bar_high_20"],
                        "n_bar_low_20": indicators["n_bar_low_20"],
                        "is_stale": is_stale,
                    },
                    "risk_filter_passed": risk_filter_passed,
                    "enter_ai_queue": enter_ai_queue,
                    "scanned_at": scanned_at,
                }
            )

        opportunities.sort(
            key=lambda item: (bool(item["risk_filter_passed"]), to_decimal(item["score"], ZERO)),
            reverse=True,
        )

        self.last_scan_at = scanned_at
        self._latest_opportunities = opportunities
        db.commit()

        serialized = self._serialize_opportunities(opportunities)
        event_bus.cache_set("scanner:opportunities", serialized)
        event_bus.publish_sync(
            channel="scanner",
            event_type="scanner.completed",
            data={
                "scanned_at": scanned_at,
                "total_scanned": len(opportunities),
                "passed_count": sum(1 for o in opportunities if o["risk_filter_passed"]),
                "ai_queue_count": sum(1 for o in opportunities if o["enter_ai_queue"]),
                "top_opportunities": serialized[:5],
            },
            source="scanner",
        )
        log_event(
            logger,
            20,
            "Market scan completed",
            total_scanned=len(opportunities),
            ai_queue_count=sum(1 for o in opportunities if o["enter_ai_queue"]),
        )
        return opportunities

    def _serialize_opportunities(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for idx, item in enumerate(items, start=1):
            copied = dict(item)
            copied["score"] = item["score"]
            copied["rank"] = idx
            result.append(copied)
        return result

    def get_status(self, db: Session | None = None) -> dict[str, Any]:
        if not self._latest_opportunities and db is not None and self.state == "RUNNING":
            self.run_scan(db)
        passed = [o for o in self._latest_opportunities if o.get("risk_filter_passed")]
        ai_queue = [o for o in self._latest_opportunities if o.get("enter_ai_queue")]
        return {
            "status": self.state,
            "state": self.state,
            "is_running": self.state == "RUNNING",
            "last_scan_at": self.last_scan_at,
            "scan_interval_seconds": self.scan_interval_seconds,
            "active_filters": self.active_filters,
            "filters": self.active_filters,
            "total_scanned": len(self._latest_opportunities),
            "opportunities_count": len(passed),
            "ai_queue_count": len(ai_queue),
        }

    def get_opportunities(
        self,
        db: Session | None = None,
        exchange: str | None = None,
        market_type: str | None = None,
        only_passed: bool = False,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        if (not self._latest_opportunities or exchange or market_type) and db is not None:
            self.run_scan(db, exchange=exchange, market_type=market_type)
        items = list(self._latest_opportunities)
        if exchange:
            items = [i for i in items if i["exchange"] == exchange]
        if market_type:
            items = [i for i in items if i["market_type"] == market_type]
        if only_passed:
            items = [i for i in items if i["risk_filter_passed"]]
        return items[:limit]

    def get_rankings(
        self,
        db: Session | None = None,
        exchange: str | None = None,
        market_type: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        if not self._latest_opportunities and db is not None:
            self.run_scan(db, exchange=exchange, market_type=market_type)
        items = list(self._latest_opportunities)
        if exchange:
            items = [i for i in items if i["exchange"] == exchange]
        if market_type:
            items = [i for i in items if i["market_type"] == market_type]
        items.sort(
            key=lambda item: (bool(item["risk_filter_passed"]), to_decimal(item["score"], ZERO)),
            reverse=True,
        )
        ranked: list[dict[str, Any]] = []
        for idx, item in enumerate(items[:limit], start=1):
            entry = dict(item)
            entry["rank"] = idx
            ranked.append(entry)
        return ranked

    def start_scanner(
        self,
        db: Session | None = None,
        exchange: str | None = None,
        market_type: str | None = None,
        **filter_overrides: Any,
    ) -> dict[str, Any]:
        self.state = "RUNNING"
        if filter_overrides:
            self.update_filters(
                exchanges=filter_overrides.get("exchanges"),
                market_types=filter_overrides.get("market_types"),
                min_quote_volume_24h=filter_overrides.get("min_quote_volume_24h"),
                max_spread_bps=filter_overrides.get("max_spread_bps"),
                min_volatility=filter_overrides.get("min_volatility"),
                scan_interval_seconds=filter_overrides.get("scan_interval_seconds"),
            )
        opportunities: list[dict[str, Any]] = []
        if db is not None:
            opportunities = self.run_scan(db, exchange=exchange, market_type=market_type)
            db.add(
                SystemEvent(
                    component="scanner",
                    event_type="SCANNER_STARTED",
                    severity="INFO",
                    payload={
                        "state": self.state,
                        "scanned_count": len(opportunities),
                        "active_filters": self.active_filters,
                    },
                )
            )
            db.commit()
        status = self.get_status(db)
        status["opportunities"] = opportunities
        return status

    def pause_scanner(self, db: Session | None = None) -> dict[str, Any]:
        self.state = "PAUSED"
        if db is not None:
            db.add(
                SystemEvent(
                    component="scanner",
                    event_type="SCANNER_PAUSED",
                    severity="INFO",
                    payload={"state": self.state, "last_scan_at": self.last_scan_at},
                )
            )
            db.commit()
        event_bus.publish_sync(
            channel="scanner",
            event_type="scanner.paused",
            data={"state": self.state, "last_scan_at": self.last_scan_at},
            source="scanner",
        )
        return self.get_status(db)


scanner_service = ScannerService()
