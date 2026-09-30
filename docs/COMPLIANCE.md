# Compliance Readiness — Controls Matrix

This document maps Plaidify's implemented controls to common audit frameworks
(**SOC 2** Trust Service Criteria and **ISO/IEC 27001** Annex A) to accelerate a
future audit. It is a readiness aid, **not** a certification or a statement of
compliance — certification requires an independent auditor and organizational
processes (see [Out of scope](#out-of-scope-organizational)). Plaidify has not
been released or run in production yet, so none of these controls has
operating evidence behind it.

Status legend:
- ✅ **Implemented** — enforced in code/infra in this repo (evidence linked).
- ⚙️ **Operator** — supported, but the deploying organization must configure/run it.
- 🏢 **Organizational** — a process/legal/personnel control outside the codebase.

Cross-reference: [SECURITY.md](../SECURITY.md) (how each control works, and its
known limits), [KMS_INTEGRATION.md](KMS_INTEGRATION.md),
[DISASTER_RECOVERY.md](DISASTER_RECOVERY.md),
[HIGH_AVAILABILITY.md](HIGH_AVAILABILITY.md), [THREAT_MODEL.md](THREAT_MODEL.md).

## Security (SOC 2 Common Criteria / ISO 27001)

| Control area | SOC 2 | ISO 27001 (A.) | Status | Implementation & evidence |
| ------------ | ----- | -------------- | ------ | ------------------------- |
| Logical access — authentication | CC6.1 | A.5.15, A.8.5 | ✅ | 15-minute access tokens bound to audience and the user's token version; rotating refresh tokens stored as hashes, with reuse detection; API keys (SHA-256 hashed, `X-API-Key` only, optional expiry); OAuth2 (Google/GitHub) checked against this app's client ids, verified email required — `src/routers/auth.py`, `src/auth_utils.py`, `src/oauth_providers.py` |
| Session termination | CC6.2 | A.5.18 | ✅ | Password reset, `POST /auth/sessions/revoke-all` and deactivation end every session; deactivation also revokes API keys — `src/auth_utils.py`, `src/routers/admin.py` |
| Authorization / least privilege | CC6.3 | A.5.15, A.8.2 | ✅ | Admin RBAC (`is_admin`); API-key and agent scopes and sites (`[]` denies all); consent grants bound to the agent — `src/routers/admin.py`, `src/dependencies.py`, `src/routers/consent.py` |
| Brute-force / credential protection | CC6.1 | A.8.5 | ✅ | bcrypt; sign-in throttling (5 failures per username and address, or 20 per username, in 15 minutes → 15-minute lock); `MFA_MAX_ATTEMPTS` codes per challenge; rate limits on sign-in, registration, password reset, `/connect` and `/mfa/submit`, and a default limit on every other endpoint — `src/routers/auth.py`, `src/routers/connection.py`, `src/app.py`. |
| Encryption at rest | CC6.1 | A.8.24 | ✅ | Per-user envelope encryption of credentials, job results and webhook secrets/payloads; pluggable KMS (local/AWS/Azure/Vault) — `src/database.py`, `src/crypto.py`, `src/kms.py` |
| Encryption in transit | CC6.7 | A.8.24 | ✅/⚙️ | HTTPS redirect and HSTS, always on in production; RSA-OAEP encryption of credentials from the hosted page, the Python SDK and the native screens; TLS is terminated at your proxy — `src/app.py`, `nginx/` |
| Key management & rotation | CC6.1 | A.8.24 | ✅/⚙️ | Master-key rotation (`plaidify rotate-key --re-encrypt` plus an hourly background pass), audit-key rotation with chain seals, KMS migration that fails on skipped rows; HSM-backed wrapping via a managed KMS — `src/database.py`, `scripts/migrate_to_kms.py`, [SECURITY.md](../SECURITY.md#key-rotation-procedure) |
| Secrets management | CC6.1 | A.8.24 | ✅/⚙️ | No secrets in source (gitleaks in CI and pre-commit); secrets never in URLs; Key Vault references in IaC; compose passwords as Docker secrets — `.pre-commit-config.yaml`, `infra/main.bicep`, `.secrets/README.md` |
| Network / edge hardening | CC6.6 | A.8.20, A.8.23 | ✅ | Security headers (CSP, framing allowlist, HSTS), CORS production guard, body-size cap, proxy trust list; the browser refuses private-network addresses; webhooks go only to public `https` addresses; API docs off in production — `src/app.py`, `src/core/network_policy.py`, `src/routers/webhooks.py` |
| Audit logging & integrity | CC7.2 | A.8.15 | ✅ | HMAC-SHA256 hash chain keyed outside the database, signed head row, retention checkpoints, admin-only verification — `src/audit.py` |
| Change management / SDLC | CC8.1 | A.8.25, A.8.28 | ✅/🏢 | CI: lint, lock drift, tests on 3.11–3.13, browser and client suites, migrations, image smoke test, CodeQL, secret scan; CODEOWNERS; pre-commit hooks — `.github/workflows/ci.yml`. Branch protection and required reviews on `main` are not configured yet (owner action). |
| Vulnerability management | CC7.1 | A.8.8 | ✅/⚙️ | Hash-locked dependencies; strict `pip-audit` and `npm audit` on every change and weekly; CodeQL; Dependabot version updates — `.github/workflows/ci.yml`, `.github/workflows/dependency-audit.yml`, `.github/dependabot.yml`. Dependabot alerts and security updates must be enabled in the repository settings. |
| Monitoring & alerting | CC7.2 | A.8.15, A.8.16 | ⚙️ | Prometheus metrics, alert rules with `promtool` tests, Grafana dashboard, OpenTelemetry traces, Sentry — `monitoring/`, `src/tracing.py`. Alerts reach someone only once the Alertmanager receiver is configured. |
| Incident response | CC7.3–7.5 | A.5.24–5.28 | 🏢/⚙️ | Runbooks + DR procedures provided; org must define on-call, severities, comms — `docs/RUNBOOK.md`, `docs/DISASTER_RECOVERY.md` |

## Availability (SOC 2 A-series)

| Control | SOC 2 | ISO 27001 | Status | Evidence |
| ------- | ----- | --------- | ------ | -------- |
| Resilience / fault tolerance | A1.1 | A.8.6 | ✅ | Circuit breakers, retry-with-backoff, bounded health probes, job heartbeats, a reaper for stuck jobs, graceful drain, durable webhook outbox — `src/core/circuit_breaker.py`, `src/access_jobs.py`, `src/background_services.py` |
| Capacity planning | A1.1 | A.8.6 | ⚙️ | Load-test harness + suggested SLOs + workflow — `docs/LOAD_TESTING.md`, `scripts/run-loadtest.sh` |
| High availability | A1.2 | A.8.14 | ⚙️ | Stateless API tier + zone-redundant database and Redis parameters + multi-region guidance — `docs/HIGH_AVAILABILITY.md`, `infra/main.bicep` |
| Backup & recovery | A1.2 | A.8.13 | ⚙️ | Encrypted, verified `pg_dump` backups (script, opt-in compose service, Kubernetes CronJob) and a restore procedure — `scripts/backup_db.sh`, `deploy/backup-cronjob.yaml`, `docs/DISASTER_RECOVERY.md`. No restore drill has been run yet. |

## Confidentiality & Privacy (SOC 2 C / P-series, GDPR)

| Control | SOC 2 | ISO 27001 | Status | Evidence |
| ------- | ----- | --------- | ------ | -------- |
| Data classification & handling | C1.1 | A.5.12 | ✅ | Credentials, results and webhook payloads encrypted per user; secrets kept out of URLs and logs; sensitive blueprint fields never logged — `src/database.py`, `src/logging_config.py` |
| Consent management | P3.1 | A.5.34 | ✅ | Consent requests and grants with scopes and expiry, bound to the requesting agent and enforced on `/fetch_data` — `src/routers/consent.py`, `src/routers/links.py` |
| Right to erasure (GDPR Art. 17) | P4.2 | A.5.34 | ✅ | `DELETE /auth/me` (password confirmation for password accounts) erases the user's data; the audit trail is preserved and may hold usernames from failed sign-ins — `src/routers/auth.py`, `src/database.py` |
| Data retention | C1.2 | A.5.33 | ✅/⚙️ | Audit retention, job-result erasure, token and delivery cleanup, all configurable — `src/app.py`, `AUDIT_RETENTION_DAYS`, `RESULT_RETENTION_DAYS` ([SECURITY.md](../SECURITY.md#data-retention)) |
| Processing integrity | PI1.1 | A.8.24 | ✅ | Tamper-evident audit chain; scoped, consented data access — `src/audit.py`, `src/routers/consent.py` |

## Pre-audit / pre-pentest checklist

Before engaging an auditor or penetration tester:

- [ ] Deploy with `ENV=production`, `DEBUG=false`, non-wildcard `CORS_ORIGINS`, `FORWARDED_ALLOW_IPS` set to the proxy.
- [ ] `REGISTRATION_ENABLED` unset (off); first administrator provisioned through `BOOTSTRAP_USER_*`, then those values removed.
- [ ] Managed KMS configured (`KMS_PROVIDER` ≠ `local`) with the key in an HSM-backed vault.
- [ ] Separate `AUDIT_HMAC_KEY` and `LINK_LAUNCH_SECRET`; secrets sourced from a vault (no plaintext env in source/CI); rotate any shared dev secrets.
- [ ] `HEALTH_CHECK_TOKEN` and `METRICS_TOKEN` set; metrics and Grafana not publicly exposed.
- [ ] Backups scheduled + a restore drill completed and timed against the RTO.
- [ ] Alerting wired to a real channel (Alertmanager); on-call defined.
- [ ] Branch protection on `main`, Dependabot alerts and security updates enabled; CI green (CodeQL, `pip-audit`, secret scan) on `main`.
- [ ] Review [THREAT_MODEL.md](THREAT_MODEL.md) mitigations and the known limits in [SECURITY.md](../SECURITY.md#known-limits); confirm scope with the tester.

## Out of scope (organizational)

These cannot be satisfied by code and must be handled by the operating organization:

- Independent SOC 2 Type II / ISO 27001 audit and certification.
- Personnel controls: background checks, security training, access reviews, onboarding/offboarding.
- Vendor/sub-processor risk management and Data Processing Agreements (DPAs).
- Formal, signed policies (information security, acceptable use, incident response, BCP/DR).
- Physical security (inherited from the cloud provider — collect their attestations).
- Legal: privacy notice, records of processing (GDPR Art. 30), breach-notification procedures.
