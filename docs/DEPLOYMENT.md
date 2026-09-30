# Plaidify — Production Deployment Guide

## Prerequisites

| Component   | Minimum Version | Purpose                            |
|-------------|----------------|------------------------------------|
| Python      | 3.11 – 3.13    | Runtime (the image runs 3.11; CI tests all three) |
| PostgreSQL  | 16+            | Primary database                   |
| Redis       | 7+             | Shared state: RSA keys, rate limits, link sessions, MFA state, access-job queue |
| Playwright  | pinned in `requirements.txt` | Browser automation engine (Chromium headless shell) |
| Docker      | 24+ with Compose v2 | Container deployment (recommended) |

---

## Quick Start (Docker Compose)

```bash
# 1. Clone & enter
git clone <repo-url> && cd plaidify

# 2. Create .env from template and set the two required values
cp .env.example .env
python3 -c 'import base64,os; print("ENCRYPTION_KEY=" + base64.urlsafe_b64encode(os.urandom(32)).decode())' >> .env
python3 -c 'import secrets; print("JWT_SECRET_KEY=" + secrets.token_urlsafe(64))' >> .env

# 3. Create the Postgres and Redis passwords (the only place they live)
mkdir -p .secrets && chmod 700 .secrets
openssl rand -base64 32 > .secrets/postgres_password
openssl rand -base64 32 > .secrets/redis_password
chmod 644 .secrets/*   # containers read these as non-root users (.secrets/README.md)

# 4. Launch: Postgres and Redis start, the one-shot `migrate` service applies
#    the Alembic migrations, then the API starts
docker compose up -d

# 5. Verify
curl http://127.0.0.1:8000/health
```

The development stack publishes the API, Postgres and Redis on `127.0.0.1`
only, and Redis requires the password from `.secrets/redis_password` (it holds
the RSA keys that decrypt hosted-link credentials). The containers build
`DATABASE_URL` and `REDIS_URL` from the secret files when they start, so no
password goes into `.env`.

Evaluating without any setup: `docker compose -f docker-compose.demo.yml up --build`
runs the sandbox (API plus three bundled demo sites) on `127.0.0.1:8000`.

## Production Compose Stack

```bash
cp .env.production.example .env.production   # set ENCRYPTION_KEY, JWT_SECRET_KEY, CORS_ORIGINS
mkdir -p .secrets nginx/certs && chmod 700 .secrets
openssl rand -base64 32 > .secrets/postgres_password
openssl rand -base64 32 > .secrets/redis_password
chmod 644 .secrets/*   # containers read these as non-root users (.secrets/README.md)

# Add your TLS certificate and key as nginx/certs/fullchain.pem and
# nginx/certs/privkey.pem (git-ignored and never part of the image).

docker compose -f docker-compose.production.yml build
docker compose -f docker-compose.production.yml up -d
curl -f https://your.domain/health
```

What the production stack does compared to the default compose file:

