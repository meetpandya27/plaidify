"""Add audit_logs timestamp DESC index for retention queries

Revision ID: i9j0k1l2m3n4
Revises: h8i9j0k1l2m3
Create Date: 2026-04-16 00:00:00.000000
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "i9j0k1l2m3n4"
down_revision = "h8i9j0k1l2m3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Composite index for efficient retention cleanup and tail queries
    op.create_index(
        "ix_audit_logs_timestamp_desc",
        "audit_logs",
        ["timestamp"],
        postgresql_using="btree",
        postgresql_ops={"timestamp": "DESC"},
    )
    # Index for efficient ordered retrieval by ID (used by hash chain verification)
    op.create_index(
        "ix_audit_logs_id_desc",
        "audit_logs",
        ["id"],
        postgresql_using="btree",
        postgresql_ops={"id": "DESC"},
    )


def downgrade() -> None:
    op.drop_index("ix_audit_logs_id_desc", table_name="audit_logs")
    op.drop_index("ix_audit_logs_timestamp_desc", table_name="audit_logs")
