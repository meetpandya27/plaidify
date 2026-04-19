"""Add CASCADE deletes to foreign keys and indexes on user_id columns.

Revision ID: j0k1l2m3n4o5
Revises: i9j0k1l2m3n4
Create Date: 2026-04-16 12:00:00.000000
"""
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
    ("refresh_tokens", "fk_refresh_tokens_user_id", "user_id", "users.id", "CASCADE"),
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
    ("scheduled_refresh_jobs", "fk_scheduled_refresh_jobs_access_token", "access_token", "access_tokens.token", "CASCADE"),
    ("scheduled_refresh_jobs", "fk_scheduled_refresh_jobs_user_id", "user_id", "users.id", "CASCADE"),
]

# New indexes for query performance
_NEW_INDEXES = [
    ("ix_links_user_id", "links", ["user_id"]),
    ("ix_access_tokens_user_id", "access_tokens", ["user_id"]),
    ("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"]),
    ("ix_webhooks_user_id", "webhooks", ["user_id"]),
]


def upgrade() -> None:
    # SQLite does not support ALTER TABLE ... DROP/ADD CONSTRAINT.
    # This migration targets PostgreSQL (the production database).
    # For SQLite dev databases, recreate via create_all().
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return

    for table, constraint_name, local_col, ref, ondelete in _FK_CASCADES:
        ref_table, ref_col = ref.split(".")
        # Drop the old unnamed FK (PostgreSQL auto-names them)
        # We use batch mode to be safe, but for PG we can also introspect.
        with op.batch_alter_table(table) as batch:
            # Drop existing FK on this column (naming varies, use batch to handle)
            try:
                batch.drop_constraint(constraint_name, type_="foreignkey")
            except Exception:
                pass  # Constraint may not exist with this name; batch handles it
            batch.create_foreign_key(
                constraint_name, ref_table, [local_col], [ref_col], ondelete=ondelete
            )

    for idx_name, table, columns in _NEW_INDEXES:
        op.create_index(idx_name, table, columns)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return

    for idx_name, table, columns in reversed(_NEW_INDEXES):
        op.drop_index(idx_name, table_name=table)

    for table, constraint_name, local_col, ref, _ in reversed(_FK_CASCADES):
        ref_table, ref_col = ref.split(".")
        with op.batch_alter_table(table) as batch:
            try:
                batch.drop_constraint(constraint_name, type_="foreignkey")
            except Exception:
                pass
            batch.create_foreign_key(
                constraint_name, ref_table, [local_col], [ref_col]
            )
