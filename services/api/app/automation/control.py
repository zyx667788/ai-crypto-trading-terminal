from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.core.precision import utc_iso, utc_now
from app.models.automation import AutomationControl
from app.models.entities import AuditLog

logger = get_logger("terminal.automation.control")

GLOBAL_CONTROL_ID = "global"


class AutomationStatus(dict):
    """
    Dictionary subclass providing both attribute-style (.running)
    and key-style (['running']) access to automation control status.
    """

    def __getattr__(self, item: str) -> Any:
        try:
            return self[item]
        except KeyError as exc:
            raise AttributeError(item) from exc

    def __setattr__(self, key: str, value: Any) -> None:
        self[key] = value


def _record_audit(
    db: Session,
    actor: str,
    action: str,
    resource: str,
    before: dict[str, Any],
    after: dict[str, Any],
) -> None:
    audit = AuditLog(
        actor=actor,
        action=action,
        resource=resource,
        before_json=before,
        after_json=after,
    )
    db.add(audit)


def get_control_row(db: Session, for_update: bool = False) -> AutomationControl:
    """
    Retrieve the singleton AutomationControl row (id='global').
    If the row does not exist, automatically initialize it with fail-closed defaults.
    If the row already exists, never overwrite or clear existing manual blocks.
    When for_update=True, acquires a row-level lock (SELECT ... FOR UPDATE).
    """
    stmt = select(AutomationControl).where(AutomationControl.id == GLOBAL_CONTROL_ID)
    if for_update:
        stmt = stmt.with_for_update()

    row = db.execute(stmt).scalar_one_or_none()
    if row is not None:
        return row

    # Row doesn't exist yet -> auto-initialize with fail-closed defaults
    # (running=False, paused=True, manual_block_reason=None)
    init_row = AutomationControl(
        id=GLOBAL_CONTROL_ID,
        running=False,
        paused=True,
        environment="demo",
        revision=1,
        updated_by="system",
        pause_reason="Default initialized closed",
        manual_block_reason=None,
    )
    db.add(init_row)
    try:
        db.commit()
        db.refresh(init_row)
    except IntegrityError:
        # Another concurrent worker initialized the row simultaneously
        db.rollback()
        stmt = select(AutomationControl).where(AutomationControl.id == GLOBAL_CONTROL_ID)
        if for_update:
            stmt = stmt.with_for_update()
        init_row = db.execute(stmt).scalar_one()

    return init_row


def can_create_orders(db: Session) -> bool:
    """
    Gate check for order creation.
    Fail-closed policy:
    Returns False if:
      - settings.AUTO_TRADING_ENABLED is False
      - Database lookup fails
      - Automation is not running (running == False)
      - Automation is paused (paused == True)
      - A manual block is active (manual_block_reason is not None and not empty)
    """
    if not getattr(settings, "AUTO_TRADING_ENABLED", False):
        return False

    try:
        row = get_control_row(db, for_update=False)
        if not row.running:
            return False
        if row.paused:
            return False
        if row.manual_block_reason and str(row.manual_block_reason).strip():
            return False
        return True
    except Exception as exc:
        logger.error(
            "can_create_orders_check_failed_fail_closed",
            extra={"structured_data": {"error": str(exc)}},
        )
        return False


def _build_status(row: AutomationControl, db: Session) -> AutomationStatus:
    orders_allowed = can_create_orders(db)
    return AutomationStatus(
        id=row.id,
        running=row.running,
        paused=row.paused,
        environment=row.environment,
        revision=row.revision,
        updated_by=row.updated_by,
        pause_reason=row.pause_reason,
        manual_block_reason=row.manual_block_reason,
        created_at=utc_iso(row.created_at) if row.created_at else None,
        updated_at=utc_iso(row.updated_at) if row.updated_at else None,
        auto_trading_enabled=bool(getattr(settings, "AUTO_TRADING_ENABLED", False)),
        can_create_orders=orders_allowed,
    )


def get_status(db: Session) -> AutomationStatus:
    """
    Get current automation status.
    Auto-initializes the global row if missing without modifying existing blocks.
    Does not rely on in-memory process state.
    """
    row = get_control_row(db, for_update=False)
    return _build_status(row, db)


def start(
    db: Session,
    environment: str = "demo",
    actor: str = "operator",
    expected_revision: int | None = None,
) -> AutomationStatus:
    """
    Start the automation engine.
    Fails closed (ValueError) if AUTO_TRADING_ENABLED is False or if a manual block is active.
    Applies row-level lock and increments revision to prevent lost updates.
    Commits immediately for cross-process/cross-session visibility.
    """
    if not getattr(settings, "AUTO_TRADING_ENABLED", False):
        raise ValueError(
            "Cannot start automation: AUTO_TRADING_ENABLED is False (safety gate)."
        )

    row = get_control_row(db, for_update=True)

    if row.manual_block_reason and str(row.manual_block_reason).strip():
        raise ValueError(
            f"Cannot start automation: manual block is active ('{row.manual_block_reason}'). "
            f"Clear the manual block first."
        )

    if expected_revision is not None and row.revision != expected_revision:
        raise ValueError(
            f"Concurrency conflict: expected revision {expected_revision}, got {row.revision}."
        )

    before = row.to_dict()

    row.running = True
    row.paused = False
    row.environment = environment
    row.pause_reason = None
    row.updated_by = actor
    row.updated_at = utc_now()
    row.revision += 1

    after = row.to_dict()
    _record_audit(
        db=db,
        actor=actor,
        action="automation.start",
        resource=f"automation:{row.id}",
        before=before,
        after=after,
    )

    db.commit()
    db.refresh(row)
    return _build_status(row, db)


