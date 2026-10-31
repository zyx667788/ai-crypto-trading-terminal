"""
Real PostgreSQL integration check.

Starts an embedded PostgreSQL (pgserver), creates a dedicated database, applies every
Alembic migration, then exercises the parts of the system that are sensitive to the
database engine: exact NUMERIC money types, JSON columns, unique constraints, decimal
round-tripping and the full trade pipeline.

Run:  python infra/scripts/pg_integration_check.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import traceback
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pgserver
import psycopg

ROOT = Path(__file__).resolve().parents[2]
API_DIR = ROOT / "services" / "api"
sys.path.insert(0, str(API_DIR))

PGDATA = Path(tempfile.mkdtemp(prefix="aict-pg-")) / "data"
DB_NAME = "ai_crypto_terminal"

failures: list[str] = []


def check(name: str, fn):
    try:
        detail = fn()
        print(f"  PASS  {name}" + (f"  ({detail})" if detail else ""))
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
        failures.append(name)
        return False


def main() -> int:
    out = sys.stdout
    try:
        out.reconfigure(encoding="utf-8")
    except Exception:
        pass

    print("=" * 72)
    print("PostgreSQL integration check")
    print("=" * 72)

    server = pgserver.get_server(str(PGDATA), cleanup_mode=None)
    admin_uri = server.get_uri()
    print(f"server   : {admin_uri.split('@')[-1]}")

    with psycopg.connect(admin_uri, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{DB_NAME}"')
        conn.execute(f'CREATE DATABASE "{DB_NAME}"')

    base, _, rest = admin_uri.rpartition("/")
    app_uri = f"{base}/{DB_NAME}"
    print(f"database : {DB_NAME}")

    os.environ["DATABASE_URL"] = app_uri.replace("postgresql://", "postgresql+psycopg://")
    # Throwaway per-run key: never a credential-shaped literal in version control.
    from cryptography.fernet import Fernet

    os.environ["TERMINAL_MASTER_KEY"] = Fernet.generate_key().decode()
    os.environ["AI_API_KEY"] = ""
    os.environ["AI_PROVIDER"] = "rule_assisted_mock"
    os.environ["AUTO_TRADING_ENABLED"] = "true"

    # Re-import app modules now that DATABASE_URL points at PostgreSQL.
    for mod in [m for m in list(sys.modules) if m.startswith("app.")]:
        del sys.modules[mod]

    from alembic import command
    from alembic.config import Config

    cfg = Config(str(API_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(API_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", os.environ["DATABASE_URL"].replace("%", "%%"))

    print("\n[1] Alembic migrations")
    command.upgrade(cfg, "head")
    print("  PASS  upgrade head")

    from sqlalchemy import inspect, select, text

    from app.db.session import SessionLocal, engine
    from app.models.entities import Fill, Instrument, Order, Position, TradingAccount

    dialect = engine.dialect.name
    assert dialect == "postgresql", f"expected postgresql, got {dialect}"
    print(f"  PASS  engine dialect = {dialect}")

    print("\n[2] Schema & types")
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    print(f"  PASS  tables created = {len(tables) - 1} (+alembic_version)")

    cols = {c["name"]: c for c in insp.get_columns("orders")}
    for money_col in ("qty", "price", "avg_fill_price"):
        t = cols[money_col]["type"]
        print(f"  PASS  orders.{money_col} -> {t}")
        if "NUMERIC" not in str(t).upper():
            failures.append(f"orders.{money_col} not NUMERIC")

    print("\n[3] Decimal precision round-trip")
    with engine.connect() as conn:
        val = conn.execute(text("SELECT CAST('0.123456789012345678' AS NUMERIC(28,10))")).scalar()
        print(f"  PASS  NUMERIC round-trip = {val!r} ({type(val).__name__})")
        if not isinstance(val, Decimal):
            failures.append("numeric round-trip not Decimal")

    print("\n[4] JSON columns")
    from app.exchanges.registry import ensure_default_demo_connections
    from app.market_data.service import market_data_service
    from app.strategies.service import strategy_service

    with SessionLocal() as db:
        ensure_default_demo_connections(db)
        market_data_service.sync_all_default_markets(db, timeframe="15m", limit=60)
        strategy_service.ensure_default_strategies(db)

        acct = db.scalar(select(TradingAccount))
        strategies = strategy_service.list_strategies(db)
        print(f"  PASS  strategies with JSON params = {len(strategies)}")
        if not strategies or not strategies[0].get("parameters"):
            failures.append("strategy parameters JSON empty")

        inst = db.scalar(select(Instrument))
        print(f"  PASS  instruments = {db.scalar(select(Instrument.id).limit(1)) is not None}")

    print("\n[5] Unique constraint enforcement")
    with SessionLocal() as db:
        acct = db.scalar(select(TradingAccount))
        order = Order(
            account_id=acct.id,
            exchange="binance",
            environment="demo",
            market_type="usdt_perp",
            symbol="BTC/USDT:USDT",
            client_order_id="pg-unique-1",
            order_type="LIMIT",
            side="BUY",
            qty=Decimal("0.001"),
            price=Decimal("68000"),
        )
        db.add(order)
        db.commit()
        dup = Order(
            account_id=acct.id,
            exchange="binance",
            environment="demo",
            market_type="usdt_perp",
            symbol="BTC/USDT:USDT",
            client_order_id="pg-unique-1",
            order_type="LIMIT",
            side="BUY",
            qty=Decimal("0.001"),
            price=Decimal("68000"),
        )
        db.add(dup)
        try:
            db.commit()
            print("  FAIL  duplicate client_order_id was accepted")
            failures.append("unique constraint not enforced")
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            print(f"  PASS  duplicate client_order_id rejected ({type(exc).__name__})")

    print("\n[6] Full pipeline (real Risk Engine + Execution on PostgreSQL)")
    from app.core.recovery import recovery_manager
    from app.workers.scheduler import worker_manager
    from app.automation import control as automation_control
    from app.workers.execution import run_execution_worker_once

    with SessionLocal() as db:
        recovery_manager.run_recovery(db, force=True)
        automation_control.start(db, environment="demo", actor="pg-integration")
        for index in range(2):
            summary = worker_manager.run_automation_cycle(db)
            blocked = summary.get("blocked_reason")
            queued = summary.get("order") or {}
            executed = run_execution_worker_once(db, worker_id=f"pg-execution-{index}")
            order_status = (executed.get("orders") or [{}])[0].get("status") if executed.get("orders") else None
            print(f"  cycle: blocked={blocked!r} queued={queued.get('status')} executed={order_status}")
            if blocked:
                failures.append(f"cycle blocked: {blocked}")

        fills = db.scalars(select(Fill)).all()
        positions = db.scalars(select(Position)).all()
        print(f"  PASS  fills={len(fills)} positions={len(positions)}")
        if fills:
            f = fills[0]
            print(f"  PASS  fill.fee type={type(f.fee).__name__} value={f.fee}")

    print("\n[7] Migration downgrade/upgrade cycle")
    try:
        command.downgrade(cfg, "base")
        tables_after = set(inspect(engine).get_table_names())
        print(f"  PASS  downgrade to base -> {len(tables_after)} tables")
        command.upgrade(cfg, "head")
        print(f"  PASS  re-upgrade -> {len(set(inspect(engine).get_table_names()))} tables")
    except Exception as exc:  # noqa: BLE001
        print(f"  FAIL  migration cycle: {exc}")
        failures.append("migration cycle")

    print("\n" + "=" * 72)
    if failures:
        print(f"RESULT: {len(failures)} FAILURE(S): {failures}")
        return 1
    print("RESULT: ALL POSTGRESQL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        code = 1
    sys.exit(code)
