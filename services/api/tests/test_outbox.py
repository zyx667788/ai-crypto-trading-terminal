"""
Comprehensive test suite for PostgreSQL Outbox service.

Validates:
1. enqueue idempotency (same transaction and across transactions).
2. Two workers / sessions competing to claim tasks without duplicates.
3. Concurrent multi-threaded claiming without overlaps.
4. ack marks task as DONE and records completion time.
5. nack retries with backoff delay, transitions back to FAILED/PENDING.
6. Reaching max_attempts transitions task to DEAD (dead-letter queue).
7. Crash recovery: expired lease reclaimed by another worker or reclaim_expired().
8. Only active lease owner can ack or nack; expired lease rejects ack/nack.
9. Transaction boundaries: claim does not auto-commit caller's uncommitted work;
   enqueue rolls back when caller rolls back.
10. Topic-based routing and isolation.
"""
from __future__ import annotations

import concurrent.futures
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select

from app.core.precision import utc_now
from app.db.session import SessionLocal
from app.models.outbox import OutboxMessage, OutboxStatus
from app.outbox import (
    InvalidLeaseError,
    MessageNotFoundError,
    OutboxError,
    ack,
    claim,
    enqueue,
    nack,
    outbox_service,
    reclaim_expired,
)


@pytest.fixture(autouse=True)
def _clean_outbox():
    """Ensure clean outbox table for each test."""
    with SessionLocal() as db:
        db.query(OutboxMessage).delete()
        db.commit()
    yield
    with SessionLocal() as db:
        db.query(OutboxMessage).delete()
        db.commit()


# ---------------------------------------------------------------------------
# 1. Enqueue Idempotency
# ---------------------------------------------------------------------------
def test_enqueue_idempotency_same_transaction(db):
    """Calling enqueue with same dedup_key multiple times in the same transaction returns the same record."""
    payload = {"order_id": "ord-1001", "symbol": "BTC/USDT", "qty": "0.5"}
    dedup_key = "exec-order:ord-1001"

    msg1 = enqueue(
        db,
        topic="execution.order",
        payload=payload,
        aggregate_id="ord-1001",
        dedup_key=dedup_key,
        max_attempts=3,
    )
    assert msg1.id is not None
    assert msg1.status == OutboxStatus.PENDING.value
    assert msg1.dedup_key == dedup_key

    # Call again with same dedup_key in same uncommitted session
    msg2 = enqueue(
        db,
        topic="execution.order",
        payload={"order_id": "ord-1001", "other": "ignored"},
        aggregate_id="ord-1001",
        dedup_key=dedup_key,
        max_attempts=3,
    )
    assert msg1.id == msg2.id

    db.commit()

    # Query DB directly to verify exactly 1 record was written
    count = db.execute(
        select(OutboxMessage).where(OutboxMessage.dedup_key == dedup_key)
    ).scalars().all()
    assert len(count) == 1


def test_enqueue_idempotency_across_transactions():
    """Calling enqueue with same dedup_key across different sessions returns the existing record."""
    dedup_key = "exec-order:ord-across-tx"
    payload = {"order_id": "ord-across-tx"}

    with SessionLocal() as db1:
        msg1 = enqueue(
            db1,
            topic="execution.order",
            payload=payload,
            dedup_key=dedup_key,
        )
        db1.commit()
        msg1_id = msg1.id

    with SessionLocal() as db2:
        msg2 = enqueue(
            db2,
            topic="execution.order",
            payload={"different": "data"},
            dedup_key=dedup_key,
        )
        db2.commit()
        assert msg2.id == msg1_id

    with SessionLocal() as db3:
        all_rows = db3.execute(
            select(OutboxMessage).where(OutboxMessage.dedup_key == dedup_key)
        ).scalars().all()
        assert len(all_rows) == 1
        assert all_rows[0].id == msg1_id


def test_enqueue_transaction_atomicity_rollback(db):
    """If caller's transaction rolls back, enqueued message is rolled back with it."""
    dedup_key = "exec-order:will-rollback"
    enqueue(
        db,
        topic="execution.order",
        payload={"data": 1},
        dedup_key=dedup_key,
    )
    db.rollback()

    # Verify not present in DB
    found = db.execute(
        select(OutboxMessage).where(OutboxMessage.dedup_key == dedup_key)
    ).scalar_one_or_none()
    assert found is None


