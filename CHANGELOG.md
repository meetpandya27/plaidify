# Changelog

All notable changes to Plaidify will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
No version has been tagged or published yet; the version string in the code
and the SDKs is `0.3.0b1` / `0.3.0-beta.1`.

---

## [Unreleased]

A full audit (September 2026) and the fixes that followed. Nothing had been
deployed or published, so breaking changes were made wherever they closed a
hole; they are all listed below. Every client must sign in again after
upgrading.

### Breaking changes

**Authentication and tokens**
- API keys (`pk_…`, agents `pk_agent_…`) are accepted only in the `X-API-Key` header; user JWTs only as `Authorization: Bearer`.
- Access tokens carry `typ=access`, `aud=plaidify:api` and a token version `tv`; hosted-link launch tokens are signed with `LINK_LAUNCH_SECRET` (or a key derived from `JWT_SECRET_KEY`) for `aud=plaidify:link-launch` and can no longer be used as a login. Existing tokens stop working.
- Refresh tokens are stored as hashes (downgrading past that migration deletes them all); presenting a rotated refresh token again revokes all of that user's refresh tokens.
- Password reset, `POST /auth/sessions/revoke-all` and deactivating an account end every session at once.
- Startup fails when `JWT_SECRET_KEY` is shorter than 32 characters, when `LINK_LAUNCH_SECRET` is short or equal to it, or when `OAUTH_ENABLED` is set without the enabled providers' client ids (and the GitHub client secret).
- Self-registration is off in production unless `REGISTRATION_ENABLED` is set explicitly. The bootstrap administrator is only ever created, never promoted from an existing account; a clash stops startup in production.
- Sign-ups prove their email address first, by default in production (`REGISTRATION_EMAIL_VERIFICATION`): `POST /auth/register` answers `202 {"status": "verification_sent", "detail": …}` whether or not the username or address is taken and emails the address a one-time token (or a note that the address has an account or the username is taken); the new `POST /auth/verify-email {token}` creates the account and returns its tokens (`400` for an unknown, used or expired token, `409` when the name was taken meanwhile). `EMAIL_VERIFICATION_URL` makes the email a link to your page. Production refuses to start with registration enabled, verification on and no `SMTP_HOST`/`SMTP_FROM`. Elsewhere sign-ups stay instant unless the setting is on, and `/auth/verify-email` answers `404`.

