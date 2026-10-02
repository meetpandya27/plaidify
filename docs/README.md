# Plaidify — Technical Documentation

> For the product overview and quick start, see the [main README](../README.md).
> For agent-facing integration patterns, see [AGENTS.md](AGENTS.md).

## System Overview

Plaidify is a FastAPI control plane for authenticated web access. The HTTP app
is built in `src/app.py` (`src/main.py` re-exports it for `gunicorn src.main:app`).
Routers in `src/routers/` own the public API; the engine, access jobs, shared
state, background services, SDKs and the hosted Link page live in their own
modules.

```text
Client, backend, mobile shell, or agent
  -> access token (Bearer) or API key (X-API-Key)
  -> direct connect or hosted link session
  -> access job (in the web process, or queued for the executor) and MFA state
  -> connector / blueprint execution in a read-only headless browser
  -> structured result, public token, webhook, audit entry
```

## API Surface

The OpenAPI schema (`/openapi.json`, with `/docs` and `/redoc`; off in
production unless `DOCS_ENABLED=true`) is the full contract. Authentication
column: **JWT** = `Authorization: Bearer <access token>` only; **JWT or key** =
that or `X-API-Key: pk_…`; **admin** = a JWT of an administrator; **public** =
no credentials (a token in the path or body is the capability).

| Area | Endpoints | Authentication |
| --- | --- | --- |
| System | `GET /`, `/health`, `/status`, `/organizations/summary`, `/organizations/search`, `/organizations/{id}`, `/link/error-taxonomy`, `/blueprints`, `/blueprints/{site}` | public |
| | `GET /health/detailed` | `HEALTH_CHECK_TOKEN`, JWT or key (open outside production while no token is set; 404 in production without one) |
| | `GET /metrics` (not in the schema) | `METRICS_TOKEN` when set |
| | `POST /blueprints/generate` | admin |
| Accounts | `POST /auth/register`, `/auth/verify-email`, `/auth/token` (form), `/auth/oauth2`, `/auth/refresh`, `/auth/forgot-password`, `/auth/reset-password` | public (rate-limited) |
| | `GET`/`DELETE /auth/me`, `GET /auth/sessions`, `POST /auth/sessions/revoke-all` | JWT |
| Direct connect | `POST /connect` | JWT or key, or the `link_token` of a live hosted session |
| | `POST /encryption/session`, `GET /encryption/public_key/{link_token}`, `POST /mfa/submit`, `GET /mfa/status/{session_id}` | public (rate-limited; the link token or MFA session id is the capability) |
| | `POST /disconnect` | JWT or key |
| Access jobs | `GET /access_jobs` | JWT or key |
| | `GET /access_jobs/{job_id}`, `POST /access_jobs/{job_id}/cancel` | JWT or key for owned jobs; an ownerless job by its id |
| Link flow | `POST /create_link`, `/submit_credentials`, `/submit_instructions`, `/fetch_data`, `/exchange/public_token`; `GET /links`, `/tokens`; `DELETE /links/{link_token}`, `/tokens/{token}` | JWT or key |
| Hosted link | `POST /link/sessions`, `POST /link/bootstrap` | JWT or key |
| | `GET /link?token=…`, `POST /link/sessions/bootstrap`, `GET /link/sessions/{link_token}/status`, `POST /link/sessions/{link_token}/event`, `GET /link/events/{link_token}` | public (token as capability) |
| | `POST /link/sessions/public` | public; refused in production unless `PUBLIC_LINK_SESSIONS_ENABLED` |
| Consent | `POST /consent/request`, `/consent/{id}/approve`, `/consent/{id}/deny`; `GET /consent`; `DELETE /consent/{consent_token}` | JWT or key (agents cannot approve or deny) |
| API keys, agents | `/api-keys`, `/agents` (create, list, get, update, delete) | JWT |
| Webhooks | `POST /webhooks/register`, `/webhooks/test`; `GET /webhooks`, `/webhooks/{id}/deliveries`; `DELETE /webhooks/{id}` | JWT |
| Scheduled refresh | `POST /refresh/schedule`, `PATCH`/`DELETE /refresh/schedule/{access_token}`, `GET /refresh/jobs` | JWT |
| | `GET /refresh/admin/jobs` | admin |
| Registry | `GET /registry/search`, `/registry/{site}` | public |
| | `POST /registry/publish`, `DELETE /registry/{site}` | JWT (a site belongs to its first publisher) |
| Audit | `GET /audit/logs` (your own entries) | JWT |
| | `GET /audit/verify` | admin |
| Admin | `GET /admin/users`, `POST /admin/users/{id}/promote`, `POST /admin/users/{id}/set-active` | admin |

