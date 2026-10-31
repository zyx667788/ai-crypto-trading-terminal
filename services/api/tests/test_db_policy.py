"""Tests for database dialect policy and multi-process startup protection."""
from __future__ import annotations

import sys
import pytest

from app.core.config import settings
from app.core.db_policy import (
    DatabasePolicyError,
    assert_trading_runtime_database,
    dialect_of,
    redact_url,
    worker_role_requires_postgres,
)
from app.workers import runner


def test_dialect_of():
    assert dialect_of("postgresql+psycopg://user:pass@localhost:5432/db") == "postgresql"
    assert dialect_of("postgresql://user:pass@localhost:5432/db") == "postgresql"
    assert dialect_of("sqlite:///data/terminal.db") == "sqlite"
    assert dialect_of("sqlite:///:memory:") == "sqlite"
    assert dialect_of("mysql+pymysql://user:pass@localhost/db") == "mysql"
    assert dialect_of("") == "other"


def test_redact_url():
    url = "postgresql+psycopg://terminal_user:secret_password@127.0.0.1:5432/ai_crypto_terminal"
    redacted = redact_url(url)
    assert "secret_password" not in redacted
    assert "***" in redacted
    assert "terminal_user" in redacted

    sqlite_url = "sqlite:///data/terminal.db"
    assert redact_url(sqlite_url) == sqlite_url


def test_sqlite_worker_refused_with_database_policy_error(monkeypatch):
    """sqlite + role='execution-worker' -> DatabasePolicyError with postgresql and remediation."""
    monkeypatch.setattr(settings, "DATABASE_URL", "sqlite:///data/terminal.db")
    monkeypatch.setattr(settings, "REQUIRE_DB_DIALECT", "postgresql")
    monkeypatch.setattr(settings, "DB_POLICY_ALLOW_SQLITE_IN_PRODUCTION", False)

    with pytest.raises(DatabasePolicyError) as exc_info:
        assert_trading_runtime_database(role="execution-worker", allow_sqlite=False)

    msg = str(exc_info.value)
    assert "sqlite" in msg.lower()
    assert "execution-worker" in msg
    assert "postgresql" in msg.lower()
    assert "alembic upgrade head" in msg
    assert "--allow-unsafe-sqlite" in msg


def test_sqlite_worker_with_allow_sqlite_accepted(monkeypatch):
    """sqlite + role='execution-worker' + allow_sqlite=True -> returns report, no raise."""
    monkeypatch.setattr(settings, "DATABASE_URL", "sqlite:///data/terminal.db")
    monkeypatch.setattr(settings, "REQUIRE_DB_DIALECT", "postgresql")
    monkeypatch.setattr(settings, "DB_POLICY_ALLOW_SQLITE_IN_PRODUCTION", False)

    report = assert_trading_runtime_database(role="execution-worker", allow_sqlite=True)
    assert report["dialect"] == "sqlite"
    assert report["role"] == "execution-worker"
    assert report["enforced"] is False
    assert "migration_head" in report


def test_postgresql_url_accepted_for_worker(monkeypatch):
    """postgresql URL -> accepted for worker."""
    monkeypatch.setattr(
        settings,
        "DATABASE_URL",
        "postgresql+psycopg://terminal_user:pass123@127.0.0.1:5432/ai_crypto_terminal",
    )
    monkeypatch.setattr(settings, "REQUIRE_DB_DIALECT", "postgresql")

    report = assert_trading_runtime_database(role="execution-worker", allow_sqlite=False)
    assert report["dialect"] == "postgresql"
    assert report["role"] == "execution-worker"
    assert report["enforced"] is True
    assert "pass123" not in report["url"]


def test_sqlite_api_role_accepted_in_test_env(monkeypatch):
    """sqlite + role='api' + APP_ENV='test' -> accepted."""
    monkeypatch.setattr(settings, "DATABASE_URL", "sqlite:///data/terminal.db")
    monkeypatch.setattr(settings, "APP_ENV", "test")

    report = assert_trading_runtime_database(role="api", allow_sqlite=False)
    assert report["dialect"] == "sqlite"
    assert report["role"] == "api"


def test_runner_main_returns_2_on_sqlite_worker_without_flag(monkeypatch, capsys):
    """runner.main() returns 2 on sqlite worker without the flag."""
    monkeypatch.setattr(settings, "DATABASE_URL", "sqlite:///data/terminal.db")
    monkeypatch.setattr(settings, "REQUIRE_DB_DIALECT", "postgresql")
    monkeypatch.setattr(settings, "DB_POLICY_ALLOW_SQLITE_IN_PRODUCTION", False)
    monkeypatch.setattr(sys, "argv", ["runner", "--role", "execution-worker"])

    called = []
    monkeypatch.setattr(
        runner.worker_manager,
        "run_background_loop",
        lambda *args, **kwargs: called.append((args, kwargs)),
    )

    exit_code = runner.main()
    assert exit_code == 2
    assert len(called) == 0

    stderr = capsys.readouterr().err
    assert "DatabasePolicyError" in stderr
    assert "NOT SAFE FOR MULTI-PROCESS" in stderr


def test_runner_main_succeeds_with_allow_unsafe_sqlite(monkeypatch, capsys):
    """runner.main() succeeds and calls run_background_loop when --allow-unsafe-sqlite is set."""
    monkeypatch.setattr(settings, "DATABASE_URL", "sqlite:///data/terminal.db")
    monkeypatch.setattr(sys, "argv", ["runner", "--role", "execution-worker", "--allow-unsafe-sqlite"])

    called = []
    monkeypatch.setattr(
        runner.worker_manager,
        "run_background_loop",
        lambda *args, **kwargs: called.append((args, kwargs)),
    )

    exit_code = runner.main()
    assert exit_code == 0
    assert len(called) == 1
    stdout = capsys.readouterr().out
    assert "Database policy report:" in stdout


def test_worker_role_requires_postgres(monkeypatch):
    monkeypatch.setattr(settings, "DB_POLICY_ALLOW_SQLITE_IN_PRODUCTION", False)
    monkeypatch.setattr(settings, "REQUIRE_DB_DIALECT", "postgresql")
    assert worker_role_requires_postgres() is True

    monkeypatch.setattr(settings, "DB_POLICY_ALLOW_SQLITE_IN_PRODUCTION", True)
    assert worker_role_requires_postgres() is False

    monkeypatch.setattr(settings, "DB_POLICY_ALLOW_SQLITE_IN_PRODUCTION", False)
    monkeypatch.setattr(settings, "REQUIRE_DB_DIALECT", "sqlite")
    assert worker_role_requires_postgres() is False
