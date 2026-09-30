# Disaster Recovery Runbook

This runbook covers backup, restore, and failover for a self-hosted Plaidify
deployment. Pair it with [RUNBOOK.md](RUNBOOK.md) (day-2 operations) and
[KMS_INTEGRATION.md](KMS_INTEGRATION.md) (key management).

## Recovery objectives

| Metric | Target | Notes |
| ------ | ------ | ----- |
| RPO (max data loss) | ≤ 15 min | Achieved with WAL archiving / managed-PostgreSQL PITR. Scheduled `pg_dump` alone gives an RPO equal to the backup interval (1 hour by default). |
| RTO (max downtime) | ≤ 1 hour | Restore + migrate + redeploy. Only a restore drill (below) shows whether you meet it. |

## What must be protected

Plaidify stores **envelope-encrypted** site credentials. Two assets are required
to recover usable data — losing either makes encrypted credentials unrecoverable:

1. **The database** — users, links, access tokens (ciphertext), consents, audit log.
2. **The master key material** — how the per-user DEKs are unwrapped:
   - `LocalKMSProvider` (default): the `ENCRYPTION_KEY` (and any `ENCRYPTION_KEY_PREVIOUS`) env values.
   - `AWS` / `Azure` / `Vault` providers: the CMK / Key Vault key / Transit key referenced by `KMS_*` settings.

A third secret protects the backups themselves: the **age private key** that
decrypts the dump archives (see below).

> Back up the key material **separately** from the database (different blast
> radius). Store it in a secrets manager, not alongside the dump. A database
> backup without the key is cryptographically useless — which is the point.

## Backups

### Database

`scripts/backup_db.sh` writes compressed `pg_dump` custom-format archives,
**encrypted with [age](https://age-encryption.org) before they touch the
disk**: a dump holds users, the audit log and credential ciphertext, so a plain
one sitting in a backup directory is a data leak waiting to happen. The backup
host needs only the age *public* key; keep the private key offline or in your
secrets manager, next to — not with — the database.

```bash
# Once: a key pair for backups. Keep backup-key.txt (the private key) safe.
age-keygen -o backup-key.txt          # prints the public key: age1...
```

Archives go to `BACKUP_DIR`, which defaults to `/var/backups/plaidify` —
outside the checkout, never inside the repository (`backups/` and `*.dump*`
are git-ignored as a second line of defence).

```bash
DATABASE_URL=postgres://user:pass@host:5432/plaidify \
BACKUP_AGE_RECIPIENT=age1... \
BACKUP_DIR=/var/backups/plaidify \
BACKUP_RETENTION=14 \
  scripts/backup_db.sh backup

scripts/backup_db.sh verify                 # newest archive: envelope check
BACKUP_AGE_IDENTITY_FILE=backup-key.txt \
  scripts/backup_db.sh verify               # full read of the dump inside
scripts/backup_db.sh list
```

`backup` refuses to write an unencrypted dump unless you set
`BACKUP_ALLOW_UNENCRYPTED=true`. The script also accepts libpq settings
(`PGHOST`, `PGUSER`, `PGDATABASE`, with `PGPASSWORD`, `~/.pgpass` or
`POSTGRES_PASSWORD_FILE`) instead of a URL, so the password never has to be on
a command line.

#### Scheduling

- **Docker Compose:** the production stack has an opt-in `backup` service
  (image `deploy/backup/Dockerfile`: the script, the PostgreSQL 16 client and
  age). It dumps every hour, verifies each archive, keeps a week of them in the
  `backups` volume, and reads the Postgres password from the stack's secret:

  ```bash
  echo 'age1...' > .secrets/backup_age_recipients && chmod 644 .secrets/backup_age_recipients
  docker compose -f docker-compose.production.yml --profile backup up -d backup
  docker compose -f docker-compose.production.yml logs -f backup
  ```

  Tune with `BACKUP_INTERVAL_SECONDS` and `BACKUP_RETENTION`.
- **Kubernetes:** [deploy/backup-cronjob.yaml](../deploy/backup-cronjob.yaml)
  runs the same image hourly with a read-only root filesystem.
- **Plain hosts:** cron or a systemd timer, e.g.

  ```cron
  0 * * * * DATABASE_URL=... BACKUP_AGE_RECIPIENT=age1... BACKUP_DIR=/var/backups/plaidify /opt/plaidify/scripts/backup_db.sh backup >> /var/log/plaidify-backup.log 2>&1
  ```

- **Azure:** the Flexible Server takes automatic backups (7-day retention,
  geo-redundant with `postgresGeoRedundantBackup=true`) and supports
  point-in-time restore; logical dumps are an addition, not a replacement.

Then ship the encrypted archives off-host to object storage (S3 / Azure Blob /
GCS) with a lifecycle/retention policy, e.g. `rclone copy /var/backups/plaidify
remote:plaidify-backups` after each run. For an RPO better than the dump
interval, use your platform's **point-in-time recovery** (managed PostgreSQL
PITR or self-managed WAL archiving) in addition to logical dumps.

