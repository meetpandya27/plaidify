"""Tests for scripts/provision_app_db_role.py (the least-privilege app role).

The privilege checks need a real PostgreSQL server and an admin connection:
set PLAIDIFY_TEST_POSTGRES_ADMIN_URL (CI's migrations job does). Without it
only the argument checks run.
"""

import importlib.util
import os
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "provision_app_db_role.py"
ADMIN_URL = os.environ.get("PLAIDIFY_TEST_POSTGRES_ADMIN_URL", "")


def _load_script():
    spec = importlib.util.spec_from_file_location("provision_app_db_role", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestArguments:
    def test_requires_a_postgres_admin_url(self, monkeypatch, capsys):
        script = _load_script()
        monkeypatch.setenv("DATABASE_URL", "sqlite:///plaidify.db")
        monkeypatch.setenv("APP_DB_PASSWORD", "x" * 32)
        assert script.main() == 2
        assert "PostgreSQL admin connection string" in capsys.readouterr().err

    def test_requires_a_real_password(self, monkeypatch, capsys):
        script = _load_script()
        monkeypatch.setenv("DATABASE_URL", "postgresql://admin@db/plaidify")
        monkeypatch.setenv("APP_DB_PASSWORD", "short")
        assert script.main() == 2
        assert "APP_DB_PASSWORD" in capsys.readouterr().err


def _as_role(url: str, role: str, password: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, f"{role}:{password}@{host}", parts.path, parts.query, ""))


@pytest.mark.skipif(not ADMIN_URL, reason="PLAIDIFY_TEST_POSTGRES_ADMIN_URL not set")
class TestAgainstPostgres:
    @pytest.fixture
    def admin(self):
        import psycopg2

        conn = psycopg2.connect(ADMIN_URL)
        conn.autocommit = True
        yield conn
        conn.close()

    @pytest.fixture
    def role(self, admin):
        name = f"plaidify_app_test_{uuid.uuid4().hex[:8]}"
        table = f"role_probe_{uuid.uuid4().hex[:8]}"
        with admin.cursor() as cur:
            cur.execute(f"CREATE TABLE {table} (id serial PRIMARY KEY, note text)")
        yield name, table
        with admin.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {table}")
            cur.execute(f"DROP TABLE IF EXISTS {table}_later")
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,))
            if cur.fetchone():
                cur.execute(f"DROP OWNED BY {name}")
                cur.execute(f"DROP ROLE {name}")

    def test_app_role_can_use_rows_but_not_change_the_schema(self, admin, role):
        import psycopg2

        name, table = role
        script = _load_script()
        password = uuid.uuid4().hex
        script.provision(ADMIN_URL, name, password)

        # A table created by a later migration is covered by default privileges.
        with admin.cursor() as cur:
            cur.execute(f"CREATE TABLE {table}_later (id serial PRIMARY KEY)")

        app = psycopg2.connect(_as_role(ADMIN_URL, name, password))
        app.autocommit = True
        try:
            with app.cursor() as cur:
                cur.execute(f"INSERT INTO {table} (note) VALUES ('ok') RETURNING id")
                row_id = cur.fetchone()[0]
                cur.execute(f"UPDATE {table} SET note = 'changed' WHERE id = %s", (row_id,))
                cur.execute(f"SELECT note FROM {table} WHERE id = %s", (row_id,))
                assert cur.fetchone()[0] == "changed"
                cur.execute(f"INSERT INTO {table}_later DEFAULT VALUES")
                cur.execute(f"DELETE FROM {table} WHERE id = %s", (row_id,))

                for ddl in (
                    "CREATE TABLE sneaky (id int)",
                    f"DROP TABLE {table}",
                    f"ALTER TABLE {table} ADD COLUMN extra text",
                ):
                    with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                        cur.execute(ddl)
        finally:
            app.close()

    def test_rerun_rotates_the_password(self, admin, role):
        import psycopg2

        name, _ = role
        script = _load_script()
        first, second = uuid.uuid4().hex, uuid.uuid4().hex
        script.provision(ADMIN_URL, name, first)
        script.provision(ADMIN_URL, name, second)

        psycopg2.connect(_as_role(ADMIN_URL, name, second)).close()
        with pytest.raises(psycopg2.OperationalError):
            psycopg2.connect(_as_role(ADMIN_URL, name, first))

    def test_refuses_to_reuse_the_admin_account(self, admin):
        script = _load_script()
        with admin.cursor() as cur:
            cur.execute("SELECT current_user")
            current = cur.fetchone()[0]
        with pytest.raises(SystemExit, match="must differ from the admin"):
            script.provision(ADMIN_URL, current, uuid.uuid4().hex)
