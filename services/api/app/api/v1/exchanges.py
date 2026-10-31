from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.exchanges.registry import (
    create_connection,
    delete_connection,
    ensure_default_demo_connections,
    get_account,
    get_account_balances,
    get_account_capabilities,
    get_connection,
    get_connection_status,
    list_accounts,
    list_connections,
    list_supported_exchanges,
    sync_connection,
    test_connection,
)
from app.schemas.domain import ExchangeConnectionCreate, ExchangeConnectionOut

router = APIRouter(tags=["exchanges"])


@router.get("/exchanges")
def get_supported_exchanges() -> list[dict[str, Any]]:
    return list_supported_exchanges()


@router.get("/exchange-connections", response_model=list[ExchangeConnectionOut])
def get_exchange_connections(db: Session = Depends(get_db)):
    ensure_default_demo_connections(db)
    return list_connections(db)


@router.post("/exchange-connections", response_model=ExchangeConnectionOut, status_code=201)
def post_exchange_connection(payload: ExchangeConnectionCreate, db: Session = Depends(get_db)):
    try:
        return create_connection(db, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/exchange-connections/{connection_id}/test")
def post_test_connection(connection_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    if get_connection(db, connection_id) is None:
        raise HTTPException(status_code=404, detail="Exchange connection not found")
    return test_connection(db, connection_id)


@router.get("/exchange-connections/{connection_id}/status")
def get_connection_status_endpoint(connection_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    if get_connection(db, connection_id) is None:
        raise HTTPException(status_code=404, detail="Exchange connection not found")
    return get_connection_status(db, connection_id)


@router.post("/exchange-connections/{connection_id}/sync")
def post_sync_connection(connection_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    if get_connection(db, connection_id) is None:
        raise HTTPException(status_code=404, detail="Exchange connection not found")
    return sync_connection(db, connection_id)


@router.delete("/exchange-connections/{connection_id}")
def remove_connection(connection_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    if get_connection(db, connection_id) is None:
        raise HTTPException(status_code=404, detail="Exchange connection not found")
    return delete_connection(db, connection_id)


@router.get("/accounts")
def get_accounts(exchange: str | None = None, db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    ensure_default_demo_connections(db)
    rows = list_accounts(db, exchange=exchange)
    return [
        {
            "id": a.id,
            "connection_id": a.connection_id,
            "exchange": a.exchange,
            "environment": a.environment,
            "account_type": a.account_type,
            "region": a.region,
            "capabilities": a.capabilities,
            "is_active": a.is_active,
            "trading_locked": a.trading_locked,
            "lock_reason": a.lock_reason,
            "sync_status": a.sync_status,
            "last_reconciled_at": a.last_reconciled_at,
        }
        for a in rows
    ]


@router.get("/accounts/{account_id}/balances")
def get_balances(account_id: str, db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    if get_account(db, account_id) is None:
        raise HTTPException(status_code=404, detail="Trading account not found")
    return [
        {
            "asset": b.asset,
            "total": str(b.total),
            "available": str(b.available),
            "locked": str(b.locked),
            "usd_value": str(b.usd_value),
            "observed_at": b.observed_at,
        }
        for b in get_account_balances(db, account_id)
    ]


@router.get("/accounts/{account_id}/capabilities")
def get_capabilities(account_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    if get_account(db, account_id) is None:
        raise HTTPException(status_code=404, detail="Trading account not found")
    return get_account_capabilities(db, account_id)