def pause(
    db: Session,
    reason: str = "Operator paused new entries",
    actor: str = "operator",
    expected_revision: int | None = None,
) -> AutomationStatus:
    """
    Pause automation entries.
    Applies row-level lock and increments revision to prevent lost updates.
    Commits immediately for cross-process/cross-session visibility.
    """
    row = get_control_row(db, for_update=True)

    if expected_revision is not None and row.revision != expected_revision:
        raise ValueError(
            f"Concurrency conflict: expected revision {expected_revision}, got {row.revision}."
        )

    before = row.to_dict()

    row.paused = True
    row.pause_reason = reason
    row.updated_by = actor
    row.updated_at = utc_now()
    row.revision += 1

    after = row.to_dict()
    _record_audit(
        db=db,
        actor=actor,
        action="automation.pause",
        resource=f"automation:{row.id}",
        before=before,
        after=after,
    )

    db.commit()
    db.refresh(row)
    return _build_status(row, db)


def resume(
    db: Session,
    actor: str = "operator",
    expected_revision: int | None = None,
) -> AutomationStatus:
    """
    Resume automation entries if paused.
    Fails closed if AUTO_TRADING_ENABLED is False or if a manual block is active.
    """
    if not getattr(settings, "AUTO_TRADING_ENABLED", False):
        raise ValueError(
            "Cannot resume automation: AUTO_TRADING_ENABLED is False (safety gate)."
        )

    row = get_control_row(db, for_update=True)

    if row.manual_block_reason and str(row.manual_block_reason).strip():
        raise ValueError(
            f"Cannot resume automation: manual block is active ('{row.manual_block_reason}'). "
            f"Clear the manual block first."
        )

    if expected_revision is not None and row.revision != expected_revision:
        raise ValueError(
            f"Concurrency conflict: expected revision {expected_revision}, got {row.revision}."
        )

    before = row.to_dict()

    row.paused = False
    row.pause_reason = None
    row.updated_by = actor
    row.updated_at = utc_now()
    row.revision += 1

    after = row.to_dict()
    _record_audit(
        db=db,
        actor=actor,
        action="automation.resume",
        resource=f"automation:{row.id}",
        before=before,
        after=after,
    )

    db.commit()
    db.refresh(row)
    return _build_status(row, db)


def stop(
    db: Session,
    reason: str = "Operator stopped",
    actor: str = "operator",
    expected_revision: int | None = None,
) -> AutomationStatus:
    """
    Stop the automation engine completely (running=False, paused=False).
    """
    row = get_control_row(db, for_update=True)

    if expected_revision is not None and row.revision != expected_revision:
        raise ValueError(
            f"Concurrency conflict: expected revision {expected_revision}, got {row.revision}."
        )

    before = row.to_dict()

    row.running = False
    row.paused = False
    row.pause_reason = reason
    row.updated_by = actor
    row.updated_at = utc_now()
    row.revision += 1

    after = row.to_dict()
    _record_audit(
        db=db,
        actor=actor,
        action="automation.stop",
        resource=f"automation:{row.id}",
        before=before,
        after=after,
    )

    db.commit()
    db.refresh(row)
    return _build_status(row, db)


def set_manual_block(
    db: Session,
    reason: str,
    actor: str = "operator",
    expected_revision: int | None = None,
) -> AutomationStatus:
    """
    Manually block automation entries for safety.
    Sets manual_block_reason and forces paused=True.
    """
    if not reason or not reason.strip():
        raise ValueError("A non-empty reason is required to set a manual block.")

    row = get_control_row(db, for_update=True)

    if expected_revision is not None and row.revision != expected_revision:
        raise ValueError(
            f"Concurrency conflict: expected revision {expected_revision}, got {row.revision}."
        )

    before = row.to_dict()

    row.manual_block_reason = reason.strip()
    row.paused = True
    row.pause_reason = f"Manual block: {reason.strip()}"
    row.updated_by = actor
    row.updated_at = utc_now()
    row.revision += 1

    after = row.to_dict()
    _record_audit(
        db=db,
        actor=actor,
        action="automation.manual_block",
        resource=f"automation:{row.id}",
        before=before,
        after=after,
    )

    db.commit()
    db.refresh(row)
    return _build_status(row, db)


def clear_manual_block(
    db: Session,
    actor: str = "operator",
    expected_revision: int | None = None,
) -> AutomationStatus:
    """
    Clear manual safety block. Does not automatically unpause or start automation.
    """
    row = get_control_row(db, for_update=True)

    if expected_revision is not None and row.revision != expected_revision:
        raise ValueError(
            f"Concurrency conflict: expected revision {expected_revision}, got {row.revision}."
        )

    before = row.to_dict()

    row.manual_block_reason = None
    row.updated_by = actor
    row.updated_at = utc_now()
    row.revision += 1

    after = row.to_dict()
    _record_audit(
        db=db,
        actor=actor,
        action="automation.clear_manual_block",
        resource=f"automation:{row.id}",
        before=before,
        after=after,
    )

    db.commit()
    db.refresh(row)
    return _build_status(row, db)