- Only nginx publishes ports (80, 443). PostgreSQL, Redis, the API and the executor are reachable from the compose network alone.
- Redis requires a password and persists to disk (AOF plus snapshots).
- Plaidify builds `DATABASE_URL` and `REDIS_URL` from Docker secrets at container start.
- Database migrations run in a dedicated one-shot `migrate` service before the API and the executor start.
- Detached `/connect` jobs run in a separate `access-executor` service backed by Redis dispatch.
- nginx terminates TLS and is the edge proxy (see [Reverse proxy and client addresses](#reverse-proxy-and-client-addresses)).
- The API is health-checked by `plaidify-healthcheck` (plain-HTTP `GET /health`, 200 only; a redirect fails it), and nginx starts once the API is healthy.
- The executor has its own health check: its endpoint on port 9101 answers `/health` with 503 once the worker's event loop stops running.
- Chromium keeps its sandbox (see [Chromium sandbox](#chromium-sandbox)).
- An opt-in `backup` service takes encrypted, verified hourly dumps (see [DISASTER_RECOVERY.md](DISASTER_RECOVERY.md)).

### Reverse proxy and client addresses

The API only believes `X-Forwarded-For` and `X-Forwarded-Proto` from the peers
in `FORWARDED_ALLOW_IPS` (read by `gunicorn.conf.py`; default: loopback only).
Behind a proxy that is not listed, every request looks like plain HTTP from
the proxy's address: in production the HTTPS redirect answers every request
with a 307 back to the same URL (a loop), and all clients share one
rate-limit bucket.

- **Compose:** `docker-compose.production.yml` sets `FORWARDED_ALLOW_IPS` to
  the private address ranges; the API's port is reachable only from the
  compose network, where nginx is the only client that sets those headers.
- **Azure Container Apps:** `infra/main.bicep` sets it for the environment's
  ingress (parameter `forwardedAllowIps`).
- **Your own proxy:** set it to the proxy's address or network, e.g.
  `FORWARDED_ALLOW_IPS=10.0.1.0/24`. Use `*` only if nothing but the proxy can
  reach the port.

The proxy must overwrite, not append to, `X-Forwarded-For` if it is the edge
(`proxy_set_header X-Forwarded-For $remote_addr;` in nginx, as
`nginx/nginx.conf` does). Otherwise a client can put any address it likes in
front of its real one.

Health checks and scrapes talk to the container directly over plain HTTP:
`/health` and `/metrics` answer 200 there without redirecting. nginx does not
publish `/metrics`.

### Chromium sandbox

Chromium runs with its sandbox (`BROWSER_CHROMIUM_SANDBOX=true`, the default)
as the image's unprivileged `plaidify` user. The sandbox needs the user,
PID and network namespaces that Docker's default seccomp profile blocks, so
the compose files run the API and the executor with
`deploy/seccomp/chromium.json`: Docker's default profile plus exactly the
`clone`/`setns`/`unshare` rule Playwright documents, together with
`no-new-privileges`. No extra capabilities are added and the container is not
privileged.

Hosts that restrict unprivileged user namespaces through AppArmor (Ubuntu
23.10 and later: `sysctl kernel.apparmor_restrict_unprivileged_userns` is 1)
block the sandbox inside containers too; allow them on the host
(`sysctl -w kernel.apparmor_restrict_unprivileged_userns=0`, persisted in
`/etc/sysctl.d/`) or give the container an AppArmor profile that permits
`userns`. The symptom is `No usable sandbox!` in the browser's launch error.

If your runtime can't apply a custom seccomp profile, the alternative is
`BROWSER_CHROMIUM_SANDBOX=false`, which leaves the container boundary as the
only isolation between a malicious page's code and a process that holds
`ENCRYPTION_KEY` and the database credentials. Prefer a runtime that
supports the profile.

### Image variants

- Default: the API, executor and migration job, with the Chromium headless
  shell (no full Chrome, no X server).
- KMS: `docker build --build-arg INSTALL_KMS=true -t plaidify:kms .` adds the
  AWS, Azure and HashiCorp Vault SDKs (from the hash-locked
  `requirements-kms.lock`) for `KMS_PROVIDER=aws|azure|vault`
  ([KMS_INTEGRATION.md](KMS_INTEGRATION.md)).
- Demo: `docker build --target demo .` adds `scripts/demo.py` and runs the
  sandbox (used by `docker-compose.demo.yml`).

Every Python dependency is installed from the hash-locked `requirements.lock`
with `pip install --require-hashes`, so the image contains exactly the
versions CI tested and audited. The build context is an allow-list
(`.dockerignore`): `.env*`, `.secrets/`, `nginx/certs/`, backups, databases,
virtualenvs and `node_modules` never reach the builder. Application code in
the image is owned by root and read-only for the `plaidify` user; anything the
app writes goes to `/tmp` or a mounted volume.

## Release Validation

Run these checks before shipping a release candidate (Python 3.11–3.13,
dependencies from `requirements-dev.lock`):

```bash
PYTHONPATH=$PWD python -m pytest tests/ -q
python -m playwright install --only-shell chromium          # once
(cd frontend-next && npm ci && npm run build)               # the hosted page the browser tests load
PYTHONPATH=$PWD python -m pytest tests/test_hosted_link_e2e.py -q -m playwright
SKIP_BROWSER_TESTS=0 PYTHONPATH=$PWD python -m pytest tests/test_engine_integration.py -q
python scripts/demo.py --all
```

CI runs the same suite on Python 3.11–3.13 (with a real Redis), the browser
suites and the demo, the client SDK suites, migrations against PostgreSQL 16
(upgrade, `alembic check`, downgrade, upgrade), the compose, Prometheus and
Bicep configuration checks, and a production-mode smoke test of the built
image.

---

## Environment Variables Reference

Every variable, with its default and a one-line explanation, is in
[`.env.example`](../.env.example); [`.env.production.example`](../.env.production.example)
is the starting point for the production compose stack. The tables below
cover everything an operator sets or tunes.

### Required (no defaults — the app will not start without these)

| Variable          | Description                                         | Generate with                                                                 |
|-------------------|-----------------------------------------------------|-------------------------------------------------------------------------------|
| `ENCRYPTION_KEY`  | Base64url 256-bit key that wraps every user's data key (`KMS_PROVIDER=local`). Lose it and the stored credentials are gone. | `python -c "import base64,os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"` |
| `JWT_SECRET_KEY`  | Signs access tokens. At least 32 characters; startup refuses shorter. | `openssl rand -hex 32`                                                        |

### Keys and secrets

| Variable | Default | Description |
|----------|---------|-------------|
| `LINK_LAUNCH_SECRET` | unset | Signs hosted-link launch tokens. At least 32 characters and different from `JWT_SECRET_KEY`. Unset: derived from `JWT_SECRET_KEY` (HKDF), so rotating that key also voids pending launch tokens. |
| `AUDIT_HMAC_KEY` | unset | Signs the audit hash chain; at least 32 characters, kept outside the database. Unset: derived from `ENCRYPTION_KEY` (HKDF). |
| `AUDIT_HMAC_KEY_PREVIOUS` | unset | The previous audit key while rotating it ([SECURITY.md](../SECURITY.md#audit-logging)). |
| `ENCRYPTION_KEY_VERSION` | `1` | Current key version. Increment on rotation. |
| `ENCRYPTION_KEY_PREVIOUS` | unset | The previous key while rotating ([SECURITY.md](../SECURITY.md#key-rotation-procedure)). |
| `KMS_PROVIDER` | `local` | `local`, `aws`, `azure` or `vault` ([KMS_INTEGRATION.md](KMS_INTEGRATION.md)). The cloud providers need the `INSTALL_KMS=true` image. |
| `KMS_KEY_ID`, `KMS_REGION` | unset | AWS key ARN or alias, and region. Azure and Vault take `KMS_AZURE_*` / `KMS_VAULT_*` instead (`.env.example`). |

### Database

| Variable            | Default                   | Description                                           |
|---------------------|---------------------------|-------------------------------------------------------|
| `DATABASE_URL`      | `sqlite:///plaidify.db`   | SQLAlchemy URL. PostgreSQL is required when `ENV=production` (SQLite is refused at startup). |
| `DB_POOL_SIZE`      | `20`                      | Connection pool size per process (ignored for SQLite) |
| `DB_MAX_OVERFLOW`   | `10`                      | Extra connections beyond pool_size, per process       |
| `DB_POOL_RECYCLE`   | `3600`                    | Seconds before a connection is recycled                |

#### Database connection budget

Each API worker process and each executor process has its own pool, so the
peak number of connections is

```
replicas × GUNICORN_WORKERS × (DB_POOL_SIZE + DB_MAX_OVERFLOW)
  + executor replicas × (DB_POOL_SIZE + DB_MAX_OVERFLOW) + migrations
```

and it must stay below the server's `max_connections` minus its reserved
connections. The defaults (20 + 10 per process) are sized for a single
process; the production compose stack sets 5 + 5 (3 workers + executor = 40
against PostgreSQL's default of 100) and the Azure template sets 2 + 2 against
a Burstable B1ms server (see [AZURE_DEPLOYMENT.md](AZURE_DEPLOYMENT.md)).

### Redis

| Variable    | Default | Description                                              |
|-------------|---------|----------------------------------------------------------|
| `REDIS_URL` | unset | Required in production (startup fails without a reachable Redis) and whenever more than one worker runs outside development. Example: `redis://:password@redis:6379/0`; `rediss://` for TLS. |
| `REDIS_SOCKET_TIMEOUT_SECONDS` | `5.0` | Socket timeout of the link-session store and the job dispatcher |
| `ENGINE_REDIS_SOCKET_TIMEOUT` | `2.0` | Socket timeout of the engine's calls (MFA sessions, site rate limits) |

### Access jobs and background services

| Variable | Default | Description |
|----------|---------|-------------|
| `ACCESS_JOB_EXECUTION_MODE` | `inprocess` | `inprocess` runs jobs in the web workers; `redis-worker` queues them for the executor (`python -m src.access_job_worker`). The production compose stack and the Azure template use `redis-worker`. |
| `ACCESS_JOB_WORKER_CONCURRENCY` | `2` | Jobs one executor process runs at once |
| `ACCESS_WORKER_METRICS_PORT` | `9101` | The executor's `/metrics` and `/health` (0 disables them; the compose health check and the Azure probes need them) |
| `ACCESS_JOB_HEARTBEAT_SECONDS` | `10.0` | How often a running job renews its lock, its stream claim and its heartbeat |
| `ACCESS_JOB_STALE_AFTER_SECONDS` | `90` | A running job without a heartbeat for this long is failed by the reaper (its process died) and its lock freed |
| `ACCESS_JOB_QUEUE_TIMEOUT_SECONDS` | `600` | A job that no executor picked up within this time is failed |
| `ACCESS_JOB_DEADLINE_MARGIN_SECONDS` | `120` | Added to `ENGINE_TIMEOUT_SECONDS + MFA_TIMEOUT_SECONDS` to form a job's hard deadline |
| `ACCESS_JOB_DRAIN_SECONDS` | `25.0` | On SIGTERM the executor stops taking jobs and gives running ones this long before cancelling them; keep it below the stop grace period |
| `ACCESS_JOB_REAPER_INTERVAL_SECONDS` | `30.0` | How often the reaper looks for stuck jobs |
| `ACCESS_JOB_RECLAIM_IDLE_MS` | `30000` | A queued stream message this long without a heartbeat may be claimed by another executor; a reclaimed job runs only if it never started |
| `ACCESS_JOB_PAYLOAD_TTL` | `3600` | Seconds a queued job's encrypted payload lives in Redis |
| `ACCESS_JOB_WORKER_BLOCK_MS`, `ACCESS_JOB_STREAM_KEY`, `ACCESS_JOB_CONSUMER_GROUP` | `5000`, `plaidify:access_jobs:stream`, `plaidify-access-jobs` | Queue plumbing: idle wait, stream and consumer group names |
| `BACKGROUND_SERVICES_ENABLED` | `true` | Let this process take part in the scheduled-refresh, webhook-outbox and reaper leases. Each service runs in one process at a time; in `redis-worker` mode only in the executor. |
| `REFRESH_TICK_SECONDS`, `REFRESH_MAX_CONCURRENCY` | `30.0`, `5` | Scheduled refresh: how often due schedules are read, and how many run at once |
| `RESULT_RETENTION_DAYS` | `30` | Stored (encrypted) access-job results are erased after this many days |

### Webhooks

| Variable | Default | Description |
|----------|---------|-------------|
| `WEBHOOK_MAX_ATTEMPTS` | `10` | Delivery attempts per event before it is marked failed |
| `WEBHOOK_RETRY_BASE_SECONDS`, `WEBHOOK_RETRY_MAX_SECONDS` | `15.0`, `3600.0` | First retry delay, doubling up to the maximum |
| `WEBHOOK_TIMEOUT_SECONDS` | `10.0` | Timeout of one delivery request |
| `WEBHOOK_POLL_INTERVAL_SECONDS` | `5.0` | How often the outbox looks for due deliveries |
| `WEBHOOK_DELIVERY_RETENTION_DAYS` | `7` | Finished deliveries stay listed this long |
| `WEBHOOK_ALLOW_PRIVATE_TARGETS` | `false` | Allow private-network destinations on development networks. Ignored in production, where destinations must be public `https://` URLs. |

### Process (gunicorn)

`gunicorn.conf.py` is the only place worker settings live; the image, the
compose files and the Azure template all start `gunicorn src.main:app -c gunicorn.conf.py`.

| Variable | Default | Description |
|----------|---------|-------------|
| `GUNICORN_WORKERS` (or `WEB_CONCURRENCY`) | one per available CPU (container quota aware), 2 to 4 | Uvicorn worker processes. Each may start its own browser pool, so memory is usually the limit. |
| `GUNICORN_BIND` | `0.0.0.0:8000` | Listen address |
| `GUNICORN_TIMEOUT` | `120` | Worker timeout (seconds) |
| `GUNICORN_GRACEFUL_TIMEOUT` | `30` | Graceful shutdown (keep below the orchestrator's stop grace period) |
| `GUNICORN_KEEPALIVE`, `GUNICORN_LOG_LEVEL` | `5`, `info` | Keep-alive seconds; gunicorn's log level |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1,::1` | Proxies trusted for `X-Forwarded-*` (addresses or CIDR networks) |
| `PROMETHEUS_MULTIPROC_DIR` | private temp dir | Where workers share metric values (see Monitoring) |

### Accounts and sign-in

| Variable                          | Default   | Description                       |
|-----------------------------------|-----------|-----------------------------------|
| `JWT_ALGORITHM`                   | `HS256`   | JWT signing algorithm             |
| `JWT_ACCESS_TOKEN_EXPIRE_MINUTES` | `15`      | Access token TTL (minutes)        |
| `JWT_REFRESH_TOKEN_EXPIRE_MINUTES`| `10080`   | Refresh token TTL (7 days)        |
| `REGISTRATION_ENABLED` | `true` in development, `false` in production | Public `POST /auth/register`. In production it is off unless set explicitly; provision accounts with `BOOTSTRAP_USER_*`. |
| `BOOTSTRAP_USER_USERNAME`, `BOOTSTRAP_USER_EMAIL`, `BOOTSTRAP_USER_PASSWORD` | unset | With all three set, startup creates this administrator (idempotent). It never promotes an existing account: a clash with one stops startup in production. Remove them once the account exists. |
| `OAUTH_ENABLED` | `false` | `POST /auth/oauth2` social login. With it on, startup fails unless every provider in `OAUTH_ALLOWED_PROVIDERS` (default `google,github`) has its ids: `OAUTH_GOOGLE_CLIENT_ID` for Google; `OAUTH_GITHUB_CLIENT_ID` and `OAUTH_GITHUB_CLIENT_SECRET` for GitHub. |
| `OAUTH_AUTO_REGISTER` | `true` | Create an account on the first login with a verified email |

### Password-reset mail

| Variable | Default | Description |
|----------|---------|-------------|
| `SMTP_HOST`, `SMTP_FROM` | unset | Both are needed to send reset mail. Without them `POST /auth/forgot-password` sends nothing (production logs a warning at startup). |
| `SMTP_PORT`, `SMTP_STARTTLS`, `SMTP_TIMEOUT_SECONDS` | `587`, `true`, `10.0` | Connection settings; disable STARTTLS only for a local relay |
| `SMTP_USERNAME`, `SMTP_PASSWORD` | unset | SMTP login, if the relay needs one |
| `PASSWORD_RESET_URL` | unset | Your reset page, with `{token}` in it. Unset: the email carries the one-time code for `POST /auth/reset-password`. |

### Server

| Variable         | Default        | Description                                           |
|------------------|----------------|-------------------------------------------------------|
| `APP_NAME`       | `Plaidify`     | Application name                                      |
| `APP_VERSION`    | `0.3.0b1`      | Reported version                                      |
| `ENV`            | `development`  | `development`, `staging`, or `production`              |
| `DEBUG`          | `false`        | Debug mode; startup refuses `true` in production      |
| `LOG_LEVEL`      | `INFO`         | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`       |
| `LOG_FORMAT`     | `json`         | `json` (structured) or `text`                         |
| `CORS_ORIGINS`   | `http://localhost:3000,http://localhost:8000,http://localhost:8080` | Comma-separated allowed origins; `*` is refused in production. |
| `ENFORCE_HTTPS`  | `false`        | Redirect plain HTTP to HTTPS (except `/health` and `/metrics`) and send HSTS. Always on in production; needs `FORWARDED_ALLOW_IPS` behind a proxy. |
| `DOCS_ENABLED`   | `false`        | Serve `/docs`, `/redoc` and `/openapi.json` in production. They are always served outside production. |
| `CONNECTORS_DIR` | `connectors`   | Directory holding the connector blueprints            |

### Hosted Link

| Variable | Default | Description |
|----------|---------|-------------|
| `PUBLIC_LINK_SESSIONS_ENABLED` | `false` | Allow anonymous `POST /link/sessions/public` in production (it is always allowed outside production) |
| `PUBLIC_LINK_ALLOWED_ORIGINS` | `""` | Comma-separated origins allowed to create anonymous sessions; when set, other origins are refused |
| `LINK_LAUNCH_TOKEN_EXPIRE_SECONDS` | `300` | Lifetime of the signed tokens from `POST /link/bootstrap` |
| `LINK_EVENT_KEEPALIVE_SECONDS` | `15.0` | Keep-alive interval of the `GET /link/events/{token}` stream |

### Observability and health

| Variable | Default | Description |
|----------|---------|-------------|
| `SENTRY_DSN` | unset | Sentry DSN for application error reporting |
| `OTEL_ENDPOINT` | unset | OTLP/gRPC endpoint for traces. `https://` (or no scheme) uses TLS, `http://` is plaintext; the standard `OTEL_EXPORTER_OTLP_*` variables (CA, client certificate, headers, `..._INSECURE`) apply |
| `HEALTH_CHECK_TOKEN` | unset | Bearer token for `GET /health/detailed` (a signed-in user or an API key is accepted too). In production the endpoint answers 404 until it is set; elsewhere it is open while unset. |
| `METRICS_TOKEN` | unset | When set, `GET /metrics` requires `Authorization: Bearer <METRICS_TOKEN>`. Set it wherever others can reach the API port (the Azure ingress publishes `/metrics`). |

### Rate Limiting

| Variable              | Default      | Description                     |
|-----------------------|--------------|---------------------------------|
| `RATE_LIMIT_ENABLED`  | `true`       | Enable/disable rate limiting    |
| `RATE_LIMIT_AUTH`     | `5/minute`   | `/auth/token`, `/auth/oauth2`, `/auth/refresh` |
| `RATE_LIMIT_CONNECT`  | `10/minute`  | `POST /connect`                 |
| `RATE_LIMIT_MFA`      | `5/minute`   | `POST /mfa/submit`              |
| `RATE_LIMIT_ENCRYPTION` | `10/minute` | `POST /encryption/session` and `GET /encryption/public_key/{token}` (they generate RSA keys) |
| `RATE_LIMIT_DEFAULT`  | `60/minute`  | Every other endpoint, per client address and path (the endpoints above keep their own limit too). Exempt: `/health`, `/metrics`, the hosted `/link` page, its static assets, its status polling (`/link/sessions/{token}/status`) and event stream (`/link/events/{token}`) ([SECURITY.md](../SECURITY.md#rate-limiting)) |

Limits are per client address, which is only right when the proxy is trusted
(see [Reverse proxy and client addresses](#reverse-proxy-and-client-addresses)).
The limiter uses Redis when `REDIS_URL` is set and fails open if Redis stops
answering mid-request.

### Engine and browser

| Variable                    | Default | Description                                     |
|-----------------------------|---------|-------------------------------------------------|
| `ENGINE_TIMEOUT_SECONDS`    | `300`   | Automation budget per connection; time spent waiting for an MFA answer is not counted |
| `MFA_TIMEOUT_SECONDS`       | `300`   | How long a connection waits for the user's MFA answer before it ends as `mfa_timeout` |
| `MFA_MAX_ATTEMPTS`          | `3`     | Codes accepted per challenge before a rejected code fails the connection |
| `ENGINE_TRUSTED_CONNECTORS` | `""`    | Extra site keys you vouch for: they may run `execute_js` steps, and `BROWSER_BLOCK_PRIVATE_NETWORKS` decides their private-network access. Bundled connectors are trusted already; every other blueprint (generated, registry, user-supplied) is not. |
| `ENGINE_ALLOW_INTERNAL_CONNECTORS` | `false` | Run internal/fixture/sandbox connectors and reach loopback outside `DEMO_MODE`; tests and local development only |
| `BROWSER_HEADLESS`          | `true`  | Run Playwright in headless mode                  |
| `BROWSER_POOL_SIZE`         | `5`     | Max concurrent browser contexts per process      |
| `BROWSER_IDLE_TIMEOUT`      | `300`   | Seconds before idle context is closed            |
| `BROWSER_NAVIGATION_TIMEOUT`| `30000` | Navigation timeout (ms)                          |
| `BROWSER_ACTION_TIMEOUT`    | `10000` | Action timeout (click, fill) (ms)                |
| `BROWSER_BLOCK_RESOURCES`   | `true`  | Block images/fonts/analytics for speed           |
| `BROWSER_STEALTH`           | `true`  | Randomized viewport and user agent               |
| `BROWSER_CHROMIUM_SANDBOX`  | `true`  | Chromium's sandbox (see [Chromium sandbox](#chromium-sandbox)) |
| `BROWSER_BLOCK_PRIVATE_NETWORKS` | `true` | Refuse private, loopback, link-local, CGNAT and cloud-metadata addresses for trusted connectors too (untrusted ones are always refused) |
| `STRICT_READ_ONLY_MODE`     | `true`  | The per-phase read-only rules ([SECURITY.md](../SECURITY.md#runtime-safety-model)); domain scoping and the address policy apply either way |
| `BROWSER_ALLOW_READ_DOWNLOADS` | `true` | Capture file downloads during the read phase |
| `BROWSER_DOWNLOAD_ROOT`     | `/tmp/plaidify-downloads` | Root directory for temporary browser downloads (one directory per session, removed afterwards) |
| `BROWSER_MAX_DOWNLOAD_BYTES` | `10485760` | Largest download returned inline with a result; larger files are reported but omitted |
| `PLAIDIFY_SELECTOR_CACHE_PATH` | unset | Writable file that keeps learned selectors across restarts (memory only when unset) |

### LLM Extraction and resilience

| Variable            | Default   | Description                                         |
|---------------------|-----------|-----------------------------------------------------|
| `LLM_PROVIDER`      | `openai`  | `openai` or `anthropic`                              |
| `LLM_API_KEY`       | unset     | API key for the provider. Unset: no LLM extraction, and `POST /blueprints/generate` answers 503. |
| `LLM_MODEL`         | unset     | Model override (e.g. `gpt-4o`)                       |
| `LLM_BASE_URL`      | unset     | Custom base URL (Azure OpenAI, local servers)        |
| `LLM_MAX_TOKENS`    | `4096`    | Max completion tokens                                |
| `LLM_TEMPERATURE`   | `0.0`     | Temperature (0.0 = deterministic)                    |
| `LLM_TIMEOUT`       | `60.0`    | HTTP timeout for LLM calls (seconds)                 |
| `LLM_TOKEN_BUDGET`  | `30000`   | Max input tokens sent to the LLM; larger pages are truncated |
| `LLM_FALLBACK_MODEL`| unset     | Fallback model if the primary fails                  |
| `LLM_EFFORT`        | `low`     | Reasoning effort for models that support it: `low`, `medium`, `high`, `xhigh`, `max`; empty for the model default |
| `LLM_SERVER_SIDE_FALLBACKS` | `true` | Anthropic API: re-run a declined request on Anthropic's fallback model |
| `LLM_CIRCUIT_FAILURE_THRESHOLD`, `LLM_CIRCUIT_RESET_SECONDS` | `5`, `30.0` | Consecutive LLM failures that open the circuit; seconds before a trial call |
| `LLM_RETRY_MAX_ATTEMPTS` | `2` | Extra retries (with backoff) on LLM rate-limit errors |
| `BROWSER_CIRCUIT_FAILURE_THRESHOLD`, `BROWSER_CIRCUIT_RESET_SECONDS` | `5`, `30.0` | Consecutive browser-launch failures that open the circuit; seconds before a trial launch |

### Audit retention and demo mode

| Variable | Default | Description |
|----------|---------|-------------|
| `AUDIT_RETENTION_DAYS` | `730` | Older audit entries are pruned daily; the chain stays verifiable ([SECURITY.md](../SECURITY.md#audit-logging)) |
| `DEMO_MODE` | `false` | Makes the bundled sandbox connectors discoverable. Never in production. |
| `DEMO_PORTAL_URL` | `http://127.0.0.1:8799` | Base URL of the bundled demo portal |

---

## Docker Secrets

Both compose stacks read passwords from files in `.secrets/` (git-ignored,
excluded from the build context; see [`.secrets/README.md`](../.secrets/README.md)):

```bash
mkdir -p .secrets && chmod 700 .secrets
openssl rand -base64 32 > .secrets/postgres_password
openssl rand -base64 32 > .secrets/redis_password
chmod 644 .secrets/*   # containers read these as non-root users (.secrets/README.md)
```

The same files feed Postgres, Redis and the app, so there is one source for
each password; nothing goes into `.env` or `.env.production`. The monitoring
stack adds `grafana_admin_password`, `alertmanager_webhook_url` and
`metrics_token` (the API's `METRICS_TOKEN`, which Prometheus sends), and the
backup service `backup_age_recipients`.

> **Important:** Never commit the `.secrets/` directory or `nginx/certs/`. Both are in `.gitignore`.

---

## Production Checklist

### Security

- [ ] Set strong `ENCRYPTION_KEY` and `JWT_SECRET_KEY` (never reuse dev values), plus separate `AUDIT_HMAC_KEY` and `LINK_LAUNCH_SECRET`
- [ ] Set `HEALTH_CHECK_TOKEN` (otherwise `/health/detailed` answers 404) and `METRICS_TOKEN` wherever `/metrics` is reachable from outside the private network
- [ ] Configure `SMTP_HOST` / `SMTP_FROM` (and `PASSWORD_RESET_URL`) if users reset their own passwords
- [ ] Set `ENV=production` and `ENFORCE_HTTPS=true`
- [ ] Set `FORWARDED_ALLOW_IPS` to your proxy's network, and have the edge proxy overwrite `X-Forwarded-For`
- [ ] Configure `CORS_ORIGINS` to your exact frontend domain(s)
- [ ] Prefer `POST /link/bootstrap` plus `POST /link/sessions/bootstrap` for hosted-link launches
- [ ] Leave `PUBLIC_LINK_SESSIONS_ENABLED=false` unless you intentionally support anonymous hosted-link bootstrapping
- [ ] Leave `REGISTRATION_ENABLED` unset (off in production) and provision the first administrator with `BOOTSTRAP_USER_*`; remove those values once it exists
- [ ] Leave `DOCS_ENABLED` unset: the OpenAPI schema maps every endpoint
- [ ] Place behind a reverse proxy (nginx, Caddy, ALB) that terminates TLS
- [ ] Restrict database and Redis access to the application network only
- [ ] Keep the Chromium sandbox on (seccomp profile applied)
- [ ] Set `DEBUG=false`

### Database

- [ ] Use PostgreSQL (not SQLite) with the `DATABASE_URL` env var
- [ ] Run all migrations before the new code starts: `alembic upgrade head` (the compose stacks and the Azure workflow do this for you)
- [ ] Connect the app as a role without DDL rights (`scripts/provision_app_db_role.py`); keep the owner for migrations
- [ ] Schedule encrypted backups and run a restore drill ([DISASTER_RECOVERY.md](DISASTER_RECOVERY.md))
- [ ] Size `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` with the [connection budget](#database-connection-budget)

### Redis

- [ ] Set `REDIS_URL` for shared state across workers
- [ ] Enable persistence (AOF): queued access jobs, link sessions and MFA state live there
- [ ] Set a Redis password (`redis://:password@host:6379/0`)

### Process Management

`gunicorn.conf.py` configures:

- **Workers**: one uvicorn worker per available CPU (it reads the container's CPU quota), between 2 and 4; override with `GUNICORN_WORKERS`
- **Timeouts**: 120s request, 30s graceful shutdown
- **No preload**: every worker imports the app itself, so no socket, thread or gRPC channel is shared across a fork
- **Metrics**: Prometheus multiprocess mode, so `/metrics` reports all workers

The image runs [tini](https://github.com/krallin/tini) as PID 1: signals reach
gunicorn (or the executor) and exited browser processes are reaped.

In local and default compose setups (`ACCESS_JOB_EXECUTION_MODE=inprocess`),
access jobs run inside the web workers. On shutdown they are cancelled and
marked as such instead of being left in `running`; they do not survive a
restart.

In the production compose stack and on Azure (`redis-worker`), the API queues
jobs in a Redis stream and the `access-executor` service runs them, so
restarting the API does not touch them. What an executor restart does:

- **SIGTERM** (a deploy, `docker compose stop`): the executor stops taking
  jobs, gives running ones `ACCESS_JOB_DRAIN_SECONDS` (25 s) to finish, then
  cancels the rest (status `cancelled`, locks freed) and closes the browser.
  Jobs still queued wait in the stream for the next executor.
- **Crash or kill**: the reaper (in whichever process holds its lease) fails
  a running job once its heartbeat is `ACCESS_JOB_STALE_AFTER_SECONDS` (90 s)
  old and frees its lock. A job that had started is never re-run — logging in
  to a site twice is not safe to repeat blindly — so its caller sees `failed`
  and starts again. A job that never started is picked up by another executor.
- Jobs past their hard deadline, or never picked up within
  `ACCESS_JOB_QUEUE_TIMEOUT_SECONDS`, are failed the same way.

```bash
# Run directly
gunicorn src.main:app -c gunicorn.conf.py

# Or via Docker Compose
docker compose up -d
```

### GitHub Deployment Configuration

The only deployment workflow is `.github/workflows/deploy-azure.yml` (Azure
Container Apps). It deploys `main` only, for a commit whose CI run passed,
through a protected GitHub environment. Set up:

- GitHub variables: `AZURE_RESOURCE_GROUP`, `AZURE_LOCATION`, `AZURE_NAME_PREFIX`, `AZURE_APP_ENV`, `AZURE_CORS_ORIGINS`
- GitHub secrets: `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `AZURE_POSTGRES_ADMIN_LOGIN`, `AZURE_POSTGRES_ADMIN_PASSWORD`, `ENCRYPTION_KEY`, `JWT_SECRET_KEY`
- Optional GitHub secrets: `LLM_API_KEY`, `HEALTH_CHECK_TOKEN` (without it `/health/detailed` answers 404), `SMTP_PASSWORD`, `BOOTSTRAP_USER_PASSWORD`
- Optional GitHub variables: `AZURE_LLM_PROVIDER`, `AZURE_LLM_MODEL`; `AZURE_SMTP_HOST` + `AZURE_SMTP_FROM` (with `AZURE_SMTP_PORT`, `AZURE_SMTP_USERNAME`, `AZURE_PASSWORD_RESET_URL`) for password-reset mail; `AZURE_BOOTSTRAP_USER_USERNAME` + `AZURE_BOOTSTRAP_USER_EMAIL` (with the secret `BOOTSTRAP_USER_PASSWORD`) for the first administrator; `AZURE_AUDIT_HMAC_KEY_ROTATING` while rotating the audit key

The workflow generates `AUDIT_HMAC_KEY`, `LINK_LAUNCH_SECRET` and
`METRICS_TOKEN` once, keeps them in Key Vault, and passes them to the apps, so
an Azure deployment never derives those keys from `ENCRYPTION_KEY` /
`JWT_SECRET_KEY` and never serves `/metrics` without a token
([AZURE_DEPLOYMENT.md](AZURE_DEPLOYMENT.md#runtime-wiring)).

See [SELF_HOST.md](SELF_HOST.md) for the identity, role and environment setup.

### Monitoring

- **Prometheus metrics** at `GET /metrics` on the API (all gunicorn workers aggregated) and on the executor's port 9101. Scrape them on the internal network; nginx does not publish `/metrics`. With `METRICS_TOKEN` set, the scrape must send it as a bearer token (the executor's endpoint takes none). Dashboards, alert rules and Alertmanager: [monitoring/README.md](../monitoring/README.md).
- **Public health check** at `GET /health` runs `SELECT 1` against the database and returns `200` or `503`. Probes must see exactly 200: a redirect means the HTTPS or proxy settings are wrong.
- **Detailed health check** at `GET /health/detailed` reports the database, Redis, KMS and browser-pool state (`healthy`, or `degraded` with 503 when the database, Redis or KMS fails). It takes `HEALTH_CHECK_TOKEN` as a bearer token, or a signed-in user or API key; in production it answers 404 until the token is set, elsewhere it is open while the token is unset.
- **Structured logs** use JSON format by default for log aggregation (ELK, Datadog, etc.)
- **Traces** go to `OTEL_ENDPOINT` over OTLP/gRPC with TLS unless the endpoint is `http://`.

### Scaling

- Increase `BROWSER_POOL_SIZE` for higher concurrent scraping throughput
- Scale horizontally by running more API containers and executors (`docker compose up --scale access-executor=2`); recheck the [connection budget](#database-connection-budget)
- Redis is **required** for multi-worker/multi-container deployments: RSA keys, rate limits, link and MFA sessions, locks and leases are shared through it

---

## Database Migrations

```bash
# Apply all pending migrations
alembic upgrade head

# Check current migration state
alembic current

# Create a new migration after model changes
alembic revision --autogenerate -m "description"
```

With Docker Compose, migrations run in the `migrate` service on every
`up`; to run them by hand: `docker compose run --rm migrate`.

---

## TLS Configuration

Plaidify does not terminate TLS itself. Use a reverse proxy, and tell the API
to trust it (`FORWARDED_ALLOW_IPS`):

**Caddy (auto-TLS):**
```
plaidify.example.com {
    reverse_proxy localhost:8000 {
        header_up X-Forwarded-For {remote_host}
    }
}
```

**nginx** (the full version the production stack uses is `nginx/nginx.conf`):
```nginx
server {
    listen 443 ssl;
    server_name plaidify.example.com;
    ssl_certificate /etc/ssl/cert.pem;
    ssl_certificate_key /etc/ssl/key.pem;

    location = /metrics { return 404; }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

With TLS termination in place and the proxy trusted, `ENFORCE_HTTPS=true`
adds HSTS and redirects plain-HTTP requests that came through the proxy to
HTTPS.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| The process exits with `PLAIDIFY CONFIGURATION ERROR` | A required variable is missing or a value is invalid (the `Details:` line says which) | Set `ENCRYPTION_KEY` and `JWT_SECRET_KEY`; fix the named setting |
| "SQLite is not supported in production" | `ENV=production` without `DATABASE_URL` | Point `DATABASE_URL` at PostgreSQL |
| Startup stops: `JWT_SECRET_KEY must be at least 32 characters`, `DEBUG must be false in production`, `REDIS_URL is required in production…`, `OAUTH_ENABLED is set but … is missing` | A production or security precondition | Fix the setting the message names |
| Startup stops: `BOOTSTRAP_USER_USERNAME / BOOTSTRAP_USER_EMAIL match an existing account…` | The bootstrap values clash with an account someone else holds; it is never promoted | Pick another username and email, or remove `BOOTSTRAP_USER_*` |
| Every request answers `307` to the same URL | The proxy isn't trusted, so the API sees plain HTTP | Set `FORWARDED_ALLOW_IPS` to the proxy's network |
| All clients hit `429` together | Rate limits keyed on the proxy's address | Same: set `FORWARDED_ALLOW_IPS`; have the edge proxy overwrite `X-Forwarded-For` |
| `/health` returns `503` | The database is unreachable (`/health` checks only the database) | Check `DATABASE_URL` and the network; `/health/detailed` shows Redis and KMS too |
| `/health/detailed` returns `404` | `ENV=production` without `HEALTH_CHECK_TOKEN` | Set `HEALTH_CHECK_TOKEN` and send it as a bearer token |
| Executor container `unhealthy` | Its event loop stopped (`/health` on port 9101 answers 503) or the endpoint is disabled | Check its logs; `ACCESS_WORKER_METRICS_PORT` must not be 0 |
| "No usable sandbox" in browser logs | The seccomp profile isn't applied | Apply `deploy/seccomp/chromium.json` (see [Chromium sandbox](#chromium-sandbox)) |
| `too many connections` from Postgres | Pools exceed `max_connections` | Lower `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` or workers ([budget](#database-connection-budget)) |
| `429 Too Many Requests` | Rate limit exceeded | Adjust `RATE_LIMIT_*` env vars or wait |
| `423 Locked` from `POST /auth/token` | Too many failed sign-ins: 5 for one username from one address, or 20 for the username overall, within 15 minutes | Wait 15 minutes; a password reset clears the lock |
| Browser timeouts | Slow target sites | Increase `BROWSER_NAVIGATION_TIMEOUT` |
| High memory usage | Too many browser contexts | Lower `BROWSER_POOL_SIZE` or `GUNICORN_WORKERS` |
