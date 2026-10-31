from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.events import event_bus
from app.core.security import mask_secret
from app.db.session import get_db
from app.core.admin_auth import require_admin
from app.execution.engine import execution_engine
from app.models.entities import SystemEvent
from app.portfolio.service import portfolio_service
from app.risk.engine import risk_engine
from app.schemas.domain import (
    KillSwitchRequest,
    OrderSubmitRequest,
    ResetDailyLocksRequest,
    ResumeTradingRequest,
    RiskLimitsSchema,
)

router = APIRouter(tags=["risk-orders-positions"])


# ----------------------------------------------------------------------- risk
@router.get("/risk/profile")
def get_risk_profile(account_id: str | None = None, db: Session = Depends(get_db)) -> dict[str, Any]:
    return risk_engine.get_profile(db, account_id=account_id)


@router.put("/risk/profile")
def update_risk_profile(
    payload: RiskLimitsSchema,
    account_id: str | None = None,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return risk_engine.update_profile(db, payload, account_id=account_id)


@router.get("/risk/status")
def get_risk_status(account_id: str | None = None, db: Session = Depends(get_db)) -> dict[str, Any]:
    return risk_engine.get_risk_status(db, account_id=account_id)


@router.get("/risk/events")
def get_risk_events(limit: int = Query(default=50, ge=1, le=500), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return risk_engine.get_risk_events(db, limit=limit)


@router.post("/risk/kill-switch", dependencies=[Depends(require_admin)])
def activate_kill_switch(payload: KillSwitchRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    result = risk_engine.trigger_kill_switch(db, scope=payload.scope, reason=payload.reason)
    event_bus.publish_sync("risk", "risk.kill_switch_activated", result, source="risk")
    return result


@router.post("/risk/resume", dependencies=[Depends(require_admin)])
def resume_trading(payload: ResumeTradingRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        result = risk_engine.resume_trading(db, scope=payload.scope, confirmation_code=payload.confirmation_code, reason=payload.reason)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    event_bus.publish_sync("risk", "risk.resumed", result, source="risk")
    return result


@router.post("/risk/reset-daily-locks", dependencies=[Depends(require_admin)])
def reset_daily_locks(payload: ResetDailyLocksRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    """
    Operator acknowledgement for the daily-loss / loss-streak lock (§3.7 rule 8).
    Records a watermark; never deletes order, fill or journal history.
    """
    try:
        result = risk_engine.reset_daily_locks(
            db,
            account_id=payload.account_id,
            confirmation_code=payload.confirmation_code,
            reason=payload.reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    event_bus.publish_sync("risk", "risk.daily_locks_reset", result, source="risk")
    return result


@router.post("/risk/flatten", dependencies=[Depends(require_admin)])
def flatten_positions(account_id: str | None = None, db: Session = Depends(get_db)) -> dict[str, Any]:
    result = execution_engine.flatten_all_positions(db, account_id=account_id)
    event_bus.publish_sync("positions", "positions.flattened", result, source="execution")
    return result


# --------------------------------------------------------------------- orders
@router.post("/orders")
def create_order(payload: OrderSubmitRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    result = execution_engine.submit_order(db, payload)
    event_bus.publish_sync("orders", "order.submitted", result, source=payload.trade_plan.exchange)
    return result


@router.get("/orders")
def list_orders(
    account_id: str | None = None,
    status: str | None = None,
    symbol: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    return execution_engine.get_orders(db, account_id=account_id, status=status, symbol=symbol, limit=limit)


@router.get("/orders/{order_id}")
def get_order(order_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    order = execution_engine.get_order(db, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


@router.post("/orders/{order_id}/cancel")
def cancel_order(order_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        result = execution_engine.cancel_order(db, order_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    event_bus.publish_sync("orders", "order.canceled", result, source="execution")
    return result


@router.post("/orders/{order_id}/reconcile")
def reconcile_order(
    order_id: str,
    simulated_remote_status: str = Query(default="FILLED"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    try:
        result = execution_engine.reconcile_unknown_order(db, order_id, simulated_remote_status=simulated_remote_status)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    event_bus.publish_sync("orders", "order.reconciled", result, source="execution")
    return result


@router.get("/fills")
def list_fills(
    account_id: str | None = None,
    order_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    return execution_engine.get_fills(db, account_id=account_id, order_id=order_id, limit=limit)


# ------------------------------------------------------------------ positions
@router.get("/positions")
def list_positions(
    account_id: str | None = None,
    status: str = "OPEN",
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    return portfolio_service.list_positions(db, status=status) if not account_id else [
        p for p in portfolio_service.list_positions(db, status=status) if p["account_id"] == account_id
    ]


@router.post("/positions/{position_id}/close")
def close_position(position_id: str, reason: str = "MANUAL_CLOSE", db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        result = execution_engine.close_position(db, position_id, reason=reason)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    event_bus.publish_sync("positions", "position.closed", result, source="execution")
    return result


@router.get("/portfolio/summary")
def portfolio_summary(db: Session = Depends(get_db)) -> dict[str, Any]:
    return portfolio_service.get_portfolio_summary(db)


@router.get("/portfolio/transfers")
def list_transfers(limit: int = Query(default=100, ge=1, le=500), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return portfolio_service.list_transfers(db, limit=limit)


@router.post("/portfolio/reconcile")
def reconcile_portfolio(force_fix: bool = False, db: Session = Depends(get_db)) -> dict[str, Any]:
    result = portfolio_service.reconcile_all_accounts(db, force_fix=force_fix)
    event_bus.publish_sync("orders", "portfolio.reconciled", result, source="portfolio")
    return result
