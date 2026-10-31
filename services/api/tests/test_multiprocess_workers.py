"""Real PostgreSQL + real OS-process integration tests for V1.2 phase 1.

These tests intentionally do not use the app's Session in the parent pytest process:
every state transition and assertion is performed against the same server used by
independently launched API/worker processes.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

pgserver = pytest.importorskip("pgserver")
psycopg = pytest.importorskip("psycopg")

ROOT = Path(__file__).resolve().parents[3]
API_DIR = ROOT / "services" / "api"
pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def postgres():
    data_dir = Path(tempfile.mkdtemp(prefix="aict-mp-pg-")) / "data"
    server = pgserver.get_server(str(data_dir))
    admin_uri = server.get_uri()
    db_name = "aict_multiprocess_test"
    with psycopg.connect(admin_uri, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
        conn.execute(f'CREATE DATABASE "{db_name}"')
    base, _, _ = admin_uri.rpartition("/")
    app_uri = f"{base}/{db_name}"
    yield {"admin_uri": admin_uri, "app_uri": app_uri, "db_name": db_name}
    try:
        with psycopg.connect(admin_uri, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
    except Exception:
        pass
    cleanup = getattr(server, "cleanup", None)
    if callable(cleanup):
        cleanup()


@pytest.fixture()
def process_env(postgres):
    with psycopg.connect(postgres["app_uri"], autocommit=True) as conn:
        tables = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename <> 'alembic_version'"
        ).fetchall()
        for (table,) in tables:
            conn.execute(f'TRUNCATE TABLE "{table}" CASCADE')
    from cryptography.fernet import Fernet

    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URL": postgres["app_uri"].replace("postgresql://", "postgresql+psycopg://"),
            "PYTHONPATH": str(API_DIR),
            "PYTHONIOENCODING": "utf-8",
            "TERMINAL_MASTER_KEY": Fernet.generate_key().decode(),
            "AI_PROVIDER": "rule_assisted_mock",
            "AI_API_KEY": "",
            "AI_BASE_URL": "",
            "LIVE_TRADING_ENABLED": "false",
            "AUTO_TRADING_ENABLED": "false",
        }
    )
    return env


def run_process(env, command, *args, expect=0, timeout=90):
    cmd = [sys.executable, str(API_DIR / "tests" / "multiprocess_worker.py"), command, *map(str, args)]
    completed = subprocess.run(
        cmd,
        cwd=API_DIR,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if completed.returncode != expect:
        raise AssertionError(
            f"process {command} exited {completed.returncode}, expected {expect}\n"
            f"STDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
        )
    result = None
    for line in completed.stdout.splitlines():
        if line.startswith("RESULT="):
            result = json.loads(line[len("RESULT="):])
    return {"result": result, "stdout": completed.stdout, "stderr": completed.stderr}


def with_auto(env, enabled: bool):
    updated = env.copy()
    updated["AUTO_TRADING_ENABLED"] = "true" if enabled else "false"
    return updated


def scalar(env, sql):
    with psycopg.connect(env["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")) as conn:
        return conn.execute(sql).fetchone()[0]


def test_auto_false_blocks_every_worker(process_env):
    env = with_auto(process_env, False)
    run_process(env, "bootstrap")
    run_process(env, "produce", "--key", "auto-off")
    consumed = run_process(env, "consume", "--worker-id", "execution-off")
    market = run_process(env, "market", "--worker-id", "market-off")

    assert consumed["result"]["blocked_reason"] == "AUTO_TRADING_DISABLED"
    assert market["result"]["blocked_reason"] == "AUTO_TRADING_DISABLED"
    assert scalar(env, "SELECT count(*) FROM orders") == 0
    assert scalar(env, "SELECT count(*) FROM outbox_messages WHERE status='PENDING'") == 1


def test_pause_and_kill_switch_are_cross_process_and_recovery_preserves_kill(process_env):
    env = with_auto(process_env, True)
    run_process(env, "bootstrap")

    run_process(env, "produce", "--key", "paused-task")
    run_process(env, "pause")
    paused = run_process(env, "consume", "--worker-id", "execution-paused")
    assert "AUTOMATION_PAUSED" in paused["result"]["blocked_reason"]
    assert scalar(env, "SELECT count(*) FROM orders") == 0

    run_process(env, "resume")
    run_process(env, "kill")
    run_process(env, "produce", "--key", "killed-task")
    killed = run_process(env, "consume", "--worker-id", "execution-killed")
    assert "KILL_SWITCH_ACTIVE" in killed["result"]["blocked_reason"]
    assert scalar(env, "SELECT count(*) FROM orders") == 0

    recovered = run_process(env, "recover", "--worker-id", "watchdog-after-kill")
    assert recovered["result"]["blocked"] is True
    reason = scalar(env, "SELECT reason FROM kill_switch_states WHERE scope='global'")
    assert "cross-process integration kill" in reason
    assert scalar(env, "SELECT count(*) FROM orders") == 0


def test_multiple_worker_processes_never_claim_the_same_order(process_env):
    env = with_auto(process_env, True)
    run_process(env, "bootstrap")
    run_process(env, "produce", "--key", "compete-1")
    run_process(env, "produce", "--key", "compete-2")

    processes = []
    for index in range(4):
        cmd = [
            sys.executable,
            str(API_DIR / "tests" / "multiprocess_worker.py"),
            "consume",
            "--worker-id",
            f"competitor-{index}",
            "--limit",
            "1",
        ]
        processes.append(subprocess.Popen(cmd, cwd=API_DIR, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
    outputs = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=90)
        outputs.append(stdout + "\n" + stderr)
        assert process.returncode == 0, stderr or stdout

    assert scalar(env, "SELECT count(*) FROM orders") == 2, "\n".join(outputs)
    assert scalar(env, "SELECT count(DISTINCT client_order_id) FROM orders") == 2
    assert scalar(env, "SELECT count(*) FROM outbox_messages WHERE status='DONE'") == 2
    assert scalar(env, "SELECT sum(attempts) FROM outbox_messages") == 2


def test_worker_crash_after_submit_is_replayed_idempotently(process_env):
    env = with_auto(process_env, True)
    run_process(env, "bootstrap")
    run_process(env, "produce", "--key", "crash-after-submit")

    crashed = run_process(
        env,
        "crash-after-submit",
        "--worker-id",
        "crashed-worker",
        "--lease-seconds",
        "1",
        expect=17,
    )
    assert "BEFORE_CRASH=" in crashed["stdout"]
    assert scalar(env, "SELECT count(*) FROM orders") == 1
    assert scalar(env, "SELECT status FROM outbox_messages") == "CLAIMED", crashed["stdout"] + crashed["stderr"]

    time.sleep(1.4)
    recovered = run_process(env, "consume", "--worker-id", "recovery-worker", "--lease-seconds", "10")
    assert recovered["result"]["completed"] + recovered["result"].get("settled", 0) == 1
    assert scalar(env, "SELECT count(*) FROM orders") == 1
    assert scalar(env, "SELECT count(DISTINCT client_order_id) FROM orders") == 1
    assert scalar(env, "SELECT status FROM outbox_messages") == "DONE"
    assert scalar(env, "SELECT attempts FROM outbox_messages") == 1

    # A second healthy worker cannot replay the completed task or create a duplicate.
    run_process(env, "consume", "--worker-id", "recovery-worker-2")
    assert scalar(env, "SELECT count(*) FROM orders") == 1


def test_crashed_submitted_task_is_caught_up_not_killed_by_pause(process_env):
    env = with_auto(process_env, True)
    run_process(env, "bootstrap")
    run_process(env, "produce", "--key", "crash-then-pause")
    run_process(env, "crash-after-submit", "--worker-id", "pause-crasher", "--lease-seconds", "1", expect=17)
    assert scalar(env, "SELECT count(*) FROM orders") == 1

    run_process(env, "pause")
    time.sleep(1.4)
    recovered = run_process(env, "consume", "--worker-id", "paused-recovery")

    assert recovered["result"]["settled"] == 1
    assert "AUTOMATION_PAUSED" in recovered["result"]["blocked_reason"]
    assert scalar(env, "SELECT status FROM outbox_messages") == "DONE"
    assert scalar(env, "SELECT count(*) FROM orders") == 1
    assert scalar(env, "SELECT count(*) FROM outbox_messages WHERE status='DEAD'") == 0


def test_worker_crash_after_claim_is_reclaimed_without_an_order(process_env):
    env = with_auto(process_env, True)
    run_process(env, "bootstrap")
    run_process(env, "produce", "--key", "crash-after-claim")
    run_process(env, "crash-after-claim", "--worker-id", "claim-crasher", "--lease-seconds", "1", expect=17)
    assert scalar(env, "SELECT count(*) FROM orders") == 0

    time.sleep(1.4)
    recovered = run_process(env, "consume", "--worker-id", "claim-recovery")
    assert recovered["result"]["completed"] + recovered["result"].get("settled", 0) == 1
    assert scalar(env, "SELECT count(*) FROM orders") == 1
    assert scalar(env, "SELECT status FROM outbox_messages") == "DONE"
