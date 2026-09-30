"""Sync ORM drift found on PostgreSQL: column types, key prefix width, indexes

Revision ID: p6q7r8s9t0u1
Revises: o5p6q7r8s9t0
Create Date: 2026-09-29

Closes the remaining differences between src/database.py and the migration
history, as reported by ``alembic check`` against PostgreSQL:

- ``agents.rate_limit`` was created as INTEGER but the model (and the agents
  router) store a limit string such as ``"30/minute"``. A bare number meant
  requests per minute and is converted to that form.
- ``agents.description`` is TEXT in the model.
- ``api_keys.key_prefix`` was VARCHAR(12), but agent keys store a 16-character
  prefix (``pk_agent_`` + 7), which PostgreSQL rejected. Widened to 32.
- ``api_keys.key_hash`` carried both a UNIQUE constraint and the unique index
  ``ix_api_keys_key_hash``; the redundant constraint is dropped.
- ``ix_audit_logs_id_desc`` / ``ix_audit_logs_timestamp_desc`` duplicated the
  primary key and ``ix_audit_logs_timestamp`` (a B-tree is scanned in either
  direction) and are dropped.
- Indexes the models declare but no migration created are added.

Index work is conditional on the inspector so a SQLite development database
that ran the older, SQLite-skipping ``j0k1l2m3n4o5`` converges too.
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "p6q7r8s9t0u1"
down_revision: Union[str, Sequence[str], None] = "o5p6q7r8s9t0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Indexes declared by the models (index=True) that no earlier revision created.
_MODEL_INDEXES = [
    ("ix_audit_logs_id", "audit_logs", ["id"]),
    ("ix_consent_grants_access_token", "consent_grants", ["access_token"]),
    ("ix_consent_grants_consent_request_id", "consent_grants", ["consent_request_id"]),
    ("ix_consent_grants_user_id", "consent_grants", ["user_id"]),
    ("ix_consent_requests_access_token", "consent_requests", ["access_token"]),
    ("ix_consent_requests_user_id", "consent_requests", ["user_id"]),
    ("ix_password_reset_tokens_id", "password_reset_tokens", ["id"]),
    ("ix_public_tokens_token", "public_tokens", ["token"]),
    ("ix_refresh_tokens_id", "refresh_tokens", ["id"]),
    ("ix_scheduled_refresh_jobs_user_id", "scheduled_refresh_jobs", ["user_id"]),
    ("ix_webhooks_id", "webhooks", ["id"]),
]

# Created by j0k1l2m3n4o5; missing only on SQLite databases migrated before
# that revision stopped skipping SQLite. Created here if absent, never dropped
# here (j0k1l2m3n4o5's downgrade owns them).
_J0K1_INDEXES = [
    ("ix_links_user_id", "links", ["user_id"]),
    ("ix_access_tokens_user_id", "access_tokens", ["user_id"]),
    ("ix_webhooks_user_id", "webhooks", ["user_id"]),
]

# Redundant with the primary key / ix_audit_logs_timestamp.
_DESC_INDEXES = [
    ("ix_audit_logs_timestamp_desc", "timestamp"),
    ("ix_audit_logs_id_desc", "id"),
]


def _index_names(table: str) -> set[str]:
    return {ix["name"] for ix in sa.inspect(op.get_bind()).get_indexes(table)}


def _key_hash_unique_constraints() -> list[str]:
    return [
        uc["name"]
        for uc in sa.inspect(op.get_bind()).get_unique_constraints("api_keys")
        if uc["column_names"] == ["key_hash"] and uc.get("name")
    ]


def upgrade() -> None:
    bind = op.get_bind()
    sqlite = bind.dialect.name == "sqlite"

    # ── agents: rate_limit INTEGER -> VARCHAR, description VARCHAR -> TEXT ──
    if sqlite:
        # SQLite kept whatever was written (INTEGER affinity); convert the
        # numeric ones before the table copy turns them into text.
        op.execute(
            "UPDATE agents SET rate_limit = CAST(rate_limit AS TEXT) || '/minute' WHERE typeof(rate_limit) = 'integer'"
        )
    with op.batch_alter_table("agents") as batch:
        batch.alter_column(
            "rate_limit",
            existing_type=sa.Integer(),
            type_=sa.String(),
            existing_nullable=True,
            postgresql_using="CASE WHEN rate_limit IS NULL THEN NULL ELSE rate_limit::text || '/minute' END",
        )
        batch.alter_column("description", existing_type=sa.String(), type_=sa.Text(), existing_nullable=True)

    # ── api_keys: widen key_prefix, drop the duplicate UNIQUE on key_hash ──
    if sqlite:
        # The inline UNIQUE is unnamed on SQLite; name it for the table copy.
        with op.batch_alter_table("api_keys", naming_convention={"uq": "uq_%(table_name)s_%(column_0_name)s"}) as batch:
            batch.alter_column("key_prefix", existing_type=sa.String(12), type_=sa.String(32), existing_nullable=False)
            batch.drop_constraint("uq_api_keys_key_hash", type_="unique")
    else:
        op.alter_column(
            "api_keys", "key_prefix", existing_type=sa.String(12), type_=sa.String(32), existing_nullable=False
        )
        for name in _key_hash_unique_constraints():
            op.drop_constraint(name, "api_keys", type_="unique")

    # ── indexes ──
    existing = _index_names("audit_logs")
    for name, _column in _DESC_INDEXES:
        if name in existing:
            op.drop_index(name, table_name="audit_logs")

    for name, table, columns in _MODEL_INDEXES + _J0K1_INDEXES:
        if name not in _index_names(table):
            op.create_index(name, table, columns)


def downgrade() -> None:
    bind = op.get_bind()
    sqlite = bind.dialect.name == "sqlite"

    for name, table, _columns in reversed(_MODEL_INDEXES):
        if name in _index_names(table):
            op.drop_index(name, table_name=table)

    existing = _index_names("audit_logs")
    for name, column in reversed(_DESC_INDEXES):
        if name not in existing:
            op.create_index(
                name,
                "audit_logs",
                [column],
                postgresql_using="btree",
                postgresql_ops={column: "DESC"},
            )

    if sqlite:
        with op.batch_alter_table("api_keys") as batch:
            batch.alter_column("key_prefix", existing_type=sa.String(32), type_=sa.String(12), existing_nullable=False)
            batch.create_unique_constraint("uq_api_keys_key_hash", ["key_hash"])
    else:
        if not _key_hash_unique_constraints():
            op.create_unique_constraint("api_keys_key_hash_key", "api_keys", ["key_hash"])
        op.alter_column(
            "api_keys",
            "key_prefix",
            existing_type=sa.String(32),
            type_=sa.String(12),
            existing_nullable=False,
            postgresql_using="left(key_prefix, 12)",
        )

    with op.batch_alter_table("agents") as batch:
        batch.alter_column("description", existing_type=sa.Text(), type_=sa.String(), existing_nullable=True)
        # Lossy: keeps the leading request count of "N/period".
        batch.alter_column(
            "rate_limit",
            existing_type=sa.String(),
            type_=sa.Integer(),
            existing_nullable=True,
            postgresql_using="CAST(substring(rate_limit from '^[0-9]+') AS INTEGER)",
        )
