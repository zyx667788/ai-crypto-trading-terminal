"""add risk lock reset watermark

Revision ID: 0002_risk_lock_reset
Revises: 0001_initial_schema
Create Date: 2026-10-30 10:00:00.000000

Adds `risk_profiles.lock_reset_at` / `lock_reset_reason`.

Why: §3.7 requires the daily-loss lock to persist "until the user explicitly resets it".
The safe, append-only way to express that is a watermark — counters only consider activity
after the operator's acknowledgement. No order, fill or journal row is ever deleted.

Note: revision 0001 builds the schema from the live ORM metadata, so a brand-new database
already contains these columns. Every step here is therefore guarded so the migration is
idempotent and can also upgrade an older database that predates the columns.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_risk_lock_reset"
down_revision: Union[str, None] = "0001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _existing_columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    try:
        return {col["name"] for col in inspector.get_columns(table)}
    except Exception:  # table missing entirely
        return set()


def upgrade() -> None:
    columns = _existing_columns("risk_profiles")
    if not columns:
        return
    with op.batch_alter_table("risk_profiles") as batch:
        if "lock_reset_at" not in columns:
            batch.add_column(sa.Column("lock_reset_at", sa.DateTime(timezone=True), nullable=True))
        if "lock_reset_reason" not in columns:
            batch.add_column(sa.Column("lock_reset_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    columns = _existing_columns("risk_profiles")
    with op.batch_alter_table("risk_profiles") as batch:
        if "lock_reset_reason" in columns:
            batch.drop_column("lock_reset_reason")
        if "lock_reset_at" in columns:
            batch.drop_column("lock_reset_at")
