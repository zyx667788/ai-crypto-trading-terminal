from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.precision import utc_iso, utc_now
from app.db.session import Base
from app.db.types import UTCDateTime


class AutomationControl(Base):
    """
    Singleton persistence entity for system automation control (V1.2 Stage 1).

    Tracks whether automated trading is running, paused, current environment,
    optimistic concurrency revision, audit information, and manual safety blocks.
    Default row is fail-closed.
    """
    __tablename__ = "automation_controls"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default="global")
    running: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    environment: Mapped[str] = mapped_column(String(32), nullable=False, default="demo")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_by: Mapped[str | None] = mapped_column(String(64), nullable=True, default="system")
    pause_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    manual_block_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, onupdate=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "running": self.running,
            "paused": self.paused,
            "environment": self.environment,
            "revision": self.revision,
            "updated_by": self.updated_by,
            "pause_reason": self.pause_reason,
            "manual_block_reason": self.manual_block_reason,
            "created_at": utc_iso(self.created_at) if self.created_at else None,
            "updated_at": utc_iso(self.updated_at) if self.updated_at else None,
        }
