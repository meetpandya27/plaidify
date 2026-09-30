#!/usr/bin/env python3
"""Create or update the application's least-privilege PostgreSQL role.

Run right after ``alembic upgrade head``, connected as the account that ran
the migrations (the server admin). The API and the access executor then
connect as this role, which may read and write the application's rows and
nothing else: no DDL, no role or database management. A leaked application
connection string can't drop tables or read other databases.

Idempotent: it creates the role or resets its password, then (re)grants
access to every table and sequence, and sets default privileges so tables
the admin creates in later migrations are covered too.

Environment:
    DATABASE_URL     admin connection string (the one migrations used)
    APP_DB_ROLE      role name (default: plaidify_app)
    APP_DB_PASSWORD  the role's password; only its SCRAM verifier is sent
    APP_DB_SCHEMA    schema holding the tables (default: public)

Used by the Azure migration job (infra/main.bicep); for compose or another
host, run it once after migrating and point the app's DATABASE_URL at the role.
"""

from __future__ import annotations

import os
import sys

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import encrypt_password

# Attributes the application role must not have. They are off for a new role;
# changing them takes a superuser, which a managed server's admin is not, so
# the script checks them instead of setting them.
ELEVATED_ATTRIBUTES = ("rolsuper", "rolcreatedb", "rolcreaterole", "rolreplication", "rolbypassrls")

GRANTS = (
    "GRANT USAGE ON SCHEMA {schema} TO {role}",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {schema} TO {role}",
    "GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA {schema} TO {role}",
    # Tables and sequences the admin creates in future migrations.
    "ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {role}",
    "ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO {role}",
)


def provision(database_url: str, role: str, password: str, schema: str = "public") -> None:
    conn = psycopg2.connect(database_url)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT current_database(), current_user")
            database, admin = cur.fetchone()
            if role == admin:
                raise SystemExit(f"APP_DB_ROLE must differ from the admin account ({admin}).")

            # Hash client-side so the plain password never reaches the server's
            # statement log.
            verifier = encrypt_password(password, role, conn, "scram-sha-256")
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
            verb = "ALTER" if cur.fetchone() else "CREATE"
            cur.execute(
                sql.SQL(f"{verb} ROLE {{}} WITH LOGIN PASSWORD %s").format(sql.Identifier(role)),
                (verifier,),
            )
            cur.execute(
                sql.SQL("SELECT {} FROM pg_roles WHERE rolname = %s").format(
                    sql.SQL(", ").join(sql.Identifier(a) for a in ELEVATED_ATTRIBUTES)
                ),
                (role,),
            )
            elevated = [a for a, on in zip(ELEVATED_ATTRIBUTES, cur.fetchone(), strict=True) if on]
            if elevated:
                raise SystemExit(f"Role {role!r} has {', '.join(elevated)}; remove them before using it for the app.")

            names = {
                "database": sql.Identifier(database),
                "schema": sql.Identifier(schema),
                "role": sql.Identifier(role),
            }
            # Every role may connect by default (PUBLIC); grant only where a
            # hardened server revoked that.
            cur.execute("SELECT has_database_privilege(%s, current_database(), 'CONNECT')", (role,))
            if not cur.fetchone()[0]:
                cur.execute(sql.SQL("GRANT CONNECT ON DATABASE {database} TO {role}").format(**names))
            for statement in GRANTS:
                cur.execute(sql.SQL(statement).format(**names))
            # PostgreSQL 15+ no longer lets every role create objects in
            # public; make older servers behave the same.
            if conn.server_version < 150000:
                cur.execute(sql.SQL("REVOKE CREATE ON SCHEMA {schema} FROM PUBLIC").format(**names))
    finally:
        conn.close()
    action = "created" if verb == "CREATE" else "updated"
    print(f"Database role {role!r} {action}; data access granted on schema {schema!r} of {database!r}.")


def main() -> int:
    database_url = os.environ.get("DATABASE_URL", "")
    role = os.environ.get("APP_DB_ROLE", "plaidify_app")
    password = os.environ.get("APP_DB_PASSWORD", "")
    schema = os.environ.get("APP_DB_SCHEMA", "public")

    if not database_url.startswith(("postgresql://", "postgres://")):
        print("DATABASE_URL must be the PostgreSQL admin connection string.", file=sys.stderr)
        return 2
    if len(password) < 16:
        print("APP_DB_PASSWORD must be set (16 characters or more).", file=sys.stderr)
        return 2

    provision(database_url, role, password, schema)
    return 0


if __name__ == "__main__":
    sys.exit(main())