Contract rules that apply everywhere:

- Secrets travel only in JSON bodies (`/connect`, `/mfa/submit`,
  `/submit_credentials`, `/fetch_data`, …), never in query strings.
- List endpoints page with `limit` / `offset` (`/links`, `/tokens`,
  `/api-keys`, `/agents`, `/registry/search`, `/admin/users`, `/audit/logs`;
  `GET /access_jobs` takes `limit` only).
- Scopes are lists of field names, `"balance"` or `"read:balance"`; omitted
  means every field, `[]` means none.
- Timestamps are ISO 8601 with a UTC offset.

### Sign-up

`POST /auth/register {username, email, password}` creates the account and
returns its tokens, unless sign-ups prove their address first
(`REGISTRATION_EMAIL_VERIFICATION`, on by default in production). Then it
answers `202 {"status": "verification_sent", "detail": …}` whether or not the
username or address is taken, and emails the address: a one-time token (a
link to `EMAIL_VERIFICATION_URL` when set; valid 24 hours, and a new sign-up
for the address replaces it), or a note that the address already has an
account or that the username is taken. `POST /auth/verify-email {token, password}`
creates the account and returns its tokens — `password` is the one from
registration, so the token alone does nothing. An unknown, used or expired
token, or a wrong password, is the same `400` (the token is not spent), and
`409` means the username or address was taken in the meantime (register
again). While verification is off, `/auth/verify-email` answers `404` and a
taken username or address is `400` at registration.

### Webhooks

`POST /webhooks/register {link_token, url, secret}` subscribes to a link you
own (a hosted session or a `/create_link` token); in production `url` must be
a public `https://` address. Events: `LINK_OPEN`, `LINK_COMPLETE` (carries the
one-time `public_token`), `LINK_ERROR`, `LINK_EXIT` and `MFA_REQUIRED` as
`{event, link_token, timestamp, data}`, and the refresh events `DATA_REFRESHED`
/ `REFRESH_FAILED` ([SCHEDULED_REFRESH.md](SCHEDULED_REFRESH.md)). Every body
also carries `delivery_id` and `webhook_id`. Deliveries are retried with
backoff (`WEBHOOK_MAX_ATTEMPTS`) and listed at `GET /webhooks/{id}/deliveries`.

Each request is signed with the webhook's secret. Verify it over the raw body
before parsing it:

```python
import hashlib
import hmac
import time


def verify(secret: str, raw_body: bytes, timestamp: str, signature: str, tolerance: int = 300) -> bool:
    """X-Plaidify-Timestamp and X-Plaidify-Signature from the request headers."""
    if abs(time.time() - int(timestamp)) > tolerance:
        return False
    digest = hmac.new(secret.encode(), timestamp.encode() + b"." + raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest("sha256=" + digest, signature)
```

`X-Plaidify-Delivery` stays the same across retries of one event: use it to
drop duplicates.

## Core Runtime Modules