# ---------------------------------------------------------------------------
# 2. Worker Competition and Claim Atomicity
# ---------------------------------------------------------------------------
def test_two_workers_competing_claim_no_duplicates():
    """Two workers competing to claim messages must receive disjoint subsets with zero overlap."""
    with SessionLocal() as db:
        for i in range(4):
            enqueue(
                db,
                topic="execution.order",
                payload={"order_index": i},
                dedup_key=f"compete-order-{i}",
            )
        db.commit()

    # Worker 1 claims 2 messages
    with SessionLocal() as db1:
        claimed_w1 = claim(
            db1,
            worker_id="worker-alpha",
            topics=["execution.order"],
            limit=2,
            auto_commit=True,
        )

    # Worker 2 claims 2 messages
    with SessionLocal() as db2:
        claimed_w2 = claim(
            db2,
            worker_id="worker-beta",
            topics=["execution.order"],
            limit=2,
            auto_commit=True,
        )

    assert len(claimed_w1) == 2
    assert len(claimed_w2) == 2

    ids_w1 = {m.id for m in claimed_w1}
    ids_w2 = {m.id for m in claimed_w2}

    # Must be completely disjoint
    assert ids_w1.isdisjoint(ids_w2)
    assert len(ids_w1 | ids_w2) == 4

    # Both workers try to claim more: queue is now empty
    with SessionLocal() as db3:
        claimed_empty = claim(
            db3,
            worker_id="worker-gamma",
            topics=["execution.order"],
            limit=5,
        )
        assert len(claimed_empty) == 0


def test_concurrent_multithreaded_claiming():
    """Concurrent worker threads claiming batches from outbox must never claim the same message."""
    total_messages = 12
    with SessionLocal() as db:
        for i in range(total_messages):
            enqueue(
                db,
                topic="execution.order",
                payload={"seq": i},
                dedup_key=f"concurrent-seq-{i}",
            )
        db.commit()

    claimed_by_worker: dict[str, list[str]] = {}

    def worker_run(worker_name: str) -> list[str]:
        result_ids = []
        with SessionLocal() as session:
            # Claim in small chunks
            batch = claim(
                session,
                worker_id=worker_name,
                topics=["execution.order"],
                limit=3,
                auto_commit=True,
            )
            result_ids.extend([m.id for m in batch])
        return result_ids

    workers = [f"worker-{i}" for i in range(4)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(worker_run, w): w for w in workers}
        for future in concurrent.futures.as_completed(futures):
            w = futures[future]
            claimed_by_worker[w] = future.result()

    all_claimed_ids = [mid for ids in claimed_by_worker.values() for mid in ids]
    unique_claimed_ids = set(all_claimed_ids)

    # Absolutely no duplicate delivery
    assert len(all_claimed_ids) == len(unique_claimed_ids)
    assert len(unique_claimed_ids) == total_messages


# ---------------------------------------------------------------------------
# 3. Ack Completion
# ---------------------------------------------------------------------------
def test_ack_success_marks_done(db):
    """Acknowledging a claimed message marks it DONE and records processed_at."""
    msg = enqueue(db, topic="execution.order", payload={"action": "place_order"})
    db.commit()

    claimed = claim(db, worker_id="worker-1", topics=["execution.order"], limit=1, auto_commit=True)
    assert len(claimed) == 1
    assert claimed[0].id == msg.id
    assert claimed[0].status == OutboxStatus.CLAIMED.value

    # Ack the message
    acked = ack(db, claimed[0].id, worker_id="worker-1", auto_commit=True)
    assert acked.status == OutboxStatus.DONE.value
    assert acked.processed_at is not None

    # Cannot be claimed again
    again = claim(db, worker_id="worker-2", topics=["execution.order"], limit=10)
    assert len(again) == 0

    # Repeating ack raises InvalidLeaseError
    with pytest.raises(InvalidLeaseError) as exc_info:
        ack(db, claimed[0].id, worker_id="worker-1")
    assert "Cannot ack message" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 4. Nack Retry and Reschedule
