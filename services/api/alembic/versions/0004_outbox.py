"""add outbox_messages table

Revision ID: 0004_outbox
Revises: 0003_automation_control
Create Date: 2026-10-31 09:30:00.000000

Creates `outbox_messages` table for reliable cross-process task distribution.
Guarded with table existence check to remain idempotent across SQLite / Postgres.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004_outbox"
down_revision: Union[str, None] = "0003_automation_control"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    try:
        return table_name in inspector.get_table_names()
    except Exception:
        return False


def upgrade() -> None:
    if not _table_exists("outbox_messages"):
        op.create_table(
            "outbox_messages",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("topic", sa.String(64), nullable=False),
            sa.Column("aggregate_id", sa.String(64), nullable=True),
            sa.Column("dedup_key", sa.String(128), nullable=False),
            sa.Column("payload_json", sa.JSON(), nullable=False),
            sa.Column("status", sa.String(32), nullable=False, server_default="PENDING"),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
            sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("claimed_by", sa.String(64), nullable=True),
            sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("dedup_key", name="uq_outbox_messages_dedup_key"),
        )
        op.create_index("ix_outbox_messages_topic", "outbox_messages", ["topic"])
        op.create_index("ix_outbox_messages_aggregate_id", "outbox_messages", ["aggregate_id"])
        op.create_index("ix_outbox_messages_dedup_key", "outbox_messages", ["dedup_key"])
        op.create_index("ix_outbox_messages_status", "outbox_messages", ["status"])
        op.create_index("ix_outbox_messages_available_at", "outbox_messages", ["available_at"])
        op.create_index("ix_outbox_messages_claimed_by", "outbox_messages", ["claimed_by"])
        op.create_index("ix_outbox_messages_lease_expires_at", "outbox_messages", ["lease_expires_at"])
        op.create_index("ix_outbox_messages_created_at", "outbox_messages", ["created_at"])
        op.create_index("ix_outbox_claim", "outbox_messages", ["topic", "status", "available_at"])
        op.create_index("ix_outbox_lease", "outbox_messages", ["status", "lease_expires_at"])


def downgrade() -> None:
    if _table_exists("outbox_messages"):
        op.drop_table("outbox_messages")