| Module | Responsibility |
| --- | --- |
| `src/app.py` | App lifecycle, startup checks, middleware (security headers, HTTPS redirect, body limit), metrics, the `/ui-next` static mount, router registration, maintenance loops |
| `src/routers/` | The API surface above |
| `src/access_jobs.py` | Access-job lifecycle, per-credential locks, heartbeats, reaper, the Redis-stream executor |
| `src/access_job_worker.py` | The executor process (`python -m src.access_job_worker`): runs queued jobs and, in `redis-worker` mode, the background services; serves `/metrics` and `/health` on `ACCESS_WORKER_METRICS_PORT` |
| `src/background_services.py` | Scheduled refresh, webhook outbox and reaper, each under a lease in one process |
| `src/scheduled_refresh.py` | Database-backed refresh schedules |
| `src/session_store.py` | Hosted-link sessions, launch tokens and event fan-out (Redis, or memory in development) |
| `src/core/engine.py`, `src/core/browser_pool.py`, `src/core/step_executor.py` | Connector execution, the Chromium pool, blueprint steps, extraction (selectors and LLM) |
| `src/core/read_only_policy.py`, `src/core/network_policy.py` | The per-phase read-only rules, domain scoping and the private-address block |
| `src/core/mfa_manager.py` | MFA challenges and answers (Redis or memory) |
| `src/database.py`, `src/crypto.py`, `src/kms.py` | SQLAlchemy models, envelope encryption, key rotation, KMS providers |
| `src/audit.py` | The HMAC audit chain: append, prune, seal, verify |
| `src/mcp_server.py` | MCP server over stdio, SSE or streamable HTTP |
| `frontend-next/` | The hosted Link page (React/Vite), built into `frontend-next/dist` and served at `/link` |

## Primary Flows

### Direct Connect and Access Jobs

1. A client calls `POST /connect` (or an SDK `connect()` helper) with the site
   and credentials, optionally RSA-encrypted to a key from
   `POST /encryption/session`.
2. Plaidify starts an access job. Within a moment the call answers
   `connected` with the data, `mfa_required` with a `session_id`, or
   `pending` with a `job_id`.
3. The client answers MFA with `POST /mfa/submit {session_id, code}` and polls
   `GET /access_jobs/{job_id}` until a terminal status: `completed`, `failed`,
   `blocked` (another job holds the same credential), `cancelled` or
   `mfa_timeout`. While a job waits for a code it reports `mfa_required`
   (`mfa_state: "awaiting_code"`); after a rejected code, `metadata.mfa_error`
   and `metadata.attempts_remaining` say so.

A completed job's result is stored encrypted under the owner's key and kept
for `RESULT_RETENTION_DAYS`; a job without an owner keeps no result.

### Hosted Link and Bootstrap Launches

1. Your backend creates a signed one-time launch token with `POST /link/bootstrap`.
2. The client redeems it with `POST /link/sessions/bootstrap` (from an allowed origin, when the token names any).
3. The hosted `/link?token=…` page runs in a browser, an iframe or a native web
   view, and posts lifecycle events to its host ([MOBILE_LINK_INTEGRATION.md](MOBILE_LINK_INTEGRATION.md)).
4. On completion the host receives a one-time `public_token`; your backend
   exchanges it with `POST /exchange/public_token` for a durable access token.

Backends can also create sessions directly with `POST /link/sessions` (body
`{site?, allowed_origins?}`). Anonymous `POST /link/sessions/public` is for
development and, in production, only with `PUBLIC_LINK_SESSIONS_ENABLED=true`
and origin restrictions.

### Agent and MCP Access

Agents can integrate through:

- Raw REST calls to the Plaidify API.
- The Python SDK in `sdk/`.
- The TypeScript SDK in `sdk-js/` (server, browser and React Native).
- The MCP server in `src/mcp_server.py`.

An agent's API key needs a consent grant bound to that agent to read data
through `/fetch_data`. Keep browser execution and credential handling inside
Plaidify; agents work with structured results, consent grants and job status.

## Persistence and State

