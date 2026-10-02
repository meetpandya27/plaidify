# Security Policy

## Reporting Vulnerabilities

If you discover a security vulnerability in Plaidify, please report it responsibly:

1. **Do NOT** open a public GitHub issue.
2. Email **security@plaidify.dev** with:
   - Description of the vulnerability
   - Steps to reproduce
   - Potential impact assessment
3. You will receive acknowledgment within **48 hours**.
4. We aim to provide a fix within **7 business days** for critical issues.

## Supported Versions

No version of Plaidify has been released yet: there are no tags, and neither
SDK is published. Security fixes land on `main`; run a recent commit of it.
This table will list supported versions once releases exist.

## Security Architecture

### Encryption

| What | Algorithm | Key | Notes |
|------|-----------|-----|-------|
| Stored site credentials | AES-256-GCM | the owner's 256-bit data key (DEK) | Per-user key isolation |
| Stored access-job results, webhook secrets and queued webhook payloads | AES-256-GCM | the owner's DEK | A job without an owner stores no result |
| Credentials in queued job payloads (Redis) | AES-256-GCM | `ENCRYPTION_KEY` | Payloads expire after `ACCESS_JOB_PAYLOAD_TTL` |
| Key wrapping (DEKs) | AES-256-GCM, or the KMS's own wrap | `ENCRYPTION_KEY` (`KMS_PROVIDER=local`) or AWS KMS / Azure Key Vault / Vault Transit | [KMS_INTEGRATION.md](docs/KMS_INTEGRATION.md) |
| Credentials in transit from the hosted page, the Python SDK and the native screens | RSA-2048 OAEP (SHA-256) | one-time key per attempt | The private key is destroyed after one decryption; 10-minute TTL. The JavaScript SDK's `connect` sends plain JSON over TLS. |
| User passwords | bcrypt | 12 rounds, `2b` | Passwords over 72 bytes are refused, not truncated |
| Access tokens | JWT, `HS256` by default | `JWT_SECRET_KEY` (≥ 32 characters) | `typ=access`, `aud=plaidify:api`, 15-minute lifetime |
| Hosted-link launch tokens | JWT `HS256` | `LINK_LAUNCH_SECRET`, or an HKDF derivation of `JWT_SECRET_KEY` | `aud=plaidify:link-launch`, one-time, 5-minute lifetime |
| Refresh tokens, API keys | random values stored as SHA-256 digests | — | The raw value is shown once |
| Audit chain | HMAC-SHA256 | `AUDIT_HMAC_KEY`, or an HKDF derivation of `ENCRYPTION_KEY` | See [Audit Logging](#audit-logging) |
| Webhook signatures | HMAC-SHA256 | the webhook's own secret | `sha256=` + hex of `"{timestamp}." + raw body` |

### Key Hierarchy

```
Master key (ENCRYPTION_KEY, or a managed KMS key)
  └── wraps each user's DEK
        └── encrypts that user's credentials, job results, webhook secrets and payloads
```

### Authentication

1. **Registration**: `POST /auth/register` (off in production unless
   `REGISTRATION_ENABLED` is set). Passwords need upper and lower case, a digit
   and a special character; bcrypt-hashed. With
   `REGISTRATION_EMAIL_VERIFICATION` (on by default in production) every
   sign-up gets the same `202`, in the same time, whether or not the username
   or address is taken; only the address learns which, by email: a one-time
   token (24 hours, stored as a SHA-256 digest; a new sign-up for the address
   replaces it), or a note that the address already has an account or that the
   username is taken. `POST /auth/verify-email` with the token and the password
   chosen at registration creates the account and marks the address verified.
   The token alone does not: opening a sign-up you did not start does nothing,
   because that password is not in the email (the email names the username so
   the sign-up can be recognised). With it off the account is created at once,
   and a taken username or address answers `400`.
2. **Sign-in**: `POST /auth/token` (OAuth2 password form) returns an access
   token and a refresh token. Failed attempts are counted per username and
   client address (5 in 15 minutes locks that pair for 15 minutes) and per
   username (20 locks the username); the answer is `423` with `Retry-After`.
   The throttle table stores only HMACs, and unknown usernames are counted
   too, so a lock does not reveal whether an account exists.
3. **API access**: `Authorization: Bearer <access token>` or
   `X-API-Key: pk_…`. API keys never travel as bearer tokens. An access token
   carries the user's token version: a password reset, "sign out everywhere"
   (`POST /auth/sessions/revoke-all`) or deactivation ends every one at once.
   Deactivation also revokes the user's API keys.
4. **Refresh**: `POST /auth/refresh` rotates the refresh token (one
   conditional update, so concurrent reuse cannot mint two). Presenting a
   token that was already rotated revokes every refresh token of that user;
   access tokens already issued run out within their 15 minutes.
5. **OAuth2** (`POST /auth/oauth2`, off by default): Google tokens are checked
   against `OAUTH_GOOGLE_CLIENT_ID`, GitHub tokens against the configured
   GitHub app; startup fails if an enabled provider lacks them. Only a
   verified email links to an existing account.

### Runtime Safety Model

The browser works under `src/core/read_only_policy.py` and
`src/core/network_policy.py`. In every phase of a run:

- **Domain scoping.** Navigation stays on http(s) within the blueprint's
  `domain` (and subdomains) plus its `allowed_domains`.
- **Address policy.** Requests to private, loopback, link-local, CGNAT,
  multicast, reserved and cloud-metadata addresses are refused. The hostname
  is resolved again for every request, so redirects, sub-resources and
  page-initiated fetches are covered. Untrusted blueprints are always held to
  it; `BROWSER_BLOCK_PRIVATE_NETWORKS` (default on) holds trusted ones too.
  Loopback is allowed only with `DEMO_MODE` or `ENGINE_ALLOW_INTERNAL_CONNECTORS`.
- **Login and MFA.** Steps may fill and click, but form submissions and
  PUT/PATCH/DELETE may only go to the blueprint's declared
  `auth.submit_targets` / `mfa.submit_targets`. Clicks whose text or target
  looks like moving money or changing the account are refused.
- **Read phase** (after login). No fill, select or `execute_js` steps, no
  form submissions or navigation POSTs, no PUT/PATCH/DELETE, and no clicks
  that submit or confirm. JSON `fetch` POSTs stay allowed, because
  single-page apps read that way.
- **Cleanup.** Only the declared `logout_targets` may be navigated or
  submitted to.
- **Trust.** `execute_js` runs only for trusted blueprints (bundled with
  Plaidify, or listed in `ENGINE_TRUSTED_CONNECTORS`), and only while logging
  in. Generated, registry and user-supplied blueprints are untrusted.
- **Browser.** TLS certificates are verified, service workers are blocked,
  extra windows opened in the read phase are closed, JavaScript dialogs are
  dismissed, and downloads go to a per-session directory that is removed
  afterwards. Chromium runs with its OS sandbox (`BROWSER_CHROMIUM_SANDBOX`,
  default on; the compose files apply `deploy/seccomp/chromium.json`). Values
  the browser typed are masked in its error messages before they are logged
  or returned.
- **LLM extraction.** Only the simplified page (hidden inputs and typed values
  removed, secret-looking URL parameters redacted) goes to the model, which is
  told to treat it as untrusted data; values it returns that do not appear on
  the page are dropped.

`STRICT_READ_ONLY_MODE=false` turns off the per-phase rules above; domain
scoping and the address policy stay on. Refused actions are recorded in the
job's metadata and the audit log.

### API Key, Agent and Consent Restrictions

- API keys may carry allowed scopes (`"field"` or `"read:field"`); agents
  carry allowed scopes and sites. Omitted means everything; `[]` or an
  unreadable stored value means nothing.
- The effective permission is the narrowest combination of the key's, the
  agent's, the access token's and (when sent) the consent grant's scopes. It
  applies to `/connect`, link creation, hosted sessions, `/fetch_data` and
  job results, including the job metadata that names fields.
- An agent's key must present a consent grant bound to that agent to call
  `/fetch_data`. Only the account owner (a login or the owner's own API key)
  approves or denies consent.
- An agent's own `rate_limit` (`N/period`) is enforced across all its requests.

### Hosted Link

- Launch tokens (`POST /link/bootstrap`) are signed with their own key and
  audience, expire after `LINK_LAUNCH_TOKEN_EXPIRE_SECONDS`, redeem once, and
  — when they name allowed origins — only from those origins.
- The page may be framed only by `'self'` and the session's allowed origins
  (`frame-ancestors`), and posts events only to those origins, never `*`.
- `/connect` through the page's `link_token` is accepted only while the
  session accepts credentials, and only for its own site.
- Event payloads never carry access tokens, credentials or extracted data;
  the page shows no token on screen. The browser-safe completion value is a
  10-minute, one-time `public_token` that your backend exchanges.

### Webhooks

Registering a webhook requires owning the link or session. Destinations must
be public `https://` URLs in production; every attempt resolves the host
again, connects only to an address it checked, and never follows redirects.
Deliveries are stored (encrypted) in an outbox and retried with backoff; each
is signed as shown above and carries `X-Plaidify-Delivery` for de-duplication.
Receivers should reject timestamps older than a few minutes.

### Logging

- Access logs blank secret query parameters and path segments that are
  themselves tokens (`/tokens/{token}`, `/access_jobs/{id}`, …).
- Tokens appear in application logs and audit entries only as fingerprints.
- Values of blueprint fields marked `sensitive` are never logged.

### Rate Limiting

Per client address (behind a proxy this needs `FORWARDED_ALLOW_IPS`), in Redis
when `REDIS_URL` is set. The limiter fails open if Redis stops answering.

| Endpoint | Limit |
|----------|-------|
| `POST /auth/register`, `POST /auth/verify-email` | 3/minute |
| `POST /auth/token`, `POST /auth/oauth2`, `POST /auth/refresh` | `RATE_LIMIT_AUTH` (5/minute) |
| `POST /auth/forgot-password` / `POST /auth/reset-password` | 3/minute / 5/minute |
| `POST /connect` | `RATE_LIMIT_CONNECT` (10/minute) |
| `POST /mfa/submit` | `RATE_LIMIT_MFA` (5/minute) |
| `POST /encryption/session`, `GET /encryption/public_key/{token}` | `RATE_LIMIT_ENCRYPTION` (10/minute) |
| `POST /refresh/schedule`, `PATCH /refresh/schedule/{token}` | 30/minute |
| Everything else | `RATE_LIMIT_DEFAULT` (60/minute) per client address and path; exempt: `/health`, `/metrics`, the hosted `/link` page, its static assets, its status polling (`/link/sessions/{token}/status`) and event stream (`/link/events/{token}`) |

### Security Headers

Responses from the application carry `X-Content-Type-Options: nosniff`,
`X-XSS-Protection: 1; mode=block`,
`Referrer-Policy: strict-origin-when-cross-origin`,
`Permissions-Policy: camera=(), microphone=(), geolocation=()`,
`Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors …`
and `X-Request-ID`. `X-Frame-Options: SAMEORIGIN` is sent except on a hosted
page that allows other origins to frame it. With `ENFORCE_HTTPS` or in
production, `Strict-Transport-Security: max-age=31536000; includeSubDomains`
is added and plain-HTTP requests are redirected, except `/health` and
`/metrics` (probes and scrapes use plain HTTP inside the network).

### CORS and Operational Endpoints

- A wildcard `CORS_ORIGINS` is refused at startup in production.
- In production `/docs`, `/redoc` and `/openapi.json` are off unless
  `DOCS_ENABLED=true`, and `GET /health/detailed` answers 404 until
  `HEALTH_CHECK_TOKEN` is set.
- `GET /metrics` requires `Authorization: Bearer <METRICS_TOKEN>` when that is
  set; without it, anyone who can reach the port can read the metrics.
- Request bodies are capped at 1 MB, chunked uploads included.

### Key Rotation Procedure

Rotating `ENCRYPTION_KEY` (the local KMS provider's master key). What it
protects: every per-user DEK, credentials and webhook secrets written before
per-user DEKs existed (encrypted directly under the master key), access-job
payloads queued in Redis, and — unless `AUDIT_HMAC_KEY` is set — the audit
chain's signing key. Nothing is lost if you follow the steps in order.

1. Generate a new 256-bit key:
   ```bash
   python -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
   ```
2. On every instance set `ENCRYPTION_KEY_PREVIOUS` to the current key,
   `ENCRYPTION_KEY` to the new key, and increment `ENCRYPTION_KEY_VERSION`;
   restart (a rolling restart is fine). From now on new data is written under
   the new key, and everything still under the old key is read through the
   `ENCRYPTION_KEY_PREVIOUS` fallback.
3. Re-key the stored data. From a checkout of the deployed commit, with the
   server's dependencies and the CLI installed (`pip install ./sdk`) and the
   same environment (it connects to `DATABASE_URL`):
   ```bash
   plaidify rotate-key --re-encrypt
   ```
   The command takes the old key from `ENCRYPTION_KEY_PREVIOUS` and the new one
   from `ENCRYPTION_KEY` (or pass `--old-key` / `--new-key`). It re-wraps every
   per-user DEK under the new key, moves credentials still under the old master
   key under their owner's DEK, re-encrypts webhook secrets that are under the
   old master key, stamps every row with the new key version and, when the
   audit chain is keyed from `ENCRYPTION_KEY`, seals the chain (see Audit
   Logging). It is safe to re-run. If any row cannot be decrypted with either
   key it exits with status 1 and lists those rows (table and hashed id);
   everything else is committed. Keep the previous key until a run exits 0.

   If you skip this step, the application's hourly background job does the
   same work 100 rows per run and logs `Key rotation pass complete` when a
   pass finds nothing left; the CLI is the fast path.
4. Wait at least `ACCESS_JOB_PAYLOAD_TTL` (default one hour) after the restart
   in step 2, so access-job payloads queued in Redis under the old key have been
   consumed or have expired.
5. Remove `ENCRYPTION_KEY_PREVIOUS` and restart.

External KMS providers (`KMS_PROVIDER=aws|azure|vault`) rotate the key that
wraps DEKs themselves and keep old versions for unwrapping; keep old key
versions enabled. Moving from one provider to another is
`scripts/migrate_to_kms.py` (see the script's docstring and
[KMS_INTEGRATION.md](docs/KMS_INTEGRATION.md)): it re-wraps every DEK, moves
master-key credentials and webhook secrets under their owner's DEK, and exits
non-zero if anything was skipped.

Other keys: changing `JWT_SECRET_KEY` invalidates every access token (refresh
tokens are random values, not JWTs, and keep working), resets sign-in
lockouts, and — unless `LINK_LAUNCH_SECRET` is set — invalidates pending
launch tokens. Changing `LINK_LAUNCH_SECRET` only invalidates launch tokens
(five minutes' worth). `AUDIT_HMAC_KEY` rotation is described below.

## Audit Logging

- Auth events, token operations, consent grants and data access are logged,
  with the user or agent, the client address and a token fingerprint as the
  resource. Failed sign-ins record the attempted username.
- Read-only policy blocks encountered during access jobs are recorded in both access-job metadata and the audit log
- Tamper-evident hash chain: each entry stores an HMAC-SHA256 over its own fields
  and the previous entry's hash. The key lives outside the database: set
  `AUDIT_HMAC_KEY` (at least 32 characters, e.g.
  `python -c "import secrets; print(secrets.token_urlsafe(48))"`) in your secrets
  manager. If it is unset, a key is derived from `ENCRYPTION_KEY` (HKDF).
- Appends are serialized (a PostgreSQL advisory lock; the chain-head row's write
  lock on SQLite), so concurrent requests cannot fork the chain. The chain-head
  row records the newest entry under its own MAC, so deleting the newest entries
  is detected as well as edits, gaps and reordering.
- Retention: configurable via `AUDIT_RETENTION_DAYS` (default: 730 days / 2 years).
  Pruning deletes the oldest entries and appends a signed checkpoint recording
  where the retained chain starts; verification starts there. Delete audit rows
  only through `src.audit.prune_audit_logs` — a bare `DELETE` breaks the chain.
- Signing-key changes: to rotate `AUDIT_HMAC_KEY`, move the old value to
  `AUDIT_HMAC_KEY_PREVIOUS`, set the new one and restart. The application's
  hourly key-rotation job then verifies the chain under both keys and appends a
  seal signed with the new key (log line `Audit chain sealed under the current
  signing key`); after that, remove `AUDIT_HMAC_KEY_PREVIOUS`. The same seal is
  written during an `ENCRYPTION_KEY` rotation when the audit key is derived from
  it (step 3 above). A chain that fails verification is never sealed. Entries
  signed with a removed key are reported as `sealed` rather than verified.
- Verification endpoint: `GET /audit/verify` (administrators only). It streams
  the chain in batches and reports `valid`, the entries checked and the first
  errors found. Users read their own entries at `GET /audit/logs`.

## Data Retention

| Data | Retention | Cleanup |
|------|-----------|---------|
| Audit log | `AUDIT_RETENTION_DAYS` (730) | Daily, keeping the chain verifiable |
| Stored access-job results | `RESULT_RETENTION_DAYS` (30) | Daily; the job row (status, timings) stays |
| Refresh tokens | Until expiry (7 days); revoked ones are kept until then so reuse is detected | Hourly |
| Password-reset tokens | 1 hour, single use | Hourly |
| Pending sign-ups (email verification) | 24 hours, single use; a new sign-up for the address replaces the row | Hourly |
| Sign-in throttle rows | A day after the last failure, once no lock is active | Hourly |
| Webhook deliveries | `WEBHOOK_DELIVERY_RETENTION_DAYS` (7) after they finish | By the outbox |
| Link sessions, one-time RSA keys | 10 minutes | Redis TTL (or in-memory expiry in development) |
| Queued job payloads | `ACCESS_JOB_PAYLOAD_TTL` (1 hour) | Redis TTL |
| Public tokens | 10 minutes, single use | On exchange or expiry |
| Browser downloads | The browser session | Removed when the context is released |
| A whole account | Until `DELETE /auth/me` (password confirmation for password accounts) | Credentials, tokens, links, consents, API keys, agents, webhooks and their deliveries, jobs, schedules and published registry blueprints are erased; audit entries stay |

The cleanup jobs run under leases: each runs in one process at a time.

## Known limits

These are open, and deliberately written down:

- With `REGISTRATION_EMAIL_VERIFICATION=false` (the default outside
  production), registration reveals whether a username or email is taken.
- JSON POSTs are allowed in the read phase, for single-page apps; a site that
  changes state through a JSON POST is not stopped by the method rules.
- Python connectors (`*_connector.py` in `CONNECTORS_DIR`) are trusted code:
  they run inside the server process, outside the browser policy, and one
  that hangs past `ENGINE_TIMEOUT_SECONDS` keeps its thread until it returns.
- The executor (or, in `inprocess` mode, the API) runs the browser in the same
  process that holds `ENCRYPTION_KEY` and the database credentials; Chromium's
  sandbox is the boundary. Per-job isolation is a design, not code yet
  ([ISOLATED_ACCESS_RUNTIME.md](docs/ISOLATED_ACCESS_RUNTIME.md)).
- The Azure template and its workflow have never been deployed
  ([AZURE_DEPLOYMENT.md](docs/AZURE_DEPLOYMENT.md)).
- No backup restore drill has been run ([DISASTER_RECOVERY.md](docs/DISASTER_RECOVERY.md)).
