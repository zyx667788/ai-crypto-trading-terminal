"""Small durable repository for recovery checkpoints and worker heartbeats."""
from __future__ import annotations

import os
import socket
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.precision import utc_iso, utc_now
from app.models.runtime import RecoveryCheckpoint, WorkerHeartbeat

RECOVERY_KEY = "global"

# Roles that must have at least one ONLINE worker for the terminal to be ready.
# The watchdog is deliberately NOT required: it is allowed to run as a
# cron/launchd one-shot that only fires occasionally, so its heartbeat age is
# not evidence of a broken terminal.
REQUIRED_WORKER_ROLES = ["market-worker", "execution-worker"]


def _utc_aware(value: Any) -> Any | None:
    """Treat stored naive datetimes as UTC (UTCDateTime stores UTC-naive)."""
    if value is None:
        return None
    from datetime import timezone

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _heartbeat_age_seconds(row: WorkerHeartbeat) -> float | None:
    stamp = row.updated_at or row.last_run_at
    if stamp is None:
        return None
    stamp = _utc_aware(stamp)
    return max(0.0, (utc_now() - stamp).total_seconds())


class RuntimeStateRepository:
    def load_recovery(self, db: Session) -> RecoveryCheckpoint | None:
        return db.get(RecoveryCheckpoint, RECOVERY_KEY)

    def save_recovery(self, db: Session, status: dict[str, Any]) -> dict[str, Any]:
        row = self.load_recovery(db)
        if row is None:
            row = RecoveryCheckpoint(id=RECOVERY_KEY, revision=0)
            db.add(row)
        row.state = str(status.get("state") or "RECOVERY_MODE")
        row.blockers_json = list(status.get("new_entries_blocked_due_to") or [])
        row.steps_json = list(status.get("steps") or [])[-100:]
        row.counts_json = dict(status.get("counts") or {})
        started = status.get("started_at")
        completed = status.get("completed_at")
        from datetime import datetime

        row.started_at = datetime.fromisoformat(started.replace("Z", "+00:00")) if isinstance(started, str) else None
        row.completed_at = datetime.fromisoformat(completed.replace("Z", "+00:00")) if isinstance(completed, str) else None
        row.revision = int(row.revision or 0) + 1
        row.updated_at = utc_now()
        db.commit()
        return {"revision": row.revision, "updated_at": utc_iso(row.updated_at)}

    @staticmethod
    def recovery_to_status(row: RecoveryCheckpoint) -> dict[str, Any]:
        return {
            "state": row.state,
            "ready": row.state == "READY" and not bool(row.blockers_json),
            "new_entries_blocked": bool(row.blockers_json),
            "new_entries_blocked_due_to": list(row.blockers_json or []),
            "steps": list(row.steps_json or []),
            "started_at": utc_iso(row.started_at) if row.started_at else None,
            "completed_at": utc_iso(row.completed_at) if row.completed_at else None,
            "counts": dict(row.counts_json or {}),
            "revision": row.revision,
            "updated_at": utc_iso(row.updated_at) if row.updated_at else None,
            "policy": {
                "auto_flatten_on_recovery": False,
                "auto_resume_live_entries": False,
                "requires_operator_confirmation_for_live": True,
            },
        }

    def heartbeat(
        self,
        db: Session,
        *,
        role: str,
        worker_id: str,
        status: str,
        duration_ms: int = 0,
        error: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        row = db.get(WorkerHeartbeat, worker_id)
        if row is None:
            row = WorkerHeartbeat(
                worker_id=worker_id,
                role=role,
                hostname=socket.gethostname(),
                pid=os.getpid(),
                runs=0,
                consecutive_failures=0,
            )
            db.add(row)
        now = utc_now()
        row.role = role
        row.hostname = socket.gethostname()
        row.pid = os.getpid()
        row.status = status
        row.last_run_at = now
        row.last_duration_ms = max(0, int(duration_ms))
        row.runs = int(row.runs or 0) + (0 if status in ("STARTING", "STOPPING") else 1)
        row.consecutive_failures = 0 if status in ("OK", "SKIPPED", "STARTING") else int(row.consecutive_failures or 0) + 1
        row.last_error = error[:2000] if error else None
        row.details_json = details or {}
        row.updated_at = now
        db.commit()
        return self.serialize_worker(row)

    @staticmethod
    def serialize_worker(row: WorkerHeartbeat) -> dict[str, Any]:
        age = _heartbeat_age_seconds(row)
        ttl = int(settings.WORKER_HEARTBEAT_TTL_SECONDS)
        failures = int(row.consecutive_failures or 0)
        stale = age is not None and age > ttl
        if age is None:
            liveness = "OFFLINE"
        elif stale:
            liveness = "STALE"
        elif failures >= 3:
            liveness = "FAILING"
        else:
            liveness = "ONLINE"
        return {
            "worker_id": row.worker_id,
            "role": row.role,
            "hostname": row.hostname,
            "pid": row.pid,
            "status": row.status,
            "last_run_at": utc_iso(row.last_run_at) if row.last_run_at else None,
            "last_duration_ms": row.last_duration_ms,
            "runs": row.runs,
            "consecutive_failures": row.consecutive_failures,
            "last_error": row.last_error,
            "details": dict(row.details_json or {}),
            "updated_at": utc_iso(row.updated_at) if row.updated_at else None,
            "age_seconds": age,
            "heartbeat_ttl_seconds": ttl,
            "stale": stale,
            "liveness": liveness,
            # "healthy" now means heartbeat liveness (ONLINE), not merely
            # "consecutive_failures < 3", so a SIGKILLed worker no longer
            # reports healthy forever.
            "healthy": liveness == "ONLINE",
        }

    def list_workers(self, db: Session, *, role: str | None = None) -> list[dict[str, Any]]:
        query = select(WorkerHeartbeat).order_by(WorkerHeartbeat.role, WorkerHeartbeat.worker_id)
        if role is not None:
            query = query.where(WorkerHeartbeat.role == role)
        rows = db.scalars(query).all()
        return [self.serialize_worker(row) for row in rows]

    @staticmethod
    def required_roles() -> list[str]:
        return list(REQUIRED_WORKER_ROLES)

    def readiness_summary(self, db: Session) -> dict[str, Any]:
        """Summarize whether every required worker role has an ONLINE worker.

        The watchdog is not in the required roles because it is allowed to run
        as a cron/launchd one-shot (its heartbeat may legitimately be old).
        """
        workers = self.list_workers(db)
        by_role: dict[str, list[dict[str, Any]]] = {}
        for w in workers:
            by_role.setdefault(w["role"], []).append(w)
        missing_roles = [r for r in REQUIRED_WORKER_ROLES if r not in by_role]
        stale_roles = [
            r
            for r in REQUIRED_WORKER_ROLES
            if r in by_role and not any(w["liveness"] == "ONLINE" for w in by_role[r])
        ]
        ready = not missing_roles and not stale_roles
        if ready:
            detail = "all required worker roles ONLINE"
        elif missing_roles:
            detail = "missing worker roles: " + ", ".join(missing_roles)
        else:
            detail = "stale worker roles: " + ", ".join(stale_roles)
        return {
            "ready": ready,
            "workers": workers,
            "missing_roles": missing_roles,
            "stale_roles": stale_roles,
            "detail": detail,
        }


runtime_state = RuntimeStateRepository()
