from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, JSONResponse

from app.api.v1.router import api_router
from app.core.config import DATA_DIR, LOGS_DIR, settings
from app.core.events import CHANNELS, event_bus
from app.core.logging import get_logger
from app.core.precision import utc_iso
from app.core.recovery import recovery_manager
from app.core.watchdog import watchdog_service

logger = get_logger("terminal.main")


def _seed_if_empty() -> None:
    """Idempotent bootstrap so a fresh install is immediately usable in Demo mode."""
    from app.db.session import SessionLocal
    from app.exchanges.registry import ensure_default_demo_connections
    from app.market_data.service import market_data_service
    from app.strategies.service import strategy_service

    with SessionLocal() as db:
        try:
            connections = ensure_default_demo_connections(db)
            logger.info("seeded demo connections", extra={"structured_data": {"count": len(connections)}})
        except Exception as exc:  # pragma: no cover
            logger.warning("demo connection seed failed", extra={"structured_data": {"error": str(exc)}})
        try:
            market_data_service.sync_all_default_markets(db, timeframe="15m", limit=120)
        except Exception as exc:  # pragma: no cover
            logger.warning("market seed failed", extra={"structured_data": {"error": str(exc)}})
        try:
            strategy_service.ensure_default_strategies(db)
        except Exception as exc:  # pragma: no cover
            logger.warning("strategy seed failed", extra={"structured_data": {"error": str(exc)}})
        try:
            from app.portfolio.service import portfolio_service
            from app.models.entities import TradingAccount
            from sqlalchemy import select

            for account in db.execute(select(TradingAccount)).scalars().all():
                portfolio_service.capture_equity_snapshot(db, account.id)
        except Exception as exc:  # pragma: no cover
            logger.warning("equity snapshot seed failed", extra={"structured_data": {"error": str(exc)}})


@asynccontextmanager
async def lifespan(app: FastAPI):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logger.info("starting API", extra={"structured_data": {"env": settings.APP_ENV, "db": settings.resolved_database_url}})

    # Alembic migrations are authoritative; apply them automatically on an empty database.
    from app.db.bootstrap import ensure_schema

    bootstrapped = ensure_schema()
    logger.info("schema check", extra={"structured_data": bootstrapped})

    _seed_if_empty()

    # V1.1 §9.4.6: always enter RECOVERY_MODE and reconcile before any new entry is allowed.
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        try:
            status = recovery_manager.run_recovery(db)
            logger.info("recovery complete", extra={"structured_data": status})
        except Exception as exc:
            logger.exception("recovery failed", extra={"structured_data": {"error": str(exc)}})
            recovery_manager.force_block(f"Startup recovery failed: {exc}")

    yield
    logger.info("shutting down API")


app = FastAPI(
    title="AI Crypto Trading Terminal API",
    version="1.1.0",
    description=(
        "Personal AI crypto trading terminal backend (Binance / OKX, Spot + USDT Perpetual). "
        "AI proposes structured TradePlans; a deterministic Risk Engine approves them; the Execution Engine "
        "is the only component that talks to an exchange. Live trading is disabled by default."
    ),
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.WEB_URL, "http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


app.include_router(api_router, prefix="/api/v1")


# ------------------------------------------------------------------- health
@app.get("/health", tags=["health"])
def health_root() -> dict[str, Any]:
    """Backwards-compatible aggregate health endpoint (Sprint 0 acceptance)."""
    readiness = watchdog_service.check_readiness()
    return {
        "status": "ok" if readiness["status"] == "ready" else "degraded",
        "service": settings.APP_NAME,
        "version": "1.1.0",
        "environment": settings.APP_ENV,
        "database": readiness["dependencies"]["database"],
        "cache": readiness["dependencies"]["cache"],
        "recovery_state": readiness["recovery"]["state"],
        "trading_allowed": readiness["trading_allowed"],
        "live_trading_enabled": settings.LIVE_TRADING_ENABLED,
        "checked_at": utc_iso(),
    }


@app.get("/health/live", tags=["health"])
def health_live() -> dict[str, Any]:
    return watchdog_service.check_liveness()


@app.get("/health/ready", tags=["health"])
def health_ready() -> dict[str, Any]:
    return watchdog_service.check_readiness()


@app.get("/metrics", tags=["health"], response_class=PlainTextResponse)
def metrics() -> str:
    return watchdog_service.metrics_text()


# ---------------------------------------------------------------- websockets
def _register_ws(route: str) -> None:
    channel = route.rstrip("/").split("/")[-1]

    async def _handler(websocket: WebSocket) -> None:
        await event_bus.subscribe(channel, websocket)
        try:
            for event in event_bus.get_recent_events(channel, limit=10):
                await websocket.send_json(event)
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            event_bus.unsubscribe(channel, websocket)
        except Exception:
            event_bus.unsubscribe(channel, websocket)

    app.add_api_websocket_route(route, _handler, name=f"ws_{channel}")


for _channel in CHANNELS:
    _register_ws(f"/ws/{_channel}")


@app.get("/", include_in_schema=False)
def root() -> dict[str, Any]:
    return {
        "service": settings.APP_NAME,
        "version": "1.1.0",
        "docs": "/docs",
        "health": "/health",
        "api": "/api/v1",
        "websocket_channels": [f"/ws/{c}" for c in CHANNELS],
    }
