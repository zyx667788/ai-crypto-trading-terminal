"""Durable runtime coordination shared by the API and worker processes."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.precision import utc_now
from app.db.session import Base
from app.db.types import UTCDateTime


class RecoveryCheckpoint(Base):
    """The last committed recovery state; process memory is only a write cache."""

    __tablename__ = "recovery_checkpoints"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default="global")
    state: Mapped[str] = mapped_column(String(64), nullable=False, default="HOST_BOOT")
    blockers_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    steps_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    counts_json: Mapped[dict[str, int]] = mapped_column(JSON, nullable=False, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, onupdate=utc_now)


class WorkerHeartbeat(Base):
    """A worker's durable identity and health, visible from every other process."""

    __tablename__ = "worker_heartbeats"

    worker_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    role: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    hostname: Mapped[str] = mapped_column(String(128), nullable=False)
    pid: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="STARTING")
    last_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    runs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    details_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    stopping: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, onupdate=utc_now, index=True)
