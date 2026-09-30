# Plaidify Operations Runbook

## Table of Contents

1. [Before going live (owner checklist)](#before-going-live-owner-checklist)
2. [Deployment](#deployment)
3. [Key Rotation](#key-rotation)
4. [Incident Response](#incident-response)
5. [Common Issues](#common-issues)
6. [Monitoring & Alerts](#monitoring--alerts)
7. [Backup & Recovery](#backup--recovery)

Commands use the production compose stack:

```bash
C="docker compose -f docker-compose.production.yml"
```

Settings live in `.env.production`. Containers read it only when they are
created, so after editing it run `$C up -d <service>` (which recreates the
container); `$C restart` keeps the old environment, and exporting a variable in
your shell changes nothing inside the containers.

---

## Before going live (owner checklist)

Plaidify has not been released or run in production yet. These steps are the
repository owner's or operator's; the code cannot do them.

**Repository and supply chain**

- [ ] Enable Dependabot alerts and security updates in the repository settings.
- [ ] Protect `main`: required pull-request review (with "Require review from Code Owners"; `.github/CODEOWNERS` exists) and required CI checks.
- [ ] Claim the package names `plaidify` on PyPI and `@plaidify/client` on npm, and configure trusted publishing for `publish-sdk.yml` (environment `pypi`) and `publish-sdk-js.yml` (environment `npm`), each environment protected by required reviewers.
- [ ] Rebuild or delete the stale public image `ghcr.io/meetpandya27/plaidify:latest` (built before these fixes), or make the package private.
- [ ] Check whether the credential in the `plaidify.db` committed early in the history (commits `c637f16`, `c0a08d3`; removed in `89a3b2c`) was real; if it was, change that password.

**Deployment**

- [ ] Azure: recreate the deployment identity as [SELF_HOST.md](SELF_HOST.md) step 2 describes (resource-group scope, conditioned role assignment) and remove any older subscription-wide grants.
- [ ] Set, in production: `AUDIT_HMAC_KEY` and `LINK_LAUNCH_SECRET` (separate from `ENCRYPTION_KEY` / `JWT_SECRET_KEY`), `HEALTH_CHECK_TOKEN`, `METRICS_TOKEN`, and the `SMTP_*` settings (plus `PASSWORD_RESET_URL`) if users reset their own passwords. On Azure the workflow generates the audit key, the launch secret and the metrics token; the rest are GitHub secrets and variables ([DEPLOYMENT.md](DEPLOYMENT.md#github-deployment-configuration)).
- [ ] Provision the first administrator with `BOOTSTRAP_USER_*` (on Azure: `AZURE_BOOTSTRAP_USER_USERNAME`, `AZURE_BOOTSTRAP_USER_EMAIL`, `BOOTSTRAP_USER_PASSWORD`), change its password after the first sign-in, then remove those settings.
- [ ] Upgrading a deployment that ran the old code: list the administrators (`GET /admin/users`) and confirm none was promoted by the old bootstrap step, which promoted any existing account holding the configured username or email.
- [ ] Wire Alertmanager to a real receiver ([monitoring/README.md](../monitoring/README.md#delivering-alerts)).
- [ ] Schedule encrypted backups and run a restore drill; record it in [DISASTER_RECOVERY.md](DISASTER_RECOVERY.md#restore-drill-quarterly).

---

## Deployment

### Pre-deployment Checklist

- [ ] CI is green for the commit (tests on Python 3.11–3.13, browser suites, SDKs, migrations on PostgreSQL, image smoke test, dependency audit)
- [ ] Python test suite passes: `PYTHONPATH=$PWD python -m pytest tests/ -q`
- [ ] Hosted-link browser contract passes: `PYTHONPATH=$PWD python -m pytest tests/test_hosted_link_e2e.py -q -m playwright` (needs `python -m playwright install --only-shell chromium` and a built `frontend-next/dist`)
- [ ] Environment variables configured (see `.env.example` and [DEPLOYMENT.md](DEPLOYMENT.md#environment-variables-reference))
- [ ] `ENV=production` is set (the app then refuses SQLite and requires Redis)
- [ ] `CORS_ORIGINS` does NOT contain `*`
- [ ] `FORWARDED_ALLOW_IPS` lists the reverse proxy (otherwise the HTTPS redirect loops)
- [ ] `DATABASE_URL` points to PostgreSQL (not SQLite) and `REDIS_URL` is configured (the compose stack builds both from `.secrets/`)
- [ ] `ENCRYPTION_KEY` and `JWT_SECRET_KEY` are set, plus the keys in the owner checklist above

### Rolling Deployment

Azure: run the `Deploy Azure` workflow from `main` ([SELF_HOST.md](SELF_HOST.md)); it
migrates first and only then creates the new revisions. Compose:

```bash
# 1. Build (or pull, with PLAIDIFY_IMAGE set to the commit's tag)
$C build

# 2. Run database migrations BEFORE the new code starts
$C run --rm migrate

# 3. Replace the API and the executor (the executor drains running jobs on SIGTERM)
$C up -d --no-deps plaidify access-executor

# 4. Verify health (both must report healthy)
$C ps plaidify access-executor
curl -fsS https://your.domain/health

# 5. Monitor logs for errors
$C logs -f --tail=100 plaidify access-executor
```

### First administrator without `BOOTSTRAP_USER_*`

Where the bootstrap settings can't be used (registration is off in
production), create the first administrator from a shell in the API
container — compose:
`$C exec plaidify sh`; Azure: `az containerapp exec --name <api-app> --resource-group <rg>`.
Start `python` there and run:

```python
import getpass
from src.database import SessionLocal, User, create_user_dek
from src.dependencies import get_password_hash

username, email = input("Username: "), input("Email: ")
password = getpass.getpass("Password (the API's password rules are not checked here): ")
with SessionLocal() as db:
    db.add(User(username=username, email=email, hashed_password=get_password_hash(password),
                encrypted_dek=create_user_dek(), is_admin=True))
    db.commit()
```

### Rollback

```bash
# 1. Only if the new migration is incompatible with the old code: downgrade
#    while the NEW image (which knows that migration) is still current.
#    Downgrading past the refresh-token hashing migration deletes every
#    refresh token (everyone signs in again).
$C run --rm migrate alembic downgrade -1

# 2. Roll back to the previous image
PLAIDIFY_IMAGE=<registry>/plaidify:<previous-sha> $C up -d --no-deps plaidify access-executor

# 3. Verify
curl -fsS https://your.domain/health
```

---

## Key Rotation

### Master key (`ENCRYPTION_KEY`)

Follow [SECURITY.md, "Key Rotation Procedure"](../SECURITY.md#key-rotation-procedure). In short:

1. Generate a new key; in `.env.production` set `ENCRYPTION_KEY_PREVIOUS` to the
   current key, `ENCRYPTION_KEY` to the new one, and increment
   `ENCRYPTION_KEY_VERSION`; then `$C up -d plaidify access-executor`.
2. Re-key the stored data with `plaidify rotate-key --re-encrypt` (from a server
   checkout with the same environment), or let the hourly background pass do it
   and watch for `Key rotation pass complete` in the logs.
3. After `ACCESS_JOB_PAYLOAD_TTL` (one hour) and a clean rotation run, remove
   `ENCRYPTION_KEY_PREVIOUS` and recreate the containers again.

### JWT secret (`JWT_SECRET_KEY`)

Every access token stops working at once (clients holding a refresh token get a
new one at `POST /auth/refresh`; refresh tokens are not JWTs). Sign-in lockouts
reset, and unless `LINK_LAUNCH_SECRET` is set, pending hosted-link launch tokens
stop working too.

```bash
# 1. Generate a new secret and put it in .env.production
openssl rand -hex 32

# 2. Recreate the containers
$C up -d plaidify access-executor
```

To end every session, not just access tokens, users sign out everywhere
(`POST /auth/sessions/revoke-all`) or an administrator deactivates the account.

### Audit key (`AUDIT_HMAC_KEY`)

Move the old value to `AUDIT_HMAC_KEY_PREVIOUS`, set the new one, recreate the
containers, wait for `Audit chain sealed under the current signing key`, then
remove `AUDIT_HMAC_KEY_PREVIOUS` ([SECURITY.md](../SECURITY.md#audit-logging)).

On Azure both keys live in Key Vault (`KV=<vault>`):

```bash
az keyvault secret set --vault-name "$KV" --name audit-hmac-key-previous \
  --value "$(az keyvault secret show --vault-name "$KV" --name audit-hmac-key --query value -o tsv)" --output none
az keyvault secret set --vault-name "$KV" --name audit-hmac-key \
  --value "$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')" --output none
gh variable set AZURE_AUDIT_HMAC_KEY_ROTATING --env production --body true
```

Run the Deploy Azure workflow (the new revisions get both keys; if the log
line never comes, restart them with `az containerapp revision restart`), wait
for the line, then `gh variable delete AZURE_AUDIT_HMAC_KEY_ROTATING --env production`
and run it again. Leave `audit-hmac-key-previous` in Key Vault: with purge
protection a deleted name stays taken for the soft-delete retention period
(90 days by default), and the next rotation just writes a new version of it. `link-launch-secret` and `metrics-token`
rotate by setting a new version and restarting the API's revision
(`az containerapp revision restart`).

---

## Incident Response

### 1. Rate Limit Errors (429)

**Symptoms**: Users getting HTTP 429 responses.

```bash
# Current limiter keys in Redis (SCAN, never KEYS, in production)
$C exec redis sh -c 'redis-cli -a "$(cat /run/secrets/redis_password)" --no-auth-warning --scan --pattern "LIMITS*" | head -20'
```

All clients limited at once? The API is probably keying every request on the
proxy's address: check `FORWARDED_ALLOW_IPS` ([DEPLOYMENT.md](DEPLOYMENT.md#reverse-proxy-and-client-addresses)).
To raise a limit, change the matching `RATE_LIMIT_*` setting in
`.env.production` and run `$C up -d plaidify`. `RATE_LIMIT_DEFAULT` covers every
endpoint without its own limit, per client address and path
([SECURITY.md](../SECURITY.md#rate-limiting)).

### 2. Browser Pool Exhaustion

**Symptoms**: `/connect` requests timing out, high latency, `PlaidifyBrowserPoolSaturated`.

```bash
# The executor does the browser work in redis-worker mode
$C exec access-executor python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:9101/metrics').read().decode())" | grep plaidify_browser_pool
```

Raise `BROWSER_POOL_SIZE` in `.env.production` (watch memory: each context is
a Chromium tab) and run `$C up -d access-executor`, or scale executors
(`$C up -d --scale access-executor=2`) within the
[connection budget](DEPLOYMENT.md#database-connection-budget).

### 3. Database Connection Pool Exhaustion

**Symptoms**: 500 errors, "connection pool exhausted" in logs.

```bash
# Who holds the connections?
$C exec postgres psql -U plaidify -c \
  "SELECT usename, application_name, state, count(*) FROM pg_stat_activity GROUP BY 1, 2, 3 ORDER BY 4 DESC;"

# Long-running queries
$C exec postgres psql -U plaidify -c \
  "SELECT pid, now() - pg_stat_activity.query_start AS duration, query
   FROM pg_stat_activity WHERE state = 'active' ORDER BY duration DESC LIMIT 10;"
```

Raising `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` is only safe inside the server's
`max_connections`: see the [connection budget](DEPLOYMENT.md#database-connection-budget).

### 4. Audit Chain Verification Fails

**Symptoms**: `GET /audit/verify` reports `valid: false`.

```bash
# Needs an administrator's access token
curl -s -H "Authorization: Bearer $ADMIN_TOKEN" https://your.domain/audit/verify | jq .
```

The report lists the first entries that fail and why. Document the finding
and investigate before changing anything: the chain is never sealed while it
fails verification, and rows must never be deleted by hand.

### 5. Redis Unavailable

**Symptoms**: `/health/detailed` reports `redis: error` and answers 503; new
connections fail with 503 (in production the per-credential lock fails closed);
hosted-link and MFA flows in progress break; rate limits stop being enforced
(the limiter fails open).

```bash
# Redis itself
$C exec redis sh -c 'redis-cli -a "$(cat /run/secrets/redis_password)" --no-auth-warning ping'

# Plaidify's view of it
curl -s -H "Authorization: Bearer $HEALTH_CHECK_TOKEN" https://your.domain/health/detailed | jq .

# Restart Redis (AOF persistence keeps the job queue)
$C restart redis
```

Link and MFA flows in progress during the outage have to be restarted by the user.

### 6. Emergency Password Reset (Admin)

Prefer the normal flow: `POST /auth/forgot-password` sends the user a reset
email when SMTP is configured. If it is not, set a temporary password inside
the API container, which also ends the account's sessions:

```bash
$C exec plaidify python - <<'EOF'
from src.auth_utils import end_user_sessions
from src.database import SessionLocal, User
from src.dependencies import get_password_hash

with SessionLocal() as db:
    user = db.query(User).filter_by(username="target_user").one()
    user.hashed_password = get_password_hash("Temp-Passw0rd-change-me!")
    end_user_sessions(db, user.id)  # revokes refresh tokens, invalidates access tokens
    db.commit()
EOF
```

Give the user the temporary password over a separate channel and have them
change it.

---

## Common Issues

### "ENCRYPTION_KEY must decode to 32 bytes"
The key is malformed. Generate a new one (for a fresh install only — an
existing deployment needs its original key to read its data):
```bash
python -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
```

### "SQLite is not supported in production"
Set `DATABASE_URL` to a PostgreSQL connection string, e.g.
`postgresql://user:password@host:5432/plaidify`.

### "CORS wildcard (*) is not allowed in production"
Set explicit origins, e.g. `CORS_ORIGINS=https://app.example.com,https://dashboard.example.com`.

### `GET /health/detailed` answers 404
`ENV=production` without `HEALTH_CHECK_TOKEN`. Set it and send it as a bearer token.

### Extraction Timeouts
- Check `BROWSER_NAVIGATION_TIMEOUT` (default 30s) and `ENGINE_TIMEOUT_SECONDS` (default 300s)
- Check `GUNICORN_TIMEOUT` (default 120s) for requests that wait on a job
- Watch the browser-pool and latency panels in Grafana
- Consider increasing `BROWSER_POOL_SIZE` or the number of executors

### Audit Log Growing Too Large
- Lower `AUDIT_RETENTION_DAYS` (default 730); the daily retention job prunes
  older entries and appends a signed checkpoint, so the chain still verifies.
- Never `DELETE` audit rows by hand: that breaks the chain. To prune at once,
  call `src.audit.prune_audit_logs(db)` (it reads `AUDIT_RETENTION_DAYS`).

---

## Monitoring & Alerts

### Prometheus Metrics

What the API (`/metrics`, all workers aggregated; bearer `METRICS_TOKEN` when
set) and the executor (port 9101) export, and the alert that watches each.
Rules: `monitoring/alert_rules.yml`; setup: [monitoring/README.md](../monitoring/README.md).

| Metric | Type | Alert |
|--------|------|-------|
| `http_requests_total{status="5xx"}` | Counter | `PlaidifyHighErrorRate`: > 5% of responses for 5m |
| `http_request_duration_seconds_bucket{le="1.0"}` (per handler) | Histogram | `PlaidifySlowRequests`: > 5% of fast-path requests over 1s |
| `http_request_duration_highr_seconds_bucket` | Histogram | `PlaidifyHighLatencyP95`: overall p95 > 10s |
| `plaidify_browser_pool_active_contexts` / `plaidify_browser_pool_capacity_contexts` | Gauges | `PlaidifyBrowserPoolSaturated`: ≥ 80% busy for 10m |
| `plaidify_blueprint_extractions_total{status}` | Counter | `PlaidifyExtractionFailureRate`: > 20% errors for 15m |
| `plaidify_mfa_challenges_total{mfa_type}` | Counter | dashboard only |
| `plaidify_worker_heartbeat_timestamp_seconds` | Gauge | `PlaidifyExecutorStalled`: executor loop idle > 2m |
| `up{job=~"plaidify\|access-executor"}` | — | `PlaidifyInstanceDown`, `PlaidifyTargetMissing`, `PlaidifyExecutorMissing` |

### Health Check Endpoints

| Endpoint | Auth Required | Purpose |
|----------|--------------|---------|
| `GET /health` | No | Load balancer / container probe: `SELECT 1` against the database; exactly 200 when healthy, over plain HTTP, never a redirect |
| `GET /health/detailed` | `HEALTH_CHECK_TOKEN`, a user token or an API key (404 in production without `HEALTH_CHECK_TOKEN`; open elsewhere while it is unset) | Database, Redis, KMS and browser-pool state; 503 when the database, Redis or KMS fails |
| `GET /status` | No | Simple API status |
| executor `GET :9101/health` | No (internal port) | Executor liveness: 503 when its event loop has stopped |

### Log Monitoring

Watch for these log messages:
- `Slow query detected` — DB performance degradation
- `Refresh failed for` — background refresh issues
- `Disabled refresh for` — a schedule hit its failure limit
- `needs the user to sign in again; schedule disabled` — a refresh met MFA or rejected credentials
- `Refresh token reuse detected` — a rotated refresh token was presented again (possible theft)
- `Redis is unreachable for access locks` — jobs are being refused
- `Redis connection lost` — state store degradation
- `Re-encrypted tokens to current key version`, `Key rotation pass complete` — key rotation progress

---

## Backup & Recovery

### Database Backup

Use `scripts/backup_db.sh` (encrypted, verified, pruned) or the compose
`backup` service; never write dumps into the checkout. Full procedure:
[DISASTER_RECOVERY.md](DISASTER_RECOVERY.md).

```bash
# Encrypted backup + verification
BACKUP_AGE_RECIPIENT=age1... DATABASE_URL=postgres://... scripts/backup_db.sh backup
scripts/backup_db.sh verify

# Restore (asks for confirmation)
BACKUP_AGE_IDENTITY_FILE=backup-key.txt DATABASE_URL=postgres://... \
  scripts/backup_db.sh restore /var/backups/plaidify/plaidify-<stamp>.dump.age
```

### Disaster Recovery

1. **Database**: Restore from the latest encrypted dump or PostgreSQL point-in-time recovery
2. **Encryption Keys**: Must be stored separately in a secure vault (not in the DB)
3. **Redis**: Not the system of record, but it holds the job queue, link and MFA
   sessions and the hosted-link keys; flows in progress are lost with it. Run it
   with persistence and a replica. Refresh schedules are in PostgreSQL.
4. **Application**: Stateless — redeploy from the container registry

### Recovery Time Objectives

Targets, not measurements: no restore drill has been run yet
([DISASTER_RECOVERY.md](DISASTER_RECOVERY.md#restore-drill-quarterly)).

| Component | RTO | RPO |
|-----------|-----|-----|
| API (stateless) | < 5 min | N/A |
| Database | ≤ 1 hour | Last backup (or PITR) |
| Redis | < 5 min | In-progress flows lost; queue kept with AOF |
| Scheduled refresh | Automatic | Persisted in DB |
