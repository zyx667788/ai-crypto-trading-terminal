from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.events import CHANNELS, event_bus
from app.core.recovery import recovery_manager
from app.core.watchdog import watchdog_service
from app.db.session import get_db

router = APIRouter(tags=["system"])


@router.get("/system/status")
def system_status() -> dict[str, Any]:
    return watchdog_service.status()


@router.get("/system/recovery")
def get_recovery(db: Session = Depends(get_db)) -> dict[str, Any]:
    return recovery_manager.get_status(db)


@router.post("/system/recovery/run")
def run_recovery(force: bool = False, db: Session = Depends(get_db)) -> dict[str, Any]:
    return recovery_manager.run_recovery(db, force=force)


@router.post("/system/recovery/acknowledge")
def acknowledge_recovery(reason: str = "Operator reviewed reconciliation and protective orders", db: Session = Depends(get_db)) -> dict[str, Any]:
    return recovery_manager.acknowledge_and_ready(reason, db=db)


@router.get("/system/events")
def system_events(limit: int = Query(default=50, ge=1, le=300), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    from sqlalchemy import select

    from app.models.entities import SystemEvent

    rows = db.execute(select(SystemEvent).order_by(SystemEvent.created_at.desc()).limit(limit)).scalars().all()
    return [
        {
            "id": e.id,
            "component": e.component,
            "event_type": e.event_type,
            "severity": e.severity,
            "payload": e.payload,
            "created_at": e.created_at,
        }
        for e in rows
    ]


@router.get("/system/alerts")
def system_alerts(limit: int = Query(default=50, ge=1, le=200)) -> list[dict[str, Any]]:
    return watchdog_service.list_alerts(limit=limit)


@router.get("/system/backups")
def list_backups() -> dict[str, Any]:
    return {
        "backups": watchdog_service.list_backups(),
        "retention": "7 daily + 4 weekly",
        "includes_plaintext_secrets": False,
    }


@router.post("/system/backups")
def create_backup(tag: str = "manual") -> dict[str, Any]:
    return watchdog_service.create_encrypted_backup(tag=tag)


@router.post("/system/backups/restore-check")
def restore_check() -> dict[str, Any]:
    return watchdog_service.verify_latest_backup_restore()


@router.get("/system/ws-channels")
def ws_channels(limit: int = 20) -> dict[str, Any]:
    return {
        "channels": list(CHANNELS),
        "recent_events": {ch: event_bus.get_recent_events(ch, limit=limit) for ch in CHANNELS},
        "status": event_bus.status(),
    }


@router.get("/system/notifications")
def notification_config() -> dict[str, Any]:
    return {
        "channels": watchdog_service.notification_channels,
        "external_heartbeat": {
            "configured": bool(watchdog_service.external_heartbeat_url),
            "note": "A whole-machine outage can only be detected by an external heartbeat service (Section 9.4.7).",
        },
        "thresholds": {
            "disk_free_pct_warn": 20,
            "memory_pressure_warn_pct": 80,
            "memory_pressure_critical_pct": 92,
            "order_unknown_timeout_minutes": 5,
            "backup_max_age_hours": 26,
        },
    }


@router.get("/system/rate-limits")
def exchange_rate_limits() -> dict[str, Any]:
    """Exchange REST budget per (exchange, market) bucket (§3.4)."""
    from app.exchanges.rate_limit import rate_limiter

    return {
        "buckets": rate_limiter.snapshot(),
        "note": "Weighted token buckets. Adapters block up to 5s for budget, then refuse the request rather than risk a ban.",
        "checked_at": __import__("app.core.precision", fromlist=["utc_iso"]).utc_iso(),
    }


@router.get("/system/config")
def public_config() -> dict[str, Any]:
    """Non-secret configuration visible to the terminal UI."""
    return {
        "app_name": settings.APP_NAME,
        "app_env": settings.APP_ENV,
        "default_environment": settings.DEFAULT_TRADING_ENVIRONMENT,
        "live_trading_enabled": settings.LIVE_TRADING_ENABLED,
        "live_max_order_notional_usdt": str(settings.LIVE_MAX_ORDER_NOTIONAL_USDT),
        "ai_provider": settings.AI_PROVIDER,
        "ai_model": settings.AI_MODEL,
        "ai_key_configured": bool(settings.AI_API_KEY),
        "ai_provider_detail": __import__("app.ai.provider", fromlist=["get_ai_provider"]).get_ai_provider().describe,
        "risk_defaults": {
            "max_risk_per_trade_pct": str(settings.DEFAULT_MAX_RISK_PER_TRADE_PCT),
            "max_daily_loss_pct": str(settings.DEFAULT_MAX_DAILY_LOSS_PCT),
            "max_total_exposure_pct": str(settings.DEFAULT_MAX_TOTAL_EXPOSURE_PCT),
            "max_symbol_exposure_pct": str(settings.DEFAULT_MAX_SYMBOL_EXPOSURE_PCT),
            "max_open_positions": settings.DEFAULT_MAX_OPEN_POSITIONS,
            "max_leverage": str(settings.DEFAULT_MAX_LEVERAGE),
            "max_slippage_bps": str(settings.DEFAULT_MAX_SLIPPAGE_BPS),
            "max_spread_bps": str(settings.DEFAULT_MAX_SPREAD_BPS),
            "max_data_stale_seconds": settings.DEFAULT_MAX_DATA_STALE_SECONDS,
        },
        "mac_mini_profile": {
            "target_hardware": "Apple Silicon Mac mini, 16GB unified memory",
            "residential_mode": "launchd + Homebrew native PostgreSQL/Redis",
            "docker_role": "development and integration testing only",
            "guest_cold_boot_note": "With FileVault enabled, automatic login is unavailable; LaunchAgents run only after a user session exists. A 100% unattended cold boot is NOT claimed.",
        },
    }
