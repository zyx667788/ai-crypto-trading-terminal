"""launchd-supervised worker entry point.

Usage:
    python -m app.workers.runner --role market-worker
    python -m app.workers.runner --role execution-worker
    python -m app.workers.runner --role recovery-watchdog
"""
from __future__ import annotations

import argparse
import sys

from app.workers.scheduler import worker_manager


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
    args = parser.parse_args()
    worker_manager.run_background_loop(
        args.role,
        worker_id=args.worker_id,
        once=args.once,
        lease_seconds=args.lease_seconds,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
