"""Database dialect and runtime multi-process safety policy.

Production deployments run multiple independent OS processes (API, market-worker,
execution-worker, recovery-watchdog). Because SQLite lacks cross-process row-level
locking (FOR UPDATE SKIP LOCKED) and transactional outbox claim semantics across
processes, running background workers against SQLite is unsafe by default.

This module enforces dialect safety at startup and provides URL redaction.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy.engine.url import make_url


class DatabasePolicyError(RuntimeError):
    """Raised when the database configuration violates multi-process safety rules."""


def dialect_of(url: str) -> str:
    """Return 'postgresql', 'sqlite', or the driver backend name from a database URL."""
    if not url:
        return "other"
    try:
        sa_url = make_url(url)
        backend = sa_url.get_backend_name()
        if backend in {"postgresql", "sqlite"}:
            return backend
        return backend or "other"
    except Exception:
        # Fallback heuristic if URL parsing fails
        lower = url.lower()
        if lower.startswith("sqlite"):
            return "sqlite"
        if lower.startswith("postgresql") or lower.startswith("postgres"):
            return "postgresql"
        return "other"


def redact_url(url: str) -> str:
    """Mask the password component of a database URL."""
    if not url:
        return ""
    try:
        sa_url = make_url(url)
        return sa_url.render_as_string(hide_password=True)
    except Exception:
        # Fallback using urllib.parse
        try:
            parsed = urlsplit(url)
            if parsed.password is not None:
                netloc = parsed.netloc
                user = parsed.username or ""
                host_port = ""
                if "@" in netloc:
                    host_port = netloc.split("@", 1)[1]
                new_netloc = f"{user}:***@{host_port}" if user else f":***@{host_port}"
                return urlunsplit((parsed.scheme, new_netloc, parsed.path, parsed.query, parsed.fragment))
        except Exception:
            pass
        return url


def worker_role_requires_postgres() -> bool:
    """Return whether worker roles strictly require PostgreSQL under current settings."""
    from app.core.config import settings

    if settings.DB_POLICY_ALLOW_SQLITE_IN_PRODUCTION:
        return False
    if settings.REQUIRE_DB_DIALECT == "sqlite":
        return False
    return True


def _get_migration_head() -> str:
    """Compute the expected migration head lazily, returning 'unknown' on error."""
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        alembic_ini = Path(__file__).resolve().parents[2] / "alembic.ini"
        if not alembic_ini.exists():
            return "unknown"
        cfg = Config(str(alembic_ini))
        cfg.set_main_option("script_location", str(alembic_ini.parent / "alembic"))
        script = ScriptDirectory.from_config(cfg)
        head = script.get_current_head()
        return str(head) if head is not None else "unknown"
    except Exception:
        return "unknown"


def assert_trading_runtime_database(*, role: str, allow_sqlite: bool = False) -> dict[str, Any]:
    """Assert that the configured database is safe for the specified runtime role.

    Parameters:
        role: Runtime role name, e.g. "api", "market-worker", "execution-worker", "recovery-watchdog".
        allow_sqlite: Explicit escape hatch (e.g. from --allow-unsafe-sqlite CLI flag).

    Returns:
        Report dict: {
            "dialect": str,
            "url": str (redacted),
            "role": str,
            "enforced": bool,
            "migration_head": str,
        }

    Raises:
        DatabasePolicyError: If the dialect is not permitted for the given role.
    """
    from app.core.config import settings

    url = settings.resolved_database_url
    dialect = dialect_of(url)
    redacted = redact_url(url)
    migration_head = _get_migration_head()

    is_worker = role != "api" and ("worker" in role.lower() or "watchdog" in role.lower())

    if dialect == "postgresql":
        return {
            "dialect": "postgresql",
            "url": redacted,
            "role": role,
            "enforced": True,
            "migration_head": migration_head,
        }

    if dialect == "sqlite":
        # Check explicit escape hatches first
        if allow_sqlite:
            return {
                "dialect": "sqlite",
                "url": redacted,
                "role": role,
                "enforced": False,
                "migration_head": migration_head,
            }

        if settings.DB_POLICY_ALLOW_SQLITE_IN_PRODUCTION:
            return {
                "dialect": "sqlite",
                "url": redacted,
                "role": role,
                "enforced": False,
                "migration_head": migration_head,
            }

        if settings.REQUIRE_DB_DIALECT == "sqlite":
            return {
                "dialect": "sqlite",
                "url": redacted,
                "role": role,
                "enforced": False,
                "migration_head": migration_head,
            }

        # For worker role: default settings APP_ENV="development" must NOT silently allow SQLite.
        # Worker role must be refused unless allow_sqlite=True or DB_POLICY_ALLOW_SQLITE_IN_PRODUCTION=True
        # (or REQUIRE_DB_DIALECT == "sqlite", handled above).
        if is_worker:
            msg = (
                f"Multi-process worker role '{role}' is not permitted to run on database dialect '{dialect}'. "
                f"SQLite does not provide cross-process transactional outbox locking (FOR UPDATE SKIP LOCKED) "
                f"and is NOT SAFE FOR MULTI-PROCESS execution. "
                f"Remediation: (1) Set DATABASE_URL to a postgresql+psycopg:// connection string "
                f"(e.g. postgresql+psycopg://user:pass@host:5432/dbname), (2) run 'alembic upgrade head', "
                f"or (3) pass --allow-unsafe-sqlite for local single-process experiments."
            )
            raise DatabasePolicyError(msg)

        # For non-worker roles (e.g. 'api'):
        # Allowed in test/development when ENFORCE_DB_DIALECT_IN_PRODUCTION is False or APP_ENV in test/dev
        app_env = (settings.APP_ENV or "").lower()
        if app_env in {"test", "development"} and not settings.ENFORCE_DB_DIALECT_IN_PRODUCTION:
            return {
                "dialect": "sqlite",
                "url": redacted,
                "role": role,
                "enforced": False,
                "migration_head": migration_head,
            }
        if app_env in {"test", "development"}:
            # API role permitted in development / test
            return {
                "dialect": "sqlite",
                "url": redacted,
                "role": role,
                "enforced": False,
                "migration_head": migration_head,
            }

        # In production for API role:
        msg = (
            f"Role '{role}' is not permitted on database dialect '{dialect}' in environment '{settings.APP_ENV}'. "
            f"Remediation: Set DATABASE_URL to a postgresql+psycopg:// URL and run 'alembic upgrade head'."
        )
        raise DatabasePolicyError(msg)

    # For other dialects:
    if settings.REQUIRE_DB_DIALECT == "any":
        return {
            "dialect": dialect,
            "url": redacted,
            "role": role,
            "enforced": False,
            "migration_head": migration_head,
        }

    msg = (
        f"Database dialect '{dialect}' is unsupported for role '{role}'. "
        f"Remediation: Set DATABASE_URL to a postgresql+psycopg:// connection string."
    )
    raise DatabasePolicyError(msg)
