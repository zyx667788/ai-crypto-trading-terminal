"""Pytest fixtures: isolated, deterministic test database per test session."""
from __future__ import annotations

import os
import tempfile
from decimal import Decimal
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

# Force an isolated DB + a throwaway per-session master key BEFORE importing app modules.
# The key is generated at runtime so no credential-shaped literal ever lives in the repo.
_TMP_DIR = Path(tempfile.mkdtemp(prefix="aict-test-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP_DIR / 'test.db').as_posix()}"
os.environ["TERMINAL_MASTER_KEY"] = Fernet.generate_key().decode()
os.environ["LIVE_TRADING_ENABLED"] = "false"

# Tests must never hit a real LLM: no network dependency, no cost, deterministic output.
# The provider adapter has its own suite (`test_ai_provider.py`) that mocks HTTP.
os.environ["AI_API_KEY"] = ""
os.environ["AI_PROVIDER"] = "rule_assisted_mock"
os.environ["AI_BASE_URL"] = ""

from app.db.session import Base, SessionLocal, engine  # noqa: E402
import app.models  # noqa: E402,F401


@pytest.fixture(scope="session", autouse=True)
def _create_schema():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture(scope="session")
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def seeded(db):
    """Ensure demo connections, instruments, candles, snapshots and strategies exist."""
    from app.exchanges.registry import ensure_default_demo_connections
    from app.market_data.service import market_data_service
    from app.strategies.service import strategy_service

    ensure_default_demo_connections(db)
    market_data_service.sync_all_default_markets(db, timeframe="15m", limit=120)
    strategy_service.ensure_default_strategies(db)
    return db
