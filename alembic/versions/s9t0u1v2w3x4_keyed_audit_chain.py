"""Keyed audit chain: per-entry signing key id and the chain head

Revision ID: s9t0u1v2w3x4
Revises: r8s9t0u1v2w3
Create Date: 2026-09-29

``audit_logs.key_id`` names the HMAC-SHA256 key an entry was signed with.
Entries written before this revision were hashed with plain SHA-256 and are
marked ``sha256``; verification accepts them only as the chain's prefix.

``audit_chain_head`` is a single row (id = 1) that every append rewrites under
the chain lock. It records the newest entry and carries its own MAC, so
deleting the newest entries is detectable, and on SQLite it is the row whose
write serializes appends. The application creates it on the first append.
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "s9t0u1v2w3x4"
down_revision: Union[str, Sequence[str], None] = "r8s9t0u1v2w3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "audit_logs",
        sa.Column("key_id", sa.String(16), nullable=False, server_default="sha256"),
    )
    op.create_table(
        "audit_chain_head",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("last_entry_id", sa.Integer(), nullable=True),
        sa.Column("last_entry_hash", sa.String(64), nullable=True),
        sa.Column("key_id", sa.String(16), nullable=True),
        sa.Column("seal_pending", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("head_mac", sa.String(64), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("audit_chain_head")
    with op.batch_alter_table("audit_logs") as batch:
        batch.drop_column("key_id")
