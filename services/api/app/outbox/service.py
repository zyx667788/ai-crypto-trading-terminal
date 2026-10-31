"""
PostgreSQL Transactional Outbox Service.

Provides durable, cross-process task distribution and consumption for Execution Workers,
avoiding external streaming broker dependencies (e.g. Redis Streams).

Transaction boundary & concurrency semantics:
---------------------------------------------
1. `enqueue`:
   - Runs strictly within the caller's active database session and transaction.
   - NEVER calls `db.commit()`.
   - Business mutations (such as creating an Order or executing a TradePlan) and
     the corresponding Outbox message are committed or rolled back together atomically.
   - Idempotent: duplicate calls with the same `dedup_key` return the existing
     record without raising duplicate key errors or inserting duplicate rows,
     even when called multiple times in the same uncommitted transaction.

2. `claim`:
   - Atomically claims pending or retry-eligible messages.
   - Uses `FOR UPDATE SKIP LOCKED` on PostgreSQL so concurrent worker processes
     or threads grab disjoint batches without contention.
   - SQLite dialect safely ignores row-level lock clauses and executes sequentially
     (with an internal serial guard for multi-threaded testing).
   - Does NOT automatically commit the caller's uncommitted business transactions;
     pushes updates via `db.flush()` by default. The worker or consumer loop controls
     its own commit boundary.
   - Also safely claims expired leases if the prior worker crashed before acknowledging.

3. `ack` / `nack`:
   - Only the worker that currently holds an active, unexpired lease on the message
     is permitted to acknowledge (`ack`) or reject (`nack`).
   - If the lease has expired or is held by another worker, `InvalidLeaseError` is raised.
   - `nack` records `last_error` and calculates the next `available_at` based on `retry_delay`.
   - If `attempts >= max_attempts`, `nack` transitions the message to `DEAD`.

4. `renew`:
   - Extends an active lease in place without releasing ownership. Long-running order
     tasks must call it before the lease expires, otherwise `reclaim_expired` will hand
     the same message to a second worker while the first one is still executing.
     Renewal never increments `attempts` and never mutates status.

5. `reclaim_expired`:
   - Crash recovery: scans for messages stuck in `CLAIMED` status whose lease has expired.
   - Transitions messages with `attempts >= max_attempts` to `DEAD`.
   - Otherwise transitions them to `PENDING` with cleared lease metadata so other workers
     can pick them up immediately.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.core.precision import utc_now
from app.models.outbox import OutboxMessage, OutboxStatus, new_uuid

logger = get_logger("terminal.outbox")

# SQLite in-process serial guard for multi-threaded testing environments.
# On PostgreSQL, FOR UPDATE SKIP LOCKED handles cross-process concurrency natively.
_sqlite_claim_lock = threading.Lock()


class OutboxError(Exception):
    """Base exception for all outbox operations."""


class InvalidLeaseError(OutboxError, ValueError):
    """Raised when an operation is attempted with an invalid, expired, or unowned lease."""


class MessageNotFoundError(OutboxError, LookupError):
    """Raised when the specified outbox message does not exist."""


def _ensure_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _is_postgresql(db: Session) -> bool:
    bind = db.get_bind() if hasattr(db, "get_bind") else db.bind
    return bind is not None and bind.dialect.name == "postgresql"


class OutboxService:
    """Outbox queue management service."""

    def enqueue(
        self,
        db: Session,
        topic: str,
        payload: dict[str, Any] | Any,
        aggregate_id: str | None = None,
        dedup_key: str | None = None,
        max_attempts: int = 5,
        available_at: datetime | None = None,
    ) -> OutboxMessage:
        """
        Enqueue a message in the caller's active database transaction.

        Guarantees:
        - Atomicity: aligns with caller's business transaction. NEVER commits.
        - Idempotency: multiple invocations with the same `dedup_key` return the
          existing message without error or duplicate insertion.
        """
        if not topic:
            raise ValueError("topic must not be empty")
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")

        key = dedup_key or new_uuid()
        now = utc_now()
        effective_available_at = _ensure_utc(available_at) or now

        # 1. Fast-path check: check session's pending new objects before querying
        for obj in db.new:
            if isinstance(obj, OutboxMessage) and obj.dedup_key == key:
                return obj

        # 2. Check existing record in DB / identity map
        existing = db.execute(
            select(OutboxMessage).where(OutboxMessage.dedup_key == key)
        ).scalar_one_or_none()
        if existing is not None:
            return existing

        # 3. Create new message
        msg = OutboxMessage(
            id=new_uuid(),
            topic=topic,
            aggregate_id=aggregate_id,
            dedup_key=key,
            payload_json=payload if payload is not None else {},
            status=OutboxStatus.PENDING.value,
            attempts=0,
            max_attempts=max_attempts,
            available_at=effective_available_at,
            created_at=now,
        )

        # 4. Insert and flush within the caller's active transaction.
        # On PostgreSQL, use a SAVEPOINT (begin_nested) to avoid poisoning the outer
        # transaction if a concurrent insert of the same dedup_key occurs simultaneously.
        if _is_postgresql(db):
            try:
                with db.begin_nested():
                    db.add(msg)
                    db.flush()
            except IntegrityError:
                existing = db.execute(
                    select(OutboxMessage).where(OutboxMessage.dedup_key == key)
                ).scalar_one_or_none()
                if existing is not None:
                    return existing
                raise
        else:
            try:
                db.add(msg)
                db.flush()
            except IntegrityError:
                existing = db.execute(
                    select(OutboxMessage).where(OutboxMessage.dedup_key == key)
                ).scalar_one_or_none()
                if existing is not None:
                    return existing
                raise

        return msg

    def claim(
        self,
        db: Session,
        worker_id: str,
        topics: list[str] | str,
        lease_seconds: int = 60,
        limit: int = 10,
        *,
        now: datetime | None = None,
        auto_commit: bool = False,
    ) -> list[OutboxMessage]:
        """
        Atomically claim up to `limit` pending or retry-ready messages.

        Uses PostgreSQL FOR UPDATE SKIP LOCKED to prevent lock contention between
        workers. SQLite gracefully executes sequentially.

        Does NOT automatically commit uncompleted caller transactions unless
        `auto_commit=True` is explicitly specified.
        """
        if not worker_id:
            raise ValueError("worker_id must not be empty")
        if isinstance(topics, str):
            topics = [topics]
        if not topics:
            return []
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if limit <= 0:
            raise ValueError("limit must be positive")

        if not _is_postgresql(db):
            with _sqlite_claim_lock:
                return self._do_claim(
                    db=db,
                    worker_id=worker_id,
                    topics=topics,
                    lease_seconds=lease_seconds,
                    limit=limit,
                    now=now,
                    auto_commit=auto_commit,
                )

        return self._do_claim(
            db=db,
            worker_id=worker_id,
            topics=topics,
            lease_seconds=lease_seconds,
            limit=limit,
            now=now,
            auto_commit=auto_commit,
        )

    def _do_claim(
        self,
        db: Session,
        worker_id: str,
        topics: list[str],
        lease_seconds: int,
        limit: int,
        now: datetime | None,
        auto_commit: bool,
    ) -> list[OutboxMessage]:
        current_time = _ensure_utc(now) or utc_now()
        lease_expires = current_time + timedelta(seconds=lease_seconds)

        stmt = (
            select(OutboxMessage)
            .where(
                OutboxMessage.topic.in_(topics),
                OutboxMessage.attempts < OutboxMessage.max_attempts,
                or_(
                    and_(
                        OutboxMessage.status.in_(
                            [OutboxStatus.PENDING.value, OutboxStatus.FAILED.value]
                        ),
                        OutboxMessage.available_at <= current_time,
                    ),
                    and_(
                        OutboxMessage.status == OutboxStatus.CLAIMED.value,
                        OutboxMessage.lease_expires_at < current_time,
                    ),
                ),
            )
            .order_by(OutboxMessage.created_at.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )

        candidates = list(db.execute(stmt).scalars().all())
        claimed_messages: list[OutboxMessage] = []

        for msg in candidates:
            msg.status = OutboxStatus.CLAIMED.value
            msg.claimed_by = worker_id
            msg.claimed_at = current_time
            msg.lease_expires_at = lease_expires
            msg.attempts += 1
            claimed_messages.append(msg)

        if claimed_messages:
            db.flush()
            if auto_commit:
                db.commit()

        return claimed_messages

    def ack(
        self,
        db: Session,
        message_id: str,
        worker_id: str,
        *,
        now: datetime | None = None,
        auto_commit: bool = False,
    ) -> OutboxMessage:
        """
        Acknowledge successful completion of a message task.

        Only the worker currently holding a valid, unexpired lease can ack.
        """
        if not worker_id:
            raise ValueError("worker_id must not be empty")

        stmt = select(OutboxMessage).where(OutboxMessage.id == message_id).with_for_update()
        msg = db.execute(stmt).scalar_one_or_none()
        if msg is None:
            raise MessageNotFoundError(f"OutboxMessage {message_id} not found")

        if msg.status != OutboxStatus.CLAIMED.value:
            raise InvalidLeaseError(
                f"Cannot ack message {message_id} in status {msg.status!r} (expected CLAIMED)"
            )

        if msg.claimed_by != worker_id:
            raise InvalidLeaseError(
                f"Worker {worker_id!r} does not hold lease for message {message_id} "
                f"(currently claimed by {msg.claimed_by!r})"
            )

        current_time = _ensure_utc(now) or utc_now()
        if msg.lease_expires_at is not None and msg.lease_expires_at < current_time:
            raise InvalidLeaseError(
                f"Lease for message {message_id} expired at {msg.lease_expires_at} "
                f"(current time: {current_time})"
            )

        msg.status = OutboxStatus.DONE.value
        msg.processed_at = current_time
        db.flush()
        if auto_commit:
            db.commit()
        return msg

    def renew(
        self,
        db: Session,
        message_id: str,
        worker_id: str,
        lease_seconds: int = 60,
        *,
        now: datetime | None = None,
        auto_commit: bool = False,
    ) -> OutboxMessage:
        """
        Extend an active lease without releasing it.

        A single order task can outlive the initial lease (network retries, exchange
        rate limits, reconciliation). Without renewal the message is reclaimed by
        `reclaim_expired` and re-executed while the first worker is still running,
        which is exactly the duplicate-submission window this queue is meant to close.

        Only the current owner, holding an unexpired lease, may renew. Renewal never
        increments `attempts` and never changes the message status.
        """
        if not worker_id:
            raise ValueError("worker_id must not be empty")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")

        stmt = select(OutboxMessage).where(OutboxMessage.id == message_id).with_for_update()
        msg = db.execute(stmt).scalar_one_or_none()
        if msg is None:
            raise MessageNotFoundError(f"OutboxMessage {message_id} not found")

        if msg.status != OutboxStatus.CLAIMED.value:
            raise InvalidLeaseError(
                f"Cannot renew message {message_id} in status {msg.status!r} (expected CLAIMED)"
            )
        if msg.claimed_by != worker_id:
            raise InvalidLeaseError(
                f"Worker {worker_id!r} does not hold lease for message {message_id} "
                f"(currently claimed by {msg.claimed_by!r})"
            )

        current_time = _ensure_utc(now) or utc_now()
        if msg.lease_expires_at is not None:
            previous_expiry = _ensure_utc(msg.lease_expires_at)
            if previous_expiry is not None and previous_expiry < current_time:
                raise InvalidLeaseError(
                    f"Lease for message {message_id} already expired at {previous_expiry}; "
                    f"the message must be reclaimed before it can be renewed"
                )

        msg.lease_expires_at = current_time + timedelta(seconds=lease_seconds)
        db.flush()
        if auto_commit:
            db.commit()
        return msg

    def nack(
        self,
        db: Session,
        message_id: str,
        worker_id: str,
        error: str | Exception | None = None,
        retry_delay: int | float = 0,
        *,
        now: datetime | None = None,
        auto_commit: bool = False,
    ) -> OutboxMessage:
        """
        Negatively acknowledge a message task upon failure.

        Releases the lease and either schedules a retry (status FAILED) or transitions
        to DEAD if attempts reach max_attempts.
        """
        if not worker_id:
            raise ValueError("worker_id must not be empty")

        stmt = select(OutboxMessage).where(OutboxMessage.id == message_id).with_for_update()
        msg = db.execute(stmt).scalar_one_or_none()
        if msg is None:
            raise MessageNotFoundError(f"OutboxMessage {message_id} not found")

        if msg.status != OutboxStatus.CLAIMED.value:
            raise InvalidLeaseError(
                f"Cannot nack message {message_id} in status {msg.status!r} (expected CLAIMED)"
            )

        if msg.claimed_by != worker_id:
            raise InvalidLeaseError(
                f"Worker {worker_id!r} does not hold lease for message {message_id} "
                f"(currently claimed by {msg.claimed_by!r})"
            )

        current_time = _ensure_utc(now) or utc_now()
        if msg.lease_expires_at is not None and msg.lease_expires_at < current_time:
            raise InvalidLeaseError(
                f"Lease for message {message_id} expired at {msg.lease_expires_at} "
                f"(current time: {current_time})"
            )

        err_text = str(error) if error is not None else None
        msg.last_error = err_text
        msg.claimed_by = None
        msg.claimed_at = None
        msg.lease_expires_at = None

        if msg.attempts >= msg.max_attempts:
            msg.status = OutboxStatus.DEAD.value
            msg.processed_at = current_time
        else:
            msg.status = OutboxStatus.FAILED.value
            delay_seconds = max(0.0, float(retry_delay or 0))
            msg.available_at = current_time + timedelta(seconds=delay_seconds)

        db.flush()
        if auto_commit:
            db.commit()
        return msg

    def reclaim_expired(
        self,
        db: Session,
        *,
        now: datetime | None = None,
        topic: str | list[str] | None = None,
        auto_commit: bool = False,
    ) -> list[OutboxMessage]:
        """
        Crash recovery: reclaim expired leases.

        Messages whose lease expired and whose attempts reached max_attempts transition
        to DEAD. All others transition to PENDING with cleared lease metadata.
        """
        current_time = _ensure_utc(now) or utc_now()
        filters = [
            OutboxMessage.status == OutboxStatus.CLAIMED.value,
            OutboxMessage.lease_expires_at < current_time,
        ]
        if topic is not None:
            if isinstance(topic, str):
                filters.append(OutboxMessage.topic == topic)
            else:
                filters.append(OutboxMessage.topic.in_(topic))

        stmt = select(OutboxMessage).where(*filters).with_for_update(skip_locked=True)
        expired = list(db.execute(stmt).scalars().all())
        reclaimed: list[OutboxMessage] = []

        for msg in expired:
            old_worker = msg.claimed_by
            if msg.attempts >= msg.max_attempts:
                msg.status = OutboxStatus.DEAD.value
                msg.processed_at = current_time
                msg.last_error = (
                    f"Lease expired (worker {old_worker}); "
                    f"attempts ({msg.attempts}) reached max_attempts ({msg.max_attempts})"
                )
            else:
                msg.status = OutboxStatus.PENDING.value
                msg.claimed_by = None
                msg.claimed_at = None
                msg.lease_expires_at = None
                msg.available_at = current_time
                msg.last_error = f"Lease expired (worker {old_worker}); reclaimed for retry"
            reclaimed.append(msg)

        if reclaimed:
            db.flush()
            if auto_commit:
                db.commit()

        return reclaimed

    def get(self, db: Session, message_id: str) -> OutboxMessage | None:
        """Retrieve an outbox message by id."""
        return db.execute(
            select(OutboxMessage).where(OutboxMessage.id == message_id)
        ).scalar_one_or_none()


# Singleton service instance + function aliases
outbox_service = OutboxService()
enqueue = outbox_service.enqueue
claim = outbox_service.claim
ack = outbox_service.ack
renew = outbox_service.renew
nack = outbox_service.nack
reclaim_expired = outbox_service.reclaim_expired
