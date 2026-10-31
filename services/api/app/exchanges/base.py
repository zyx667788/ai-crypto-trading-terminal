from __future__ import annotations

import hashlib
import math
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Any, Protocol, runtime_checkable

from app.core.precision import (
    BPS_DIVISOR,
    ONE,
    ZERO,
    calc_spread_bps,
    round_price_to_tick,
    round_qty_to_lot,
    to_decimal,
    utc_now,
)
from app.core.security import mask_secret, redact_sensitive_data


KNOWN_QUOTES = ("USDT", "USDC", "FDUSD", "BUSD", "USD", "BTC", "ETH")

TIMEFRAME_SECONDS: dict[str, int] = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}


def infer_market_type(symbol: str, default: str = "usdt_perp") -> str:
    cleaned = (symbol or "").strip().upper()
    if ":USDT" in cleaned or cleaned.endswith("-SWAP") or cleaned.endswith("_PERP"):
        return "usdt_perp"
    if "/" in cleaned and ":" not in cleaned:
        return "spot"
    if cleaned.count("-") == 1:
        return "spot"
    return default


def _split_base_quote(raw_symbol: str) -> tuple[str, str]:
    s = (raw_symbol or "").strip().upper()
    if not s:
        raise ValueError("Symbol cannot be empty")

    # Strip settle suffix like :USDT
    if ":" in s:
        s = s.split(":", 1)[0]

    # Strip OKX swap suffix
    if s.endswith("-SWAP"):
        s = s[:-5]
    elif s.endswith("-PERP"):
        s = s[:-5]

    if "/" in s:
        base, quote = s.split("/", 1)
        return base.strip(), quote.strip()

    if "-" in s:
        parts = [p for p in s.split("-") if p]
        if len(parts) >= 2:
            return parts[0].strip(), parts[1].strip()

    if "_" in s:
        parts = [p for p in s.split("_") if p]
        if len(parts) >= 2:
            return parts[0].strip(), parts[1].strip()

    for quote in KNOWN_QUOTES:
        if s.endswith(quote) and len(s) > len(quote):
            return s[: -len(quote)], quote

    return s, "USDT"


def to_canonical_symbol(exchange: str, market_type: str, native_symbol: str) -> str:
    """
    Convert exchange-native or user-provided symbol into canonical format:
    - spot: "BTC/USDT"
    - usdt_perp: "BTC/USDT:USDT"
    """
    _ = exchange  # Normalized uniformly across exchanges
    mtype = (market_type or infer_market_type(native_symbol, "spot")).strip().lower()
    base, quote = _split_base_quote(native_symbol)
    if mtype == "usdt_perp":
        return f"{base}/{quote}:{quote}"
    return f"{base}/{quote}"


def to_native_symbol(exchange: str, market_type: str, canonical_symbol: str) -> str:
    """
    Convert canonical symbol into exchange-native format:
    - Binance spot/usdt_perp: "BTCUSDT"
    - OKX spot: "BTC-USDT"
    - OKX usdt_perp: "BTC-USDT-SWAP"
    """
    ex = (exchange or "binance").strip().lower()
    mtype = (market_type or infer_market_type(canonical_symbol, "spot")).strip().lower()
    base, quote = _split_base_quote(canonical_symbol)

    if ex == "okx":
        if mtype == "usdt_perp":
            return f"{base}-{quote}-SWAP"
        return f"{base}-{quote}"

    # Default / Binance native symbol
    return f"{base}{quote}"


@runtime_checkable
class ExchangeAdapter(Protocol):
    """
    Unified Exchange Adapter Protocol (Section 7.1).
    Implemented by BinanceAdapter and OKXAdapter.
    """

    exchange: str
    environment: str
    region: str

    def connect(self) -> dict[str, Any]: ...

    def get_capabilities(self) -> dict[str, Any]: ...

    def get_instruments(self, market_type: str | None = None) -> list[dict[str, Any]]: ...

    def get_balance(self) -> list[dict[str, Any]]: ...

    def get_ticker(self, symbol: str, market_type: str | None = None) -> dict[str, Any]: ...

    def get_klines(
        self,
        symbol: str,
        timeframe: str = "15m",
        limit: int = 100,
        market_type: str | None = None,
    ) -> list[dict[str, Any]]: ...

    def get_orderbook(
        self,
        symbol: str,
        market_type: str | None = None,
        depth: int = 10,
    ) -> dict[str, Any]: ...

    def get_funding_rate(
        self,
        symbol: str,
        market_type: str = "usdt_perp",
    ) -> dict[str, Any]: ...

    def get_positions(
        self,
        symbol: str | None = None,
        market_type: str | None = None,
    ) -> list[dict[str, Any]]: ...

    def place_order(
        self,
        order_request: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]: ...

    def get_order(
        self,
        symbol: str | None = None,
        order_id: str | None = None,
        client_order_id: str | None = None,
        market_type: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any] | None: ...

    def cancel_order(
        self,
        symbol: str | None = None,
        order_id: str | None = None,
        client_order_id: str | None = None,
        market_type: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]: ...

    def get_fills(
        self,
        symbol: str | None = None,
        since: datetime | None = None,
        order_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]: ...

    def subscribe_market(
        self,
        symbols: list[str],
        market_type: str = "usdt_perp",
        channels: list[str] | None = None,
    ) -> dict[str, Any]: ...

    def subscribe_private(self) -> dict[str, Any]: ...

    def reconcile(
        self,
        local_orders: list[dict[str, Any]] | None = None,
        local_positions: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]: ...


# Shared in-process state store per (exchange, environment) for deterministic stateful Demo/Testnet operation
_SIMULATED_EXCHANGE_STATE: dict[tuple[str, str], dict[str, Any]] = {}


