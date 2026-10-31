from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.ai.service import ai_service
from app.core.events import event_bus
from app.db.session import get_db
from app.schemas.domain import StrategyCreateRequest, StrategyUpdateRequest
from app.strategies.service import strategy_service

router = APIRouter(tags=["strategies-and-ai"])


# ------------------------------------------------------------------ strategies
@router.get("/strategies")
def list_strategies(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return strategy_service.list_strategies(db)


@router.post("/strategies", status_code=201)
def create_strategy(payload: StrategyCreateRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        strategy = strategy_service.create_strategy(db, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return strategy_service.get_strategy(db, strategy.id)


@router.get("/strategies/{strategy_id}")
def get_strategy(strategy_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        return strategy_service.get_strategy(db, strategy_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.put("/strategies/{strategy_id}")
def update_strategy(strategy_id: str, payload: StrategyUpdateRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        strategy_service.update_strategy(db, strategy_id, payload)
        return strategy_service.get_strategy(db, strategy_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/strategies/{strategy_id}/start")
def start_strategy(strategy_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        return strategy_service.start_strategy(db, strategy_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/strategies/{strategy_id}/pause")
def pause_strategy(strategy_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        return strategy_service.pause_strategy(db, strategy_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/trade-signals")
def list_trade_signals(
    strategy_id: str | None = None,
    symbol: str | None = None,
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    return strategy_service.list_signals(db, strategy_id=strategy_id, symbol=symbol, status=status, limit=limit)


# ------------------------------------------------------------------------- AI
@router.get("/agents/status")
def get_agent_status(db: Session = Depends(get_db)) -> dict[str, Any]:
    from app.ai.provider import get_ai_provider

    payload = ai_service.get_agents_status(db)
    payload["ai_provider"] = get_ai_provider().describe
    return payload


@router.get("/agents/provider")
def get_ai_provider_status() -> dict[str, Any]:
    """Non-secret description of the configured AI provider (never returns the key)."""
    from app.ai.provider import get_ai_provider

    return get_ai_provider().describe


@router.post("/agents/provider/test")
def test_ai_provider() -> dict[str, Any]:
    """
    Round-trip a tiny JSON prompt to prove the provider is reachable and schema-capable.
    The exchange API keys are not involved and the provider key is never echoed.
    """
    from app.ai.provider import AIProviderError, get_ai_provider

    provider = get_ai_provider()
    if not provider.is_configured:
        raise HTTPException(status_code=400, detail="AI provider is not configured (missing AI_API_KEY / AI_BASE_URL).")
    try:
        completion = provider.complete_json(
            system_prompt=(
                "You are a JSON-only API. Reply with exactly one JSON object and nothing else. "
                "Never use markdown, code fences, or prose."
            ),
            user_payload={"task": 'Return exactly {"ok": true}'},
            temperature=0.0,
            max_tokens=128,
        )
        parsed = completion.as_json()
    except AIProviderError as exc:
        return {"ok": False, "error": type(exc).__name__, "detail": str(exc)[:300], "provider": provider.describe}
    return {
        "ok": True,
        "provider": provider.describe,
        "model": completion.model,
        "latency_ms": completion.latency_ms,
        "prompt_tokens": completion.prompt_tokens,
        "completion_tokens": completion.completion_tokens,
        "reply": parsed,
    }


@router.get("/agents/runs")
def list_agent_runs(
    run_type: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    return ai_service.list_agent_runs(db, run_type=run_type, limit=limit)


@router.get("/decisions")
def list_decisions(
    symbol: str | None = None,
    valid_only: bool = False,
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    return ai_service.list_decisions(db, symbol=symbol, valid_only=valid_only, limit=limit)


@router.post("/decisions/generate")
def generate_decision(
    simulate_invalid_json: bool = False,
    simulate_timeout: bool = False,
    signal_id: str | None = None,
    symbol: str | None = None,
    exchange: str | None = None,
    market_type: str | None = None,
    timeframe: str | None = None,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    Runs the 5-agent workflow. Fault-injection flags exist solely to prove Fail-Closed behaviour.
    """
    result = ai_service.generate_trade_plan(
        db,
        signal_id=signal_id,
        symbol=symbol,
        exchange=exchange,
        market_type=market_type,
        timeframe=timeframe,
        simulate_invalid_json=simulate_invalid_json,
        simulate_timeout=simulate_timeout,
    )
    if result is None:
        event_bus.publish_sync("agents", "ai.plan_blocked", {"reason": "NO_VALID_TRADEPLAN"}, source="ai")
        return {
            "status": "BLOCKED_FAIL_CLOSED",
            "trade_plan": None,
            "message": "AI output failed schema validation, timed out, or no candidate existed. New entries are blocked for this cycle.",
        }
    event_bus.publish_sync("agents", "ai.plan_generated", result.to_dict(), source="ai")
    return {"status": "OK", "trade_plan": result.to_dict()}
