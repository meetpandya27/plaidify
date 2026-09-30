"""Revision u1a2b3c4d5e6: session versions, verified emails, agent-bound consent, throttles, leases."""

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.config import Config

from alembic import command

REPO = Path(__file__).resolve().parents[1]
BEFORE = "t0u1v2w3x4y5"
REVISION = "u1a2b3c4d5e6"


def _config(connection) -> Config:
    config = Config(str(REPO / "alembic.ini"))
    config.set_main_option("script_location", str(REPO / "alembic"))
    config.attributes["connection"] = connection
    config.attributes["configure_logger"] = False
    return config


def _migrate(engine, step, revision):
    with engine.begin() as conn:
        step(_config(conn), revision)


@pytest.fixture
def engine(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'u1a.db'}")
    yield engine
    engine.dispose()


def test_existing_rows_get_versions_and_verified_emails(engine):
    _migrate(engine, command.upgrade, BEFORE)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO users (id, username, email, hashed_password, oauth_provider, oauth_sub) VALUES "
                "(1, 'password-user', 'p@example.com', 'hash', NULL, NULL), "
                "(2, 'oauth-user', 'o@example.com', NULL, 'google', 'g-1'), "
                "(3, 'linked-user', 'l@example.com', 'hash', 'github', 'gh-1')"
            )
        )

    _migrate(engine, command.upgrade, REVISION)

    with engine.connect() as conn:
        rows = conn.execute(sa.text("SELECT id, token_version, email_verified FROM users ORDER BY id")).all()
        tables = set(sa.inspect(conn).get_table_names())
        consent_columns = {c["name"] for c in sa.inspect(conn).get_columns("consent_grants")}
    # Only an account created from a verified provider email counts as verified;
    # a password account (even one linked before) has never proven its address.
    assert [(r.id, r.token_version, bool(r.email_verified)) for r in rows] == [
        (1, 0, False),
        (2, 0, True),
        (3, 0, False),
    ]
    assert {"login_throttles", "maintenance_leases"} <= tables
    assert "agent_id" in consent_columns

    _migrate(engine, command.downgrade, BEFORE)
    with engine.connect() as conn:
        user_columns = {c["name"] for c in sa.inspect(conn).get_columns("users")}
        assert "login_throttles" not in set(sa.inspect(conn).get_table_names())
    assert not {"token_version", "email_verified"} & user_columns
