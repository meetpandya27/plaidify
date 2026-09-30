"""
JOB-01 / JOB-13: the Alembic history builds the schema the models describe.

The SQLite checks always run. The PostgreSQL checks run when the suite itself
runs on PostgreSQL (``DATABASE_URL``) or ``PLAIDIFY_TEST_POSTGRES_URL`` points
at a server; they migrate a throwaway schema, so the suite's tables are never
touched.
"""

import hashlib
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory

from alembic import command
from src.database import Base

REPO = Path(__file__).resolve().parents[1]


def _config(connection=None) -> Config:
    config = Config(str(REPO / "alembic.ini"))
    config.set_main_option("script_location", str(REPO / "alembic"))
    config.attributes["connection"] = connection
    config.attributes["configure_logger"] = False
    return config


def _drift(connection) -> list:
    context = MigrationContext.configure(connection, opts={"compare_type": True})
    return compare_metadata(context, Base.metadata)


def _tables(connection) -> set[str]:
    return set(sa.inspect(connection).get_table_names())


def _upgrade(engine, revision="head", *, time_zone=None) -> None:
    with engine.begin() as conn:
        if time_zone:
            conn.execute(sa.text(f"SET TIME ZONE '{time_zone}'"))
        command.upgrade(_config(conn), revision)


def _downgrade(engine, revision) -> None:
    with engine.begin() as conn:
        command.downgrade(_config(conn), revision)


def test_history_has_a_single_head():
    assert len(ScriptDirectory.from_config(_config()).get_heads()) == 1


# ── SQLite ────────────────────────────────────────────────────────────────────


@pytest.fixture
def sqlite_engine(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'migrations.db'}")
    yield engine
    engine.dispose()


class TestSQLiteMigrations:
    def test_upgrade_downgrade_upgrade(self, sqlite_engine):
        _upgrade(sqlite_engine)
        with sqlite_engine.connect() as conn:
            assert set(Base.metadata.tables) <= _tables(conn)
        _downgrade(sqlite_engine, "base")
        with sqlite_engine.connect() as conn:
            assert _tables(conn) == {"alembic_version"}
        _upgrade(sqlite_engine)
        with sqlite_engine.connect() as conn:
            assert set(Base.metadata.tables) <= _tables(conn)

    def test_refresh_tokens_are_hashed_in_place(self, sqlite_engine):
        _upgrade(sqlite_engine, "q7r8s9t0u1v2")
        with sqlite_engine.begin() as conn:
            conn.execute(sa.text("INSERT INTO users (id, username) VALUES (1, 'u')"))
            conn.execute(
                sa.text(
                    "INSERT INTO refresh_tokens (token, user_id, expires_at, revoked) "
                    "VALUES ('raw-token', 1, '2030-01-01 00:00:00', NULL)"
                )
            )
        _upgrade(sqlite_engine, "r8s9t0u1v2w3")
        with sqlite_engine.connect() as conn:
            row = conn.execute(sa.text("SELECT token_hash, revoked FROM refresh_tokens")).one()
        assert row.token_hash == hashlib.sha256(b"raw-token").hexdigest()
        assert not row.revoked

    def test_agent_rate_limits_become_limit_strings(self, sqlite_engine):
        _upgrade(sqlite_engine, "o5p6q7r8s9t0")
        with sqlite_engine.begin() as conn:
            conn.execute(sa.text("INSERT INTO users (id, username) VALUES (1, 'u')"))
            conn.execute(
                sa.text(
                    "INSERT INTO agents (id, name, owner_id, rate_limit, is_active) VALUES "
                    "('a1', 'n', 1, 60, 1), ('a2', 'n', 1, '30/minute', 1), ('a3', 'n', 1, NULL, 1)"
                )
            )
        _upgrade(sqlite_engine, "p6q7r8s9t0u1")
        with sqlite_engine.connect() as conn:
            rows = dict(conn.execute(sa.text("SELECT id, rate_limit FROM agents")).all())
        assert rows == {"a1": "60/minute", "a2": "30/minute", "a3": None}


def test_env_accepts_a_percent_sign_in_the_database_url(tmp_path):
    """ConfigParser interpolation used to crash env.py on any '%' (e.g. an encoded password)."""
    directory = tmp_path / "pct%dir"
    directory.mkdir()
    env = dict(os.environ, DATABASE_URL=f"sqlite:///{directory / 'migrations.db'}", PYTHONPATH=str(REPO))
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-2000:]


# ── PostgreSQL ────────────────────────────────────────────────────────────────


def _postgres_url():
    url = os.environ.get("PLAIDIFY_TEST_POSTGRES_URL")
    if url:
        return url
    database_url = os.environ.get("DATABASE_URL", "")
    return database_url if database_url.startswith("postgresql") else None


