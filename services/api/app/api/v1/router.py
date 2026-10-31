"""Aggregated /api/v1 router."""
from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import automation, exchanges, markets, risk_orders, strategies_ai, system

api_router = APIRouter()
api_router.include_router(exchanges.router)
api_router.include_router(markets.router)
api_router.include_router(strategies_ai.router)
api_router.include_router(risk_orders.router)
api_router.include_router(automation.router)
api_router.include_router(system.router)

__all__ = ["api_router"]