**API contract**
- Secrets travel only in JSON bodies: `POST /mfa/submit {session_id, code}`, `POST /submit_credentials {link_token, username?, password?, encrypted_username?, encrypted_password?}`, `POST /fetch_data {access_token, consent_token?}` and `POST /submit_instructions`. The query-string forms and `GET /fetch_data` (now 405) are gone.
- `POST /connect` requires an API key, an access token, or the `link_token` of a live hosted-link session (connectable state, same site, and an agent-created session keeps the agent's site limits). The caller owns a one-shot connect's job; its result is stored encrypted and readable at `GET /access_jobs/{job_id}`. Jobs without an owner return `result: null`.
- `POST /disconnect` requires authentication and a JSON body `{link_token}`.
- An agent's API key needs a consent grant bound to that agent to call `POST /fetch_data`; a grant's scopes limit the fields returned.
- `POST /api-keys`: `scopes` is a JSON list (`"field"` or `"read:field"`), `expires_days` is 1–3650, unknown fields are rejected (422). Agents take `allowed_sites` / `allowed_scopes` lists; `[]` means none, a bare string is rejected. An empty or unreadable stored list denies everything.
- List endpoints paginate with `limit` / `offset`: `/links`, `/tokens`, `/agents`, `/registry/search`, `/api-keys`.
- `POST /blueprints/generate` is administrator-only and returns a draft; `save: true` writes it keyed by the URL's hostname, and it runs as an untrusted blueprint.
- `GET /organizations/search` returns only organizations backed by a real connector unless `include_unsupported=true`.
- `GET /audit/verify` is administrator-only. `GET /refresh/jobs` lists only the caller's schedules, with tokens masked; administrators use `GET /refresh/admin/jobs`.
- Access jobs: new terminal status `mfa_timeout`; an unattended (scheduled) refresh that meets MFA ends as `failed` with `error_code: "mfa_required"` and disables its schedule (`needs_reauth`). Responses add `error_code`, `mfa_state` (`awaiting_code` / `verifying`) and `mfa_attempts`; after a rejected code `metadata.mfa_error = "invalid_code"` and `metadata.attempts_remaining`. New `POST /access_jobs/{job_id}/cancel`.
- Webhooks: registering one requires owning the link or session; destinations must be public `https://` URLs in production; deliveries go through a durable outbox with retries. Each request carries `X-Plaidify-Delivery`, `X-Plaidify-Timestamp` and `X-Plaidify-Signature: sha256=<hex HMAC-SHA256 of "{timestamp}." + raw body>`, and the body gains `delivery_id` and `webhook_id`. `REFRESH_FAILED` gains `reason` (`needs_reauth` or `max_failures`).
- Hosted link: `POST /link/sessions` takes an optional body `{site?, allowed_origins?}`; `GET /link/sessions/{token}/status` returns `allowed_origins`; `/link` rejects a URL with more than one `token` (400). The legacy static page (`/ui`, `frontend/`) is gone.
- Timestamps in responses are timezone-aware (`+00:00`).
- In production `/docs`, `/redoc` and `/openapi.json` are off unless `DOCS_ENABLED=true`, and `GET /health/detailed` answers 404 without `HEALTH_CHECK_TOKEN`. `GET /metrics` requires `METRICS_TOKEN` when it is set.

**Hosted page and SDKs**
- The hosted page never shows the public token (it goes to the host app only), sends `EXIT` only on an explicit exit or page teardown, and sends telemetry as `{"event": "TELEMETRY", "name": "step_view", …}` (the field was `event`). Theme parameters are `accent`, `bg`, `radius` and `logo` (a `data:` URI up to 32 KB); `?server=` works only in development builds.
- JavaScript SDK: `login(username, password)` posts the OAuth2 form to `/auth/token`; `register` sends the username; `registerWebhook` posts to `/webhooks/register` with a secret; `createApiKey` takes a scope list; list methods return arrays; consent ids are strings.
- Python SDK: `register()` returns `RegistrationPending` when the server answers `202`, and `verify_email(token)` finishes the sign-up; JavaScript SDK: `register()` resolves to `AuthToken | RegistrationPending`, and `verifyEmail(token)`.
- Python SDK: `create_api_key(name, scopes=[…], expires_days=…)`; the CLI dropped `-p` (it prompts, or reads `--password-stdin`). `plaidify audit verify` / `audit logs` take a user's access token and refuse API keys up front; `plaidify login` prints one. JS `listRefreshJobs()` returns `{ jobs: RefreshJobInfo[] }` with masked tokens.
- Swift and Android: the hosted web view is the default; the native Link screens run only with `experimentalNativeScreens`. New native session types; the Android bridge is `window.plaidifyLink.postMessage` and results come back through the Activity result.

**Blueprints**
- `schema_version` is required and unknown keys are rejected. The `extract` step action is removed; `iframe` runs its nested steps. New `auth.success` / `auth.failure`, `auth.submit_targets`, `mfa.submit_targets`, `logout_targets` and `allowed_domains`.
- `execute_js` steps run only for trusted connectors (bundled, or listed in `ENGINE_TRUSTED_CONNECTORS`).
- Number transforms return `null` instead of `0.0` for unparseable input.
- `connectors/template_connector.py` is now `template_connector.py.example`.

**Operations**
- Python 3.10 is no longer supported (3.11–3.13). Install with `pip install --require-hashes -r requirements-dev.lock`.
- OTLP endpoints without a scheme use TLS. Development compose ports bind to `127.0.0.1`, and development Redis has a password (`.secrets/redis_password`).
- gunicorn no longer preloads the app; application code in the image is read-only.

### Security
- The read-only policy covers every phase: login and MFA may only submit to their declared targets, cleanup only to logout targets, navigation stays on the blueprint's domains, and private, loopback, link-local, CGNAT and cloud-metadata addresses are refused on every browser request. Service workers are blocked; TLS certificate checks are on; Chromium keeps its sandbox (the compose files apply `deploy/seccomp/chromium.json`).
- No secrets in URLs; access logs redact secret query parameters and token-bearing path segments; typed values are scrubbed from browser error text; tokens are logged as fingerprints.
- Per-user envelope encryption now also covers stored job results and webhook secrets and payloads. Key rotation re-wraps every user key (`plaidify rotate-key --re-encrypt`) with an `ENCRYPTION_KEY_PREVIOUS` fallback, and the KMS migration moves every secret and fails loudly on anything it skips.
- The audit chain is an HMAC-SHA256 chain keyed by `AUDIT_HMAC_KEY` (or derived from `ENCRYPTION_KEY`), with serialized appends, a signed head row, checkpoints when pruning, key-rotation seals and streamed verification.
- Registration no longer reveals whether a username or address is taken when sign-ups are email-verified (the production default); only the mailbox learns it.
- Sign-in throttling per username and address and per username (the throttle table holds only HMACs), OAuth tokens checked against this app's client ids, agent rate limits enforced, `RATE_LIMIT_DEFAULT` applied to every endpoint without its own limit (CORS wraps it, so browsers can read the 429), failed sign-ins audited without the typed username, request bodies capped for chunked uploads too, passwords over 72 bytes refused instead of truncated, and bad input answered with 4xx instead of 500.
- The MCP server's HTTP transports bind `127.0.0.1` by default and check the `Host` header.

### Reliability
- Redis-worker mode tolerates restarts: heartbeats, conditional claims, a graceful SIGTERM drain, a reaper for orphaned or overdue jobs, and per-credential locks that fail closed without Redis in production.
- Scheduled refresh runs from the database under a lease in one process; the webhook outbox and the maintenance jobs run under leases too (`src/background_services.py`).
- The executor serves `/metrics` and `/health` on `ACCESS_WORKER_METRICS_PORT` (9101); metrics use Prometheus multiprocess mode, so one scrape covers every gunicorn worker.
- The hosted page's MFA, "Try again" and event delivery work; the live-events stream no longer blocks a worker; browser crashes no longer jam the pool; wrong passwords and MFA rejections are detected.
- The Anthropic provider calls the Messages API through the official `anthropic` SDK (1.9.0) instead of raw HTTP, streaming each reply. Requests, errors, retries and server-side fallbacks behave as before; a stream that breaks or ends early counts as a failed call; the SDK's DEBUG logging, which includes request bodies (page content), stays off.

### Ops and CI
- CI: lint of the whole repository, a lock-drift check with `pip-audit` and `npm audit`, tests on Python 3.11–3.13 with Redis, a Playwright job (hosted-link E2E, engine browser tests, the demo), a PostgreSQL migrations job, a container smoke test, client jobs (hosted page, JavaScript, Python, Swift, Android SDKs), configuration checks, CodeQL (Python, JavaScript/TypeScript, Actions) and a weekly dependency audit. Actions are pinned to commit SHAs.
- Hash-locked requirement files for the app, development and KMS; the image installs them with `--require-hashes`, runs as a non-root user and has an allow-listed build context.
- Production compose: nginx in front, a separate executor, migrations as a one-shot service, Redis with a password and persistence, an opt-in encrypted backup service. The Azure template puts PostgreSQL and Redis on a private network, connects as a least-privilege role and deploys only CI-green `main` after migrating; its workflow generates and keeps `AUDIT_HMAC_KEY`, `LINK_LAUNCH_SECRET` and `METRICS_TOKEN` in Key Vault and passes optional SMTP and bootstrap-administrator settings.
- Monitoring: alert rules that match the exported metrics, with `promtool` tests; a dashboard that shows data.

### Docs
- Every setting is documented in `.env.example` and `docs/DEPLOYMENT.md`; the README, security, threat-model, compliance, operations and SDK docs describe what the code does now, including its known limits. The owner's pre-launch checklist is in `docs/RUNBOOK.md`.

---

## 2026-03-15 to 2026-06-23 — work never recorded here

These changes landed on `main` between 0.3.0-alpha.1 and the audit without
changelog entries or a release. Summarised from the git history:

- **March 15–20** — LLM extraction: DOM simplification, OpenAI / Anthropic providers, structured prompts, a selector cache, blueprint schema v3 and a multimodal fallback (#1–#7). Agent integration: the hosted Link page, webhooks, the event stream, SDK helpers and the MCP server; public-token exchange; the blueprint registry (#22), consent engine (#24), access-token scoping (#25) and the audit hash chain (#27). Hydro One connector.
- **April 15** — Phase 5: modular routers, the JavaScript SDK, KMS envelope encryption, scheduled refresh, API keys and agents, Locust load tests, gunicorn configuration.
- **April 19–25** — Beta preparation (`0.3.0b1`, production compose, Azure deployment). Hosted link: event payloads stripped of tokens (#62), multi-origin framing (#63), reliable lifecycle events (#64); the React rewrite in `frontend-next/` became the default page (#69–#74) with a design system, institution branding, schema-driven forms, an error taxonomy, accessibility work, i18n and dark mode, loading states and UX telemetry (#75–#82). Native Swift (#83) and Android (#84) SDKs; refresh presets, `PATCH`, `create_link` binding and `REFRESH_FAILED` (#85); pluggable KMS providers (#86); ORM and migration alignment (#87).
- **May 3** — `docs/SELF_HOST.md` (#100).
- **June 23** — The runnable multi-site sandbox demo (#116); dependency updates for 16 advisories (#117); observability, security and operations hardening (#118); circuit breakers and retries (#119); OpenTelemetry tracing (#120); admin RBAC and session management (#121); account deletion, KMS health check and the DR runbook (#122); OAuth2 social login (#123); the monitoring stack and scheduled backups (#124); KMS provider tests (#125); HA parameters and the HA and load-testing guides (#126); the compliance matrix and threat model (#127); README (#128, #129).

---

## [0.3.0-alpha.1] — 2026-03-15

### Phase 2 (Week 1): Python SDK & CLI + Security Hardening

The first deliverable of Phase 2 — a pip-installable Python SDK and CLI, plus a complete security hardening pass closing 7 issues (#8–#14).

### Added

- **Python SDK** (`sdk/plaidify/`) — PyPI-ready package (`pip install plaidify`)
  - `Plaidify` async client — wraps all API endpoints with typed return values
  - `PlaidifySync` synchronous client — blocking wrapper for non-async code
  - `connect()` one-call method — connect + extract in a single call with optional `mfa_handler` callback
  - **MFA auto-handling** — pass an async `mfa_handler(challenge) -> code` callback and MFA is resolved inline
  - Link flow methods — `create_link()`, `submit_credentials()`, `fetch_data()` (Plaid-style multi-step)
  - Auth methods — `register()`, `login()`, `me()` with auto JWT persistence
  - Blueprint discovery — `list_blueprints()`, `get_blueprint(site)`
  - Link/token management — `list_links()`, `delete_link()`, `list_tokens()`, `delete_token()`
  - Full type annotations + `py.typed` marker for IDE autocomplete
  - Typed models: `ConnectResult`, `BlueprintInfo`, `MFAChallenge`, `LinkResult`, `AuthToken`, `UserProfile`, `HealthStatus`
  - Exception hierarchy: `PlaidifyError` → `ConnectionError`, `AuthenticationError`, `MFARequiredError`, `BlueprintNotFoundError`, `ServerError`, `RateLimitedError`, `InvalidTokenError`
  - HTTP error → exception mapping (401→InvalidToken, 404→BlueprintNotFound, 429→RateLimited, 502→Connection, 5xx→Server)
  - Configurable: `server_url`, `api_key`, `timeout`, `max_retries`, custom headers
  - `PLAIDIFY_SERVER_URL` and `PLAIDIFY_API_KEY` env var support
- **CLI tool** (`plaidify` command) — Click-based command-line interface
  - `plaidify connect <site> -u <user> -p <pass>` — test a blueprint from the terminal with formatted output
  - `plaidify blueprint list` — list all available blueprints on the server
  - `plaidify blueprint info <site>` — show detailed blueprint metadata
  - `plaidify blueprint validate <file>` — validate a JSON blueprint against the V2 schema (12 actions, 10 field types, selectors)
  - `plaidify blueprint test <file> -u <user> -p <pass>` — run a blueprint against a live site and display results
  - `plaidify serve` — start the Plaidify API server (replaces `uvicorn` command)
  - `plaidify demo` — start both servers + auto-open browser (replaces `python run_demo.py`)
  - `plaidify health` — check server health
  - Interactive MFA prompt in CLI connect flow
  - `--json-output` flag for machine-readable output
- **SDK test suite** — 82 tests covering client, sync client, models, exceptions, CLI blueprint validation
  - Mock HTTP with `respx` for fast, deterministic client tests
  - Full CLI validation tests (valid, missing fields, unknown actions, invalid JSON, V1 compat)
- **SDK packaging** (`sdk/pyproject.toml`) — hatchling build, `[project.scripts]` entry point, PyPI metadata
- **Rate limiting** (#8) — slowapi-based rate limiting on auth (5/min) and connect (10/min) endpoints
- **CORS enforcement** (#9) — default origins restricted to localhost; wildcard blocked in production
- **Security headers** (#10) — X-Content-Type-Options, X-Frame-Options, X-XSS-Protection, Referrer-Policy, Permissions-Policy, HSTS in production
- **JWT refresh tokens** (#11) — 15-minute access tokens + 7-day refresh tokens with single-use rotation via `POST /auth/refresh`
- **Client-side RSA encryption** (#12) — ephemeral RSA-2048 keypairs for in-transit credential encryption (WebCrypto + Python SDK)
- **Envelope encryption** (#13) — per-user AES-256-GCM Data Encryption Keys (DEKs) wrapped by master key; lazy migration for existing users
- **Key rotation with versioning** (#14) — `key_version` column on AccessToken, `unwrap_dek` fallback to previous master key, `re_encrypt_tokens()` background job, CLI `plaidify rotate-key` command
- **Alembic migration** — `key_version` column on `access_tokens` table

### Changed

- **Updated `requirements.txt`** — added `click>=8.0.0`, `respx>=0.21.0`, `slowapi>=0.1.9`
- **Encryption upgraded** — from Fernet (AES-128-CBC) to AES-256-GCM with per-user DEK envelope encryption
- **JWT access token lifetime** — reduced from 1 week to 15 minutes (refresh tokens handle renewal)

---

## [0.2.0] — 2026-03-14

### Phase 1: Real Browser Engine

Replaces the stub engine with a real Playwright-powered browser automation layer. Plaidify can now actually log into websites, navigate multi-step auth flows, handle MFA, and extract structured data.

### Added

- **Playwright integration** — real browser automation replaces stub logic; Chromium launched via async Playwright API
- **Browser Pool Manager** (`src/core/browser_pool.py`) — pool of reusable browser contexts with configurable max concurrency, idle timeout cleanup, session isolation, resource blocking (images/fonts/analytics), stealth mode (randomized viewport, user-agent)
- **Blueprint V2 Schema** (`src/core/blueprint.py`) — Pydantic models for the complete blueprint format:
  - 12 step actions: `goto`, `fill`, `click`, `wait`, `screenshot`, `extract`, `conditional`, `scroll`, `select`, `iframe`, `wait_for_navigation`, `execute_js`
  - Typed extraction fields: `text`, `currency`, `date`, `number`, `email`, `phone`, `list`, `table`, `boolean`
  - MFA configuration: detection, OTP, email code, security questions, push notification
  - Rate limiting and health check metadata
  - Automatic V1→V2 conversion for backward compatibility
- **Step Executor** (`src/core/step_executor.py`) — interprets blueprint steps, drives Playwright with `{{variable}}` interpolation, conditional branching, and per-step timeouts
- **Data Extractor** (`src/core/data_extractor.py`) — extracts and normalizes data from pages:
  - 10 built-in transforms: `strip_whitespace`, `strip_dollar_sign`, `strip_commas`, `to_lowercase`, `to_uppercase`, `to_number`, `to_currency`, `parse_date`, `regex_extract`
  - Parameterized transforms: `parse_date(%m/%d/%Y)`, `regex_extract(\d+)`
  - Type coercion for all field types
  - List/table extraction with row iteration
  - Pagination support (next-page clicking)
  - Sensitive field handling (never logged)
- **MFA Session Manager** (`src/core/mfa_manager.py`) — async MFA challenge handling:
  - Engine pauses when MFA detected, waits for user input via API
  - Auto-expiring sessions (configurable TTL, default 5 min)
  - Push MFA polling support
- **MFA API endpoints** — `POST /mfa/submit` to submit OTP codes, `GET /mfa/status/{session_id}` to check session state
- **Blueprint discovery endpoints** — `GET /blueprints` lists all available blueprints, `GET /blueprints/{site}` returns detailed info (fields, MFA support, tags)
- **Test Bank blueprint** (`connectors/test_bank.json`) — full V2 blueprint for the example test site with account data, transactions, MFA
- **Enhanced example test site** (`example_site/server.py`) — realistic test site with login, MFA (OTP), dashboard with account balance, transactions table, profile data, and logout
- **Browser engine config** — `BROWSER_HEADLESS`, `BROWSER_POOL_SIZE`, `BROWSER_IDLE_TIMEOUT`, `BROWSER_NAVIGATION_TIMEOUT`, `BROWSER_ACTION_TIMEOUT`, `BROWSER_BLOCK_RESOURCES`, `BROWSER_STEALTH` env vars
- **Phase 1 test suite** — blueprint schema tests, data extractor/transform tests, MFA manager tests, browser pool tests, Playwright integration tests, API endpoint tests

### Changed

- **Rewrote `src/core/engine.py`** — Playwright-powered execution: load blueprint → acquire browser → run auth steps → detect MFA → extract data → cleanup → release browser
- **Updated `ConnectRequest`/`ConnectResponse` models** — added `extract_fields`, `session_id`, `mfa_type`, `metadata` fields
- **Updated `src/models.py`** — added `MFASubmitRequest`, `MFAStatusResponse`, `BlueprintInfoResponse` models
- **Updated `src/main.py`** — browser pool lifecycle (start/stop), MFA error handling in `/connect`, new endpoints
- **Updated `requirements.txt`** — added `playwright>=1.40.0`
- **Updated `Dockerfile`** — installs Playwright system deps and Chromium browser
- **Updated `src/core/__init__.py`** — module docstring
- **Bumped version** to `0.2.0`

---

## [0.1.0] — 2026-03-14

### Phase 0: Foundation Hardening

The first production-quality release of Plaidify's core infrastructure. This release focuses entirely on making the existing codebase secure, tested, and maintainable — no new user-facing features beyond what was already present.

### Added

- **Pydantic Settings** (`src/config.py`) — all configuration via environment variables with validation and fail-fast on missing secrets
- **Custom exception hierarchy** (`src/exceptions.py`) — `PlaidifyError` base class with `BlueprintNotFoundError`, `ConnectionFailedError`, `AuthenticationError`, `MFARequiredError`, `InvalidTokenError`, and more
- **Global exception handler** — all `PlaidifyError` subclasses return structured JSON
- **Structured logging** (`src/logging_config.py`) — JSON format for production, colored text for development, extra data fields support
- **Health check endpoint** (`GET /health`) — reports database connectivity, app version, overall system status
- **Alembic migrations** — initial migration for users, links, access_tokens tables with proper foreign keys, indexes, and timestamps
- **CI pipeline** (`.github/workflows/ci.yml`) — lint (ruff), test (Python 3.9–3.12), security audit (pip-audit), Docker build
- **53 tests at 80% coverage** — auth (register, login, profile, OAuth2), link flow (full lifecycle, instructions, CRUD), user isolation, system endpoints, encryption, exceptions, config
- **Test fixtures** (`tests/conftest.py`) — shared DB setup/teardown, authenticated client helpers
- **`.env.example`** — documented template for all required and optional environment variables
- **`pyproject.toml`** — ruff, pytest, and coverage configuration
- **Multi-stage Dockerfile** — builder + runtime stages, non-root user, container health check
- **CORS middleware** — configurable allowed origins
- **Product plan** (`docs/PRODUCT_PLAN.md`) — full 56-week, 5-phase roadmap

### Changed

- **Removed all hardcoded secrets** — `ENCRYPTION_KEY` and `JWT_SECRET_KEY` are now required env vars (no defaults)
- **Updated `requirements.txt`** — pinned versions, added pydantic-settings, alembic, passlib, PyJWT, email-validator, ruff, pytest-cov
- **Rewrote `src/main.py`** — lifespan context manager (replaced deprecated `on_event`), removed dead code, added type hints and docstrings
- **Rewrote `src/database.py`** — modern SQLAlchemy DeclarativeBase, `created_at` timestamps, `get_db()` dependency, renamed encrypt/decrypt functions
- **Rewrote `src/core/engine.py`** — structured logging, uses custom exceptions, proper error handling for blueprint loading
- **Updated `src/models.py`** — Optional fields for OAuth2 users, password min length (8 chars), Field descriptions
- **Updated `docker-compose.yml`** — uses `.env` file, text logging for dev
- **Updated `.gitignore`** — added `.db`, `.coverage`, `htmlcov/`, protected `.env.example`

### Fixed

- Unreachable `return response_data` after `raise HTTPException` in `/connect`
- Duplicate `from fastapi import HTTPException` imports inside functions
- `UserProfileResponse` failing for OAuth2 users with `None` username/email

---

## [0.0.1] — 2025-04-17

### Initial Release

- FastAPI server with `/connect`, `/status`, `/disconnect` endpoints
- JSON blueprint system for site-specific login flows
- Link Token flow: `create_link` → `submit_credentials` → `fetch_data`
- User authentication (register, login, JWT)
- SQLite database with Fernet credential encryption
- Python connector plugin system (`BaseConnector`)
- OAuth2 login placeholder
- Docker + docker-compose support
- Basic test suite
- Frontend UI stub
