from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.events import event_bus
from app.core.recovery import recovery_manager
from app.db.session import get_db
from app.journal.service import journal_service
from app.schemas.domain import AutomationControlRequest, BacktestCreateRequest
from app.workers.scheduler import worker_manager

router = APIRouter(tags=["automation-analytics"])

LIVE_CONFIRMATION_PHRASE = "I UNDERSTAND LIVE TRADING RISK"


# ----------------------------------------------------------------- automation
@router.get("/automation/status")
def automation_status(db: Session = Depends(get_db)) -> dict[str, Any]:
    return {
        **worker_manager.get_automation_status(db),
        "recovery": recovery_manager.get_status(db),
        "workers": worker_manager.get_workers_status(db),
    }


@router.post("/automation/start")
def start_automation(payload: AutomationControlRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    if not settings.AUTO_TRADING_ENABLED:
        raise HTTPException(status_code=403, detail="Automation is disabled (AUTO_TRADING_ENABLED=false).")
    if payload.environment == "live":
        if not settings.LIVE_TRADING_ENABLED:
            raise HTTPException(
                status_code=403,
                detail="Live trading is disabled (LIVE_TRADING_ENABLED=false). Live must be explicitly enabled and confirmed by the operator.",
            )
        if (payload.live_confirmation_phrase or "") != LIVE_CONFIRMATION_PHRASE:
            raise HTTPException(status_code=403, detail=f"Live confirmation phrase must be exactly: {LIVE_CONFIRMATION_PHRASE}")
        checklist = worker_manager.live_preflight_checklist()
        if not checklist["ready"]:
            raise HTTPException(status_code=403, detail={"message": "Live pre-flight checklist incomplete.", "checklist": checklist})
    if not recovery_manager.is_ready(db):
        # Recovery must complete first; entering automation without a reconciled state is unsafe.
        raise HTTPException(status_code=409, detail={"message": "Recovery is not READY.", "recovery": recovery_manager.get_status(db)})
    try:
        result = worker_manager.start_automation(db, payload.environment)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    event_bus.publish_sync("agents", "automation.started", result, source="automation")
    return result


@router.post("/automation/pause")
def pause_automation(db: Session = Depends(get_db)) -> dict[str, Any]:
    result = worker_manager.pause_automation(db)
    event_bus.publish_sync("agents", "automation.paused", result, source="automation")
    return result


@router.post("/automation/run-cycle")
def run_automation_cycle(
    simulate_ai_invalid_json: bool = False,
    simulate_ai_timeout: bool = False,
    simulate_order_timeout: bool = False,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return worker_manager.run_automation_cycle(
        db,
        simulate_ai_invalid_json=simulate_ai_invalid_json,
        simulate_ai_timeout=simulate_ai_timeout,
        simulate_order_timeout=simulate_order_timeout,
    )


# ------------------------------------------------------------------ analytics
@router.get("/analytics/summary")
def analytics_summary(db: Session = Depends(get_db)) -> dict[str, Any]:
    return journal_service.get_analytics_summary(db)


@router.get("/analytics/equity-curve")
def analytics_equity_curve(limit: int = Query(default=500, ge=1, le=2000), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return journal_service.get_equity_curve(db, limit=limit)


@router.get("/analytics/strategy-performance")
def analytics_strategy_performance(db: Session = Depends(get_db)) -> dict[str, Any]:
    return journal_service.get_strategy_performance(db)


@router.get("/journal")
def list_journal(limit: int = Query(default=50, ge=1, le=200), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return journal_service.get_journal_entries(db, limit=limit)


@router.post("/backtests", status_code=201)
def create_backtest(payload: BacktestCreateRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    return journal_service.run_backtest(db, payload)


@router.get("/backtests")
def list_backtests(limit: int = Query(default=50, ge=1, le=200), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return journal_service.list_backtests(db, limit=limit)


@router.get("/backtests/{backtest_id}")
def get_backtest(backtest_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    result = journal_service.get_backtest(db, backtest_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Backtest not found")
    return result
