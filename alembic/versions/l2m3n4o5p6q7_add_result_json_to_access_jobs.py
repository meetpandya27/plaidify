"""Add result_json to access_jobs.

Revision ID: l2m3n4o5p6q7
Revises: k1l2m3n4o5p6
Create Date: 2026-04-19 16:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "l2m3n4o5p6q7"
down_revision = "k1l2m3n4o5p6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("access_jobs", sa.Column("result_json", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("access_jobs", "result_json")