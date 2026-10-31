"""launchd-supervised worker entry point.

Usage:
    python -m app.workers.runner --role market-worker
    python -m app.workers.runner --role execution-worker
    python -m app.workers.runner --role recovery-watchdog
"""
from __future__ import annotations

import argparse
import sys

from app.core.db_policy import DatabasePolicyError, assert_trading_runtime_database
from app.core.logging import get_logger
from app.workers.scheduler import worker_manager

logger = get_logger("terminal.workers.runner")


def main() -> int:
    parser = argparse.ArgumentParser(description="AI Crypto Terminal worker process")
    parser.add_argument(
        "--role",
        required=True,
        choices=["market-worker", "execution-worker", "recovery-watchdog"],
        help="Worker role supervised by launchd.",
    )
    parser.add_argument("--worker-id", default=None, help="Stable durable identity; defaults to role-host-pid.")
    parser.add_argument("--once", action="store_true", help="Run one bounded iteration and exit (integration testing).")
    parser.add_argument("--lease-seconds", type=int, default=60, help="Outbox claim lease duration.")
    parser.add_argument(
        "--allow-unsafe-sqlite",
        action="store_true",
        default=False,
        help="Allow running worker with SQLite (NOT SAFE FOR MULTI-PROCESS).",
    )
    args = parser.parse_args()

    if args.allow_unsafe_sqlite:
        logger.warning(
            "Worker started with --allow-unsafe-sqlite. SQLite is NOT SAFE FOR MULTI-PROCESS "
            "execution because it lacks cross-process locking for transactional outbox claims."
        )

    try:
        report = assert_trading_runtime_database(role=args.role, allow_sqlite=args.allow_unsafe_sqlite)
    except DatabasePolicyError as exc:
        print(f"DatabasePolicyError: {exc}", file=sys.stderr)
        return 2

    print(f"Database policy report: {report}")

    worker_manager.run_background_loop(
        args.role,
        worker_id=args.worker_id,
        once=args.once,
        lease_seconds=args.lease_seconds,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
