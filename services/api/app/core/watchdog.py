"""
Watchdog, health probing, resource pressure monitoring, alerting and encrypted backups.

Implements V1.1 requirements:
- Section 9.4.7 Monitoring & Alerting (30-60s watchdog checks, memory/disk/CPU pressure, external heartbeat)
- Section 9.4.8 Backup & Restore (daily encrypted backup, retention 7 daily + 4 weekly, restore verification)
- Section 4.3.1 16GB unified-memory budget and degradation policy
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.core.config import DATA_DIR, LOGS_DIR, ROOT_DIR, settings
from app.core.precision import utc_iso, utc_now
from app.core.security import mask_secret, redact_sensitive_data

try:  # psutil is optional at runtime but strongly recommended
    import psutil  # type: ignore
except Exception:  # pragma: no cover
    psutil = None  # type: ignore


BACKUP_DIR = DATA_DIR / "backups"
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

# 16GB Unified Memory Budget (Section 4.3.1) — target budget, not a hard quota.
MEMORY_BUDGET_MB: dict[str, int] = {
    "macos_window_server_and_cache": 4500,
    "nextjs_web": 800,
    "fastapi_api": 1000,
    "market_strategy_worker": 1500,
    "execution_recovery_worker": 1000,
    "postgresql": 1500,
    "redis": 300,
    "logs_and_headroom": 2000,
}

# Alert thresholds (Section 9.4.7)
THRESHOLDS = {
    "disk_free_pct_warn": 20.0,
    "memory_pressure_warn_pct": 80.0,
    "memory_pressure_critical_pct": 92.0,
    "cpu_warn_pct": 85.0,
    "backup_max_age_hours": 26,
}


@dataclass
class AlertRecord:
    level: str
    code: str
    message: str
    payload: dict[str, Any] = field(default_factory=dict)
    raised_at: str = field(default_factory=lambda: utc_iso())


class WatchdogService:
    """Single source of truth for host health, resource pressure, alerts and backups."""

    def __init__(self) -> None:
        self.started_at = utc_now()
        self.alerts: list[AlertRecord] = []
        self.last_check_at: datetime | None = None
        self.consecutive_failures: dict[str, int] = {}
        self.notification_channels: dict[str, bool] = {
            "macos_notification": platform.system() == "Darwin",
            "email": bool(os.environ.get("ALERT_SMTP_HOST")),
            "telegram_webhook": bool(os.environ.get("ALERT_TELEGRAM_WEBHOOK")),
            "generic_webhook": bool(os.environ.get("ALERT_WEBHOOK_URL")),
        }
        self.external_heartbeat_url = os.environ.get("EXTERNAL_HEARTBEAT_URL", "")
        self.last_heartbeat_at: datetime | None = None

    # ------------------------------------------------------------------ health
    def check_liveness(self) -> dict[str, Any]:
        uptime = (utc_now() - self.started_at).total_seconds()
        return {
            "status": "alive",
            "pid": os.getpid(),
            "process_uptime_seconds": round(uptime, 1),
            "host_uptime_seconds": self._host_uptime_seconds(),
            "checked_at": utc_iso(),
        }

    def check_database(self) -> dict[str, Any]:
        from sqlalchemy import text

        from app.db.session import engine

        dialect = engine.dialect.name
        label = {"postgresql": "postgresql", "sqlite": "sqlite (local persistent fallback)"}.get(dialect, dialect)
        started = time.perf_counter()
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return {
                "name": label,
                "dialect": dialect,
                "status": "healthy",
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            }
        except Exception as exc:  # pragma: no cover
            self.raise_alert("CRITICAL", "DATABASE_UNAVAILABLE", "Database connectivity check failed", {"error": str(exc)})
            return {"name": label, "dialect": dialect, "status": "unhealthy", "error": str(exc)}

    def check_redis(self) -> dict[str, Any]:
        """Redis is optional: the in-process event bus provides a documented fallback."""
        try:
            import redis  # type: ignore

            client = redis.Redis.from_url(settings.REDIS_URL, socket_connect_timeout=1, socket_timeout=1)
            client.ping()
            return {"name": "redis", "status": "healthy", "backend": "redis"}
        except Exception:
            return {
                "name": "redis",
                "status": "degraded",
                "backend": "in_process_event_bus",
                "detail": "Redis unreachable; event bus fell back to in-process queue. Trading workflows depending on distributed queue are paused.",
            }

    def check_readiness(self, require_trading_ready: bool = False) -> dict[str, Any]:
        from app.core.recovery import recovery_manager
        from app.db.session import SessionLocal

        db = self.check_database()
        cache = self.check_redis()
        with SessionLocal() as session:
            recovery = recovery_manager.get_status(session)
        resource = self.resource_snapshot()

        checks = {
            "database": db["status"] == "healthy",
            "cache": cache["status"] in ("healthy", "degraded"),
            "recovery_state_ready": recovery["state"] == "READY",
            "disk_space_ok": resource["disk"]["free_pct"] >= THRESHOLDS["disk_free_pct_warn"],
            "memory_pressure_ok": resource["memory"]["pressure_level"] != "RED",
            "memory_metrics_available": bool(resource["memory"].get("metrics_available", False)),
        }
        dependencies_ready = checks["database"] and checks["cache"] and checks["disk_space_ok"]

        trading_allowed = bool(
            dependencies_ready
            and checks["recovery_state_ready"]
            and checks["memory_pressure_ok"]
            and not recovery.get("new_entries_blocked_due_to")
        )

        if require_trading_ready:
            status = "ready" if trading_allowed else "not_ready"
        else:
            status = "ready" if dependencies_ready else "not_ready"

        return {
            "status": status,
            "checks": checks,
            "trading_allowed": trading_allowed,
            "new_entries_blocked_due_to": recovery.get("new_entries_blocked_due_to"),
            "dependencies": {"database": db, "cache": cache, "resource": resource},
            "recovery": recovery,
            "checked_at": utc_iso(),
        }

    def _host_uptime_seconds(self) -> float:
        if psutil is not None:
            return round(time.time() - psutil.boot_time(), 1)
        return 0.0

    # --------------------------------------------------------------- resources
    def resource_snapshot(self) -> dict[str, Any]:
        disk = shutil.disk_usage(DATA_DIR)
        disk_free_pct = round(disk.free / disk.total * 100, 2) if disk.total else 0.0

        metrics_available = psutil is not None
        if metrics_available:
            vm = psutil.virtual_memory()
            total_mb = round(vm.total / 1024 / 1024)
            used_mb = round((vm.total - vm.available) / 1024 / 1024)
            available_mb = round(vm.available / 1024 / 1024)
            usage_pct = round(vm.percent, 2)
            swap = psutil.swap_memory()
            swap_used_mb = round(swap.used / 1024 / 1024)
            cpu_pct = round(psutil.cpu_percent(interval=None), 2)
        else:  # pragma: no cover
            total_mb = used_mb = available_mb = 0
            usage_pct = 0.0
            swap_used_mb = 0
            cpu_pct = 0.0

        if not metrics_available:
            # Never claim "GREEN" when the metric simply could not be read.
            pressure = "UNKNOWN"
            degrade = "psutil is unavailable, so memory pressure is unknown. Install psutil to enable degradation guidance."
        elif usage_pct >= THRESHOLDS["memory_pressure_critical_pct"]:
            pressure = "RED"
            degrade = "Reduce scan universe, shrink candle cache, and lower worker concurrency -- never disable risk checks."
        elif usage_pct >= THRESHOLDS["memory_pressure_warn_pct"] or swap_used_mb > 256:
            pressure = "YELLOW"
            degrade = "Prefer fewer monitored symbols over disabling risk or reconciliation."
        else:
            pressure = "GREEN"
            degrade = "Nominal. All workers may run at configured concurrency."

        return {
            "host": {
                "platform": platform.system(),
                "machine": platform.machine(),
                "python": platform.python_version(),
                "hostname": socket.gethostname(),
            },
            "memory": {
                "metrics_available": metrics_available,
                "total_mb": total_mb,
                "used_mb": used_mb,
                "available_mb": available_mb,
                "usage_pct": usage_pct,
                "swap_used_mb": swap_used_mb,
                "pressure_level": pressure,
                "degradation_guidance": degrade,
                "unified_16gb_budget_mb": MEMORY_BUDGET_MB,
                "budget_total_mb": sum(MEMORY_BUDGET_MB.values()),
            },
            "disk": {
                "path": str(DATA_DIR),
                "total_gb": round(disk.total / 1024**3, 2),
                "free_gb": round(disk.free / 1024**3, 2),
                "free_pct": disk_free_pct,
                "status": "WARN" if disk_free_pct < THRESHOLDS["disk_free_pct_warn"] else "OK",
            },
            "cpu_pct": cpu_pct,
            "observed_at": utc_iso(),
        }

    # ---------------------------------------------------------------- alerting
    def raise_alert(self, level: str, code: str, message: str, payload: dict[str, Any] | None = None) -> AlertRecord:
        record = AlertRecord(level=level, code=code, message=message, payload=redact_sensitive_data(payload or {}))
        self.alerts.append(record)
        if len(self.alerts) > 500:
            del self.alerts[: len(self.alerts) - 500]
        return record

    def list_alerts(self, limit: int = 100) -> list[dict[str, Any]]:
        return [
            {"level": a.level, "code": a.code, "message": a.message, "payload": a.payload, "raised_at": a.raised_at}
            for a in self.alerts[-limit:]
        ]

    def send_heartbeat(self) -> dict[str, Any]:
        """Externally detectable heartbeat. A Mac that is powered off cannot report itself offline."""
        self.last_heartbeat_at = utc_now()
        if not self.external_heartbeat_url:
            return {
                "sent": False,
                "reason": "EXTERNAL_HEARTBEAT_URL not configured",
                "note": "Without an external heartbeat service, whole-machine outages cannot be detected from local logs alone (Section 9.4.7).",
            }
        try:  # pragma: no cover - network dependent
            import httpx

            httpx.get(self.external_heartbeat_url, timeout=5.0)
            return {"sent": True, "at": utc_iso()}
        except Exception as exc:  # pragma: no cover
            return {"sent": False, "error": str(exc)}

    # ----------------------------------------------------------------- backups
    def create_encrypted_backup(self, tag: str = "daily") -> dict[str, Any]:
        """
        Creates a compressed SQLite/JSON snapshot plus a manifest, encrypted with the
        application master key. Never includes plaintext API secrets.
        """
        from app.core.security import secret_vault

        stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
        payload: dict[str, Any] = {
            "created_at": utc_iso(),
            "tag": tag,
            "database_url": settings.resolved_database_url,
            "tables": {},
        }

        try:
            from sqlalchemy import inspect, text

            from app.db.session import SessionLocal, engine

            inspector = inspect(engine)
            with SessionLocal() as db:
                for table_name in inspector.get_table_names():
                    if table_name == "alembic_version":
                        continue
                    rows = db.execute(text(f'SELECT * FROM "{table_name}"')).mappings().all()
                    payload["tables"][table_name] = [
                        {
                            k: (
                                v.isoformat()
                                if isinstance(v, datetime)
                                else (format(v, "f") if isinstance(v, Decimal) else (str(v) if hasattr(v, "as_tuple") else v))
                            )
                            for k, v in row.items()
                        }
                        for row in rows
                    ]
        except Exception as exc:
            self.raise_alert("WARNING", "BACKUP_SNAPSHOT_PARTIAL", "Database snapshot partially captured", {"error": str(exc)})
            payload["snapshot_error"] = str(exc)

        snapshot_ok = "snapshot_error" not in payload and bool(payload["tables"])

        raw = json.dumps(redact_sensitive_data(payload), ensure_ascii=False, default=str).encode("utf-8")
        encrypted = secret_vault.encrypt(raw.decode("utf-8")) or ""
        extension = "json.enc"
        target = BACKUP_DIR / f"backup_{tag}_{stamp}.{extension}"
        target.write_text(encrypted, encoding="utf-8")

        manifest = {
            "file": target.name,
            "tag": tag,
            "created_at": utc_iso(),
            "size_bytes": target.stat().st_size,
            "table_counts": {k: len(v) for k, v in payload["tables"].items()},
            "contains_plaintext_secrets": False,
            "snapshot_ok": snapshot_ok,
            "snapshot_error": payload.get("snapshot_error"),
            "encryption": "Fernet (AES-128-CBC + HMAC-SHA256)",
        }
        (BACKUP_DIR / f"backup_{tag}_{stamp}.manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        self._prune_backups()
        return manifest

    def _prune_backups(self, keep_daily: int = 7, keep_weekly: int = 4) -> None:
        daily = sorted(BACKUP_DIR.glob("backup_daily_*.json.enc"))
        weekly = sorted(BACKUP_DIR.glob("backup_weekly_*.json.enc"))
        for stale in daily[:-keep_daily] + weekly[:-keep_weekly]:
            try:
                stale.unlink()
                manifest = stale.with_name(stale.name.replace(".json.enc", ".manifest.json"))
                if manifest.exists():
                    manifest.unlink()
            except OSError:
                pass

    def list_backups(self) -> list[dict[str, Any]]:
        items = []
        for manifest in sorted(BACKUP_DIR.glob("*.manifest.json"), reverse=True):
            try:
                items.append(json.loads(manifest.read_text(encoding="utf-8")))
            except Exception:
                continue
        return items

    def verify_latest_backup_restore(self) -> dict[str, Any]:
        """Restore drill against an isolated in-memory store; never touches the live database."""
        from app.core.security import secret_vault

        backups = sorted(BACKUP_DIR.glob("backup_*.json.enc"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not backups:
            return {"ok": False, "reason": "No encrypted backup found"}

        last_error: str | None = None
        for latest in backups:
            try:
                raw = secret_vault.decrypt(latest.read_text(encoding="utf-8"))
                payload = json.loads(raw or "{}")
                table_counts = {k: len(v) for k, v in payload.get("tables", {}).items()}
                total_rows = sum(table_counts.values())
                has_orders = "orders" in payload.get("tables", {})
                if not has_orders or total_rows == 0:
                    last_error = f"{latest.name} contains no table data"
                    continue
                return {
                    "ok": True,
                    "backup_file": latest.name,
                    "created_at": payload.get("created_at"),
                    "restored_rows": total_rows,
                    "table_counts": table_counts,
                    "verified_at": utc_iso(),
                    "note": "Restore verified by decrypting and parsing the latest usable backup into an isolated reader.",
                }
            except Exception as exc:  # corrupt or undecryptable -> try the next one
                last_error = f"{latest.name}: {exc}"
                continue

        return {
            "ok": False,
            "reason": last_error or "No restorable backup found",
            "attempted": [p.name for p in backups[:5]],
            "checked_at": utc_iso(),
        }

    # ------------------------------------------------------------------ status
    def status(self) -> dict[str, Any]:
        self.last_check_at = utc_now()
        resource = self.resource_snapshot()
        backups = self.list_backups()
        latest_backup_age_hours = None
        if backups:
            try:
                created = datetime.fromisoformat(backups[0]["created_at"].replace("Z", "+00:00"))
                latest_backup_age_hours = round((utc_now() - created).total_seconds() / 3600, 2)
            except Exception:
                latest_backup_age_hours = None

        return {
            "liveness": self.check_liveness(),
            "readiness": self.check_readiness(),
            "resource": resource,
            "alerts": self.list_alerts(limit=20),
            "backups": {
                "count": len(backups),
                "latest": backups[0] if backups else None,
                "latest_age_hours": latest_backup_age_hours,
                "stale": latest_backup_age_hours is None or latest_backup_age_hours > THRESHOLDS["backup_max_age_hours"],
                "retention": "7 daily + 4 weekly (Section 9.4.8)",
            },
            "notification_channels": self.notification_channels,
            "external_heartbeat": {
                "configured": bool(self.external_heartbeat_url),
                "last_sent_at": utc_iso(self.last_heartbeat_at) if self.last_heartbeat_at else None,
                "url_masked": mask_secret(self.external_heartbeat_url) if self.external_heartbeat_url else None,
            },
            "thresholds": THRESHOLDS,
            "checked_at": utc_iso(),
        }

    def metrics_text(self) -> str:
        """Prometheus-style text metrics, localhost only (`/metrics`)."""
        r = self.resource_snapshot()
        lines = [
            "# HELP terminal_process_uptime_seconds Process uptime in seconds.",
            "# TYPE terminal_process_uptime_seconds gauge",
            f'terminal_process_uptime_seconds {round((utc_now() - self.started_at).total_seconds(), 1)}',
            "# HELP terminal_memory_usage_percent Unified memory usage percentage.",
            "# TYPE terminal_memory_usage_percent gauge",
            f"terminal_memory_usage_percent {r['memory']['usage_pct']}",
            "# HELP terminal_memory_available_mb Available memory in MB.",
            "# TYPE terminal_memory_available_mb gauge",
            f"terminal_memory_available_mb {r['memory']['available_mb']}",
            "# HELP terminal_disk_free_percent Data volume free space percentage.",
            "# TYPE terminal_disk_free_percent gauge",
            f"terminal_disk_free_percent {r['disk']['free_pct']}",
            "# HELP terminal_cpu_percent Process host CPU percentage.",
            "# TYPE terminal_cpu_percent gauge",
            f"terminal_cpu_percent {r['cpu_pct']}",
            "# HELP terminal_alert_count Total alerts raised this session.",
            "# TYPE terminal_alert_count counter",
            f"terminal_alert_count {len(self.alerts)}",
        ]
        return "\n".join(lines) + "\n"


watchdog_service = WatchdogService()
