"""Access-job liveness and error fields, a DB-driven refresh schedule, and the webhook outbox

Revision ID: u1j2k3l4m5n6
Revises: u1a2b3c4d5e6
Create Date: 2026-09-30

- ``access_jobs``: ``error_code`` / ``error_type`` / ``error_status`` record
  how a job failed, so a job run by the redis-worker executor fails with the
  same error (and HTTP status) as one run in-process; ``worker_id``,
  ``heartbeat_at`` and ``deadline_at`` let the reaper find jobs whose process
  died or that ran past their deadline.
- ``scheduled_refresh_jobs``: ``next_run_at`` (the scheduler claims a due row
  by moving it forward) and ``disabled_reason`` (e.g. ``needs_reauth``).
- ``webhook_deliveries``: the durable webhook outbox, one row per event per
  endpoint, retried with exponential backoff.
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "u1j2k3l4m5n6"
down_revision: Union[str, Sequence[str], None] = "u1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("access_jobs", sa.Column("error_code", sa.String(length=64), nullable=True))
    op.add_column("access_jobs", sa.Column("error_type", sa.String(length=64), nullable=True))
    op.add_column("access_jobs", sa.Column("error_status", sa.Integer(), nullable=True))
    op.add_column("access_jobs", sa.Column("worker_id", sa.String(length=128), nullable=True))
    op.add_column("access_jobs", sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("access_jobs", sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True))

    op.add_column("scheduled_refresh_jobs", sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("scheduled_refresh_jobs", sa.Column("disabled_reason", sa.String(length=64), nullable=True))
    op.create_index("ix_scheduled_refresh_jobs_next_run_at", "scheduled_refresh_jobs", ["next_run_at"])

    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("webhook_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("event", sa.String(length=64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status_code", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["webhook_id"], ["webhooks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_webhook_deliveries_webhook_id", "webhook_deliveries", ["webhook_id"])
    op.create_index("ix_webhook_deliveries_user_id", "webhook_deliveries", ["user_id"])
    op.create_index("ix_webhook_deliveries_status", "webhook_deliveries", ["status"])
    op.create_index("ix_webhook_deliveries_next_attempt_at", "webhook_deliveries", ["next_attempt_at"])


def downgrade() -> None:
    op.drop_index("ix_webhook_deliveries_next_attempt_at", table_name="webhook_deliveries")
    op.drop_index("ix_webhook_deliveries_status", table_name="webhook_deliveries")
    op.drop_index("ix_webhook_deliveries_user_id", table_name="webhook_deliveries")
    op.drop_index("ix_webhook_deliveries_webhook_id", table_name="webhook_deliveries")
    op.drop_table("webhook_deliveries")

    op.drop_index("ix_scheduled_refresh_jobs_next_run_at", table_name="scheduled_refresh_jobs")
    with op.batch_alter_table("scheduled_refresh_jobs") as batch:
        batch.drop_column("disabled_reason")
        batch.drop_column("next_run_at")

    with op.batch_alter_table("access_jobs") as batch:
        batch.drop_column("deadline_at")
        batch.drop_column("heartbeat_at")
        batch.drop_column("worker_id")
        batch.drop_column("error_status")
        batch.drop_column("error_type")
        batch.drop_column("error_code")
