from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from sqlalchemy import (
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.precision import utc_now
from app.db.session import Base
from app.db.types import UTCDateTime


def new_uuid() -> str:
    return str(uuid.uuid4())


class OutboxStatus(str, Enum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    DONE = "DONE"
    FAILED = "FAILED"
    DEAD = "DEAD"


class OutboxMessage(Base):
    """
    PostgreSQL Outbox model for cross-process, reliable delivery of execution/order tasks.
    """

    __tablename__ = "outbox_messages"
    __table_args__ = (
        UniqueConstraint("dedup_key", name="uq_outbox_messages_dedup_key"),
        Index("ix_outbox_claim", "topic", "status", "available_at"),
        Index("ix_outbox_lease", "status", "lease_expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    topic: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    aggregate_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    dedup_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=OutboxStatus.PENDING.value, index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    available_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utc_now, index=True)
    claimed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    claimed_by: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True, index=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utc_now, index=True)
    processed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
