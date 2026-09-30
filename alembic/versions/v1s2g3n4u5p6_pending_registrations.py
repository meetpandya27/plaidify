"""Pending registrations: sign-ups waiting for their email address to be proven

Revision ID: v1s2g3n4u5p6
Revises: u1j2k3l4m5n6
Create Date: 2026-09-30

- ``pending_registrations``: with REGISTRATION_EMAIL_VERIFICATION on,
  ``POST /auth/register`` stores the username, the address and the password
  hash here and mails the address a one-time token; ``POST /auth/verify-email``
  with that token creates the account and deletes the row. Only the token's
  SHA-256 is stored. One row per address (a new sign-up replaces it); rows
  expire after 24 hours and are purged by the hourly auth cleanup.
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "v1s2g3n4u5p6"
down_revision: Union[str, Sequence[str], None] = "u1j2k3l4m5n6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pending_registrations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("hashed_password", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
        sa.UniqueConstraint("token_hash"),
    )


def downgrade() -> None:
    op.drop_table("pending_registrations")