# ---------------------------------------------------------------------------
def test_nack_retry_with_backoff_delay(db):
    """Nacking a message increments attempt, sets last_error, and delays next claim."""
    now = utc_now()
    msg = enqueue(
        db,
        topic="execution.order",
        payload={"order_id": "test-nack"},
        max_attempts=3,
        available_at=now,
    )
    db.commit()

    # Attempt 1: Worker claims
    claimed_1 = claim(db, worker_id="worker-1", topics=["execution.order"], limit=1, now=now, auto_commit=True)
    assert len(claimed_1) == 1
    assert claimed_1[0].attempts == 1

    # Worker nacks with 30-second retry delay
    nacked = nack(
        db,
        claimed_1[0].id,
        worker_id="worker-1",
        error="Exchange rate limit 429",
        retry_delay=30,
        now=now,
        auto_commit=True,
    )
    assert nacked.status == OutboxStatus.FAILED.value
    assert nacked.last_error == "Exchange rate limit 429"
    assert nacked.claimed_by is None
    assert nacked.lease_expires_at is None
    assert nacked.available_at == now + timedelta(seconds=30)

    # Immediately afterwards (e.g. at now + 5s): not yet available
    premature_claim = claim(
        db,
        worker_id="worker-2",
        topics=["execution.order"],
        limit=1,
        now=now + timedelta(seconds=5),
    )
    assert len(premature_claim) == 0

    # After retry delay elapsed (at now + 35s): available for claim
    claimed_2 = claim(
        db,
        worker_id="worker-2",
        topics=["execution.order"],
        limit=1,
        now=now + timedelta(seconds=35),
        auto_commit=True,
    )
    assert len(claimed_2) == 1
    assert claimed_2[0].id == msg.id
    assert claimed_2[0].attempts == 2
    assert claimed_2[0].claimed_by == "worker-2"


# ---------------------------------------------------------------------------
# 5. Max Attempts Reached -> DEAD
# ---------------------------------------------------------------------------
def test_max_attempts_enters_dead(db):
    """When retry attempts reach max_attempts, message transitions to DEAD."""
    now = utc_now()
    msg = enqueue(
        db,
        topic="execution.order",
        payload={"order_id": "fatal-order"},
        max_attempts=2,
        available_at=now,
    )
    db.commit()

    # Attempt 1
    c1 = claim(db, worker_id="worker-1", topics=["execution.order"], limit=1, now=now, auto_commit=True)
    assert c1[0].attempts == 1
    nack(db, c1[0].id, worker_id="worker-1", error="Attempt 1 failed", retry_delay=0, now=now, auto_commit=True)

    # Attempt 2
    c2 = claim(db, worker_id="worker-2", topics=["execution.order"], limit=1, now=now, auto_commit=True)
    assert c2[0].attempts == 2

    # Attempt 2 fails -> reaches max_attempts (2) -> DEAD
    dead_msg = nack(
        db,
        c2[0].id,
        worker_id="worker-2",
        error="Fatal exchange error: insufficient funds",
        retry_delay=10,
        now=now,
        auto_commit=True,
    )
    assert dead_msg.status == OutboxStatus.DEAD.value
    assert dead_msg.processed_at == now
    assert dead_msg.last_error == "Fatal exchange error: insufficient funds"

    # Message is dead and cannot be claimed again
    future_claim = claim(
        db,
        worker_id="worker-3",
        topics=["execution.order"],
        limit=10,
        now=now + timedelta(days=1),
    )
    assert len(future_claim) == 0


