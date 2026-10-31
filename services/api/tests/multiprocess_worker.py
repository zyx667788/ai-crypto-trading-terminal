"""OS-process helper for the PostgreSQL multi-worker integration tests.

This file is test-only. It intentionally uses os._exit to emulate an uncatchable crash.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import timedelta

from app.core.precision import utc_now


def emit(payload):
    print("RESULT=" + json.dumps(payload, default=str), flush=True)


def plan():
    return {
        "exchange": "binance",
        "market_type": "usdt_perp",
        "symbol": "BTC/USDT:USDT",
        "direction": "LONG",
        "entry_type": "LIMIT",
        "entry_price": "68000.00",
        "stop_loss": "67200.00",
        "take_profit": "69800.00",
        "confidence": "0.8",
        "timeframe": "15m",
        "thesis": "Multi-process integration test with closed-candle inputs.",
        "invalidation_conditions": ["15m close below EMA50"],
        "expires_at": (utc_now() + timedelta(minutes=30)).isoformat(),
        "strategy_version": "trend_following_v1:1",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=[
        "bootstrap", "produce", "consume", "pause", "resume", "kill", "status",
        "market", "recover", "crash-after-claim", "crash-after-submit",
    ])
    parser.add_argument("--key", default="mp-task")
    parser.add_argument("--worker-id", default="mp-worker")
    parser.add_argument("--lease-seconds", type=int, default=60)
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()

    from sqlalchemy import func, select

    from app.automation import control as automation_control
    from app.core.recovery import recovery_manager
    from app.core.runtime_state import runtime_state
    from app.db.bootstrap import ensure_schema
    from app.db.session import SessionLocal
    from app.exchanges.registry import ensure_default_demo_connections
    from app.market_data.service import market_data_service
    from app.models.entities import Order, TradingAccount
    from app.models.outbox import OutboxMessage
    from app.outbox.service import outbox_service
    from app.risk.engine import risk_engine
    from app.strategies.service import strategy_service
    from app.workers.execution import enqueue_order_task
    from app.workers.scheduler import worker_manager

    if args.command == "bootstrap":
        emit(ensure_schema())

    with SessionLocal() as db:
        if args.command == "bootstrap":
            ensure_default_demo_connections(db)
            market_data_service.sync_all_default_markets(db, timeframe="15m", limit=120)
            strategy_service.ensure_default_strategies(db)
            for account in db.scalars(select(TradingAccount)).all():
                account.is_active = True
                account.trading_locked = False
                account.sync_status = "SYNCED"
                account.lock_reason = None
            db.commit()
            recovery = recovery_manager.run_recovery(db, force=True)
            if not recovery["ready"]:
                recovery = recovery_manager.acknowledge_and_ready("integration bootstrap", db=db)
            try:
                automation = automation_control.start(db, environment="demo", actor="bootstrap")
            except ValueError as exc:
                automation = {"start_rejected": str(exc), **automation_control.get_status(db)}
            emit({"recovery": recovery["state"], "automation": automation})
            return 0

        if args.command == "produce":
            msg = enqueue_order_task(
                db,
                trade_plan=plan(),
                decision_id=f"decision-{args.key}",
                signal_id=None,
                client_order_id=args.key,
            )
            db.commit()
            emit({"outbox_id": msg.id, "client_order_id": args.key})
            return 0

        if args.command == "consume":
            result = worker_manager.run_worker_once(
                "execution-worker",
                db,
                worker_id=args.worker_id,
                lease_seconds=args.lease_seconds,
            )
            emit(result)
            return 0

        if args.command == "pause":
            emit(automation_control.pause(db, reason="cross-process integration pause", actor="api-process"))
            return 0

        if args.command == "resume":
            emit(automation_control.resume(db, actor="api-process"))
            return 0

        if args.command == "kill":
            emit(risk_engine.trigger_kill_switch(db, reason="cross-process integration kill"))
            return 0

        if args.command == "market":
            emit(worker_manager.run_worker_once("market-worker", db, worker_id=args.worker_id))
            return 0

        if args.command == "recover":
            emit(worker_manager.run_worker_once("recovery-watchdog", db, worker_id=args.worker_id))
            return 0

        if args.command in ("crash-after-claim", "crash-after-submit"):
            claimed = outbox_service.claim(
                db,
                worker_id=args.worker_id,
                topics=["execution.order"],
                lease_seconds=args.lease_seconds,
                limit=1,
                auto_commit=True,
            )
            if not claimed:
                emit({"error": "no task claimed"})
                return 2
            message = claimed[0]
            if args.command == "crash-after-submit":
                from app.execution.engine import execution_engine

                payload = dict(message.payload_json or {})
                order = execution_engine.submit_trade_plan(
                    db,
                    payload["trade_plan"],
                    decision_id=payload.get("decision_id"),
                    signal_id=payload.get("signal_id"),
                    client_order_id=payload["client_order_id"],
                )
                print("BEFORE_CRASH=" + json.dumps({"order_id": order.get("id"), "status": order.get("status")}), flush=True)
            emit({"claimed": message.id, "crashing": args.command})
            # Deliberately skip ack, close, and finally blocks.
            os._exit(17)

        if args.command == "status":
            orders = db.scalar(select(func.count()).select_from(Order)) or 0
            messages = [
                {
                    "id": row.id,
                    "status": row.status,
                    "attempts": row.attempts,
                    "claimed_by": row.claimed_by,
                    "dedup_key": row.dedup_key,
                }
                for row in db.scalars(select(OutboxMessage)).all()
            ]
            emit({
                "orders": orders,
                "messages": messages,
                "automation": automation_control.get_status(db),
                "recovery": recovery_manager.get_status(db),
                "workers": runtime_state.list_workers(db),
            })
            return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