| Model | Purpose |
| --- | --- |
| `User` | Identity, wrapped data key, admin flag, token version |
| `Link` | Intent to connect a specific site on behalf of a user |
| `AccessToken` | Encrypted credentials, optional scopes and instructions |
| `PublicToken` | One-time exchange token for hosted-link completion |
| `RefreshToken`, `PasswordResetToken` | Hashed refresh and reset tokens |
| `LoginThrottle` | HMAC-keyed sign-in failure counters |
| `ConsentRequest`, `ConsentGrant` | Scoped, time-limited access bound to an agent |
| `ApiKey`, `Agent` | Programmatic identities with scope and site restrictions |
| `AccessJob` | Job tracking, heartbeat, deadline, encrypted result |
| `ScheduledRefreshJob` | Refresh schedules |
| `Webhook`, `WebhookDelivery` | Registrations (encrypted secret) and the delivery outbox (encrypted payloads) |
| `AuditLog`, `AuditChainHead` | The hash-chained audit trail and its signed head |
| `BlueprintRecord` | Registry entries |
| `MaintenanceLease` | Which process runs each maintenance job |

Redis holds the short-lived shared state: link sessions, MFA sessions, RSA
keys, rate-limit counters, locks and leases, and the job queue.

## Configuration and Production Invariants

- `ENCRYPTION_KEY` and `JWT_SECRET_KEY` (at least 32 characters) are required.
- With `ENV=production` startup refuses SQLite, `DEBUG=true`, a missing or
  unreachable Redis and wildcard CORS, and turns HTTPS enforcement on;
  self-registration is off unless enabled explicitly.
- `ACCESS_JOB_EXECUTION_MODE=redis-worker` runs jobs in the separate executor
  (what the production compose stack and the Azure template use).
- `STRICT_READ_ONLY_MODE` (default on) enables the per-phase read-only rules.

Every setting, with its default: [`.env.example`](../.env.example) and
[DEPLOYMENT.md](DEPLOYMENT.md#environment-variables-reference).

## Local Development

```bash
pip install --require-hashes -r requirements-dev.lock
cp .env.example .env    # set ENCRYPTION_KEY and JWT_SECRET_KEY
alembic upgrade head
(cd frontend-next && npm ci && npm run build)
uvicorn src.main:app --reload
```

Tests and client suites: [CONTRIBUTING.md](../CONTRIBUTING.md).

## Related Docs

- [DEPLOYMENT.md](DEPLOYMENT.md) for production deployment and operations
- [AGENTS.md](AGENTS.md) for agent-facing integration patterns
- [MOBILE_LINK_INTEGRATION.md](MOBILE_LINK_INTEGRATION.md) for native hosted-link embedding
- [HOSTED_LINK_TELEMETRY.md](HOSTED_LINK_TELEMETRY.md) for the hosted page's analytics events
- [SCHEDULED_REFRESH.md](SCHEDULED_REFRESH.md) for refresh schedules and their webhooks
- [KMS_INTEGRATION.md](KMS_INTEGRATION.md) for managed key providers
- [ISOLATED_ACCESS_RUNTIME.md](ISOLATED_ACCESS_RUNTIME.md) for executor isolation design
- [RUNBOOK.md](RUNBOOK.md) for operational procedures
- [DISASTER_RECOVERY.md](DISASTER_RECOVERY.md) for backup, restore, and failover
- [HIGH_AVAILABILITY.md](HIGH_AVAILABILITY.md) for HA and multi-region topology
- [LOAD_TESTING.md](LOAD_TESTING.md) for load testing and capacity planning
- [COMPLIANCE.md](COMPLIANCE.md) for the SOC 2 / ISO 27001 controls matrix
- [THREAT_MODEL.md](THREAT_MODEL.md) for the STRIDE threat model
- [PRODUCT_PLAN.md](PRODUCT_PLAN.md) for roadmap context