# ---------------------------------------------------------------------------
# 6. Crash Recovery & Expired Lease
# ---------------------------------------------------------------------------
def test_expired_lease_reclaimed_by_reclaim_expired(db):
    """When a worker crashes and lease expires, reclaim_expired restores it to PENDING for another worker."""
    now = utc_now()
    msg = enqueue(db, topic="execution.order", payload={"data": "crash-test"}, max_attempts=3, available_at=now)
    db.commit()

    # Worker 1 claims with 10s lease
    c1 = claim(
        db,
        worker_id="crashed-worker",
        topics=["execution.order"],
        lease_seconds=10,
        limit=1,
        now=now,
        auto_commit=True,
    )
    assert len(c1) == 1
    assert c1[0].claimed_by == "crashed-worker"

    # Worker 1 crashes (no ack, no nack).
    # Time advances 15 seconds: lease is expired.
    advance_time = now + timedelta(seconds=15)

    # Reclaim expired leases
    reclaimed = reclaim_expired(db, now=advance_time, auto_commit=True)
    assert len(reclaimed) == 1
    assert reclaimed[0].id == msg.id
    assert reclaimed[0].status == OutboxStatus.PENDING.value
    assert reclaimed[0].claimed_by is None
    assert reclaimed[0].lease_expires_at is None
    assert "Lease expired (worker crashed-worker)" in (reclaimed[0].last_error or "")

    # Worker 2 claims the recovered message
    c2 = claim(
        db,
        worker_id="healthy-worker",
        topics=["execution.order"],
        limit=1,
        now=advance_time,
        auto_commit=True,
    )
    assert len(c2) == 1
    assert c2[0].id == msg.id
    assert c2[0].claimed_by == "healthy-worker"
    assert c2[0].attempts == 2

    # Worker 2 successfully completes
    acked = ack(db, c2[0].id, worker_id="healthy-worker", now=advance_time, auto_commit=True)
    assert acked.status == OutboxStatus.DONE.value


def test_expired_lease_with_exhausted_attempts_marked_dead_on_reclaim(db):
    """When a worker crashes on the final allowed attempt, reclaim_expired marks it DEAD."""
    now = utc_now()
    msg = enqueue(db, topic="execution.order", payload={"data": "final-crash"}, max_attempts=1, available_at=now)
    db.commit()

    # Worker claims on attempt 1 of 1
    c1 = claim(db, worker_id="crashed-w1", topics=["execution.order"], lease_seconds=5, limit=1, now=now, auto_commit=True)
    assert c1[0].attempts == 1

    # Worker crashes, lease expires
    advance_time = now + timedelta(seconds=10)
    reclaimed = reclaim_expired(db, now=advance_time, auto_commit=True)

    assert len(reclaimed) == 1
    assert reclaimed[0].id == msg.id
    assert reclaimed[0].status == OutboxStatus.DEAD.value
    assert "max_attempts (1)" in reclaimed[0].last_error


def test_claim_directly_takes_over_expired_lease_without_explicit_reclaim(db):
    """A worker calling claim directly can take over an abandoned expired lease."""
    now = utc_now()
    msg = enqueue(db, topic="execution.order", payload={"data": "takeover"}, max_attempts=3, available_at=now)
    db.commit()

    # Worker 1 claims with 5s lease
    c1 = claim(db, worker_id="worker-a", topics=["execution.order"], lease_seconds=5, limit=1, now=now, auto_commit=True)
    assert c1[0].claimed_by == "worker-a"

    # Lease expires after 10s
    advance_time = now + timedelta(seconds=10)

    # Worker 2 claims directly
    c2 = claim(
        db,
        worker_id="worker-b",
        topics=["execution.order"],
        lease_seconds=60,
        limit=1,
        now=advance_time,
        auto_commit=True,
    )
    assert len(c2) == 1
    assert c2[0].id == msg.id
    assert c2[0].claimed_by == "worker-b"
    assert c2[0].attempts == 2


# ---------------------------------------------------------------------------
# 7. Only Owner Can Ack and Nack
# ---------------------------------------------------------------------------
def test_only_lease_owner_can_ack_and_nack(db):
    """Workers other than the lease owner cannot ack or nack."""
    now = utc_now()
    msg = enqueue(db, topic="execution.order", payload={"k": "v"}, available_at=now)
    db.commit()

    claimed = claim(db, worker_id="owner-worker", topics=["execution.order"], lease_seconds=60, limit=1, now=now, auto_commit=True)
    msg_id = claimed[0].id

    # Imposter worker tries to ack
    with pytest.raises(InvalidLeaseError) as exc_ack:
        ack(db, msg_id, worker_id="imposter-worker", now=now)
    assert "does not hold lease" in str(exc_ack.value)

    # Imposter worker tries to nack
    with pytest.raises(InvalidLeaseError) as exc_nack:
        nack(db, msg_id, worker_id="imposter-worker", error="bad", now=now)
    assert "does not hold lease" in str(exc_nack.value)

    # Owner can ack within valid lease
    acked = ack(db, msg_id, worker_id="owner-worker", now=now, auto_commit=True)
    assert acked.status == OutboxStatus.DONE.value


