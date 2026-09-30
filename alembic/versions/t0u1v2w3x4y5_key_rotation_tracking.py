"""Track which master-key version protects each encrypted row

Revision ID: t0u1v2w3x4y5
Revises: s9t0u1v2w3x4
Create Date: 2026-09-29

- ``users.dek_key_version``: the ``ENCRYPTION_KEY_VERSION`` whose key wraps
  ``encrypted_dek`` (local KMS provider). NULL for rows written before this
  revision; the rotation sweep checks those and stamps them.
- ``webhooks.key_version``: like ``access_tokens.key_version``, the key
  version the secret was last encrypted under.
- Indexes on the three version columns, so the rotation sweep's "anything
  stale?" query is an index probe instead of a table scan.
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "t0u1v2w3x4y5"
down_revision: Union[str, Sequence[str], None] = "s9t0u1v2w3x4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("dek_key_version", sa.Integer(), nullable=True))
    op.add_column(
        "webhooks",
        sa.Column("key_version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_index("ix_users_dek_key_version", "users", ["dek_key_version"])
    op.create_index("ix_webhooks_key_version", "webhooks", ["key_version"])
    op.create_index("ix_access_tokens_key_version", "access_tokens", ["key_version"])


def downgrade() -> None:
    op.drop_index("ix_access_tokens_key_version", table_name="access_tokens")
    op.drop_index("ix_webhooks_key_version", table_name="webhooks")
    op.drop_index("ix_users_dek_key_version", table_name="users")
    with op.batch_alter_table("webhooks") as batch:
        batch.drop_column("key_version")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("dek_key_version")
