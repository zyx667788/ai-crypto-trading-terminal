"""add automation control persistence table

Revision ID: 0003_automation_control
Revises: 0002_risk_lock_reset
Create Date: 2026-10-31 09:00:00.000000

Creates `automation_controls` table for singleton global automation state.
Guarded with table existence check to remain idempotent across SQLite / Postgres.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003_automation_control"
down_revision: Union[str, None] = "0002_risk_lock_reset"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    try:
        return table_name in inspector.get_table_names()
    except Exception:
        return False


def upgrade() -> None:
    if not _table_exists("automation_controls"):
        op.create_table(
            "automation_controls",
            sa.Column("id", sa.String(length=32), primary_key=True),
            sa.Column("running", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("paused", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("environment", sa.String(length=32), nullable=False, server_default="demo"),
            sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("updated_by", sa.String(length=64), nullable=True),
            sa.Column("pause_reason", sa.Text(), nullable=True),
            sa.Column("manual_block_reason", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )


def downgrade() -> None:
    if _table_exists("automation_controls"):
        op.drop_table("automation_controls")
