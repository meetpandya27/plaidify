"""Store every timestamp as TIMESTAMP WITH TIME ZONE

Revision ID: q7r8s9t0u1v2
Revises: p6q7r8s9t0u1
Create Date: 2026-09-29

The application writes aware UTC datetimes, but the columns were naive
``TIMESTAMP WITHOUT TIME ZONE``. psycopg2 sends an aware value as
``timestamptz`` and PostgreSQL converted it to the *session* time zone on
assignment, so on a server whose ``TimeZone`` is not UTC the stored wall
clock was shifted and read back as if it were UTC.

The conversion below uses PostgreSQL's implicit cast, which interprets each
naive value in the session ``TimeZone`` — the same zone that shifted it on
the way in — so the stored instants come out right. Run the migration with
the server's default ``TimeZone`` (do not override ``PGTZ``); on a UTC server
this is identical to ``AT TIME ZONE 'UTC'``.

SQLite has no zoned type; SQLAlchemy stores UTC wall-clock text either way,
so nothing changes there.
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "q7r8s9t0u1v2"
down_revision: Union[str, Sequence[str], None] = "p6q7r8s9t0u1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (table, column, nullable)
TIMESTAMP_COLUMNS = [
    ("users", "created_at", True),
    ("users", "locked_until", True),
    ("users", "updated_at", True),
    ("password_reset_tokens", "expires_at", False),
    ("password_reset_tokens", "created_at", True),
    ("links", "created_at", True),
    ("access_tokens", "created_at", True),
    ("access_tokens", "updated_at", True),
    ("refresh_tokens", "expires_at", False),
    ("refresh_tokens", "created_at", True),
    ("webhooks", "created_at", True),
    ("public_tokens", "expires_at", False),
    ("public_tokens", "created_at", True),
    ("consent_requests", "created_at", True),
    ("consent_requests", "updated_at", True),
    ("consent_grants", "expires_at", False),
    ("consent_grants", "created_at", True),
    ("blueprint_registry", "created_at", True),
    ("blueprint_registry", "updated_at", True),
    ("audit_logs", "timestamp", False),
    ("api_keys", "expires_at", True),
    ("api_keys", "last_used_at", True),
    ("api_keys", "created_at", True),
    ("agents", "last_active_at", True),
    ("agents", "created_at", True),
    ("agents", "updated_at", True),
    ("access_jobs", "created_at", False),
    ("access_jobs", "started_at", True),
    ("access_jobs", "completed_at", True),
    ("scheduled_refresh_jobs", "last_refreshed", True),
    ("scheduled_refresh_jobs", "created_at", True),
]


def _convert(timezone: bool) -> None:
    if op.get_bind().dialect.name == "sqlite":
        return
    for table, column, nullable in TIMESTAMP_COLUMNS:
        op.alter_column(
            table,
            column,
            existing_type=sa.DateTime(timezone=not timezone),
            type_=sa.DateTime(timezone=timezone),
            existing_nullable=nullable,
        )


def upgrade() -> None:
    _convert(timezone=True)


def downgrade() -> None:
    _convert(timezone=False)
