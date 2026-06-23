# Compliance Readiness — Controls Matrix

This document maps Plaidify's implemented controls to common audit frameworks
(**SOC 2** Trust Service Criteria and **ISO/IEC 27001** Annex A) to accelerate a
future audit. It is a readiness aid, **not** a certification or a statement of
compliance — certification requires an independent auditor and organizational
processes (see [Out of scope](#out-of-scope-organizational)).

Status legend:
- ✅ **Implemented** — enforced in code/infra in this repo (evidence linked).
- ⚙️ **Operator** — supported, but the deploying organization must configure/run it.
- 🏢 **Organizational** — a process/legal/personnel control outside the codebase.

Cross-reference: [SECURITY.md](../SECURITY.md) (architecture),
[KMS_INTEGRATION.md](KMS_INTEGRATION.md), [DISASTER_RECOVERY.md](DISASTER_RECOVERY.md),
[HIGH_AVAILABILITY.md](HIGH_AVAILABILITY.md), [THREAT_MODEL.md](THREAT_MODEL.md).

## Security (SOC 2 Common Criteria / ISO 27001)

| Control area | SOC 2 | ISO 27001 (A.) | Status | Implementation & evidence |
| ------------ | ----- | -------------- | ------ | ------------------------- |
| Logical access — authentication | CC6.1 | A.5.15, A.8.5 | ✅ | JWT (short-lived access + rotating refresh), API keys (SHA-256 hashed), OAuth2 (Google/GitHub, verified-email required) — `src/routers/auth.py`, `src/oauth_providers.py` |
| Authorization / least privilege | CC6.3 | A.5.15, A.8.2 | ✅ | Role-based admin (`is_admin`), scoped API keys & agents, consent scopes — `src/routers/admin.py`, `src/dependencies.py` |
| Brute-force / credential protection | CC6.1 | A.8.5 | ✅ | bcrypt password hashing, account lockout (5 fails → 15 min), MFA-challenge metering, rate limiting — `src/routers/auth.py`, `src/dependencies.py` |
| Encryption at rest | CC6.1 | A.8.24 | ✅ | Envelope encryption (per-user DEK) + pluggable KMS (Local/AWS/Azure/Vault) — `src/database.py`, `src/kms.py` |
| Encryption in transit | CC6.7 | A.8.24 | ⚙️ | HSTS + HTTPS enforcement when `ENFORCE_HTTPS=true`; terminate TLS at the proxy — `src/app.py`, `nginx/` |
| Key management & rotation | CC6.1 | A.8.24 | ✅/⚙️ | Master-key rotation + background re-encryption; HSM-backed wrapping via managed KMS — `src/kms.py`, `docs/KMS_INTEGRATION.md` |
| Secrets management | CC6.1 | A.8.24 | ✅/⚙️ | No secrets in source (gitleaks in CI), Key Vault references in IaC, `.env.example` guidance — `.pre-commit-config.yaml`, `infra/main.bicep` |
| Network / edge hardening | CC6.6 | A.8.20, A.8.23 | ✅ | Security headers (CSP, X-Frame-Options, X-Content-Type-Options, HSTS), CORS production guard, request-size limits — `src/app.py` |
| Audit logging & integrity | CC7.2 | A.8.15 | ✅ | Tamper-evident hash-chained audit log + verification endpoint — `src/audit.py`, `src/database.py` |
| Change management / SDLC | CC8.1 | A.8.25, A.8.28 | ✅ | CI: lint, multi-version tests, CodeQL, secret scan, `pip-audit`, Docker build; pre-commit hooks — `.github/workflows/ci.yml` |
| Vulnerability management | CC7.1 | A.8.8 | ✅/⚙️ | `pip-audit` (strict) + CodeQL in CI; Dependabot updates; operators patch on cadence — `.github/workflows/ci.yml` |
| Monitoring & alerting | CC7.2 | A.8.15, A.8.16 | ⚙️ | Prometheus metrics + alert rules + Grafana dashboard; OpenTelemetry traces; Sentry — `monitoring/`, `src/tracing.py` |
| Incident response | CC7.3–7.5 | A.5.24–5.28 | 🏢/⚙️ | Runbooks + DR procedures provided; org must define on-call, severities, comms — `docs/RUNBOOK.md`, `docs/DISASTER_RECOVERY.md` |

## Availability (SOC 2 A-series)

| Control | SOC 2 | ISO 27001 | Status | Evidence |
| ------- | ----- | --------- | ------ | -------- |
| Resilience / fault tolerance | A1.1 | A.8.6 | ✅ | Circuit breakers, retry-with-backoff, rate-limit fail-open, bounded health probes — `src/core/circuit_breaker.py`, `src/routers/system.py` |
| Capacity planning | A1.1 | A.8.6 | ⚙️ | Load-test harness + SLOs + workflow — `docs/LOAD_TESTING.md`, `scripts/run-loadtest.sh` |
| High availability | A1.2 | A.8.14 | ⚙️ | Stateless tier + zone-redundant DB params + multi-region topology — `docs/HIGH_AVAILABILITY.md`, `infra/main.bicep` |
| Backup & recovery | A1.2 | A.8.13 | ✅/⚙️ | Backup script + hourly CronJob + DR runbook with restore drill — `scripts/backup_db.sh`, `deploy/backup-cronjob.yaml`, `docs/DISASTER_RECOVERY.md` |

## Confidentiality & Privacy (SOC 2 C / P-series, GDPR)

| Control | SOC 2 | ISO 27001 | Status | Evidence |
| ------- | ----- | --------- | ------ | -------- |
| Data classification & handling | C1.1 | A.5.12 | ✅ | Credentials encrypted per-user; PII kept out of logs — `src/database.py`, `src/logging_config.py` |
| Consent management | P3.1 | A.5.34 | ✅ | Agent consent requests/grants with scopes + expiry — `src/routers/consent.py` |
| Right to erasure (GDPR Art. 17) | P4.2 | A.5.34 | ✅ | `DELETE /auth/me` erases all user data; audit trail preserved — `src/routers/auth.py`, `src/database.py` |
| Data retention | C1.2 | A.5.33 | ✅/⚙️ | Configurable audit retention + cleanup jobs — `src/app.py`, `AUDIT_RETENTION_DAYS` |
| Processing integrity | PI1.1 | A.8.24 | ✅ | Tamper-evident audit chain; scoped, consented data access — `src/audit.py`, `src/routers/consent.py` |

## Pre-audit / pre-pentest checklist

Before engaging an auditor or penetration tester:

- [ ] Deploy with `ENV=production`, `DEBUG=false`, `ENFORCE_HTTPS=true`, non-wildcard `CORS_ORIGINS`.
- [ ] `REGISTRATION_ENABLED=false`; provision accounts via bootstrap; confirm an admin exists.
- [ ] Managed KMS configured (`KMS_PROVIDER` ≠ `local`) with the key in an HSM-backed vault.
- [ ] Secrets sourced from a vault (no plaintext env in source/CI); rotate any shared dev secrets.
- [ ] `/health/detailed` gated by `HEALTH_CHECK_TOKEN`; metrics/Grafana not publicly exposed.
- [ ] Backups scheduled + a restore drill completed and timed against the RTO.
- [ ] Alerting wired to a real channel (Alertmanager); on-call defined.
- [ ] Latest dependencies; CI green (CodeQL, `pip-audit`, secret scan) on `main`.
- [ ] Review [THREAT_MODEL.md](THREAT_MODEL.md) mitigations and confirm scope with the tester.

## Out of scope (organizational)

These cannot be satisfied by code and must be handled by the operating organization:

- Independent SOC 2 Type II / ISO 27001 audit and certification.
- Personnel controls: background checks, security training, access reviews, onboarding/offboarding.
- Vendor/sub-processor risk management and Data Processing Agreements (DPAs).
- Formal, signed policies (information security, acceptable use, incident response, BCP/DR).
- Physical security (inherited from the cloud provider — collect their attestations).
- Legal: privacy notice, records of processing (GDPR Art. 30), breach-notification procedures.
