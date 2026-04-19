"""Add access_jobs table.

Revision ID: k1l2m3n4o5p6
Revises: j0k1l2m3n4o5
Create Date: 2026-04-19 12:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "k1l2m3n4o5p6"
down_revision = "j0k1l2m3n4o5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "access_jobs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("site", sa.String(), nullable=False),
        sa.Column("job_type", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("lock_scope", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=True),
        sa.Column("metadata_json", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_access_jobs_user_id", "access_jobs", ["user_id"])
    op.create_index("ix_access_jobs_site", "access_jobs", ["site"])
    op.create_index("ix_access_jobs_job_type", "access_jobs", ["job_type"])
    op.create_index("ix_access_jobs_status", "access_jobs", ["status"])
    op.create_index("ix_access_jobs_lock_scope", "access_jobs", ["lock_scope"])
    op.create_index("ix_access_jobs_session_id", "access_jobs", ["session_id"])


def downgrade() -> None:
    op.drop_index("ix_access_jobs_session_id", table_name="access_jobs")
    op.drop_index("ix_access_jobs_lock_scope", table_name="access_jobs")
    op.drop_index("ix_access_jobs_status", table_name="access_jobs")
    op.drop_index("ix_access_jobs_job_type", table_name="access_jobs")
    op.drop_index("ix_access_jobs_site", table_name="access_jobs")
    op.drop_index("ix_access_jobs_user_id", table_name="access_jobs")
    op.drop_table("access_jobs")