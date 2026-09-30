#!/usr/bin/env bash
set -euo pipefail

# Plaidify PostgreSQL backup + restore helper.
#
# Creates compressed pg_dump custom-format archives, encrypted with age
# (https://age-encryption.org) before they touch the disk, prunes old copies,
# verifies archives, and restores them. See docs/DISASTER_RECOVERY.md for the
# full runbook (RPO/RTO targets, key recovery, failover, and restore drills).
#
# Usage:
#   scripts/backup_db.sh backup            # create a timestamped, encrypted backup
#   scripts/backup_db.sh verify [<file>]   # check an archive (default: newest) reads end to end
#   scripts/backup_db.sh restore <file>    # restore into the target database (DESTRUCTIVE)
#   scripts/backup_db.sh list              # list local backups
#   scripts/backup_db.sh schedule          # backup + verify every BACKUP_INTERVAL_SECONDS (compose service)
#
# Connection (one of):
#   DATABASE_URL              postgres://user:pass@host:5432/dbname
#   PGHOST/PGPORT/PGUSER/PGDATABASE (libpq), with the password in PGPASSWORD,
#   ~/.pgpass, or POSTGRES_PASSWORD_FILE (a Docker secret)
#
# Storage and encryption:
#   BACKUP_DIR                  directory for archives (default: /var/backups/plaidify,
#                               outside any checkout — never inside the repository)
#   BACKUP_RETENTION            number of local backups to keep (default: 14)
#   BACKUP_AGE_RECIPIENT        age public key(s), comma separated, to encrypt to
#   BACKUP_AGE_RECIPIENTS_FILE  or a file of age public keys, one per line
#   BACKUP_AGE_IDENTITY_FILE    age private key, for verify/restore of .age archives
#   BACKUP_ALLOW_UNENCRYPTED    "true" to write plain .dump files (not recommended:
#                               dumps hold users, audit logs and credential ciphertext)
#   BACKUP_INTERVAL_SECONDS     schedule interval (default: 3600)
#   BACKUP_RESTORE_CONFIRM      "restore" skips the interactive prompt (restore drills)

DATABASE_URL="${DATABASE_URL:-}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/plaidify}"
BACKUP_RETENTION="${BACKUP_RETENTION:-14}"
BACKUP_AGE_RECIPIENT="${BACKUP_AGE_RECIPIENT:-}"
BACKUP_AGE_RECIPIENTS_FILE="${BACKUP_AGE_RECIPIENTS_FILE:-}"
BACKUP_AGE_IDENTITY_FILE="${BACKUP_AGE_IDENTITY_FILE:-}"
BACKUP_ALLOW_UNENCRYPTED="${BACKUP_ALLOW_UNENCRYPTED:-false}"
BACKUP_INTERVAL_SECONDS="${BACKUP_INTERVAL_SECONDS:-3600}"

err() { echo "ERROR: $*" >&2; exit 1; }
log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

# Connection arguments for pg_dump/pg_restore: the URL when given, otherwise
# libpq's PG* environment.
PG_CONN=()
resolve_connection() {
    if [[ -n "$DATABASE_URL" ]]; then
        case "$DATABASE_URL" in
            postgres://*|postgresql://*) ;;
            *) err "DATABASE_URL must be a PostgreSQL URL (got: ${DATABASE_URL%%:*}://...)." ;;
        esac
        PG_CONN=("--dbname=$DATABASE_URL")
        return
    fi
    [[ -n "${PGHOST:-}" ]] || err "Set DATABASE_URL, or PGHOST/PGUSER/PGDATABASE for libpq."
    if [[ -z "${PGPASSWORD:-}" && -n "${POSTGRES_PASSWORD_FILE:-}" ]]; then
        [[ -r "$POSTGRES_PASSWORD_FILE" ]] || err "Cannot read POSTGRES_PASSWORD_FILE ($POSTGRES_PASSWORD_FILE)."
        PGPASSWORD="$(tr -d '\r\n' < "$POSTGRES_PASSWORD_FILE")"
        export PGPASSWORD
    fi
    PG_CONN=("--dbname=${PGDATABASE:-plaidify}")
}

describe_target() {
    if [[ -n "$DATABASE_URL" ]]; then
        # Scheme, host and database only — never the credentials.
        local rest="${DATABASE_URL#*://}"
        echo "${DATABASE_URL%%://*}://${rest#*@}" | sed 's/?.*//'
    else
        echo "${PGUSER:-?}@${PGHOST}:${PGPORT:-5432}/${PGDATABASE:-plaidify}"
    fi
}

