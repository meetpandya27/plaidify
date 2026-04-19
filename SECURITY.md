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

## Security Architecture

### Encryption

| Layer | Algorithm | Key Size | Purpose |
|-------|-----------|----------|---------|
| Credentials at rest | AES-256-GCM | 256-bit | Encrypts stored usernames/passwords |
| Envelope encryption | AES-256-GCM | 256-bit per-user DEK | Per-user key isolation |
| Key wrapping | AES-256-GCM | 256-bit master key | Wraps per-user DEKs |
| Client transport | RSA-2048 OAEP | 2048-bit ephemeral | One-shot credential encryption from client |
| Password hashing | bcrypt | 12 rounds, ident 2b | User password storage |
| JWT signing | HMAC-SHA256 | 256-bit | API access tokens |
| Audit chain | SHA-256 | 256-bit | Tamper-evident audit log |

### Key Hierarchy

```
Master Key (ENCRYPTION_KEY)
  └── wraps per-user DEK (Data Encryption Key)
        └── encrypts user credentials (username, password)
```

### Authentication Flow

1. **Registration**: Password → bcrypt hash → stored in DB
2. **Login**: Password → verify against bcrypt hash → issue JWT + refresh token
3. **API Access**: JWT Bearer token (15-min TTL) or API key (SHA-256 hashed)
4. **Token Refresh**: Refresh token rotation (old token revoked on use)

### Runtime Safety Model

- Browser-driven blueprints run in strict read-only mode by default.
- Login and MFA are the only phases where bounded mutation is permitted.
- After authentication, Plaidify blocks fill, select, execute_js, risky clicks, and state-changing browser requests.
- Navigation-style POSTs and form submissions are denied after authentication unless the flow is still in auth or MFA.
- Extra browser windows opened during the read phase are closed automatically.
- Downloads are only allowed during the read phase and are stored as temporary browser artifacts under a controlled download root.

### API Key and Agent Restrictions

- API keys are SHA-256 hashed at rest and may carry explicit allowed scopes.
- Registered agents inherit their own API key and can also carry allowed sites.
- Effective permissions are the most restrictive combination of API key scopes and agent scopes.
- Link creation, hosted link sessions, and fetch_data all enforce those site and scope constraints before execution.
- Access jobs now record agent_id and read-only policy telemetry when blocked actions occur.

### Key Rotation Procedure

1. Generate a new 256-bit key:
   ```bash
   python -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
   ```
2. Set `ENCRYPTION_KEY_PREVIOUS` to the current key value
3. Set `ENCRYPTION_KEY` to the new key value
4. Increment `ENCRYPTION_KEY_VERSION`
5. Restart the application — the background `_reencrypt_stale_tokens` job will
   automatically re-encrypt all tokens to the new key version
6. Monitor logs for `Re-encrypted N token(s)` messages
7. After all tokens are migrated, remove `ENCRYPTION_KEY_PREVIOUS`

### Rate Limiting

| Endpoint | Limit | Backend |
|----------|-------|---------|
| `/auth/register` | 3/minute | Redis (fallback: memory) |
| `/auth/token` (login) | 5/minute | Redis (fallback: memory) |
| `/connect` | 10/minute | Redis (fallback: memory) |
| All other endpoints | 60/minute | Redis (fallback: memory) |

### Security Headers

All responses include:
- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: SAMEORIGIN`
- `X-XSS-Protection: 1; mode=block`
- `Referrer-Policy: strict-origin-when-cross-origin`
- `Permissions-Policy: camera=(), microphone=(), geolocation=()`
- `Content-Security-Policy: default-src 'self'; script-src 'self'; ...`
- `Strict-Transport-Security: max-age=31536000; includeSubDomains` (production)
- `X-Request-ID: <uuid>` (correlation tracking)

### CORS Policy

- Development: configurable origins (default localhost ports)
- Production: **wildcard (*) is rejected at startup** (validated in both config.py and main.py)

## Audit Logging

- All auth events, token operations, consent grants, and data access are logged
- Read-only policy blocks encountered during access jobs are recorded in both access-job metadata and the audit log
- Tamper-evident hash chain (each entry hashes itself + previous entry's hash)
- Retention: configurable via `AUDIT_RETENTION_DAYS` (default: 730 days / 2 years)
- Verification endpoint: `GET /audit/verify`

## Data Retention

| Data Type | Retention | Cleanup |
|-----------|-----------|---------|
| Audit logs | Configurable (default 730 days) | Background job (daily) |
| Refresh tokens | JWT expiry + cleanup | Background job (hourly) |
| Link sessions | 10 minutes TTL | Auto-expire (Redis TTL / memory) |
| Browser download artifacts | Session lifetime only | Removed when browser context is released |
| Ephemeral RSA keys | 10 minutes TTL | Auto-expire |

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.3.x | ✅ Active |
| 0.2.x | ⚠️ Security fixes only |
| < 0.2 | ❌ End of life |