def test_original_owner_cannot_ack_after_lease_expired(db):
    """Even the original owner cannot ack after their lease has expired."""
    now = utc_now()
    msg = enqueue(db, topic="execution.order", payload={"k": "v"}, available_at=now)
    db.commit()

    claimed = claim(db, worker_id="slow-worker", topics=["execution.order"], lease_seconds=10, limit=1, now=now, auto_commit=True)
    msg_id = claimed[0].id

    # Time passes beyond lease expiration
    past_lease = now + timedelta(seconds=15)

    with pytest.raises(InvalidLeaseError) as exc:
        ack(db, msg_id, worker_id="slow-worker", now=past_lease)
    assert "expired at" in str(exc.value)

    with pytest.raises(InvalidLeaseError) as exc_nack:
        nack(db, msg_id, worker_id="slow-worker", error="timed out", now=past_lease)
    assert "expired at" in str(exc_nack.value)


# ---------------------------------------------------------------------------
# 8. Transaction Boundaries & No Auto-Commit on Claim
# ---------------------------------------------------------------------------
def test_claim_does_not_auto_commit_caller_transaction(db):
    """Calling claim must not commit caller's transaction by default; rolling back rolls back the claim."""
    now = utc_now()
    msg = enqueue(db, topic="execution.order", payload={"k": "v"}, available_at=now)
    db.commit()

    # Claim without auto_commit
    claimed = claim(db, worker_id="worker-tx", topics=["execution.order"], limit=1, now=now, auto_commit=False)
    assert len(claimed) == 1
    assert claimed[0].status == OutboxStatus.CLAIMED.value

    # Caller aborts their transaction
    db.rollback()

    # Verify message in DB is still PENDING and unassigned
    reloaded = outbox_service.get(db, msg.id)
    assert reloaded.status == OutboxStatus.PENDING.value
    assert reloaded.claimed_by is None
    assert reloaded.attempts == 0


# ---------------------------------------------------------------------------
# 9. Topic Isolation
# ---------------------------------------------------------------------------
def test_topic_isolation(db):
    """Workers only claim messages corresponding to the subscribed topics."""
    enqueue(db, topic="execution.order", payload={"order": 1}, dedup_key="topic-ord-1")
    enqueue(db, topic="ai.decision", payload={"decision": 1}, dedup_key="topic-ai-1")
    db.commit()

    # Claim only execution.order
    order_msgs = claim(db, worker_id="exec-worker", topics=["execution.order"], limit=10, auto_commit=True)
    assert len(order_msgs) == 1
    assert order_msgs[0].topic == "execution.order"

    # Claim only ai.decision
    ai_msgs = claim(db, worker_id="ai-worker", topics=["ai.decision"], limit=10, auto_commit=True)
    assert len(ai_msgs) == 1
    assert ai_msgs[0].topic == "ai.decision"


# ---------------------------------------------------------------------------
# 10. Message Not Found and Validation Errors
# ---------------------------------------------------------------------------
def test_ack_nack_nonexistent_message_raises_not_found(db):
    """Ack/Nack on unknown message ID raises MessageNotFoundError."""
    with pytest.raises(MessageNotFoundError):
        ack(db, "nonexistent-uuid", worker_id="w1")

    with pytest.raises(MessageNotFoundError):
        nack(db, "nonexistent-uuid", worker_id="w1", error="failed")


def test_invalid_parameters_raise_value_errors(db):
    """Validation checks on input arguments."""
    with pytest.raises(ValueError, match="topic must not be empty"):
        enqueue(db, topic="", payload={})

    with pytest.raises(ValueError, match="max_attempts must be positive"):
        enqueue(db, topic="t", payload={}, max_attempts=0)

    with pytest.raises(ValueError, match="worker_id must not be empty"):
        claim(db, worker_id="", topics=["t"])

    with pytest.raises(ValueError, match="lease_seconds must be positive"):
        claim(db, worker_id="w", topics=["t"], lease_seconds=0)

    with pytest.raises(ValueError, match="limit must be positive"):
        claim(db, worker_id="w", topics=["t"], limit=-1)

    # Empty topics returns empty list without error
    assert claim(db, worker_id="w", topics=[]) == []
