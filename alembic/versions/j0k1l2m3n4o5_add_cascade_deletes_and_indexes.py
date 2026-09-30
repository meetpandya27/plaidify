"""Add CASCADE deletes to foreign keys and indexes on user_id columns.

Revision ID: j0k1l2m3n4o5
Revises: i9j0k1l2m3n4
Create Date: 2026-04-16 12:00:00.000000

The foreign keys created by the earlier revisions are unnamed, so PostgreSQL
auto-named them (``links_user_id_fkey`` ...). They are looked up through the
inspector by (column, referred table) instead of guessed, dropped, and
recreated under a stable ``fk_<table>_<column>`` name with the ON DELETE rule.

``refresh_tokens`` is not handled here: the table does not exist until
``n4o5p6q7r8s9``, which creates it with ``ON DELETE CASCADE`` and
``ix_refresh_tokens_user_id`` already.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "j0k1l2m3n4o5"
down_revision = "i9j0k1l2m3n4"
branch_labels = None
depends_on = None

# (table, constraint_name, local_col, remote_table.remote_col, ondelete)
_FK_CASCADES = [
    ("links", "fk_links_user_id", "user_id", "users.id", "CASCADE"),
    ("access_tokens", "fk_access_tokens_link_token", "link_token", "links.link_token", "CASCADE"),
    ("access_tokens", "fk_access_tokens_user_id", "user_id", "users.id", "CASCADE"),
    ("webhooks", "fk_webhooks_user_id", "user_id", "users.id", "CASCADE"),
    ("public_tokens", "fk_public_tokens_access_token", "access_token", "access_tokens.token", "CASCADE"),
    ("public_tokens", "fk_public_tokens_user_id", "user_id", "users.id", "CASCADE"),
    ("consent_requests", "fk_consent_requests_access_token", "access_token", "access_tokens.token", "CASCADE"),
    ("consent_requests", "fk_consent_requests_user_id", "user_id", "users.id", "CASCADE"),
    ("consent_grants", "fk_consent_grants_consent_request_id", "consent_request_id", "consent_requests.id", "CASCADE"),
    ("consent_grants", "fk_consent_grants_access_token", "access_token", "access_tokens.token", "CASCADE"),
    ("consent_grants", "fk_consent_grants_user_id", "user_id", "users.id", "CASCADE"),
    ("blueprint_registry", "fk_blueprint_registry_published_by", "published_by", "users.id", "CASCADE"),
    ("api_keys", "fk_api_keys_user_id", "user_id", "users.id", "CASCADE"),
    ("agents", "fk_agents_owner_id", "owner_id", "users.id", "CASCADE"),
    ("agents", "fk_agents_api_key_id", "api_key_id", "api_keys.id", "SET NULL"),
    (
        "scheduled_refresh_jobs",
        "fk_scheduled_refresh_jobs_access_token",
        "access_token",
        "access_tokens.token",
        "CASCADE",
    ),
    ("scheduled_refresh_jobs", "fk_scheduled_refresh_jobs_user_id", "user_id", "users.id", "CASCADE"),
]

# New indexes for query performance
_NEW_INDEXES = [
    ("ix_links_user_id", "links", ["user_id"]),
    ("ix_access_tokens_user_id", "access_tokens", ["user_id"]),
    ("ix_webhooks_user_id", "webhooks", ["user_id"]),
]


def _foreign_key_names(bind, table: str, local_col: str, ref_table: str) -> list[str]:
    """Names of the existing FKs on ``table.local_col`` that point at ``ref_table``."""
    return [
        fk["name"]
        for fk in sa.inspect(bind).get_foreign_keys(table)
        if fk.get("name") and fk["constrained_columns"] == [local_col] and fk["referred_table"] == ref_table
    ]


def _replace_foreign_keys(ondelete_for) -> None:
    bind = op.get_bind()
    for table, constraint_name, local_col, ref, ondelete in _FK_CASCADES:
        ref_table, ref_col = ref.split(".")
        for existing in _foreign_key_names(bind, table, local_col, ref_table):
            op.drop_constraint(existing, table, type_="foreignkey")
        op.create_foreign_key(
            constraint_name, table, ref_table, [local_col], [ref_col], ondelete=ondelete_for(ondelete)
        )


def upgrade() -> None:
    for idx_name, table, columns in _NEW_INDEXES:
        op.create_index(idx_name, table, columns)

    # SQLite cannot ALTER a constraint in place and reflects these FKs without
    # names; a SQLite development database keeps its original FKs (it never
    # enforces them unless PRAGMA foreign_keys is on). PostgreSQL gets the
    # real ON DELETE rules.
    if op.get_bind().dialect.name == "sqlite":
        return
    _replace_foreign_keys(lambda ondelete: ondelete)


def downgrade() -> None:
    if op.get_bind().dialect.name != "sqlite":
        _replace_foreign_keys(lambda _ondelete: None)

    for idx_name, table, _columns in reversed(_NEW_INDEXES):
        op.drop_index(idx_name, table_name=table)
