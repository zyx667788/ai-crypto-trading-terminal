"""
Database bootstrap.

Migrations (Alembic) remain the single source of truth for schema changes.
On first run against an empty database this module applies them automatically so
that a fresh checkout is usable without a manual step, while still recording the
Alembic revision so future migrations apply correctly.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import inspect

from app.core.logging import get_logger

logger = get_logger("terminal.db.bootstrap")

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"

# Module-level record of the last bootstrap report
last_bootstrap_report: dict[str, Any] | None = None


def schema_exists() -> bool:
    from app.db.session import engine

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    return "alembic_version" in tables and "orders" in tables


def ensure_schema() -> dict[str, object]:
    """Apply every pending Alembic migration, including on an existing database."""
    global last_bootstrap_report
    from app.db.session import engine

    already_present = schema_exists()
    logger.info("applying Alembic migrations", extra={"structured_data": {"existing_schema": already_present}})
    from alembic import command
    from alembic.config import Config
    from alembic.migration import MigrationContext
    from alembic.script import ScriptDirectory

    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_INI.parent / "alembic"))
    # ConfigParser treats `%` as interpolation syntax, so a percent-encoded URL must be escaped.
    cfg.set_main_option("sqlalchemy.url", str(engine.url).replace("%", "%%"))

    try:
        command.upgrade(cfg, "head")

        # Verify applied revision matches alembic head
        script = ScriptDirectory.from_config(cfg)
        expected_heads = script.get_heads()
        if len(expected_heads) != 1:
            raise RuntimeError(f"Expected exactly 1 Alembic migration head, found {len(expected_heads)}: {expected_heads}")
        expected_head = expected_heads[0]

        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn)
            current_rev = ctx.get_current_revision()

        if current_rev != expected_head:
            raise RuntimeError(
                f"Alembic migration revision chain inconsistency: database is at revision '{current_rev}', "
                f"expected head '{expected_head}'"
            )

        report = {
            "bootstrapped": not already_present,
            "reason": "alembic upgrade head applied" if not already_present else "existing schema upgraded to head",
            "applied_revision": current_rev,
            "expected_head": expected_head,
        }
        last_bootstrap_report = report
        return report
    except Exception as exc:
        if isinstance(exc, RuntimeError) and "revision chain inconsistency" in str(exc):
            last_bootstrap_report = {"bootstrapped": False, "reason": str(exc)}
            raise
        if isinstance(exc, RuntimeError) and "Expected exactly 1 Alembic migration head" in str(exc):
            last_bootstrap_report = {"bootstrapped": False, "reason": str(exc)}
            raise
        logger.exception("automatic migration failed", extra={"structured_data": {"error": str(exc)}})
        report = {"bootstrapped": False, "reason": f"migration failed: {exc}"}
        last_bootstrap_report = report
        return report