### Key material

- Record `ENCRYPTION_KEY` / `ENCRYPTION_KEY_PREVIOUS` (or the external KMS key id)
  in your secrets manager with versioning enabled.
- After any key rotation, keep the previous key until every row is on the new
  version: `plaidify rotate-key --re-encrypt` exits 0, or the background pass
  logs `Key rotation pass complete` ([SECURITY.md](../SECURITY.md#key-rotation-procedure)).
- Keep the age private key for the backups in the same secrets manager.

## Restore

1. Provision a fresh PostgreSQL instance and export its `DATABASE_URL`.
2. Restore an archive (destructive against the target DB):

   ```bash
   DATABASE_URL=postgres://user:pass@host:5432/plaidify \
   BACKUP_AGE_IDENTITY_FILE=backup-key.txt \
     scripts/backup_db.sh restore /var/backups/plaidify/plaidify-<stamp>.dump.age
   ```

   From the compose volume: `docker compose -f docker-compose.production.yml run --rm -v "$PWD/backup-key.txt:/key.txt:ro" -e BACKUP_AGE_IDENTITY_FILE=/key.txt backup restore /backups/plaidify-<stamp>.dump.age`
   (it asks for confirmation; `BACKUP_RESTORE_CONFIRM=restore` skips the prompt in scripted drills).

3. Apply any migrations newer than the backup:

   ```bash
   alembic upgrade head
   ```

4. Restore the **same** `ENCRYPTION_KEY` (and `ENCRYPTION_KEY_PREVIOUS` if a
   rotation was mid-flight) / KMS settings the data was encrypted with.
5. Start the app and verify:

   ```bash
   curl -fsS https://<host>/health/detailed -H "Authorization: Bearer $HEALTH_CHECK_TOKEN"
   ```

   Expect `status: healthy` with `database`, `redis`, and `kms` all `ok`. A
   `kms` value other than `ok` means the key material does not match the data —
   stop and fix the key before serving traffic.
6. Spot-check that stored data decrypts — read a completed job with its
   owner's token (`GET /access_jobs/{job_id}` returns the decrypted result) —
   and that the audit hash chain verifies (`GET /audit/verify`, as an
   administrator).

## Redis

Redis is not the system of record, but it is not a disposable cache either. It
holds:

- the **access-job queue** (a Redis stream) and each queued job's encrypted payload,
- **hosted-link sessions** and **MFA state** for flows in progress,
- the **RSA key pairs** that decrypt credentials a hosted-link page encrypted
  for the server,
- rate-limit counters.

Losing it loses no durable data — users, links, tokens and scheduled refresh
jobs are in PostgreSQL — but every in-progress link or MFA flow fails and must
be restarted, and queued jobs that no executor had claimed are gone. Hence:

- the production compose stack runs Redis with AOF persistence (plus RDB
  snapshots) on a volume, so a restart doesn't drop the queue;
- the Azure template defaults to the **Standard** tier (primary + replica with
  automatic failover) rather than Basic's single node;
- Redis needs a password, and must never be reachable outside the application
  network.

There is nothing to back up. After a total loss, bring up a fresh instance,
point `REDIS_URL` at it, and tell users to restart any link flow that was in
progress; the app recreates its keys and consumer groups on use.

## Failover (region / instance loss)

1. Restore the database from PITR or the latest off-box archive in the standby region.
2. Deploy the app with the standby `DATABASE_URL`, `REDIS_URL`, and the **same**
   key material / KMS settings.
3. Repoint DNS / load balancer to the standby.
4. Confirm `/health/detailed` is `healthy` before re-enabling traffic.

## Restore drill (quarterly)

Untested backups are not backups. No drill has been recorded for this
deployment yet; run one before relying on the RTO above, then each quarter:

1. Restore the latest production archive into an isolated environment (a
   throwaway PostgreSQL, never the production server), with the age private
   key from the secrets manager — the drill also proves that key is where you
   think it is.
2. Run `alembic upgrade head` and the smoke checks above.
3. Confirm credential decryption and audit-chain verification succeed.
4. Record the date, the archive, the measured restore time and any problems
   below; compare against the RTO target and file follow-ups if it regressed.

| Date | Archive | Restore time | Result / follow-ups |
| ---- | ------- | ------------ | ------------------- |
| — | — | — | first drill pending |
