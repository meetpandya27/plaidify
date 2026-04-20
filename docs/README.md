# Plaidify — Technical Documentation

> For the product overview and quick start, see the [main README](../README.md).
> For agent-facing integration patterns, see [AGENTS.md](AGENTS.md).

## System Overview

Plaidify is a FastAPI control plane for authenticated web access. The live HTTP bootstrap lives in `src/app.py`; `src/main.py` re-exports the ASGI app for compatibility with existing tooling. Routers in `src/routers/` own the public API surface, while execution, shared state, SDKs, and hosted-link assets live in dedicated modules.

```text
Client, backend, mobile shell, or agent
  -> JWT or API key auth context
  -> direct connect or hosted link session
  -> access-job orchestration and MFA state
  -> connector or blueprint execution
  -> structured result, public token, or audit event
```

## Request Surfaces

| Surface | Primary code | What it covers |
| --- | --- | --- |
| System and health | `src/routers/system.py`, `src/app.py` | Root endpoints, health and status, optional `/metrics`, static `/ui` mount |
| Auth and identities | `src/routers/auth.py`, `src/routers/refresh.py`, `src/routers/api_keys.py`, `src/routers/agents.py` | JWT login and refresh, API keys, agent identities |
| Direct connect and polling | `src/routers/connection.py`, `src/routers/access_jobs.py` | `POST /connect`, detached execution, `GET /access_jobs`, MFA continuation |
| Hosted link | `src/routers/link_sessions.py`, `src/routers/links.py`, `frontend/` | `/link`, hosted session creation, signed bootstrap launch tokens, SSE event delivery |
| Consent and audit | `src/routers/consent.py`, `src/routers/audit.py` | Scoped data grants and tamper-evident audit logging |
| Registry and webhooks | `src/routers/registry.py`, `src/routers/webhooks.py` | Blueprint discovery and publication, link lifecycle delivery |

## Core Runtime Modules

| Module | Responsibility |
| --- | --- |
| `src/app.py` | App lifecycle, middleware, exception handling, metrics, static mounts, router registration |
| `src/main.py` | Compatibility entrypoint that exports `app` and `settings` |
| `src/access_jobs.py` | Access-job orchestration, lifecycle tracking, result serialization |
| `src/access_job_worker.py` | Redis-backed detached worker execution for production deployments |
| `src/session_store.py` | Shared session state for hosted link and MFA flows |
| `src/core/engine.py` and `src/core/browser_pool.py` | Connector execution, browser runtime, extraction pipeline |
| `src/database.py` | SQLAlchemy models, encryption helpers, DB session management |
| `src/models.py` | Pydantic request and response models |
| `src/mcp_server.py` | MCP server that exposes Plaidify tools over stdio or SSE |
| `frontend/` | Hosted link and embedded UI assets served by the API |

## Primary Flows

### Direct Connect and Access Jobs

1. A client calls `POST /connect` directly or uses an SDK `connect()` helper.
2. Plaidify executes the site workflow immediately or dispatches a detached access job.
3. The response can complete fast with `connected`, pause with `mfa_required`, or return `pending` with a `job_id`.
4. Clients resume MFA with `POST /mfa/submit` and poll `GET /access_jobs/{job_id}` until a persisted result is available.

Completed access jobs store `result_json`, so callers can recover final extracted data without re-running the site flow.

### Hosted Link and Bootstrap Launches

1. Authenticated backends create a signed one-time launch token with `POST /link/bootstrap`.
2. A public client redeems that token through `POST /link/sessions/bootstrap`.
3. The hosted `/link` page runs inside a browser, iframe, or native webview and emits lifecycle events to parent shells.
4. On completion, clients can consume a short-lived `public_token` or continue through standard access-token flows.

Plaidify also supports authenticated `POST /link/sessions` and controlled anonymous `POST /link/sessions/public` creation. Public session creation should stay origin-restricted in production.

### Agent and MCP Access

Agents can integrate at four levels:

- Raw REST calls to the Plaidify API.
- The Python SDK in `sdk/plaidify`.
- The TypeScript SDK in `sdk-js/` for hosted-link browser and mobile flows.
- The MCP server in `src/mcp_server.py`, available over stdio or SSE.

The recommended boundary is to keep browser execution and credential handling inside Plaidify while agents operate on structured results, consent grants, and job status.

## Persistence and State

The data model is broader than the original link-only flow. Important persisted records include:

| Model | Purpose |
| --- | --- |
| `User` | End-user identity, wrapped DEK, account status |
| `Link` | Intent to connect a specific site on behalf of a user |
| `AccessToken` | Encrypted credentials, optional extraction scopes, ongoing access token state |
| `PublicToken` | One-time exchange token for hosted-link completion |
| `RefreshToken` | JWT refresh token rotation state |
| `ConsentRequest` and `ConsentGrant` | Scoped, time-limited access for agent or delegated use cases |
| `ApiKey` and `Agent` | Programmatic identities with scope and site restrictions |
| `AccessJob` | Detached execution tracking, MFA state, result persistence |
| `AuditLog` | Hash-chained audit trail entries |
| `Webhook` | Link-session event delivery registration |
| `ScheduledRefreshJob` | Persisted background refresh scheduling |

## Configuration and Production Invariants

- `ENCRYPTION_KEY` and `JWT_SECRET_KEY` are required; the app will not start without them.
- Production startup fails fast if `DEBUG=true`, if Redis is missing or unreachable, or if wildcard CORS is configured.
- `PUBLIC_LINK_SESSIONS_ENABLED` and `PUBLIC_LINK_ALLOWED_ORIGINS` govern anonymous hosted-link sessions.
- `ACCESS_JOB_EXECUTION_MODE=redis-worker` is the intended production mode for detached job durability across web-process restarts.
- `STRICT_READ_ONLY_MODE` keeps post-auth browser execution in a constrained read-oriented mode.

For the full environment-variable reference and deployment checklist, see [DEPLOYMENT.md](DEPLOYMENT.md).

## Local Development

```bash
cp .env.example .env
alembic upgrade head
uvicorn src.main:app --reload
```

Common validation commands:

```bash
pytest tests/ -q
cd sdk-js && npm run typecheck && npm test
```

## Related Docs

- [DEPLOYMENT.md](DEPLOYMENT.md) for production deployment and operations
- [AGENTS.md](AGENTS.md) for agent-facing integration patterns
- [MOBILE_LINK_INTEGRATION.md](MOBILE_LINK_INTEGRATION.md) for native hosted-link embedding
- [ISOLATED_ACCESS_RUNTIME.md](ISOLATED_ACCESS_RUNTIME.md) for executor isolation design
- [RUNBOOK.md](RUNBOOK.md) for operational procedures
- [PRODUCT_PLAN.md](PRODUCT_PLAN.md) for roadmap context