age_recipient_args() {
    local -a args=()
    local r
    if [[ -n "$BACKUP_AGE_RECIPIENT" ]]; then
        IFS=',' read -r -a recipients <<< "$BACKUP_AGE_RECIPIENT"
        for r in "${recipients[@]}"; do
            [[ -n "${r// /}" ]] && args+=("-r" "${r// /}")
        done
    fi
    if [[ -n "$BACKUP_AGE_RECIPIENTS_FILE" ]]; then
        [[ -r "$BACKUP_AGE_RECIPIENTS_FILE" ]] || err "Cannot read BACKUP_AGE_RECIPIENTS_FILE ($BACKUP_AGE_RECIPIENTS_FILE)."
        args+=("-R" "$BACKUP_AGE_RECIPIENTS_FILE")
    fi
    if (( ${#args[@]} > 0 )); then
        printf '%s\n' "${args[@]}"
    fi
}

# Print the archive's plain pg_dump stream on stdout, decrypting if needed.
open_archive() {
    local file="$1"
    case "$file" in
        *.dump.age)
            command -v age >/dev/null 2>&1 || err "age not found (https://age-encryption.org)."
            [[ -r "$BACKUP_AGE_IDENTITY_FILE" ]] || err "Set BACKUP_AGE_IDENTITY_FILE to the age private key to read ${file}."
            age --decrypt -i "$BACKUP_AGE_IDENTITY_FILE" "$file"
            ;;
        *.dump) cat "$file" ;;
        *) err "Not a Plaidify backup archive: ${file}" ;;
    esac
}

list_archives() {
    # Newest first.
    ls -1t "${BACKUP_DIR}"/plaidify-*.dump "${BACKUP_DIR}"/plaidify-*.dump.age 2>/dev/null || true
}

