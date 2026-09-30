"""Session versions, verified emails, agent-bound consent, sign-in throttles, maintenance leases

Revision ID: u1a2b3c4d5e6
Revises: t0u1v2w3x4y5
Create Date: 2026-09-30

- ``users.token_version``: carried in every access token (``tv``). A password
  reset, "sign out everywhere" and deactivation bump it, which ends every
  access token issued before.
- ``users.email_verified``: OAuth sign-in links an identity into an existing
  account only when that account's address is proven. Accounts that were
  created from a verified provider email (OAuth subject, no password) are
  marked verified; password accounts start unverified.
- ``consent_requests.agent_id`` / ``consent_grants.agent_id``: the agent a
  consent was asked by and granted to. ``/fetch_data`` by an agent key needs a
  grant bound to that agent. Existing rows keep NULL (owner-only grants).
- ``login_throttles``: failed password sign-ins per (username, client
  address) and per username, keyed by HMACs of those values.
- ``maintenance_leases``: which API process runs the periodic cleanup jobs.

SQLite cannot add a foreign key to an existing table; like the earlier
revisions, a SQLite development database gets the columns without the
constraint (it does not enforce foreign keys anyway).
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "u1a2b3c4d5e6"
down_revision: Union[str, Sequence[str], None] = "t0u1v2w3x4y5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_AGENT_TABLES = ("consent_requests", "consent_grants")


def upgrade() -> None:
    bind = op.get_bind()
    sqlite = bind.dialect.name == "sqlite"

    op.add_column("users", sa.Column("token_version", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("users", sa.Column("email_verified", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.execute(
        sa.text(
            "UPDATE users SET email_verified = :verified WHERE oauth_sub IS NOT NULL AND hashed_password IS NULL"
        ).bindparams(verified=True)
    )

    for table in _AGENT_TABLES:
        op.add_column(table, sa.Column("agent_id", sa.String(), nullable=True))
        if not sqlite:
            op.create_foreign_key(f"fk_{table}_agent_id", table, "agents", ["agent_id"], ["id"], ondelete="CASCADE")
        op.create_index(f"ix_{table}_agent_id", table, ["agent_id"])

    op.create_table(
        "login_throttles",
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("subject", sa.String(64), nullable=False),
        sa.Column("failures", sa.Integer(), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_index("ix_login_throttles_subject", "login_throttles", ["subject"])
    op.create_index("ix_login_throttles_updated_at", "login_throttles", ["updated_at"])

    op.create_table(
        "maintenance_leases",
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("holder", sa.String(128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("name"),
    )


def downgrade() -> None:
    sqlite = op.get_bind().dialect.name == "sqlite"

    op.drop_table("maintenance_leases")
    op.drop_index("ix_login_throttles_updated_at", table_name="login_throttles")
    op.drop_index("ix_login_throttles_subject", table_name="login_throttles")
    op.drop_table("login_throttles")

    for table in reversed(_AGENT_TABLES):
        op.drop_index(f"ix_{table}_agent_id", table_name=table)
        if not sqlite:
            op.drop_constraint(f"fk_{table}_agent_id", table, type_="foreignkey")
        with op.batch_alter_table(table) as batch:
            batch.drop_column("agent_id")

    with op.batch_alter_table("users") as batch:
        batch.drop_column("email_verified")
        batch.drop_column("token_version")
