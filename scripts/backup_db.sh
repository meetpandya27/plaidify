#!/usr/bin/env bash
set -euo pipefail

# Plaidify PostgreSQL backup + restore helper.
#
# Creates compressed pg_dump custom-format archives, prunes old local copies,
# and restores from an archive. See docs/DISASTER_RECOVERY.md for the full
# runbook (RPO/RTO targets, key recovery, failover, and restore drills).
#
# Usage:
#   scripts/backup_db.sh backup           # create a timestamped backup
#   scripts/backup_db.sh restore <file>   # restore from an archive (DESTRUCTIVE)
#   scripts/backup_db.sh list             # list local backups
#
# Environment:
#   DATABASE_URL       postgres://user:pass@host:5432/dbname   (required)
#   BACKUP_DIR         directory for archives                  (default: ./backups)
#   BACKUP_RETENTION   number of local backups to keep         (default: 14)

DATABASE_URL="${DATABASE_URL:-}"
BACKUP_DIR="${BACKUP_DIR:-./backups}"
BACKUP_RETENTION="${BACKUP_RETENTION:-14}"

err() { echo "ERROR: $*" >&2; exit 1; }

require_pg_url() {
    [[ -n "$DATABASE_URL" ]] || err "DATABASE_URL is required."
    case "$DATABASE_URL" in
        postgres://*|postgresql://*) ;;
        *) err "DATABASE_URL must be a PostgreSQL URL (got: ${DATABASE_URL%%:*}://...)." ;;
    esac
}

cmd_backup() {
    require_pg_url
    command -v pg_dump >/dev/null 2>&1 || err "pg_dump not found (install the postgresql client)."
    mkdir -p "$BACKUP_DIR"
    local stamp file
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    file="${BACKUP_DIR}/plaidify-${stamp}.dump"

    echo "Creating backup -> ${file}"
    # -Fc: compressed custom format (supports selective/parallel restore).
    pg_dump --format=custom --no-owner --no-privileges --file="$file" "$DATABASE_URL"
    echo "Backup complete ($(du -h "$file" | cut -f1))."

    # Prune: keep the newest BACKUP_RETENTION archives.
    local -a archives
    # shellcheck disable=SC2207
    archives=($(ls -1t "${BACKUP_DIR}"/plaidify-*.dump 2>/dev/null || true))
    if (( ${#archives[@]} > BACKUP_RETENTION )); then
        echo "Pruning old backups (keeping ${BACKUP_RETENTION})..."
        local i
        for (( i=BACKUP_RETENTION; i<${#archives[@]}; i++ )); do
            echo "  removing ${archives[$i]}"
            rm -f "${archives[$i]}"
        done
    fi
}

cmd_restore() {
    local file="${1:-}"
    [[ -n "$file" ]] || err "Usage: backup_db.sh restore <file>"
    [[ -f "$file" ]] || err "Backup file not found: ${file}"
    require_pg_url
    command -v pg_restore >/dev/null 2>&1 || err "pg_restore not found (install the postgresql client)."

    echo "WARNING: this will DROP and recreate objects in the target database."
    echo "  target: ${DATABASE_URL%%\?*}"
    echo "  source: ${file}"
    read -r -p "Type 'restore' to continue: " confirm
    [[ "$confirm" == "restore" ]] || err "Aborted."

    # --clean --if-exists drops existing objects first; --no-owner keeps it
    # portable across environments with different role names.
    pg_restore --clean --if-exists --no-owner --no-privileges \
        --dbname="$DATABASE_URL" "$file"
    echo "Restore complete. Run 'alembic upgrade head' to apply any newer migrations."
}

cmd_list() {
    mkdir -p "$BACKUP_DIR"
    ls -1lht "${BACKUP_DIR}"/plaidify-*.dump 2>/dev/null || echo "No backups in ${BACKUP_DIR}."
}

main() {
    local action="${1:-}"
    case "$action" in
        backup)  cmd_backup ;;
        restore) shift; cmd_restore "${1:-}" ;;
        list)    cmd_list ;;
        *)       err "Usage: backup_db.sh {backup|restore <file>|list}" ;;
    esac
}

main "$@"
