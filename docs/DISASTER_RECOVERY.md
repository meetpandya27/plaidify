# Disaster Recovery Runbook

This runbook covers backup, restore, and failover for a self-hosted Plaidify
deployment. Pair it with [RUNBOOK.md](RUNBOOK.md) (day-2 operations) and
[KMS_INTEGRATION.md](KMS_INTEGRATION.md) (key management).

## Recovery objectives

| Metric | Target | Notes |
| ------ | ------ | ----- |
| RPO (max data loss) | ≤ 15 min | Achieved with WAL archiving / managed-PostgreSQL PITR. Scheduled `pg_dump` alone gives an RPO equal to the backup interval. |
| RTO (max downtime) | ≤ 1 hour | Restore + migrate + redeploy. Validated by the quarterly restore drill below. |

## What must be protected

Plaidify stores **envelope-encrypted** site credentials. Two assets are required
to recover usable data — losing either makes encrypted credentials unrecoverable:

1. **The database** — users, links, access tokens (ciphertext), consents, audit log.
2. **The master key material** — how the per-user DEKs are unwrapped:
   - `LocalKMSProvider` (default): the `ENCRYPTION_KEY` (and any `ENCRYPTION_KEY_PREVIOUS`) env values.
   - `AWS` / `Azure` / `Vault` providers: the CMK / Key Vault key / Transit key referenced by `KMS_*` settings.

> Back up the key material **separately** from the database (different blast
> radius). Store it in a secrets manager, not alongside the dump. A database
> backup without the key is cryptographically useless — which is the point.

## Backups

### Database

Use the bundled helper (compressed `pg_dump` custom-format archives with local
retention pruning):

```bash
DATABASE_URL=postgres://user:pass@host:5432/plaidify \
BACKUP_DIR=/var/backups/plaidify \
BACKUP_RETENTION=14 \
  scripts/backup_db.sh backup
```

Schedule it (cron / systemd timer / Kubernetes CronJob), e.g. hourly:

```cron
0 * * * * DATABASE_URL=... BACKUP_DIR=/var/backups/plaidify /opt/plaidify/scripts/backup_db.sh backup >> /var/log/plaidify-backup.log 2>&1
```

Then ship archives off-box to object storage (S3 / Azure Blob / GCS) with
server-side encryption and a lifecycle/retention policy. For an RPO better than
the dump interval, prefer your platform's **point-in-time recovery** (managed
PostgreSQL PITR or self-managed WAL archiving) in addition to logical dumps.

### Key material

- Record `ENCRYPTION_KEY` / `ENCRYPTION_KEY_PREVIOUS` (or the external KMS key id)
  in your secrets manager with versioning enabled.
- After any key rotation, confirm the previous key is retained until
  `re_encrypt_tokens` has re-wrapped all rows to the new version.

## Restore

1. Provision a fresh PostgreSQL instance and export its `DATABASE_URL`.
2. Restore the most recent archive (destructive against the target DB):

   ```bash
   DATABASE_URL=postgres://user:pass@host:5432/plaidify \
     scripts/backup_db.sh restore /var/backups/plaidify/plaidify-<stamp>.dump
   ```

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
6. Spot-check that an existing access token decrypts (e.g. trigger a refresh for
   one link) and that the audit hash chain verifies (`/audit/verify`).

## Redis

Redis holds rate-limit counters, transient link-session state, and short-lived
caches — **not** the source of truth. It does not need backup. On loss, bring up
a fresh instance and point `REDIS_URL` at it; the app fails open for rate limits
and rehydrates session state on use.

## Failover (region / instance loss)

1. Restore the database from PITR or the latest off-box archive in the standby region.
2. Deploy the app with the standby `DATABASE_URL`, `REDIS_URL`, and the **same**
   key material / KMS settings.
3. Repoint DNS / load balancer to the standby.
4. Confirm `/health/detailed` is `healthy` before re-enabling traffic.

## Restore drill (quarterly)

Untested backups are not backups. Each quarter:

1. Restore the latest production archive into an isolated environment.
2. Run `alembic upgrade head` and the smoke checks above.
3. Confirm credential decryption and audit-chain verification succeed.
4. Record the measured restore time and compare against the RTO target; file
   follow-ups if it regressed.
