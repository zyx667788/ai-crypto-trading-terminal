from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from sqlalchemy import DateTime, Numeric, String, TypeDecorator


class DecimalNumeric(TypeDecorator):
    """
    Stores monetary/price/quantity values as NUMERIC(28, 10) on PostgreSQL
    and exact string representation on SQLite, always returning Python `Decimal`.
    """

    impl = Numeric(28, 10)
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(Numeric(28, 10))
        return dialect.type_descriptor(String(64))

    def process_bind_param(self, value: Any, dialect) -> Any:
        if value is None:
            return None
        dec = Decimal(str(value))
        if dialect.name == "postgresql":
            return dec
        return format(dec, "f")

    def process_result_value(self, value: Any, dialect) -> Decimal | None:
        if value is None:
            return None
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))


class UTCDateTime(TypeDecorator):
    """Ensures all timestamps read/written through SQLAlchemy are timezone-aware UTC datetimes."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