cmd_backup() {
    resolve_connection
    command -v pg_dump >/dev/null 2>&1 || err "pg_dump not found (install the postgresql client)."

    local -a age_args=()
    local line
    while IFS= read -r line; do age_args+=("$line"); done < <(age_recipient_args)
    if (( ${#age_args[@]} == 0 )) && [[ "$BACKUP_ALLOW_UNENCRYPTED" != "true" ]]; then
        err "No age recipient configured. Set BACKUP_AGE_RECIPIENT or BACKUP_AGE_RECIPIENTS_FILE (or BACKUP_ALLOW_UNENCRYPTED=true)."
    fi
    if (( ${#age_args[@]} > 0 )); then
        command -v age >/dev/null 2>&1 || err "age not found (https://age-encryption.org)."
    fi

    mkdir -p "$BACKUP_DIR"
    chmod 700 "$BACKUP_DIR" 2>/dev/null || true
    # Leftovers of runs that were killed mid-dump (a day old, so a concurrent
    # run's file is never touched).
    find "$BACKUP_DIR" -maxdepth 1 -name 'plaidify-*.partial' -mmin +1440 -delete 2>/dev/null || true
    local stamp file tmp
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    if (( ${#age_args[@]} > 0 )); then
        file="${BACKUP_DIR}/plaidify-${stamp}.dump.age"
    else
        file="${BACKUP_DIR}/plaidify-${stamp}.dump"
    fi
    tmp="${file}.partial"

    log "Creating backup of $(describe_target) -> ${file}"
    (
        umask 077
        # -Fc: compressed custom format (supports selective/parallel restore).
        # Encrypted in the pipe, so the plain dump never touches the disk.
        if (( ${#age_args[@]} > 0 )); then
            pg_dump --format=custom --no-owner --no-privileges "${PG_CONN[@]}" | age "${age_args[@]}" -o "$tmp"
        else
            pg_dump --format=custom --no-owner --no-privileges "${PG_CONN[@]}" --file="$tmp"
        fi
    ) || { rm -f "$tmp"; err "pg_dump failed; no backup written."; }
    mv "$tmp" "$file"
    log "Backup complete ($(du -h "$file" | cut -f1))."

    # Prune: keep the newest BACKUP_RETENTION archives.
    local -a archives=()
    while IFS= read -r line; do [[ -n "$line" ]] && archives+=("$line"); done < <(list_archives)
    if (( ${#archives[@]} > BACKUP_RETENTION )); then
        log "Pruning old backups (keeping ${BACKUP_RETENTION})..."
        local i
        for (( i=BACKUP_RETENTION; i<${#archives[@]}; i++ )); do
            log "  removing ${archives[$i]}"
            rm -f "${archives[$i]}"
        done
    fi
}

cmd_verify() {
    local file="${1:-}"
    if [[ -z "$file" ]]; then
        file="$(list_archives | head -n 1)"
        [[ -n "$file" ]] || err "No backups in ${BACKUP_DIR}."
    fi
    [[ -f "$file" ]] || err "Backup file not found: ${file}"
    command -v pg_restore >/dev/null 2>&1 || err "pg_restore not found (install the postgresql client)."
    if [[ "$file" == *.dump.age && -z "$BACKUP_AGE_IDENTITY_FILE" ]]; then
        # The backup host normally holds only public keys; decryption is the
        # restore drill's job.
        age_header_ok "$file" || err "${file} is not an age file."
        log "Verified ${file}: age envelope present (set BACKUP_AGE_IDENTITY_FILE to check the dump inside)."
        return
    fi
    # A full read (restored to /dev/null) catches truncation and corruption
    # that the table of contents alone would not.
    open_archive "$file" | pg_restore --file=/dev/null \
        || err "${file} is damaged: pg_restore could not read it to the end."
    local entries
    entries="$(open_archive "$file" | pg_restore --list | grep -c '^[0-9]' || true)"
    (( entries > 0 )) || err "${file} did not read as a pg_dump archive."
    log "Verified ${file}: ${entries} archive entries read end to end."
}

age_header_ok() {
    head -c 64 "$1" | grep -q "age-encryption.org/v1"
}

cmd_restore() {
    local file="${1:-}"
    [[ -n "$file" ]] || err "Usage: backup_db.sh restore <file>"
    [[ -f "$file" ]] || err "Backup file not found: ${file}"
    resolve_connection
    command -v pg_restore >/dev/null 2>&1 || err "pg_restore not found (install the postgresql client)."

    echo "WARNING: this will DROP and recreate objects in the target database."
    echo "  target: $(describe_target)"
    echo "  source: ${file}"
    local confirm="${BACKUP_RESTORE_CONFIRM:-}"
    if [[ -z "$confirm" ]]; then
        read -r -p "Type 'restore' to continue: " confirm
    fi
    [[ "$confirm" == "restore" ]] || err "Aborted."

    # --clean --if-exists drops existing objects first; --no-owner keeps it
    # portable across environments with different role names.
    open_archive "$file" | pg_restore --clean --if-exists --no-owner --no-privileges "${PG_CONN[@]}"
    log "Restore complete. Run 'alembic upgrade head' to apply any newer migrations."
}

cmd_list() {
    local found=false file
    while IFS= read -r file; do
        [[ -n "$file" ]] || continue
        found=true
        ls -lh "$file"
    done < <(list_archives)
    [[ "$found" == true ]] || echo "No backups in ${BACKUP_DIR}."
}

cmd_schedule() {
    [[ "$BACKUP_INTERVAL_SECONDS" =~ ^[0-9]+$ ]] && (( BACKUP_INTERVAL_SECONDS >= 60 )) \
        || err "BACKUP_INTERVAL_SECONDS must be a whole number of seconds, at least 60."
    log "Backing up every ${BACKUP_INTERVAL_SECONDS}s into ${BACKUP_DIR} (keeping ${BACKUP_RETENTION})."
    trap 'log "Stopping backup schedule."; exit 0' TERM INT
    while :; do
        if ( cmd_backup && cmd_verify ); then
            log "Scheduled backup OK."
        else
            log "Scheduled backup FAILED; retrying at the next interval."
        fi
        sleep "$BACKUP_INTERVAL_SECONDS" &
        wait $!
    done
}

main() {
    local action="${1:-}"
    case "$action" in
        backup)   cmd_backup ;;
        verify)   shift; cmd_verify "${1:-}" ;;
        restore)  shift; cmd_restore "${1:-}" ;;
        list)     cmd_list ;;
        schedule) cmd_schedule ;;
        *)        err "Usage: backup_db.sh {backup|verify [<file>]|restore <file>|list|schedule}" ;;
    esac
}

main "$@"
