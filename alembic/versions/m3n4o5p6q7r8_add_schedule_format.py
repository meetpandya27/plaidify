"""Add schedule_format to scheduled_refresh_jobs.

Revision ID: m3n4o5p6q7r8
Revises: l2m3n4o5p6q7
Create Date: 2026-04-24 12:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "m3n4o5p6q7r8"
down_revision = "l2m3n4o5p6q7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "scheduled_refresh_jobs",
        sa.Column(
            "schedule_format",
            sa.String(),
            nullable=False,
            server_default="interval",
        ),
    )


def downgrade() -> None:
    op.drop_column("scheduled_refresh_jobs", "schedule_format")