class BaseSimulatedExchangeAdapter:
    """
    Deterministic, stateful Demo/Testnet/Live adapter base for Binance and OKX.
    Never logs or returns raw API secrets or passphrases.
    """

    exchange: str = "binance"
    default_fee_rate_spot: Decimal = Decimal("0.0010")
    default_fee_rate_perp: Decimal = Decimal("0.0005")
    base_prices: dict[str, Decimal] = {
        "BTC": Decimal("68450.00"),
        "ETH": Decimal("3540.00"),
        "SOL": Decimal("164.50"),
    }

    def __init__(
        self,
        environment: str = "demo",
        api_key: str | None = None,
        api_secret: str | None = None,
        passphrase: str | None = None,
        region: str = "GLOBAL",
        enable_withdraw_permission_simulation: bool = False,
        ip_whitelist_configured: bool = True,
        account_id: str | None = None,
    ) -> None:
        self.environment = (environment or "demo").strip().lower()
        self.region = (region or "GLOBAL").strip().upper()
        self._api_key = api_key or f"{self.exchange}_{self.environment}_demo_key"
        self._api_secret = api_secret or f"{self.exchange}_{self.environment}_demo_secret"
        self._passphrase = passphrase
        self.api_key_masked = mask_secret(self._api_key)
        self.ip_whitelist_configured = bool(ip_whitelist_configured)
        self.account_id = account_id

        key_upper = self._api_key.upper()
        self._has_withdraw_permission = bool(
            enable_withdraw_permission_simulation or ("WITHDRAW" in key_upper)
        )
        self._connected = False
        self._state = self._get_or_init_state()

    def __repr__(self) -> str:
        return (
            f"<{self.__class__.__name__} exchange={self.exchange!r} "
            f"environment={self.environment!r} api_key={self.api_key_masked!r}>"
        )

    def _build_default_instruments(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    def _build_default_balances(self) -> dict[str, dict[str, Decimal]]:
        return {
            "USDT": {
                "total": Decimal("10000.00000000"),
                "available": Decimal("10000.00000000"),
                "locked": Decimal("0.00000000"),
            },
            "BTC": {
                "total": Decimal("0.15000000"),
                "available": Decimal("0.15000000"),
                "locked": Decimal("0.00000000"),
            },
            "ETH": {
                "total": Decimal("2.00000000"),
                "available": Decimal("2.00000000"),
                "locked": Decimal("0.00000000"),
            },
        }

    def _get_or_init_state(self) -> dict[str, Any]:
        state_key = (self.exchange, self.environment)
        if state_key not in _SIMULATED_EXCHANGE_STATE:
            instruments = self._build_default_instruments()
            inst_map: dict[tuple[str, str], dict[str, Any]] = {}
            for inst in instruments:
                inst_map[(inst["market_type"], inst["canonical_symbol"])] = inst
                inst_map[(inst["market_type"], inst["native_symbol"])] = inst

            _SIMULATED_EXCHANGE_STATE[state_key] = {
                "instruments": instruments,
                "instrument_map": inst_map,
                "balances": self._build_default_balances(),
                "positions": {},  # key: (market_type, canonical_symbol, position_side) -> dict
                "orders_by_id": {},  # exchange_order_id -> dict
                "orders_by_client_id": {},  # client_order_id -> dict
                "fills": [],  # list of fill dicts
                "subscriptions_market": set(),
                "subscriptions_private": False,
                "order_seq": 1000,
                "trade_seq": 5000,
            }
        return _SIMULATED_EXCHANGE_STATE[state_key]

    def reset_state(self) -> None:
        state_key = (self.exchange, self.environment)
        _SIMULATED_EXCHANGE_STATE.pop(state_key, None)
        self._state = self._get_or_init_state()

    def connect(self) -> dict[str, Any]:
        self._connected = True
        caps = self.get_capabilities()
        status = "REJECTED_WITHDRAW_PERMISSION" if self._has_withdraw_permission else "CONNECTED"
        return {
            "exchange": self.exchange,
            "environment": self.environment,
            "status": status,
            "connected": not self._has_withdraw_permission,
            "api_key_masked": self.api_key_masked,
            "capabilities": caps,
            "connected_at": utc_now(),
        }

    def get_capabilities(self) -> dict[str, Any]:
        restricted_regions = {"US", "RESTRICTED"}
        perp_supported = self.region not in restricted_regions
        supported_markets = ["spot", "usdt_perp"] if perp_supported else ["spot"]
        return {
            "exchange": self.exchange,
            "environment": self.environment,
            "region": self.region,
            "supported_market_types": supported_markets,
            "market_types": supported_markets,
            "spot_enabled": True,
            "usdt_perp_enabled": perp_supported,
            "can_read": True,
            "can_trade": not self._has_withdraw_permission,
            "can_withdraw": self._has_withdraw_permission,
            "has_withdraw_permission": self._has_withdraw_permission,
            "ip_whitelist_active": self.ip_whitelist_configured,
            "ip_whitelist_configured": self.ip_whitelist_configured,
            "api_key_masked": self.api_key_masked,
            "native_tp_sl_supported": True,
            "simulated": self.environment in ("demo", "testnet"),
        }

    def get_instruments(self, market_type: str | None = None) -> list[dict[str, Any]]:
        instruments: list[dict[str, Any]] = self._state["instruments"]
        if market_type:
            mtype = market_type.strip().lower()
            return [dict(item) for item in instruments if item["market_type"] == mtype]
        return [dict(item) for item in instruments]

    def _resolve_instrument(self, symbol: str, market_type: str | None = None) -> dict[str, Any]:
        mtype = (market_type or infer_market_type(symbol, "usdt_perp")).strip().lower()
        canonical = to_canonical_symbol(self.exchange, mtype, symbol)
        inst_map: dict[tuple[str, str], dict[str, Any]] = self._state["instrument_map"]
        if (mtype, canonical) in inst_map:
            return inst_map[(mtype, canonical)]

        base, quote = _split_base_quote(canonical)
        native = to_native_symbol(self.exchange, mtype, canonical)
        fallback = {
            "exchange": self.exchange,
            "market_type": mtype,
            "native_symbol": native,
            "canonical_symbol": canonical,
            "base_asset": base,
            "quote_asset": quote,
            "settle_asset": quote,
            "tick_size": Decimal("0.01"),
            "lot_size": Decimal("0.001"),
            "min_qty": Decimal("0.001"),
            "min_notional": Decimal("5.0"),
            "contract_size": Decimal("1.0"),
            "max_leverage": Decimal("20.0") if mtype == "usdt_perp" else Decimal("1.0"),
            "status": "TRADING",
        }
        inst_map[(mtype, canonical)] = fallback
        inst_map[(mtype, native)] = fallback
        self._state["instruments"].append(fallback)
        return fallback

    def _reference_price(self, base_asset: str) -> Decimal:
        base_price = self.base_prices.get(base_asset.upper(), Decimal("100.00"))
        # Slight deterministic offset per exchange so Binance and OKX prices are realistic & distinct
        if self.exchange == "okx":
            return (base_price * Decimal("1.0002")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return base_price

    def get_balance(self) -> list[dict[str, Any]]:
        now = utc_now()
        result: list[dict[str, Any]] = []
        balances: dict[str, dict[str, Decimal]] = self._state["balances"]
        for asset, bal in balances.items():
            total = to_decimal(bal["total"])
            available = to_decimal(bal["available"])
            locked = to_decimal(bal["locked"])
            if asset == "USDT":
                usd_price = ONE
            else:
                usd_price = self._reference_price(asset)
            usd_value = (total * usd_price).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            result.append(
                {
                    "exchange": self.exchange,
                    "environment": self.environment,
                    "asset": asset,
                    "total": total,
                    "available": available,
                    "locked": locked,
                    "usd_value": usd_value,
                    "observed_at": now,
                }
            )
        return result

    def get_balances(self) -> list[dict[str, Any]]:
        return self.get_balance()

    def get_klines(
        self,
        symbol: str,
        timeframe: str = "15m",
        limit: int = 100,
        market_type: str | None = None,
    ) -> list[dict[str, Any]]:
        inst = self._resolve_instrument(symbol, market_type)
        mtype = inst["market_type"]
        canonical = inst["canonical_symbol"]
        base_asset = inst["base_asset"]
        tick_size = to_decimal(inst["tick_size"])

        tf = timeframe if timeframe in TIMEFRAME_SECONDS else "15m"
        step_seconds = TIMEFRAME_SECONDS[tf]
        bars = max(1, min(int(limit), 1000))

        now = utc_now()
        current_bucket_ts = (int(now.timestamp()) // step_seconds) * step_seconds
        latest_closed_close_ts = current_bucket_ts
        ref_price = self._reference_price(base_asset)

        # Deterministic seed per (exchange, symbol, timeframe)
        seed_bytes = hashlib.sha256(f"{self.exchange}:{canonical}:{tf}".encode("utf-8")).digest()
        phase_offset = (seed_bytes[0] % 12) * 0.25

        candles: list[dict[str, Any]] = []
        # Construct a realistic series ending near ref_price with a constructive uptrend in recent bars
        for i in range(bars):
            idx_from_end = bars - 1 - i
            open_ts = latest_closed_close_ts - (idx_from_end + 1) * step_seconds
            close_ts = open_ts + step_seconds

            open_dt = datetime.fromtimestamp(open_ts, tz=timezone.utc)
            close_dt = datetime.fromtimestamp(close_ts, tz=timezone.utc)

            # Create a realistic wave + recent bullish trend over the last 30 bars
            if idx_from_end > 30:
                wave = math.sin((i + phase_offset) * 0.35) * 0.008 + math.cos(i * 0.17) * 0.004
                drift = -0.018 + (wave)
            else:
                # Steady climb from -1.8% up to 0.0% at the latest closed bar with minor pullbacks
                progress = (30 - idx_from_end) / 30.0
                pullback = math.sin((i + phase_offset) * 0.9) * 0.0018
                drift = -0.018 * (1.0 - progress) + pullback
                if idx_from_end == 0:
                    # Final closed bar closes strong near high for breakout/trend confirmation
                    drift = 0.0025

            prev_drift = drift - 0.0012 if idx_from_end <= 30 else drift - math.sin(i * 0.35) * 0.0015

            raw_open = ref_price * Decimal(str(round(1.0 + prev_drift, 6)))
            raw_close = ref_price * Decimal(str(round(1.0 + drift, 6)))
            bar_range = ref_price * Decimal("0.0032")

            high_val = max(raw_open, raw_close) + bar_range * Decimal("0.65")
            low_val = min(raw_open, raw_close) - bar_range * Decimal("0.55")

            o_price = round_price_to_tick(raw_open, tick_size)
            c_price = round_price_to_tick(raw_close, tick_size)
            h_price = round_price_to_tick(high_val, tick_size)
            l_price = round_price_to_tick(low_val, tick_size)
            if h_price < max(o_price, c_price):
                h_price = max(o_price, c_price) + tick_size
            if l_price > min(o_price, c_price):
                l_price = max(tick_size, min(o_price, c_price) - tick_size)

            base_vol = Decimal("185.0") if base_asset == "BTC" else (Decimal("1450.0") if base_asset == "ETH" else Decimal("12500.0"))
            vol_mult = Decimal("1.0") + Decimal(str(round(abs(math.sin(i * 0.45)) * 0.35, 4)))
            if idx_from_end <= 3:
                vol_mult += Decimal("0.55")
            volume = (base_vol * vol_mult).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            quote_volume = (volume * c_price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

            candles.append(
                {
                    "exchange": self.exchange,
                    "market_type": mtype,
                    "symbol": canonical,
                    "native_symbol": inst["native_symbol"],
                    "timeframe": tf,
                    "open_time": open_dt,
                    "close_time": close_dt,
                    "open": o_price,
                    "high": h_price,
                    "low": l_price,
                    "close": c_price,
                    "volume": volume,
                    "quote_volume": quote_volume,
                    "is_closed": True,
                    "source_exchange": self.exchange,
                    "received_at": now,
                }
            )

        return candles

    def get_ticker(self, symbol: str, market_type: str | None = None) -> dict[str, Any]:
        inst = self._resolve_instrument(symbol, market_type)
        mtype = inst["market_type"]
        canonical = inst["canonical_symbol"]
        tick_size = to_decimal(inst["tick_size"])

        klines = self.get_klines(canonical, timeframe="15m", limit=25, market_type=mtype)
        last_price = klines[-1]["close"] if klines else self._reference_price(inst["base_asset"])
        first_price = klines[0]["open"] if klines else last_price

        # Tight spread (~1.5 to 3 bps)
        half_spread = max(tick_size, round_price_to_tick(last_price * Decimal("0.00008"), tick_size))
        bid = round_price_to_tick(last_price - half_spread, tick_size)
        ask = round_price_to_tick(last_price + half_spread, tick_size)
        if ask <= bid:
            ask = bid + tick_size

        spread_bps = calc_spread_bps(bid, ask)
        vol_24h = sum((to_decimal(k["volume"]) for k in klines), ZERO).quantize(Decimal("0.01"))
        quote_vol_24h = (vol_24h * last_price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        change_24h_pct = (
            ((last_price - first_price) / first_price * Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if first_price > ZERO
            else ZERO
        )

        now = utc_now()
        mark_price = (
            round_price_to_tick(last_price * Decimal("1.00005"), tick_size)
            if mtype == "usdt_perp"
            else None
        )
        index_price = (
            round_price_to_tick(last_price * Decimal("0.99995"), tick_size)
            if mtype == "usdt_perp"
            else None
        )
        funding_info = self.get_funding_rate(canonical, market_type=mtype) if mtype == "usdt_perp" else {}

        return {
            "exchange": self.exchange,
            "market_type": mtype,
            "symbol": canonical,
            "native_symbol": inst["native_symbol"],
            "last": last_price,
            "bid": bid,
            "ask": ask,
            "spread_bps": spread_bps,
            "volume_24h": vol_24h,
            "quote_volume_24h": quote_vol_24h,
            "change_24h_pct": change_24h_pct,
            "mark_price": mark_price,
            "index_price": index_price,
            "funding_rate": funding_info.get("funding_rate"),
            "next_funding_time": funding_info.get("next_funding_time"),
            "event_time": now,
            "received_at": now,
            "is_stale": False,
        }

    def get_orderbook(
        self,
        symbol: str,
        market_type: str | None = None,
        depth: int = 10,
    ) -> dict[str, Any]:
        inst = self._resolve_instrument(symbol, market_type)
        mtype = inst["market_type"]
        canonical = inst["canonical_symbol"]
        tick_size = to_decimal(inst["tick_size"])
        lot_size = to_decimal(inst["lot_size"])
        ticker = self.get_ticker(canonical, market_type=mtype)

        bid_top = to_decimal(ticker["bid"])
        ask_top = to_decimal(ticker["ask"])
        levels = max(1, min(int(depth), 50))

        base_qty = Decimal("2.4") if inst["base_asset"] == "BTC" else (Decimal("18.0") if inst["base_asset"] == "ETH" else Decimal("150.0"))
        if mtype == "usdt_perp" and to_decimal(inst["contract_size"]) < ONE:
            base_qty = base_qty / to_decimal(inst["contract_size"])

        bids: list[list[Decimal]] = []
        asks: list[list[Decimal]] = []
        for lvl in range(levels):
            b_price = round_price_to_tick(bid_top - tick_size * Decimal(lvl), tick_size)
            a_price = round_price_to_tick(ask_top + tick_size * Decimal(lvl), tick_size)
            # Slightly heavier bid depth to reflect constructive bullish imbalance (~ +0.16)
            b_qty = max(lot_size, round_qty_to_lot(base_qty * Decimal(str(1.18 + lvl * 0.12)), lot_size))
            a_qty = max(lot_size, round_qty_to_lot(base_qty * Decimal(str(0.85 + lvl * 0.10)), lot_size))
            bids.append([b_price, b_qty])
            asks.append([a_price, a_qty])

        bid_vol = sum((b[1] for b in bids), ZERO)
        ask_vol = sum((a[1] for a in asks), ZERO)
        total_vol = bid_vol + ask_vol
        imbalance = (
            ((bid_vol - ask_vol) / total_vol).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            if total_vol > ZERO
            else ZERO
        )

        now = utc_now()
        return {
            "exchange": self.exchange,
            "market_type": mtype,
            "symbol": canonical,
            "native_symbol": inst["native_symbol"],
            "bids": bids,
            "asks": asks,
            "bid_volume_top": bid_vol,
            "ask_volume_top": ask_vol,
            "imbalance": imbalance,
            "spread_bps": ticker["spread_bps"],
            "event_time": now,
            "received_at": now,
        }

    def get_funding_rate(
        self,
        symbol: str,
        market_type: str = "usdt_perp",
    ) -> dict[str, Any]:
        inst = self._resolve_instrument(symbol, market_type)
        mtype = inst["market_type"]
        canonical = inst["canonical_symbol"]
        now = utc_now()
        next_hour = ((now.hour // 8) + 1) * 8
        next_funding = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=next_hour - now.hour)

        if mtype != "usdt_perp":
            return {
                "exchange": self.exchange,
                "market_type": mtype,
                "symbol": canonical,
                "native_symbol": inst["native_symbol"],
                "funding_rate": None,
                "next_funding_time": None,
                "observed_at": now,
            }

        rate = Decimal("0.00010000") if self.exchange == "binance" else Decimal("0.00012000")
        return {
            "exchange": self.exchange,
            "market_type": mtype,
            "symbol": canonical,
            "native_symbol": inst["native_symbol"],
            "funding_rate": rate,
            "next_funding_time": next_funding,
            "observed_at": now,
        }

    def get_positions(
        self,
        symbol: str | None = None,
        market_type: str | None = None,
    ) -> list[dict[str, Any]]:
        positions_map: dict[tuple[str, str, str], dict[str, Any]] = self._state["positions"]
        results: list[dict[str, Any]] = []
        target_canonical = (
            to_canonical_symbol(self.exchange, market_type or infer_market_type(symbol), symbol)
            if symbol
            else None
        )
        for (_, can_sym, _), pos in list(positions_map.items()):
            if pos["qty"] <= ZERO or pos["status"] != "OPEN":
                continue
            if market_type and pos["market_type"] != market_type:
                continue
            if target_canonical and can_sym != target_canonical:
                continue

            # Refresh mark price and unrealized PnL dynamically
            ticker = self.get_ticker(pos["symbol"], market_type=pos["market_type"])
            mark = to_decimal(ticker.get("mark_price") or ticker["last"])
            inst = self._resolve_instrument(pos["symbol"], pos["market_type"])
            c_size = to_decimal(inst["contract_size"])
            base_qty = to_decimal(pos["qty"]) * c_size if pos["market_type"] == "usdt_perp" else to_decimal(pos["qty"])
            entry = to_decimal(pos["entry_price"])

            if pos["position_side"] in ("SHORT", "SELL"):
                upnl = (entry - mark) * base_qty
            else:
                upnl = (mark - entry) * base_qty

            pos["mark_price"] = mark
            pos["notional_value"] = (base_qty * mark).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            pos["unrealized_pnl"] = upnl.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            pos["updated_at"] = utc_now()
            results.append(dict(pos))
        return results

    def _apply_fill_to_state(
        self,
        *,
        inst: dict[str, Any],
        side: str,
        position_side: str,
        reduce_only: bool,
        fill_qty: Decimal,
        fill_price: Decimal,
        fee: Decimal,
        leverage: Decimal,
        stop_loss: Decimal | None,
        take_profit: Decimal | None,
        tp_sl_confirmed: bool,
    ) -> Decimal:
        """
        Update in-memory balances and positions when an order fills.
        Returns realized_pnl generated by this fill.
        """
        mtype = inst["market_type"]
        canonical = inst["canonical_symbol"]
        base_asset = inst["base_asset"]
        c_size = to_decimal(inst["contract_size"])
        base_qty = fill_qty * c_size if mtype == "usdt_perp" else fill_qty
        notional = (base_qty * fill_price).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        realized_pnl = ZERO

        balances: dict[str, dict[str, Decimal]] = self._state["balances"]
        usdt_bal = balances.setdefault(
            "USDT",
            {"total": Decimal("10000"), "available": Decimal("10000"), "locked": ZERO},
        )

        if mtype == "spot":
            asset_bal = balances.setdefault(
                base_asset,
                {"total": ZERO, "available": ZERO, "locked": ZERO},
            )
            pos_key = ("spot", canonical, "SPOT")
            existing = self._state["positions"].get(pos_key)

            if side == "BUY":
                cost = notional + fee
                usdt_bal["available"] = max(ZERO, usdt_bal["available"] - cost)
                usdt_bal["total"] = usdt_bal["available"] + usdt_bal["locked"]
                asset_bal["available"] += base_qty
                asset_bal["total"] = asset_bal["available"] + asset_bal["locked"]

                if existing and existing["status"] == "OPEN" and existing["qty"] > ZERO:
                    old_qty = to_decimal(existing["qty"])
                    new_qty = old_qty + fill_qty
                    new_entry = ((old_qty * to_decimal(existing["entry_price"])) + (fill_qty * fill_price)) / new_qty
                    existing["qty"] = new_qty
                    existing["entry_price"] = round_price_to_tick(new_entry, inst["tick_size"])
                    existing["mark_price"] = fill_price
                    existing["notional_value"] = (new_qty * fill_price).quantize(Decimal("0.0001"))
                    existing["stop_loss"] = stop_loss or existing.get("stop_loss")
                    existing["take_profit"] = take_profit or existing.get("take_profit")
                    existing["tp_sl_confirmed"] = tp_sl_confirmed
                    existing["updated_at"] = utc_now()
                else:
                    self._state["positions"][pos_key] = {
                        "exchange": self.exchange,
                        "environment": self.environment,
                        "market_type": "spot",
                        "symbol": canonical,
                        "native_symbol": inst["native_symbol"],
                        "position_side": "SPOT",
                        "qty": fill_qty,
                        "entry_price": fill_price,
                        "mark_price": fill_price,
                        "liquidation_price": None,
                        "leverage": ONE,
                        "margin_used": notional,
                        "notional_value": notional,
                        "unrealized_pnl": ZERO,
                        "realized_pnl": ZERO,
                        "stop_loss": stop_loss,
                        "take_profit": take_profit,
                        "tp_sl_confirmed": tp_sl_confirmed,
                        "status": "OPEN",
                        "updated_at": utc_now(),
                    }
            else:
                # Spot SELL
                sell_qty = min(asset_bal["available"], base_qty) if asset_bal["available"] > ZERO else base_qty
                asset_bal["available"] = max(ZERO, asset_bal["available"] - sell_qty)
                asset_bal["total"] = asset_bal["available"] + asset_bal["locked"]
                usdt_bal["available"] += max(ZERO, notional - fee)
                usdt_bal["total"] = usdt_bal["available"] + usdt_bal["locked"]

                if existing and existing["status"] == "OPEN" and existing["qty"] > ZERO:
                    entry = to_decimal(existing["entry_price"])
                    closed_qty = min(to_decimal(existing["qty"]), fill_qty)
                    realized_pnl = ((fill_price - entry) * closed_qty - fee).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
                    rem_qty = to_decimal(existing["qty"]) - closed_qty
                    existing["qty"] = max(ZERO, rem_qty)
                    existing["realized_pnl"] = to_decimal(existing.get("realized_pnl", ZERO)) + realized_pnl
                    if existing["qty"] <= ZERO:
                        existing["status"] = "CLOSED"
                        existing["unrealized_pnl"] = ZERO
                        existing["notional_value"] = ZERO
                    existing["updated_at"] = utc_now()
            return realized_pnl

        # usdt_perp position handling
        eff_lev = max(ONE, to_decimal(leverage, default=Decimal("2")))
        if reduce_only:
            # Find open perp position for this symbol to reduce
            target_side = "SHORT" if side == "BUY" else "LONG"
            pos_key = ("usdt_perp", canonical, target_side)
            existing = self._state["positions"].get(pos_key)
            if not existing:
                # Fallback check any open position on this symbol
                for k, v in self._state["positions"].items():
                    if k[0] == "usdt_perp" and k[1] == canonical and v["status"] == "OPEN" and v["qty"] > ZERO:
                        existing = v
                        pos_key = k
                        break
            if existing and existing["status"] == "OPEN" and existing["qty"] > ZERO:
                old_qty = to_decimal(existing["qty"])
                closed_qty = min(old_qty, fill_qty)
                closed_base = closed_qty * c_size
                entry = to_decimal(existing["entry_price"])
                if existing["position_side"] == "LONG":
                    realized_pnl = ((fill_price - entry) * closed_base - fee).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
                else:
                    realized_pnl = ((entry - fill_price) * closed_base - fee).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

                rem_qty = max(ZERO, old_qty - closed_qty)
                released_margin = to_decimal(existing["margin_used"]) * (closed_qty / old_qty)
                existing["qty"] = rem_qty
                existing["margin_used"] = max(ZERO, to_decimal(existing["margin_used"]) - released_margin).quantize(Decimal("0.0001"))
                existing["notional_value"] = (rem_qty * c_size * fill_price).quantize(Decimal("0.0001"))
                existing["realized_pnl"] = to_decimal(existing.get("realized_pnl", ZERO)) + realized_pnl
                if rem_qty <= ZERO:
                    existing["status"] = "CLOSED"
                    existing["unrealized_pnl"] = ZERO
                    existing["margin_used"] = ZERO
                    existing["notional_value"] = ZERO
                existing["updated_at"] = utc_now()

                usdt_bal["locked"] = max(ZERO, usdt_bal["locked"] - released_margin)
                usdt_bal["available"] = max(ZERO, usdt_bal["available"] + released_margin + realized_pnl)
                usdt_bal["total"] = usdt_bal["available"] + usdt_bal["locked"]
            return realized_pnl

        # Opening or adding to a perpetual position
        eff_pos_side = position_side if position_side in ("LONG", "SHORT") else ("LONG" if side == "BUY" else "SHORT")
        pos_key = ("usdt_perp", canonical, eff_pos_side)
        existing = self._state["positions"].get(pos_key)
        margin_req = (notional / eff_lev).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

        usdt_bal["available"] = max(ZERO, usdt_bal["available"] - margin_req - fee)
        usdt_bal["locked"] += margin_req
        usdt_bal["total"] = usdt_bal["available"] + usdt_bal["locked"]

        liq_price = (
            round_price_to_tick(fill_price * (ONE - (ONE / eff_lev) * Decimal("0.90")), inst["tick_size"])
            if eff_pos_side == "LONG"
            else round_price_to_tick(fill_price * (ONE + (ONE / eff_lev) * Decimal("0.90")), inst["tick_size"])
        )

        if existing and existing["status"] == "OPEN" and existing["qty"] > ZERO:
            old_qty = to_decimal(existing["qty"])
            new_qty = old_qty + fill_qty
            new_entry = ((old_qty * to_decimal(existing["entry_price"])) + (fill_qty * fill_price)) / new_qty
            existing["qty"] = new_qty
            existing["entry_price"] = round_price_to_tick(new_entry, inst["tick_size"])
            existing["mark_price"] = fill_price
            existing["margin_used"] = (to_decimal(existing["margin_used"]) + margin_req).quantize(Decimal("0.0001"))
            existing["notional_value"] = (new_qty * c_size * fill_price).quantize(Decimal("0.0001"))
            existing["stop_loss"] = stop_loss or existing.get("stop_loss")
            existing["take_profit"] = take_profit or existing.get("take_profit")
            existing["tp_sl_confirmed"] = tp_sl_confirmed
            existing["updated_at"] = utc_now()
        else:
            self._state["positions"][pos_key] = {
                "exchange": self.exchange,
                "environment": self.environment,
                "market_type": "usdt_perp",
                "symbol": canonical,
                "native_symbol": inst["native_symbol"],
                "position_side": eff_pos_side,
                "qty": fill_qty,
                "entry_price": fill_price,
                "mark_price": fill_price,
                "liquidation_price": liq_price,
                "leverage": eff_lev,
                "margin_used": margin_req,
                "notional_value": notional,
                "unrealized_pnl": ZERO,
                "realized_pnl": ZERO,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "tp_sl_confirmed": tp_sl_confirmed,
                "status": "OPEN",
                "updated_at": utc_now(),
            }
        return realized_pnl

    def place_order(
        self,
        order_request: dict[str, Any] | Any | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if self._has_withdraw_permission:
            raise PermissionError(
                f"Connection on {self.exchange} has withdrawal permissions enabled; trading is rejected."
            )

        payload: dict[str, Any] = {}
        if isinstance(order_request, dict):
            payload.update(order_request)
        elif order_request is not None and hasattr(order_request, "model_dump"):
            payload.update(order_request.model_dump())
        elif order_request is not None and hasattr(order_request, "__dict__"):
            payload.update(vars(order_request))
        payload.update(kwargs)

        raw_symbol = str(payload.get("symbol") or "BTC/USDT:USDT")
        market_type = str(payload.get("market_type") or infer_market_type(raw_symbol, "usdt_perp")).lower()
        inst = self._resolve_instrument(raw_symbol, market_type)
        canonical = inst["canonical_symbol"]
        native_symbol = inst["native_symbol"]

        client_order_id = str(payload.get("client_order_id") or f"cid-{uuid.uuid4().hex[:16]}")
        # Idempotency check: if client_order_id was already processed, return the stored exchange order
        if client_order_id in self._state["orders_by_client_id"]:
            existing_order = dict(self._state["orders_by_client_id"][client_order_id])
            existing_order["idempotent_replay"] = True
            return existing_order

        raw_side = str(payload.get("side") or payload.get("direction") or "BUY").upper()
        side = "BUY" if raw_side in ("BUY", "LONG") else "SELL"
        order_type = str(payload.get("order_type") or payload.get("entry_type") or "LIMIT").upper()
        reduce_only = bool(payload.get("reduce_only", False))
        position_side = str(
            payload.get("position_side")
            or ("SPOT" if market_type == "spot" else ("LONG" if side == "BUY" else "SHORT"))
        ).upper()

        lot_size = to_decimal(inst["lot_size"])
        tick_size = to_decimal(inst["tick_size"])
        raw_qty = to_decimal(payload.get("qty") or payload.get("quantity") or inst["min_qty"])
        qty = max(lot_size, round_qty_to_lot(raw_qty, lot_size))

        ticker = self.get_ticker(canonical, market_type=market_type)
        raw_price = payload.get("price") or payload.get("entry_price")
        if raw_price is None or to_decimal(raw_price, default=ZERO) <= ZERO or order_type == "MARKET":
            exec_price = to_decimal(ticker["ask"] if side == "BUY" else ticker["bid"])
        else:
            exec_price = round_price_to_tick(to_decimal(raw_price), tick_size)

        stop_loss = (
            round_price_to_tick(to_decimal(payload["stop_loss"]), tick_size)
            if payload.get("stop_loss") is not None
            else None
        )
        take_profit = (
            round_price_to_tick(to_decimal(payload["take_profit"]), tick_size)
            if payload.get("take_profit") is not None
            else None
        )
        leverage = to_decimal(payload.get("leverage"), default=Decimal("2") if market_type == "usdt_perp" else ONE)

        simulate_timeout = bool(payload.get("simulate_timeout", False))
        simulate_partial_fill = bool(payload.get("simulate_partial_fill", False))

        self._state["order_seq"] += 1
        ex_prefix = "BN" if self.exchange == "binance" else "OKX"
        exchange_order_id = f"{ex_prefix}-{self.environment.upper()}-{self._state['order_seq']}"

        protection_order_ids: list[str] = []
        if not reduce_only and (stop_loss is not None or take_profit is not None):
            if stop_loss is not None:
                protection_order_ids.append(f"{exchange_order_id}-SL")
            if take_profit is not None:
                protection_order_ids.append(f"{exchange_order_id}-TP")
        tp_sl_confirmed = bool(protection_order_ids) or reduce_only

        if simulate_partial_fill:
            half_qty = round_qty_to_lot(qty * Decimal("0.5"), lot_size)
            filled_qty = half_qty if half_qty > ZERO else (qty * Decimal("0.5"))
            order_status = "PARTIALLY_FILLED"
        else:
            filled_qty = qty
            order_status = "FILLED"

        c_size = to_decimal(inst["contract_size"])
        base_filled_qty = filled_qty * c_size if market_type == "usdt_perp" else filled_qty
        fee_rate = self.default_fee_rate_perp if market_type == "usdt_perp" else self.default_fee_rate_spot
        fee = (base_filled_qty * exec_price * fee_rate).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

        realized_pnl = self._apply_fill_to_state(
            inst=inst,
            side=side,
            position_side=position_side,
            reduce_only=reduce_only,
            fill_qty=filled_qty,
            fill_price=exec_price,
            fee=fee,
            leverage=leverage,
            stop_loss=stop_loss,
            take_profit=take_profit,
            tp_sl_confirmed=tp_sl_confirmed,
        )

        now = utc_now()
        self._state["trade_seq"] += 1
        exchange_trade_id = f"{ex_prefix}-TRD-{self._state['trade_seq']}"
        fill_record = {
            "exchange": self.exchange,
            "market_type": market_type,
            "symbol": canonical,
            "native_symbol": native_symbol,
            "order_id": exchange_order_id,
            "exchange_order_id": exchange_order_id,
            "client_order_id": client_order_id,
            "exchange_trade_id": exchange_trade_id,
            "side": side,
            "price": exec_price,
            "qty": filled_qty,
            "fee": fee,
            "fee_currency": "USDT",
            "realized_pnl": realized_pnl,
            "occurred_at": now,
        }
        self._state["fills"].append(fill_record)

        order_record: dict[str, Any] = {
            "exchange": self.exchange,
            "environment": self.environment,
            "market_type": market_type,
            "symbol": canonical,
            "native_symbol": native_symbol,
            "client_order_id": client_order_id,
            "order_id": exchange_order_id,
            "exchange_order_id": exchange_order_id,
            "order_type": order_type,
            "side": side,
            "position_side": position_side,
            "reduce_only": reduce_only,
            "status": order_status,
            "qty": qty,
            "filled_qty": filled_qty,
            "price": exec_price,
            "avg_fill_price": exec_price,
            "fee": fee,
            "fee_currency": "USDT",
            "realized_pnl": realized_pnl,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "tp_sl_confirmed": tp_sl_confirmed,
            "protection_order_ids": protection_order_ids,
            "fills": [fill_record],
            "created_at": now,
            "updated_at": now,
        }

        self._state["orders_by_id"][exchange_order_id] = order_record
        self._state["orders_by_client_id"][client_order_id] = order_record

        if simulate_timeout:
            if payload.get("raise_on_timeout"):
                raise TimeoutError(
                    f"Simulated network timeout submitting order {client_order_id} to {self.exchange}"
                )
            return {
                "exchange": self.exchange,
                "environment": self.environment,
                "market_type": market_type,
                "symbol": canonical,
                "native_symbol": native_symbol,
                "client_order_id": client_order_id,
                "order_id": None,
                "exchange_order_id": None,
                "order_type": order_type,
                "side": side,
                "position_side": position_side,
                "reduce_only": reduce_only,
                "status": "UNKNOWN",
                "timed_out": True,
                "simulate_timeout": True,
                "qty": qty,
                "filled_qty": ZERO,
                "price": exec_price,
                "avg_fill_price": ZERO,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "tp_sl_confirmed": False,
                "protection_order_ids": [],
                "fills": [],
                "error": "Order submission timed out; state is UNKNOWN. Reconcile via get_order(client_order_id=...) before retrying.",
                "created_at": now,
                "updated_at": now,
            }

        return redact_sensitive_data(dict(order_record))

    def get_order(
        self,
        symbol: str | None = None,
        order_id: str | None = None,
        client_order_id: str | None = None,
        market_type: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any] | None:
        _ = market_type
        oid = order_id or kwargs.get("exchange_order_id")
        cid = client_order_id

        # Handle case where first positional argument was actually an order_id or client_order_id
        if symbol and not oid and not cid:
            if symbol in self._state["orders_by_id"]:
                oid = symbol
            elif symbol in self._state["orders_by_client_id"]:
                cid = symbol

        if oid and oid in self._state["orders_by_id"]:
            return redact_sensitive_data(dict(self._state["orders_by_id"][oid]))
        if cid and cid in self._state["orders_by_client_id"]:
            return redact_sensitive_data(dict(self._state["orders_by_client_id"][cid]))
        return None

    def cancel_order(
        self,
        symbol: str | None = None,
        order_id: str | None = None,
        client_order_id: str | None = None,
        market_type: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        _ = market_type
        existing = self.get_order(
            symbol=symbol,
            order_id=order_id,
            client_order_id=client_order_id,
            **kwargs,
        )
        if not existing:
            return {
                "exchange": self.exchange,
                "status": "NOT_FOUND",
                "canceled": False,
                "order_id": order_id,
                "client_order_id": client_order_id,
            }

        ex_oid = existing["exchange_order_id"]
        cid = existing["client_order_id"]
        stored = self._state["orders_by_id"].get(ex_oid) or self._state["orders_by_client_id"].get(cid)
        if stored:
            if stored["status"] not in ("FILLED",):
                stored["status"] = "CANCELED"
                stored["updated_at"] = utc_now()
            return redact_sensitive_data(dict(stored))
        return existing

    def get_fills(
        self,
        symbol: str | None = None,
        since: datetime | None = None,
        order_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        fills: list[dict[str, Any]] = list(self._state["fills"])
        if symbol:
            target = to_canonical_symbol(self.exchange, infer_market_type(symbol), symbol)
            fills = [f for f in fills if f["symbol"] == target or f["native_symbol"] == symbol]
        if order_id:
            fills = [
                f
                for f in fills
                if f["exchange_order_id"] == order_id or f["client_order_id"] == order_id
            ]
        if since:
            fills = [f for f in fills if f["occurred_at"] >= since]
        return [redact_sensitive_data(dict(f)) for f in fills[-limit:]]

    def subscribe_market(
        self,
        symbols: list[str],
        market_type: str = "usdt_perp",
        channels: list[str] | None = None,
    ) -> dict[str, Any]:
        normalized = [to_canonical_symbol(self.exchange, market_type, s) for s in symbols]
        for sym in normalized:
            self._state["subscriptions_market"].add((market_type, sym))
        return {
            "exchange": self.exchange,
            "environment": self.environment,
            "subscribed": True,
            "market_type": market_type,
            "symbols": normalized,
            "channels": channels or ["tickers", "candles", "orderbook"],
            "subscribed_at": utc_now(),
        }

    def subscribe_private(self) -> dict[str, Any]:
        self._state["subscriptions_private"] = True
        return {
            "exchange": self.exchange,
            "environment": self.environment,
            "subscribed": True,
            "channels": ["orders", "fills", "positions", "balances"],
            "subscribed_at": utc_now(),
        }

    def reconcile(
        self,
        local_orders: list[dict[str, Any]] | None = None,
        local_positions: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        exchange_orders = [dict(o) for o in self._state["orders_by_id"].values()]
        exchange_positions = self.get_positions()
        exchange_balances = self.get_balance()

        recovered_orders: list[dict[str, Any]] = []
        order_mismatches: list[dict[str, Any]] = []
        if local_orders:
            for loc in local_orders:
                cid = loc.get("client_order_id")
                oid = loc.get("exchange_order_id") or loc.get("order_id")
                remote = self.get_order(order_id=oid, client_order_id=cid)
                if remote:
                    if loc.get("status") == "UNKNOWN" or loc.get("status") != remote.get("status"):
                        recovered_orders.append(remote)
                elif loc.get("status") in ("SUBMITTED", "ACKNOWLEDGED", "UNKNOWN"):
                    order_mismatches.append(
                        {
                            "client_order_id": cid,
                            "local_status": loc.get("status"),
                            "exchange_status": "MISSING",
                        }
                    )

        position_mismatches: list[dict[str, Any]] = []
        if local_positions is not None:
            remote_by_key = {
                (p["market_type"], p["symbol"], p["position_side"]): to_decimal(p["qty"])
                for p in exchange_positions
            }
            for lp in local_positions:
                if lp.get("status", "OPEN") != "OPEN":
                    continue
                key = (
                    lp.get("market_type", "usdt_perp"),
                    lp.get("symbol"),
                    lp.get("position_side", "LONG"),
                )
                local_qty = to_decimal(lp.get("qty", ZERO))
                remote_qty = remote_by_key.get(key, ZERO)
                if local_qty != remote_qty:
                    position_mismatches.append(
                        {
                            "market_type": key[0],
                            "symbol": key[1],
                            "position_side": key[2],
                            "local_qty": local_qty,
                            "exchange_qty": remote_qty,
                        }
                    )

        in_sync = len(order_mismatches) == 0 and len(position_mismatches) == 0
        return {
            "exchange": self.exchange,
            "environment": self.environment,
            "in_sync": in_sync,
            "recovered_orders": recovered_orders,
            "order_mismatches": order_mismatches,
            "position_mismatches": position_mismatches,
            "orders": exchange_orders,
            "positions": exchange_positions,
            "balances": exchange_balances,
            "fills": self.get_fills(limit=100),
            "reconciled_at": utc_now(),
        }

# ---------------------------------------------------------------------------
# Rate-limit enforcement
#
# Every REST-like adapter method consumes exchange budget before doing work, so a
# real HTTP implementation inherits the governance for free and the simulated
# adapters exercise the same accounting (observable via /api/v1/system/rate-limits).
# ---------------------------------------------------------------------------
import threading as _threading

# One re-entrancy counter shared by every wrapped method on a given thread: an
# outer public call may call other public methods internally, and a single logical
# request must not be billed three times against the exchange budget.
_THROTTLE_LOCAL = _threading.local()

_THROTTLED_METHODS: tuple[str, ...] = (
    "get_instruments",
    "get_balance",
    "get_klines",
    "get_ticker",
    "get_orderbook",
    "get_funding_rate",
    "get_positions",
    "place_order",
    "get_order",
    "cancel_order",
    "get_fills",
    "reconcile",
)


def _infer_market_type(args: tuple, kwargs: dict) -> str | None:
    market = kwargs.get("market_type")
    if isinstance(market, str) and market:
        return market
    symbol = kwargs.get("symbol")
    if not isinstance(symbol, str):
        symbol = next((a for a in args if isinstance(a, str) and "/" in a), None)
    if isinstance(symbol, str):
        return "usdt_perp" if ":" in symbol else "spot"
    return None


def _make_throttled(method_name: str, fn):
    import functools

    from app.exchanges.rate_limit import rate_limiter

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        depth = getattr(_THROTTLE_LOCAL, "depth", 0)
        _THROTTLE_LOCAL.depth = depth + 1
        try:
            if depth == 0:
                market_type = _infer_market_type(args, kwargs)
                rate_limiter.acquire(self.exchange, market_type, method_name, block=True, timeout=5.0)
            return fn(self, *args, **kwargs)
        finally:
            _THROTTLE_LOCAL.depth = depth

    wrapper._rate_limited = True  # type: ignore[attr-defined]
    return wrapper


def _install_rate_limits(cls) -> None:
    for name in _THROTTLED_METHODS:
        fn = cls.__dict__.get(name) or getattr(cls, name, None)
        if fn is None or getattr(fn, "_rate_limited", False):
            continue
        setattr(cls, name, _make_throttled(name, fn))


_install_rate_limits(BaseSimulatedExchangeAdapter)