@pytest.fixture
def pg_engine():
    """An engine whose connections live in a fresh, throwaway schema."""
    url = _postgres_url()
    if not url:
        pytest.skip("PostgreSQL not configured (run the suite on PostgreSQL or set PLAIDIFY_TEST_POSTGRES_URL)")
    schema = f"mig_{uuid.uuid4().hex[:12]}"
    admin = sa.create_engine(url, poolclass=sa.pool.NullPool)
    with admin.begin() as conn:
        conn.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
    engine = sa.create_engine(url, poolclass=sa.pool.NullPool, connect_args={"options": f"-csearch_path={schema}"})
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


class TestPostgresMigrations:
    def test_upgrade_matches_models_and_downgrades_cleanly(self, pg_engine):
        _upgrade(pg_engine)
        with pg_engine.connect() as conn:
            assert _drift(conn) == []
        _downgrade(pg_engine, "base")
        with pg_engine.connect() as conn:
            assert _tables(conn) == {"alembic_version"}
        _upgrade(pg_engine)
        with pg_engine.connect() as conn:
            assert _drift(conn) == []

    def test_agent_keys_and_rate_limits_fit(self, pg_engine):
        """JOB-13: a 16-character agent key prefix and a "30/minute" limit used to fail on PostgreSQL."""
        _upgrade(pg_engine)
        with pg_engine.begin() as conn:
            conn.execute(sa.text("INSERT INTO users (id, username) VALUES (1, 'u')"))
            conn.execute(
                sa.text(
                    "INSERT INTO api_keys (id, name, key_hash, key_prefix, user_id, is_active) "
                    "VALUES ('k1', 'agent', :h, 'pk_agent_abcdefg', 1, true)"
                ),
                {"h": "a" * 64},
            )
            conn.execute(
                sa.text(
                    "INSERT INTO agents (id, name, owner_id, api_key_id, rate_limit, is_active) "
                    "VALUES ('agent-1', 'n', 1, 'k1', '30/minute', true)"
                )
            )
            assert conn.execute(sa.text("SELECT rate_limit FROM agents")).scalar() == "30/minute"

    @pytest.mark.parametrize("server_zone", ["UTC", "Asia/Kolkata"])
    def test_existing_timestamps_keep_their_instant(self, pg_engine, server_zone):
        """JOB-23: rows written before the migration keep their instant, also on a non-UTC server."""
        instant = datetime(2026, 3, 4, 5, 6, 7, tzinfo=timezone.utc)
        _upgrade(pg_engine, "p6q7r8s9t0u1")
        with pg_engine.begin() as conn:
            conn.execute(sa.text(f"SET TIME ZONE '{server_zone}'"))
            # What the application did: psycopg2 sends an aware datetime as
            # timestamptz, which PostgreSQL stored in the naive column as wall
            # time of the session zone (10:36:07 on an Asia/Kolkata server).
            conn.execute(sa.text("INSERT INTO users (id, username, created_at) VALUES (1, 'u', :ts)"), {"ts": instant})
        _upgrade(pg_engine, "q7r8s9t0u1v2", time_zone=server_zone)
        with pg_engine.connect() as conn:
            created = conn.execute(sa.text("SELECT created_at FROM users")).scalar()
        assert created == instant

    def test_refresh_tokens_are_hashed_in_place(self, pg_engine):
        _upgrade(pg_engine, "q7r8s9t0u1v2")
        with pg_engine.begin() as conn:
            conn.execute(sa.text("INSERT INTO users (id, username) VALUES (1, 'u')"))
            conn.execute(
                sa.text("INSERT INTO refresh_tokens (token, user_id, expires_at) VALUES ('raw-token', 1, now())")
            )
        _upgrade(pg_engine)
        with pg_engine.connect() as conn:
            row = conn.execute(sa.text("SELECT token_hash, revoked FROM refresh_tokens")).one()
        assert row.token_hash == hashlib.sha256(b"raw-token").hexdigest()
        assert row.revoked is False

    def test_env_accepts_a_percent_encoded_password(self):
        if not _postgres_url():
            pytest.skip("PostgreSQL not configured (run the suite on PostgreSQL or set PLAIDIFY_TEST_POSTGRES_URL)")
        url = sa.engine.make_url(_postgres_url())
        encoded = url.set(password=url.password or "p@ss%word").render_as_string(hide_password=False)
        assert "%" in encoded
        env = dict(os.environ, DATABASE_URL=encoded, PYTHONPATH=str(REPO))
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "current"],
            cwd=REPO,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert "interpolation" not in result.stderr
