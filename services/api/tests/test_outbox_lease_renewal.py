"""Lease renewal semantics for the durable outbox.

A claimed order task may legitimately outlive its first lease (exchange retries,
rate limiting, reconciliation). Before `renew` existed the only outcomes were:

    * ack/nack failed with InvalidLeaseError once the lease expired, leaving the
      message CLAIMED until `reclaim_expired` released it, or
    * `reclaim_expired` handed the still-running task to a second worker, which is
      the duplicate-submission window the outbox exists to close.

These tests pin the renewal contract: only the current owner, only while the lease is
still valid, never bumping attempts, never changing status.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.core.precision import utc_now
from app.db.session import SessionLocal
from app.models.outbox import OutboxMessage, OutboxStatus
from app.outbox import (
    InvalidLeaseError,
    MessageNotFoundError,
    claim,
    enqueue,
    renew,
)


@pytest.fixture(autouse=True)
def _clean_outbox():
    with SessionLocal() as db:
        db.query(OutboxMessage).delete()
        db.commit()
    yield
    with SessionLocal() as db:
        db.query(OutboxMessage).delete()
        db.commit()


def _enqueue_one(db, dedup_key: str = "exec-order:renew-1") -> OutboxMessage:
    msg = enqueue(db, topic="execution.order", payload={"client_order_id": dedup_key}, dedup_key=dedup_key)
    db.commit()
    return msg


def test_renew_extends_lease_without_touching_attempts(db):
    msg = _enqueue_one(db)
    claimed = claim(db, worker_id="worker-a", topics="execution.order", lease_seconds=30, auto_commit=True)
    assert len(claimed) == 1
    attempts_after_claim = claimed[0].attempts
    original_expiry = claimed[0].lease_expires_at

    renewed = renew(db, msg.id, "worker-a", lease_seconds=300, auto_commit=True)

    assert renewed.status == OutboxStatus.CLAIMED.value
    assert renewed.claimed_by == "worker-a"
    assert renewed.attempts == attempts_after_claim
    assert renewed.lease_expires_at > original_expiry


def test_renewed_lease_prevents_reclaim(db):
    msg = _enqueue_one(db, dedup_key="exec-order:renew-2")
    claim(db, worker_id="worker-a", topics="execution.order", lease_seconds=1, auto_commit=True)

    # Renew well past the original one-second lease.
    renew(db, msg.id, "worker-a", lease_seconds=600, auto_commit=True)

    from app.outbox import reclaim_expired

    reclaimed = reclaim_expired(db, now=utc_now() + timedelta(seconds=2), auto_commit=True)
    assert reclaimed == []

    still_claimed = db.get(OutboxMessage, msg.id)
    assert still_claimed.status == OutboxStatus.CLAIMED.value
    assert still_claimed.claimed_by == "worker-a"


def test_renew_rejects_foreign_owner(db):
    msg = _enqueue_one(db, dedup_key="exec-order:renew-3")
    claim(db, worker_id="worker-a", topics="execution.order", lease_seconds=60, auto_commit=True)

    with pytest.raises(InvalidLeaseError):
        renew(db, msg.id, "worker-b", lease_seconds=60)


def test_renew_rejects_expired_lease(db):
    msg = _enqueue_one(db, dedup_key="exec-order:renew-4")
    claim(db, worker_id="worker-a", topics="execution.order", lease_seconds=1, auto_commit=True)

    with pytest.raises(InvalidLeaseError):
        renew(db, msg.id, "worker-a", lease_seconds=60, now=utc_now() + timedelta(seconds=5))


def test_renew_rejects_message_that_is_not_claimed(db):
    msg = _enqueue_one(db, dedup_key="exec-order:renew-5")

    with pytest.raises(InvalidLeaseError):
        renew(db, msg.id, "worker-a", lease_seconds=60)


def test_renew_rejects_done_message(db):
    msg = _enqueue_one(db, dedup_key="exec-order:renew-6")
    claim(db, worker_id="worker-a", topics="execution.order", lease_seconds=60, auto_commit=True)

    from app.outbox import ack

    ack(db, msg.id, "worker-a", auto_commit=True)

    with pytest.raises(InvalidLeaseError):
        renew(db, msg.id, "worker-a", lease_seconds=60)


def test_renew_unknown_message_raises(db):
    with pytest.raises(MessageNotFoundError):
        renew(db, "does-not-exist", "worker-a", lease_seconds=60)


def test_renew_requires_positive_lease_and_owner(db):
    msg = _enqueue_one(db, dedup_key="exec-order:renew-7")
    claim(db, worker_id="worker-a", topics="execution.order", lease_seconds=60, auto_commit=True)

    with pytest.raises(ValueError):
        renew(db, msg.id, "worker-a", lease_seconds=0)
    with pytest.raises(ValueError):
        renew(db, msg.id, "", lease_seconds=60)
