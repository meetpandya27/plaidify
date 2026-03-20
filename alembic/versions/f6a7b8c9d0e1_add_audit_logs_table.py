"""Add audit_logs table

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-01-20 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "f6a7b8c9d0e1"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_type", sa.String(), nullable=False, index=True),
        sa.Column("user_id", sa.Integer(), nullable=True, index=True),
        sa.Column("resource", sa.String(), nullable=True),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("metadata_json", sa.Text(), nullable=True),
        sa.Column("timestamp", sa.DateTime(), nullable=False, index=True),
        sa.Column("prev_hash", sa.String(64), nullable=True),
        sa.Column("entry_hash", sa.String(64), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("audit_logs")
