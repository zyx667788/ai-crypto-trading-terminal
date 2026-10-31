from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.events import event_bus
from app.db.session import get_db
from app.market_data.service import market_data_service
from app.scanner.service import scanner_service

router = APIRouter(tags=["markets"])


@router.get("/markets/instruments")
def list_instruments(
    exchange: str | None = None,
    market_type: str | None = None,
    symbol: str | None = None,
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    market_data_service.sync_all_default_markets(db)
    rows = market_data_service.list_instruments(db, exchange=exchange, market_type=market_type, symbol=symbol)
    return [
        {
            "id": i.id,
            "exchange": i.exchange,
            "market_type": i.market_type,
            "native_symbol": i.native_symbol,
            "canonical_symbol": i.canonical_symbol,
            "base_asset": i.base_asset,
            "quote_asset": i.quote_asset,
            "settle_asset": i.settle_asset,
            "tick_size": str(i.tick_size),
            "lot_size": str(i.lot_size),
            "min_qty": str(i.min_qty),
            "min_notional": str(i.min_notional),
            "contract_size": str(i.contract_size),
            "max_leverage": str(i.max_leverage),
            "status": i.status,
        }
        for i in rows
    ]


@router.get("/markets/tickers")
def list_tickers(
    exchange: str | None = None,
    market_type: str | None = None,
    symbol: str | None = None,
    refresh: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    market_data_service.sync_all_default_markets(db)
    if refresh:
        market_data_service.sync_all_default_markets(db)
    rows = market_data_service.get_tickers(db, exchange=exchange, market_type=market_type, symbol=symbol, refresh=refresh)
    return [
        {
            "exchange": s.exchange,
            "market_type": s.market_type,
            "symbol": s.symbol,
            "event_time": s.event_time,
            "received_at": s.received_at,
            "last": str(s.last),
            "bid": str(s.bid),
            "ask": str(s.ask),
            "spread_bps": str(s.spread_bps),
            "volume_24h": str(s.volume_24h),
            "quote_volume_24h": str(s.quote_volume_24h),
            "change_24h_pct": str(s.change_24h_pct),
            "mark_price": str(s.mark_price) if s.mark_price is not None else None,
            "index_price": str(s.index_price) if s.index_price is not None else None,
            "funding_rate": str(s.funding_rate) if s.funding_rate is not None else None,
            "next_funding_time": s.next_funding_time,
            "orderbook_summary": s.orderbook_summary,
            "indicators": s.indicators_json,
            "is_stale": s.is_stale,
            "source_exchange": s.exchange,
        }
        for s in rows
    ]


@router.get("/markets/candles")
def get_candles(
    exchange: str = "binance",
    symbol: str = "BTC/USDT:USDT",
    timeframe: str = "15m",
    limit: int = Query(default=120, ge=10, le=1000),
    market_type: str | None = None,
    closed_only: bool = True,
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    market_data_service.sync_all_default_markets(db, timeframe=timeframe, limit=limit)
    rows = market_data_service.get_candles(
        db, exchange=exchange, symbol=symbol, timeframe=timeframe, limit=limit,
        market_type=market_type, closed_only=closed_only,
    )
    return [
        {
            "exchange": c.exchange,
            "market_type": c.market_type,
            "symbol": c.symbol,
            "timeframe": c.timeframe,
            "open_time": c.open_time,
            "close_time": c.close_time,
            "open": str(c.open),
            "high": str(c.high),
            "low": str(c.low),
            "close": str(c.close),
            "volume": str(c.volume),
            "quote_volume": str(c.quote_volume),
            "is_closed": c.is_closed,
            "source_exchange": c.source_exchange,
        }
        for c in rows
    ]


@router.get("/markets/orderbook")
def get_orderbook(
    exchange: str = "binance",
    symbol: str = "BTC/USDT:USDT",
    market_type: str | None = None,
    depth: int = Query(default=10, ge=1, le=50),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return market_data_service.get_orderbook(exchange=exchange, symbol=symbol, market_type=market_type, depth=depth, db=db)


@router.get("/markets/funding-rate")
def get_funding_rate(
    exchange: str = "binance",
    symbol: str = "BTC/USDT:USDT",
    market_type: str = "usdt_perp",
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return market_data_service.get_funding_rate(exchange=exchange, symbol=symbol, market_type=market_type, db=db)


@router.get("/scanner/status")
def scanner_status(db: Session = Depends(get_db)) -> dict[str, Any]:
    return scanner_service.get_status(db)


@router.get("/scanner/opportunities")
def scanner_opportunities(
    exchange: str | None = None,
    market_type: str | None = None,
    only_passed: bool = False,
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    return scanner_service.get_opportunities(db, exchange=exchange, market_type=market_type, only_passed=only_passed, limit=limit)


@router.get("/scanner/rankings")
def scanner_rankings(
    exchange: str | None = None,
    market_type: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    return scanner_service.get_rankings(db, exchange=exchange, market_type=market_type, limit=limit)


@router.post("/scanner/start")
def start_scanner(
    exchange: str | None = None,
    market_type: str | None = None,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    result = scanner_service.start_scanner(db, exchange=exchange, market_type=market_type) if _accepts_kwargs(scanner_service.start_scanner) else scanner_service.start_scanner(db)
    event_bus.publish_sync("scanner", "scanner.started", {"status": "RUNNING"}, source="scanner")
    return result


@router.post("/scanner/pause")
def pause_scanner(db: Session = Depends(get_db)) -> dict[str, Any]:
    result = scanner_service.pause_scanner(db)
    event_bus.publish_sync("scanner", "scanner.paused", {"status": "PAUSED"}, source="scanner")
    return result


def _accepts_kwargs(func) -> bool:
    import inspect

    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):
        return False
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()) or "exchange" in sig.parameters
