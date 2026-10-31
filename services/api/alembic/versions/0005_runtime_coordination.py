"""durable recovery checkpoints and worker heartbeats

Revision ID: 0005_runtime_coordination
Revises: 0004_outbox
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005_runtime_coordination"
down_revision: Union[str, None] = "0004_outbox"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    tables = _tables()
    if "recovery_checkpoints" not in tables:
        op.create_table(
            "recovery_checkpoints",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("state", sa.String(64), nullable=False),
            sa.Column("blockers_json", sa.JSON(), nullable=False),
            sa.Column("steps_json", sa.JSON(), nullable=False),
            sa.Column("counts_json", sa.JSON(), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
    if "worker_heartbeats" not in tables:
        op.create_table(
            "worker_heartbeats",
            sa.Column("worker_id", sa.String(96), primary_key=True),
            sa.Column("role", sa.String(48), nullable=False),
            sa.Column("hostname", sa.String(128), nullable=False),
            sa.Column("pid", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_duration_ms", sa.Integer(), nullable=False),
            sa.Column("runs", sa.Integer(), nullable=False),
            sa.Column("consecutive_failures", sa.Integer(), nullable=False),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("details_json", sa.JSON(), nullable=False),
            sa.Column("stopping", sa.Boolean(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_worker_heartbeats_role", "worker_heartbeats", ["role"])
        op.create_index("ix_worker_heartbeats_updated_at", "worker_heartbeats", ["updated_at"])


def downgrade() -> None:
    tables = _tables()
    if "worker_heartbeats" in tables:
        op.drop_table("worker_heartbeats")
    if "recovery_checkpoints" in tables:
        op.drop_table("recovery_checkpoints")
