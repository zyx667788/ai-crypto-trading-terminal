"""
Database bootstrap.

Migrations (Alembic) remain the single source of truth for schema changes.
On first run against an empty database this module applies them automatically so
that a fresh checkout is usable without a manual step, while still recording the
Alembic revision so future migrations apply correctly.
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import inspect

from app.core.logging import get_logger

logger = get_logger("terminal.db.bootstrap")

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


def schema_exists() -> bool:
    from app.db.session import engine

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    return "alembic_version" in tables and "orders" in tables


def ensure_schema() -> dict[str, object]:
    """Apply Alembic migrations when the database has not been initialised yet."""
    from app.db.session import engine

    if schema_exists():
        return {"bootstrapped": False, "reason": "schema already present"}

    logger.info("empty database detected; applying Alembic migrations")
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_INI.parent / "alembic"))
    # ConfigParser treats `%` as interpolation syntax, so a percent-encoded URL must be escaped.
    cfg.set_main_option("sqlalchemy.url", str(engine.url).replace("%", "%%"))

    try:
        command.upgrade(cfg, "head")
        return {"bootstrapped": True, "reason": "alembic upgrade head applied"}
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("automatic migration failed", extra={"structured_data": {"error": str(exc)}})
        return {"bootstrapped": False, "reason": f"migration failed: {exc}"}
