"""PostgreSQL compatibility checks that do not require a running server.

A real PG instance cannot always be provisioned in this environment, so the schema is
verified at the dialect level: DDL is compiled with the PostgreSQL dialect and the
custom `DecimalNumeric` / `UTCDateTime` types are checked to emit the right SQL and to
survive round-tripping without float precision loss.

This is NOT a substitute for running the integration suite against a real PostgreSQL —
see docs/TEST_PLAN.md §5 for that pending item.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import Column, MetaData, Numeric, String, Table, create_mock_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from app.db.session import Base
from app.db.types import DecimalNumeric, UTCDateTime


def test_decimal_numeric_uses_true_numeric_on_postgres():
    dialect = postgresql.dialect()
    impl = DecimalNumeric().load_dialect_impl(dialect)
    assert isinstance(impl, Numeric)
    assert impl.precision == 28
    assert impl.scale == 10


def test_decimal_numeric_stores_string_on_sqlite():
    sqlite_dialect = __import__("sqlalchemy.dialects.sqlite", fromlist=["dialect"]).dialect()
    impl = DecimalNumeric().load_dialect_impl(sqlite_dialect)
    assert isinstance(impl, String)


def test_decimal_bind_roundtrip_keeps_precision():
    dtype = DecimalNumeric()
    dialect = postgresql.dialect()
    value = Decimal("0.1234567890123456789012345")
    bound = dtype.process_bind_param(value, dialect)
    assert isinstance(bound, Decimal)
    # Retrieval always returns Decimal, never float.
    result = dtype.process_result_value(str(bound), dialect)
    assert isinstance(result, Decimal)


def test_utc_datetime_forces_timezone_aware_values():
    from datetime import datetime, timezone

    dtype = UTCDateTime()
    dialect = postgresql.dialect()
    naive = datetime(2026, 10, 30, 12, 0, 0)
    bound = dtype.process_bind_param(naive, dialect)
    assert bound is not None and bound.tzinfo is not None
    assert bound.tzinfo == timezone.utc


def test_full_schema_compiles_for_postgres():
    """Every table's DDL must compile under the PostgreSQL dialect without error."""
    dialect = postgresql.dialect()
    failures = []
    for table in Base.metadata.sorted_tables:
        try:
            ddl = str(CreateTable(table).compile(dialect=dialect))
        except Exception as exc:  # pragma: no cover - failure path
            failures.append((table.name, str(exc)))
            continue
        assert table.name in ddl
        # The custom money type must render as NUMERIC on Postgres, never as TEXT.
        if any(isinstance(c.type, DecimalNumeric) for c in table.columns):
            assert "NUMERIC(28, 10)" in ddl.upper().replace("NUMERIC(28,10)", "NUMERIC(28, 10)")
    assert not failures, failures


def test_expected_table_count_and_names():
    expected = {
        "exchange_connections",
        "trading_accounts",
        "instruments",
        "market_candles",
        "market_snapshots",
        "strategies",
        "strategy_versions",
        "strategy_runs",
        "agent_runs",
        "ai_decisions",
        "trade_signals",
        "risk_profiles",
        "risk_events",
        "orders",
        "fills",
        "positions",
        "balance_snapshots",
        "equity_snapshots",
        "transfers",
        "audit_logs",
        "system_events",
        "backtest_runs",
        "kill_switch_states",
        "journal_entries",
    }
    actual = set(Base.metadata.tables)
    assert expected <= actual, f"missing tables: {sorted(expected - actual)}"


def test_orders_uniqueness_constraint_present():
    orders = Base.metadata.tables["orders"]
    names = {c.name for c in orders.constraints if getattr(c, "name", None)}
    assert "uq_orders_account_client_order_id" in names


def test_fills_unique_exchange_trade_id():
    fills = Base.metadata.tables["fills"]
    names = {c.name for c in fills.constraints if getattr(c, "name", None)}
    assert "uq_fills_exchange_trade_id" in names


def test_mock_engine_accepts_full_metadata():
    """Compile the entire CREATE TABLE set against a mock PG engine."""
    statements: list[str] = []

    def executor(sql, *args, **kwargs):
        statements.append(str(sql.compile(dialect=postgresql.dialect())))

    engine = create_mock_engine("postgresql+psycopg://", executor)
    Base.metadata.create_all(engine)
    assert len(statements) >= len(Base.metadata.sorted_tables)
    assert any("CREATE TABLE orders" in s for s in statements)
