"""Store refresh tokens as SHA-256 hashes

Revision ID: r8s9t0u1v2w3
Revises: q7r8s9t0u1v2
Create Date: 2026-09-29

``refresh_tokens.token`` held the bearer value itself, so anyone who could
read the table (or a backup) could mint sessions. The column becomes
``token_hash`` (hex SHA-256 of the token) and existing rows are hashed in
place, so every refresh token issued before the upgrade keeps working.

``revoked`` becomes NOT NULL: rotation claims a token with
``UPDATE ... WHERE revoked = false``, which a NULL would never match.

Downgrade cannot recover the raw tokens, so it deletes the rows (every user
signs in again).
"""

import hashlib
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "r8s9t0u1v2w3"
down_revision: Union[str, Sequence[str], None] = "q7r8s9t0u1v2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_BATCH = 1000


def upgrade() -> None:
    bind = op.get_bind()

    # Hash in place, keyset-paginated so a large table is not held in memory.
    last_id = 0
    while True:
        rows = bind.execute(
            sa.text("SELECT id, token FROM refresh_tokens WHERE id > :last ORDER BY id LIMIT :n"),
            {"last": last_id, "n": _BATCH},
        ).fetchall()
        if not rows:
            break
        for row_id, token in rows:
            bind.execute(
                sa.text("UPDATE refresh_tokens SET token = :digest WHERE id = :id"),
                {"digest": hashlib.sha256(token.encode("utf-8")).hexdigest(), "id": row_id},
            )
        last_id = rows[-1][0]

    op.execute(sa.text("UPDATE refresh_tokens SET revoked = :f WHERE revoked IS NULL").bindparams(f=False))

    op.drop_index("ix_refresh_tokens_token", table_name="refresh_tokens")
    with op.batch_alter_table("refresh_tokens") as batch:
        batch.alter_column(
            "token",
            new_column_name="token_hash",
            existing_type=sa.String(),
            type_=sa.String(64),
            existing_nullable=False,
        )
        batch.alter_column(
            "revoked",
            existing_type=sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        )
    op.create_index("ix_refresh_tokens_token_hash", "refresh_tokens", ["token_hash"], unique=True)


def downgrade() -> None:
    op.execute("DELETE FROM refresh_tokens")
    op.drop_index("ix_refresh_tokens_token_hash", table_name="refresh_tokens")
    with op.batch_alter_table("refresh_tokens") as batch:
        batch.alter_column(
            "revoked",
            existing_type=sa.Boolean(),
            nullable=True,
            server_default=sa.false(),
        )
        batch.alter_column(
            "token_hash",
            new_column_name="token",
            existing_type=sa.String(64),
            type_=sa.String(),
            existing_nullable=False,
        )
    op.create_index("ix_refresh_tokens_token", "refresh_tokens", ["token"], unique=True)
