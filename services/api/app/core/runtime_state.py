"""Small durable repository for recovery checkpoints and worker heartbeats."""
from __future__ import annotations

import os
import socket
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.precision import utc_iso, utc_now
from app.models.runtime import RecoveryCheckpoint, WorkerHeartbeat

RECOVERY_KEY = "global"


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
            "healthy": int(row.consecutive_failures or 0) < 3,
        }

    def list_workers(self, db: Session) -> list[dict[str, Any]]:
        rows = db.scalars(select(WorkerHeartbeat).order_by(WorkerHeartbeat.role, WorkerHeartbeat.worker_id)).all()
        return [self.serialize_worker(row) for row in rows]


runtime_state = RuntimeStateRepository()
