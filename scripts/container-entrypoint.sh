#!/bin/sh

set -eu

read_secret() {
  secret_file="$1"
  if [ ! -f "$secret_file" ]; then
    echo "Missing required secret file: $secret_file" >&2
    exit 1
  fi
  tr -d '\r\n' < "$secret_file"
}

urlencode() {
  RAW_VALUE="$1" python - <<'PY'
import os
import urllib.parse

print(urllib.parse.quote(os.environ["RAW_VALUE"], safe=""))
PY
}

if [ -z "${DATABASE_URL:-}" ]; then
  db_password="${POSTGRES_PASSWORD:-}"
  if [ -z "$db_password" ] && [ -n "${POSTGRES_PASSWORD_FILE:-}" ]; then
    db_password="$(read_secret "$POSTGRES_PASSWORD_FILE")"
  fi

  if [ -n "$db_password" ]; then
    db_user="${POSTGRES_USER:-plaidify}"
    db_host="${POSTGRES_HOST:-postgres}"
    db_port="${POSTGRES_PORT:-5432}"
    db_name="${POSTGRES_DB:-plaidify}"
    db_sslmode="${POSTGRES_SSLMODE:-disable}"
    db_password_encoded="$(urlencode "$db_password")"

    DATABASE_URL="postgresql://${db_user}:${db_password_encoded}@${db_host}:${db_port}/${db_name}"
    if [ -n "$db_sslmode" ]; then
      DATABASE_URL="${DATABASE_URL}?sslmode=${db_sslmode}"
    fi
    export DATABASE_URL
  fi
fi

if [ -z "${REDIS_URL:-}" ]; then
  redis_password="${REDIS_PASSWORD:-}"
  if [ -z "$redis_password" ] && [ -n "${REDIS_PASSWORD_FILE:-}" ]; then
    redis_password="$(read_secret "$REDIS_PASSWORD_FILE")"
  fi

  if [ -n "$redis_password" ]; then
    redis_scheme="${REDIS_SCHEME:-redis}"
    redis_host="${REDIS_HOST:-redis}"
    redis_port="${REDIS_PORT:-6379}"
    redis_db="${REDIS_DB:-0}"
    redis_password_encoded="$(urlencode "$redis_password")"
    export REDIS_URL="${redis_scheme}://:${redis_password_encoded}@${redis_host}:${redis_port}/${redis_db}"
  fi
fi

exec "$@"