"""Worker heartbeat liveness tests (P1-5 fix)."""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.core.config import settings
from app.core.precision import utc_now
from app.core.runtime_state import runtime_state
from app.models.runtime import WorkerHeartbeat

TTL = int(settings.WORKER_HEARTBEAT_TTL_SECONDS)


def make_worker(db, *, worker_id: str, role: str, updated_at=None, consecutive_failures: int = 0, status: str = "OK"):
    row = WorkerHeartbeat(
        worker_id=worker_id,
        role=role,
        hostname="test-host",
        pid=1234,
        runs=1,
        consecutive_failures=consecutive_failures,
        status=status,
        last_run_at=updated_at,
        last_duration_ms=10,
    )
    row.updated_at = updated_at
    db.add(row)
    db.commit()
    return row


@pytest.fixture()
def clean_workers(db):
    yield db
    db.query(WorkerHeartbeat).delete()
    db.commit()


def test_fresh_heartbeat_is_online(db, clean_workers):
    make_worker(db, worker_id="w1", role="market-worker", updated_at=utc_now())
    w = runtime_state.list_workers(db)[0]
    assert w["liveness"] == "ONLINE"
    assert w["healthy"] is True
    assert w["stale"] is False
    assert w["heartbeat_ttl_seconds"] == TTL
    # Only assert the age is well inside the TTL: a full-suite run can take several
    # seconds between writing the row and reading it back, so a hard 5s bound flakes.
    assert w["age_seconds"] is not None
    assert 0 <= w["age_seconds"] < TTL / 2


def test_old_heartbeat_is_stale(db, clean_workers):
    make_worker(db, worker_id="w2", role="execution-worker", updated_at=utc_now() - timedelta(seconds=TTL * 10))
    w = runtime_state.list_workers(db)[0]
    assert w["liveness"] == "STALE"
    assert w["healthy"] is False
    assert w["stale"] is True
    assert w["age_seconds"] > TTL


def test_missing_updated_at_is_offline(db, clean_workers):
    # updated_at is NOT NULL in the schema, so a row without any timestamp
    # cannot be persisted; exercise the OFFLINE branch on a transient ORM row.
    row = WorkerHeartbeat(
        worker_id="w3",
        role="market-worker",
        hostname="test-host",
        pid=1234,
        runs=1,
        consecutive_failures=0,
        status="OK",
        last_run_at=None,
        updated_at=None,
        last_duration_ms=10,
    )
    w = runtime_state.serialize_worker(row)
    assert w["liveness"] == "OFFLINE"
    assert w["healthy"] is False
    assert w["age_seconds"] is None


def test_fresh_with_failures_is_failing(db, clean_workers):
    make_worker(
        db,
        worker_id="w4",
        role="execution-worker",
        updated_at=utc_now(),
        consecutive_failures=3,
        status="FAIL",
    )
    w = runtime_state.list_workers(db)[0]
    assert w["liveness"] == "FAILING"
    assert w["healthy"] is False
    assert w["stale"] is False


def test_readiness_missing_execution_worker(db, clean_workers):
    make_worker(db, worker_id="m1", role="market-worker", updated_at=utc_now())
    summary = runtime_state.readiness_summary(db)
    assert summary["ready"] is False
    assert "execution-worker" in summary["missing_roles"]
    assert summary["stale_roles"] == []


def test_readiness_stale_execution_worker(db, clean_workers):
    make_worker(db, worker_id="m1", role="market-worker", updated_at=utc_now())
    make_worker(
        db,
        worker_id="e1",
        role="execution-worker",
        updated_at=utc_now() - timedelta(seconds=TTL * 10),
    )
    summary = runtime_state.readiness_summary(db)
    assert summary["ready"] is False
    assert summary["missing_roles"] == []
    assert "execution-worker" in summary["stale_roles"]


def test_readiness_ready_when_both_required_fresh(db, clean_workers):
    make_worker(db, worker_id="m1", role="market-worker", updated_at=utc_now())
    make_worker(db, worker_id="e1", role="execution-worker", updated_at=utc_now())
    summary = runtime_state.readiness_summary(db)
    assert summary["ready"] is True
    assert summary["missing_roles"] == []
    assert summary["stale_roles"] == []
    assert "all required worker roles ONLINE" in summary["detail"]


def test_readiness_no_db_session():
    assert runtime_state is not None
    from app.workers.scheduler import worker_manager

    assert worker_manager.get_worker_readiness(None) == {"ready": None, "detail": "no database session"}
