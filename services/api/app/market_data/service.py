from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.events import event_bus
from app.core.precision import ZERO, calc_spread_bps, to_decimal, utc_now
from app.db.session import SessionLocal
from app.exchanges.base import (
    ExchangeAdapter,
    infer_market_type,
    to_canonical_symbol,
)
from app.exchanges.binance.adapter import BinanceAdapter
from app.exchanges.okx.adapter import OKXAdapter
from app.market_data.indicators import (
    calculate_orderbook_imbalance,
    compute_indicator_snapshot,
)
from app.models.entities import Instrument, MarketCandle, MarketSnapshot


def _ensure_utc(dt: datetime | None) -> datetime:
    if dt is None:
        return utc_now()
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _serialize_decimals(data: Any) -> Any:
    if isinstance(data, Decimal):
        return str(data)
    if isinstance(data, datetime):
        return _ensure_utc(data).isoformat()
    if isinstance(data, dict):
        return {k: _serialize_decimals(v) for k, v in data.items()}
    if isinstance(data, list):
        return [_serialize_decimals(v) for v in data]
    return data


class MarketDataService:
    """
    Market Data Service (Section 3.3).
    Manages Instrument metadata, closed MarketCandles, MarketSnapshots with technical indicators,
    Orderbook depth summaries, Funding rates, and data freshness (is_stale) validation.
    """

    def __init__(self, db: Session | None = None) -> None:
        self.db = db
        self._adapters: dict[tuple[str, str], ExchangeAdapter] = {}

    def _get_adapter(self, exchange: str = "binance", environment: str | None = None) -> ExchangeAdapter:
        ex = (exchange or "binance").strip().lower()
        env = (environment or settings.DEFAULT_TRADING_ENVIRONMENT or "demo").strip().lower()
        key = (ex, env)
        if key not in self._adapters:
            if ex == "okx":
                self._adapters[key] = OKXAdapter(environment=env)
            else:
                self._adapters[key] = BinanceAdapter(environment=env)
        return self._adapters[key]

    def sync_instruments(
        self,
        db: Session | str | None = None,
        exchange: str | None = None,
        market_type: str | None = None,
    ) -> list[Instrument]:
        if isinstance(db, str):
            market_type = exchange
            exchange = db
            session = self.db
        else:
            session = db or self.db

        own_session = False
        if session is None:
            session = SessionLocal()
            own_session = True

        try:
            exchanges = [exchange.lower()] if exchange else ["binance", "okx"]
            synced: list[Instrument] = []

            for ex in exchanges:
                adapter = self._get_adapter(ex)
                raw_instruments = adapter.get_instruments(market_type=market_type)
                for item in raw_instruments:
                    stmt = select(Instrument).where(
                        Instrument.exchange == item["exchange"],
                        Instrument.market_type == item["market_type"],
                        Instrument.native_symbol == item["native_symbol"],
                    )
                    inst = session.execute(stmt).scalar_one_or_none()
                    if inst is None:
                        inst = Instrument(
                            exchange=item["exchange"],
                            market_type=item["market_type"],
                            native_symbol=item["native_symbol"],
                            canonical_symbol=item["canonical_symbol"],
                            base_asset=item["base_asset"],
                            quote_asset=item["quote_asset"],
                            settle_asset=item.get("settle_asset", "USDT"),
                            tick_size=to_decimal(item["tick_size"]),
                            lot_size=to_decimal(item["lot_size"]),
                            min_qty=to_decimal(item["min_qty"]),
                            min_notional=to_decimal(item["min_notional"]),
                            contract_size=to_decimal(item["contract_size"]),
                            max_leverage=to_decimal(item.get("max_leverage", Decimal("20"))),
                            status=item.get("status", "TRADING"),
                            updated_at=utc_now(),
                        )
                        session.add(inst)
                    else:
                        inst.canonical_symbol = item["canonical_symbol"]
                        inst.tick_size = to_decimal(item["tick_size"])
                        inst.lot_size = to_decimal(item["lot_size"])
                        inst.min_qty = to_decimal(item["min_qty"])
                        inst.min_notional = to_decimal(item["min_notional"])
                        inst.contract_size = to_decimal(item["contract_size"])
                        inst.max_leverage = to_decimal(item.get("max_leverage", Decimal("20")))
                        inst.status = item.get("status", "TRADING")
                        inst.updated_at = utc_now()
                    synced.append(inst)

            session.commit()
            for inst in synced:
                session.refresh(inst)
            return synced
        finally:
            if own_session:
                session.close()

    seed_instruments = sync_instruments

    def list_instruments(
        self,
        db: Session | str | None = None,
        exchange: str | None = None,
        market_type: str | None = None,
        symbol: str | None = None,
    ) -> list[Instrument]:
        if isinstance(db, str):
            symbol = market_type
            market_type = exchange
            exchange = db
            session = self.db
        else:
            session = db or self.db

        own_session = False
        if session is None:
            session = SessionLocal()
            own_session = True

        try:
            stmt = select(Instrument)
            if exchange:
                stmt = stmt.where(Instrument.exchange == exchange.lower())
            if market_type:
                stmt = stmt.where(Instrument.market_type == market_type.lower())
            if symbol:
                norm_mtype = market_type or infer_market_type(symbol)
                canonical = to_canonical_symbol(exchange or "binance", norm_mtype, symbol)
                stmt = stmt.where(
                    (Instrument.canonical_symbol == canonical) | (Instrument.native_symbol == symbol)
                )

            rows = list(session.execute(stmt.order_by(Instrument.exchange, Instrument.market_type, Instrument.canonical_symbol)).scalars().all())
            if not rows:
                self.sync_instruments(session, exchange=exchange, market_type=market_type)
                rows = list(session.execute(stmt.order_by(Instrument.exchange, Instrument.market_type, Instrument.canonical_symbol)).scalars().all())
            return rows
        finally:
            if own_session:
                session.close()

    get_instruments = list_instruments

    def get_instrument(
        self,
        db: Session | str | None = None,
        exchange: str = "binance",
        market_type: str | None = None,
        symbol: str = "BTC/USDT:USDT",
    ) -> Instrument | None:
        if isinstance(db, str):
            symbol = market_type or symbol
            market_type = exchange
            exchange = db
            session = self.db
        else:
            session = db or self.db

        mtype = (market_type or infer_market_type(symbol)).lower()
        items = self.list_instruments(session, exchange=exchange, market_type=mtype, symbol=symbol)
        return items[0] if items else None

    def sync_candles(
        self,
        db: Session | str | None = None,
        exchange: str = "binance",
        symbol: str = "BTC/USDT:USDT",
        timeframe: str = "15m",
        limit: int = 100,
        market_type: str | None = None,
        closed_only: bool = True,
    ) -> list[MarketCandle]:
        if isinstance(db, str):
            # Called as service.sync_candles("binance", "BTC/USDT:USDT", ...)
            market_type = timeframe if timeframe in ("spot", "usdt_perp") else market_type
            timeframe = symbol if symbol in ("1m", "5m", "15m", "1h", "4h", "1d") else timeframe
            symbol = exchange
            exchange = db
            session = self.db
        else:
            session = db or self.db

        own_session = False
        if session is None:
            session = SessionLocal()
            own_session = True

        try:
            ex = exchange.lower()
            mtype = (market_type or infer_market_type(symbol)).lower()
            canonical = to_canonical_symbol(ex, mtype, symbol)
            inst = self.get_instrument(session, exchange=ex, market_type=mtype, symbol=canonical)

            adapter = self._get_adapter(ex)
            raw_klines = adapter.get_klines(
                symbol=canonical,
                timeframe=timeframe,
                limit=limit,
                market_type=mtype,
            )

            stored: list[MarketCandle] = []
            for k in raw_klines:
                if closed_only and not k.get("is_closed", True):
                    continue
                open_dt = _ensure_utc(k["open_time"])
                close_dt = _ensure_utc(k["close_time"])

                stmt = select(MarketCandle).where(
                    MarketCandle.exchange == ex,
                    MarketCandle.market_type == mtype,
                    MarketCandle.symbol == canonical,
                    MarketCandle.timeframe == timeframe,
                    MarketCandle.open_time == open_dt,
                )
                candle = session.execute(stmt).scalar_one_or_none()
                if candle is None:
                    candle = MarketCandle(
                        instrument_id=inst.id if inst else None,
                        exchange=ex,
                        market_type=mtype,
                        symbol=canonical,
                        timeframe=timeframe,
                        open_time=open_dt,
                        close_time=close_dt,
                        open=to_decimal(k["open"]),
                        high=to_decimal(k["high"]),
                        low=to_decimal(k["low"]),
                        close=to_decimal(k["close"]),
                        volume=to_decimal(k["volume"]),
                        quote_volume=to_decimal(k.get("quote_volume", ZERO)),
                        is_closed=bool(k.get("is_closed", True)),
                        source_exchange=ex,
                        received_at=utc_now(),
                    )
                    session.add(candle)
                else:
                    candle.open = to_decimal(k["open"])
                    candle.high = to_decimal(k["high"])
                    candle.low = to_decimal(k["low"])
                    candle.close = to_decimal(k["close"])
                    candle.volume = to_decimal(k["volume"])
                    candle.quote_volume = to_decimal(k.get("quote_volume", ZERO))
                    candle.is_closed = bool(k.get("is_closed", True))
                    candle.received_at = utc_now()
                stored.append(candle)

            session.commit()
            for c in stored:
                session.refresh(c)
            return stored
        finally:
            if own_session:
                session.close()

    fetch_and_store_klines = sync_candles
    fetch_klines = sync_candles
    seed_candles = sync_candles

    def get_candles(
        self,
        db: Session | str | None = None,
        exchange: str = "binance",
        symbol: str = "BTC/USDT:USDT",
        timeframe: str = "15m",
        limit: int = 100,
        market_type: str | None = None,
        closed_only: bool = True,
    ) -> list[MarketCandle]:
        if isinstance(db, str):
            symbol = exchange
            exchange = db
            session = self.db
        else:
            session = db or self.db

        own_session = False
        if session is None:
            session = SessionLocal()
            own_session = True

        try:
            ex = exchange.lower()
            mtype = (market_type or infer_market_type(symbol)).lower()
            canonical = to_canonical_symbol(ex, mtype, symbol)

            stmt = select(MarketCandle).where(
                MarketCandle.exchange == ex,
                MarketCandle.market_type == mtype,
                MarketCandle.symbol == canonical,
                MarketCandle.timeframe == timeframe,
            )
            if closed_only:
                stmt = stmt.where(MarketCandle.is_closed.is_(True))
            stmt = stmt.order_by(MarketCandle.open_time.desc()).limit(limit)
            rows = list(session.execute(stmt).scalars().all())
            if len(rows) < min(limit, 30):
                self.sync_candles(
                    session,
                    exchange=ex,
                    symbol=canonical,
                    timeframe=timeframe,
                    limit=max(limit, 60),
                    market_type=mtype,
                    closed_only=closed_only,
                )
                rows = list(session.execute(stmt).scalars().all())
            rows.reverse()
            return rows
        finally:
            if own_session:
                session.close()

    def get_orderbook(
        self,
        exchange: str = "binance",
        symbol: str = "BTC/USDT:USDT",
        market_type: str | None = None,
        depth: int = 10,
        db: Session | None = None,
    ) -> dict[str, Any]:
        _ = db
        ex = exchange.lower()
        mtype = (market_type or infer_market_type(symbol)).lower()
        canonical = to_canonical_symbol(ex, mtype, symbol)
        adapter = self._get_adapter(ex)
        ob = adapter.get_orderbook(canonical, market_type=mtype, depth=depth)
        imbalance = calculate_orderbook_imbalance(ob.get("bids", []), ob.get("asks", []))
        ob["imbalance"] = imbalance
        return ob

    def get_funding_rate(
        self,
        exchange: str = "binance",
        symbol: str = "BTC/USDT:USDT",
        market_type: str = "usdt_perp",
        db: Session | None = None,
    ) -> dict[str, Any]:
        _ = db
        ex = exchange.lower()
        mtype = (market_type or infer_market_type(symbol, "usdt_perp")).lower()
        canonical = to_canonical_symbol(ex, mtype, symbol)
        adapter = self._get_adapter(ex)
        return adapter.get_funding_rate(canonical, market_type=mtype)

    get_funding_rates = get_funding_rate

    def update_market_snapshot(
        self,
        db: Session | str | None = None,
        exchange: str = "binance",
        symbol: str = "BTC/USDT:USDT",
        market_type: str | None = None,
        timeframe: str = "15m",
        force_stale: bool = False,
        event_time: datetime | None = None,
        max_stale_seconds: int | None = None,
    ) -> MarketSnapshot:
        if isinstance(db, str):
            symbol = exchange
            exchange = db
            session = self.db
        else:
            session = db or self.db

        own_session = False
        if session is None:
            session = SessionLocal()
            own_session = True

        try:
            ex = exchange.lower()
            mtype = (market_type or infer_market_type(symbol)).lower()
            canonical = to_canonical_symbol(ex, mtype, symbol)
            inst = self.get_instrument(session, exchange=ex, market_type=mtype, symbol=canonical)

            adapter = self._get_adapter(ex)
            ticker = adapter.get_ticker(canonical, market_type=mtype)
            orderbook = self.get_orderbook(exchange=ex, symbol=canonical, market_type=mtype, depth=10)
            candles = self.get_candles(
                session,
                exchange=ex,
                symbol=canonical,
                timeframe=timeframe,
                limit=60,
                market_type=mtype,
                closed_only=True,
            )
            indicators = compute_indicator_snapshot(candles, orderbook=orderbook, closed_only=True)

            now = utc_now()
            ev_time = _ensure_utc(event_time or ticker.get("event_time") or now)
            stale_limit = int(
                max_stale_seconds
                if max_stale_seconds is not None
                else settings.DEFAULT_MAX_DATA_STALE_SECONDS
            )
            age_seconds = (now - ev_time).total_seconds()
            is_stale = bool(force_stale or ticker.get("is_stale", False) or age_seconds > stale_limit)

            last = to_decimal(ticker["last"])
            bid = to_decimal(ticker["bid"])
            ask = to_decimal(ticker["ask"])
            spread_bps = calc_spread_bps(bid, ask)

            orderbook_summary = {
                "bid_top": str(bid),
                "ask_top": str(ask),
                "bid_volume_top": str(orderbook.get("bid_volume_top", ZERO)),
                "ask_volume_top": str(orderbook.get("ask_volume_top", ZERO)),
                "imbalance": str(orderbook.get("imbalance", ZERO)),
                "spread_bps": str(spread_bps),
                "bids": [[str(p), str(q)] for p, q in (orderbook.get("bids") or [])[:5]],
                "asks": [[str(p), str(q)] for p, q in (orderbook.get("asks") or [])[:5]],
            }
            indicators_serialized = _serialize_decimals(indicators)

            stmt = (
                select(MarketSnapshot)
                .where(
                    MarketSnapshot.exchange == ex,
                    MarketSnapshot.market_type == mtype,
                    MarketSnapshot.symbol == canonical,
                )
                .order_by(MarketSnapshot.received_at.desc())
                .limit(1)
            )
            snapshot = session.execute(stmt).scalar_one_or_none()
            if snapshot is None:
                snapshot = MarketSnapshot(
                    instrument_id=inst.id if inst else None,
                    exchange=ex,
                    market_type=mtype,
                    symbol=canonical,
                    event_time=ev_time,
                    received_at=now,
                    last=last,
                    bid=bid,
                    ask=ask,
                    spread_bps=spread_bps,
                    volume_24h=to_decimal(ticker.get("volume_24h", ZERO)),
                    quote_volume_24h=to_decimal(ticker.get("quote_volume_24h", ZERO)),
                    change_24h_pct=to_decimal(ticker.get("change_24h_pct", ZERO)),
                    mark_price=to_decimal(ticker["mark_price"]) if ticker.get("mark_price") is not None else None,
                    index_price=to_decimal(ticker["index_price"]) if ticker.get("index_price") is not None else None,
                    funding_rate=to_decimal(ticker["funding_rate"]) if ticker.get("funding_rate") is not None else None,
                    next_funding_time=_ensure_utc(ticker["next_funding_time"]) if ticker.get("next_funding_time") else None,
                    orderbook_summary=orderbook_summary,
                    indicators_json=indicators_serialized,
                    is_stale=is_stale,
                )
                session.add(snapshot)
            else:
                snapshot.instrument_id = inst.id if inst else snapshot.instrument_id
                snapshot.event_time = ev_time
                snapshot.received_at = now
                snapshot.last = last
                snapshot.bid = bid
                snapshot.ask = ask
                snapshot.spread_bps = spread_bps
                snapshot.volume_24h = to_decimal(ticker.get("volume_24h", ZERO))
                snapshot.quote_volume_24h = to_decimal(ticker.get("quote_volume_24h", ZERO))
                snapshot.change_24h_pct = to_decimal(ticker.get("change_24h_pct", ZERO))
                snapshot.mark_price = to_decimal(ticker["mark_price"]) if ticker.get("mark_price") is not None else None
                snapshot.index_price = to_decimal(ticker["index_price"]) if ticker.get("index_price") is not None else None
                snapshot.funding_rate = to_decimal(ticker["funding_rate"]) if ticker.get("funding_rate") is not None else None
                snapshot.next_funding_time = (
                    _ensure_utc(ticker["next_funding_time"]) if ticker.get("next_funding_time") else None
                )
                snapshot.orderbook_summary = orderbook_summary
                snapshot.indicators_json = indicators_serialized
                snapshot.is_stale = is_stale

            session.commit()
            session.refresh(snapshot)

            event_bus.cache_set(f"snapshot:{ex}:{mtype}:{canonical}", snapshot.id)
            event_bus.publish_sync(
                channel="market",
                event_type="market.snapshot.updated",
                data={
                    "exchange": ex,
                    "market_type": mtype,
                    "symbol": canonical,
                    "last": str(snapshot.last),
                    "bid": str(snapshot.bid),
                    "ask": str(snapshot.ask),
                    "spread_bps": str(snapshot.spread_bps),
                    "mark_price": str(snapshot.mark_price) if snapshot.mark_price is not None else None,
                    "funding_rate": str(snapshot.funding_rate) if snapshot.funding_rate is not None else None,
                    "is_stale": snapshot.is_stale,
                },
                source=ex,
                also_dashboard=False,
            )
            return snapshot
        finally:
            if own_session:
                session.close()

    refresh_snapshot = update_market_snapshot
    fetch_and_update_snapshot = update_market_snapshot
    sync_snapshot = update_market_snapshot

    def check_snapshot_staleness(
        self,
        snapshot: MarketSnapshot,
        max_stale_seconds: int | None = None,
    ) -> bool:
        limit = int(
            max_stale_seconds
            if max_stale_seconds is not None
            else settings.DEFAULT_MAX_DATA_STALE_SECONDS
        )
        age = (utc_now() - _ensure_utc(snapshot.event_time)).total_seconds()
        if snapshot.is_stale or age > limit:
            snapshot.is_stale = True
            return True
        return False

    def get_latest_snapshot(
        self,
        db: Session | str | None = None,
        exchange: str = "binance",
        symbol: str = "BTC/USDT:USDT",
        market_type: str | None = None,
        refresh: bool = False,
        max_stale_seconds: int | None = None,
    ) -> MarketSnapshot:
        if isinstance(db, str):
            symbol = exchange
            exchange = db
            session = self.db
        else:
            session = db or self.db

        own_session = False
        if session is None:
            session = SessionLocal()
            own_session = True

        try:
            ex = exchange.lower()
            mtype = (market_type or infer_market_type(symbol)).lower()
            canonical = to_canonical_symbol(ex, mtype, symbol)

            if refresh:
                return self.update_market_snapshot(
                    session,
                    exchange=ex,
                    symbol=canonical,
                    market_type=mtype,
                    max_stale_seconds=max_stale_seconds,
                )

            stmt = (
                select(MarketSnapshot)
                .where(
                    MarketSnapshot.exchange == ex,
                    MarketSnapshot.market_type == mtype,
                    MarketSnapshot.symbol == canonical,
                )
                .order_by(MarketSnapshot.received_at.desc())
                .limit(1)
            )
            snapshot = session.execute(stmt).scalar_one_or_none()
            if snapshot is None:
                return self.update_market_snapshot(
                    session,
                    exchange=ex,
                    symbol=canonical,
                    market_type=mtype,
                    max_stale_seconds=max_stale_seconds,
                )

            was_stale = snapshot.is_stale
            is_now_stale = self.check_snapshot_staleness(snapshot, max_stale_seconds=max_stale_seconds)
            if is_now_stale and not was_stale:
                session.commit()
                session.refresh(snapshot)
            return snapshot
        finally:
            if own_session:
                session.close()

    get_snapshot = get_latest_snapshot
    get_ticker = get_latest_snapshot

    def get_tickers(
        self,
        db: Session | str | None = None,
        exchange: str | None = None,
        market_type: str | None = None,
        symbol: str | None = None,
        refresh: bool = False,
    ) -> list[MarketSnapshot]:
        if isinstance(db, str):
            market_type = exchange
            exchange = db
            session = self.db
        else:
            session = db or self.db

        own_session = False
        if session is None:
            session = SessionLocal()
            own_session = True

        try:
            instruments = self.list_instruments(
                session,
                exchange=exchange,
                market_type=market_type,
                symbol=symbol,
            )
            snapshots: list[MarketSnapshot] = []
            for inst in instruments:
                snap = self.get_latest_snapshot(
                    session,
                    exchange=inst.exchange,
                    symbol=inst.canonical_symbol,
                    market_type=inst.market_type,
                    refresh=refresh,
                )
                snapshots.append(snap)
            return snapshots
        finally:
            if own_session:
                session.close()

    list_snapshots = get_tickers

    def sync_all_default_markets(
        self,
        db: Session | None = None,
        timeframe: str = "15m",
        limit: int = 60,
    ) -> dict[str, Any]:
        session = db or self.db
        own_session = False
        if session is None:
            session = SessionLocal()
            own_session = True

        try:
            instruments = self.sync_instruments(session)
            snapshots_count = 0
            candles_count = 0
            for inst in instruments:
                candles = self.sync_candles(
                    session,
                    exchange=inst.exchange,
                    symbol=inst.canonical_symbol,
                    timeframe=timeframe,
                    limit=limit,
                    market_type=inst.market_type,
                    closed_only=True,
                )
                candles_count += len(candles)
                self.update_market_snapshot(
                    session,
                    exchange=inst.exchange,
                    symbol=inst.canonical_symbol,
                    market_type=inst.market_type,
                    timeframe=timeframe,
                )
                snapshots_count += 1

            return {
                "instruments_synced": len(instruments),
                "candles_synced": candles_count,
                "snapshots_synced": snapshots_count,
            }
        finally:
            if own_session:
                session.close()


market_data_service = MarketDataService()
