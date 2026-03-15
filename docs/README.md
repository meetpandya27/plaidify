# Plaidify — Technical Documentation

> For the project overview and quick start, see the [main README](../README.md).  
> For AI agent integration, see **[AGENTS.md](AGENTS.md)**.

---

## Architecture

Plaidify is a FastAPI application with a modular architecture designed to support multiple connection strategies and deployment patterns.

```
Request → FastAPI Router → Auth Middleware → Endpoint Handler
                                                    │
                              ┌─────────────────────┼──────────────────────┐
                              ▼                     ▼                      ▼
                        Direct Connect        Link Token Flow         Auth Endpoints
                        (POST /connect)       (multi-step)            (register/login)
                              │                     │
                              ▼                     ▼
                        Connection Engine ◄─────────┘
                              │
                    ┌─────────┼─────────┐
                    ▼                   ▼
              Python Connector    JSON Blueprint
              (BaseConnector)     (connectors/*.json)
                    │                   │
                    ▼                   ▼
              Custom Logic        Stub Engine (→ Playwright in Phase 1)
```

### Module Responsibilities

| Module | Purpose |
|--------|---------|
| `src/main.py` | FastAPI app, all endpoint definitions, auth utilities, exception handler |
| `src/config.py` | Pydantic Settings class — loads all config from env vars |
| `src/database.py` | SQLAlchemy models (User, Link, AccessToken, RefreshToken), AES-256-GCM + envelope encryption (per-user DEKs), key rotation, DB session management |
| `src/models.py` | Pydantic request/response schemas for API validation |
| `src/exceptions.py` | Custom exception hierarchy (PlaidifyError → BlueprintNotFoundError, etc.) |
| `src/logging_config.py` | Structured logging setup (JSON for prod, colored text for dev) |
| `src/crypto.py` | Ephemeral RSA-2048 keypair management for client-side credential encryption |
| `src/core/engine.py` | Connection engine — loads connectors, executes blueprint logic |
| `src/core/connector_base.py` | Abstract base class for Python connectors |

---

## Configuration

All configuration is via environment variables, managed by Pydantic Settings. The app **will not start** if required variables are missing.

### Required

| Variable | Description | How to Generate |
|----------|-------------|-----------------|
| `ENCRYPTION_KEY` | Base64url-encoded 256-bit key for AES-256-GCM credential encryption | `python -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"` |
| `JWT_SECRET_KEY` | Secret for signing JWT tokens | `openssl rand -hex 32` |

### Optional

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `sqlite:///plaidify.db` | SQLAlchemy connection string |
| `APP_NAME` | `Plaidify` | Application name |
| `APP_VERSION` | `0.3.0a1` | Version string |
| `DEBUG` | `false` | Enable debug mode |
| `LOG_LEVEL` | `INFO` | DEBUG, INFO, WARNING, ERROR, CRITICAL |
| `LOG_FORMAT` | `json` | `json` (production) or `text` (development) |
| `CORS_ORIGINS` | `http://localhost:3000,...` | Comma-separated allowed origins (no wildcard in production) |
| `CONNECTORS_DIR` | `connectors` | Path to blueprint directory |
| `JWT_ALGORITHM` | `HS256` | JWT signing algorithm |
| `JWT_ACCESS_TOKEN_EXPIRE_MINUTES` | `15` | Access token expiry |
| `JWT_REFRESH_TOKEN_EXPIRE_MINUTES` | `10080` (1 week) | Refresh token expiry |
| `ENCRYPTION_KEY_VERSION` | `1` | Current encryption key version (increment on rotation) |
| `ENCRYPTION_KEY_PREVIOUS` | (none) | Previous master key for fallback during rotation |
| `RATE_LIMIT_ENABLED` | `true` | Enable rate limiting |
| `RATE_LIMIT_AUTH` | `5/minute` | Rate limit for auth endpoints |
| `RATE_LIMIT_CONNECT` | `10/minute` | Rate limit for /connect |
| `ENFORCE_HTTPS` | `false` | Redirect HTTP→HTTPS + HSTS (auto-enabled in production) |

---

## Database

### ORM Models

**User** — A registered Plaidify account.
| Column | Type | Notes |
|--------|------|-------|
| id | Integer | Primary key, auto-increment |
| username | String | Unique, nullable (OAuth2 users may not have one) |
| email | String | Unique, nullable |
| hashed_password | Text | bcrypt hash, nullable |
| oauth_provider | String | e.g., 'google', 'github' |
| oauth_sub | String | Provider's user ID |
| is_active | Boolean | Default true |
| created_at | DateTime | UTC timestamp |
| encrypted_dek | Text | Per-user Data Encryption Key wrapped by master key (AES-256-GCM) |

**Link** — A user's intent to connect to a site.
| Column | Type | Notes |
|--------|------|-------|
| link_token | String | Primary key (UUID) |
| site | String | Blueprint name |
| user_id | Integer | FK → users.id |
| created_at | DateTime | UTC timestamp |

**AccessToken** — Stored encrypted credentials for a linked site.
| Column | Type | Notes |
|--------|------|-------|
| token | String | Primary key (UUID) |
| link_token | String | FK → links.link_token |
| username_encrypted | Text | AES-256-GCM encrypted via user DEK |
| password_encrypted | Text | AES-256-GCM encrypted via user DEK |
| instructions | Text | Optional processing instructions |
| user_id | Integer | FK → users.id |
| key_version | Integer | Encryption key version (for rotation tracking) |
| created_at | DateTime | UTC timestamp |

**RefreshToken** — Refresh token for JWT token rotation.
| Column | Type | Notes |
|--------|------|-------|
| id | Integer | Primary key, auto-increment |
| token | String | Unique, cryptographically random |
| user_id | Integer | FK → users.id |
| expires_at | DateTime | Token expiry (default 7 days) |
| revoked | Boolean | Set to true on use (rotation) |
| created_at | DateTime | UTC timestamp |

### Migrations

We use Alembic for database migrations. Never use `Base.metadata.create_all()` in production.

```bash
# Create a new migration after changing models
alembic revision --autogenerate -m "Description of change"

# Apply migrations
alembic upgrade head

# Rollback one step
alembic downgrade -1
```

---

## Authentication

### Flow

1. User registers via `POST /auth/register` → receives JWT access token + refresh token
2. User logs in via `POST /auth/token` → receives JWT access token + refresh token
3. JWT is included as `Authorization: Bearer <token>` on protected endpoints
4. Access tokens expire after 15 minutes; refresh via `POST /auth/refresh`
5. Refresh tokens are single-use (rotation) and expire after 7 days
6. All link/token/data endpoints enforce user ownership (isolation)

### Token Structure

```json
{
  "sub": "123",        // User ID as string
  "exp": 1710500000   // Expiry timestamp
}
```

---

## Exception Handling

All custom exceptions inherit from `PlaidifyError` and are caught by a global FastAPI exception handler that returns structured JSON:

```json
{
  "error": "No blueprint found for site: nonexistent_site"
}
```

### Exception Hierarchy

```
PlaidifyError (500)
├── BlueprintNotFoundError (404)
├── BlueprintValidationError (422)
├── ConnectionFailedError (502)
├── AuthenticationError (401)
├── MFARequiredError (403)
├── SiteUnavailableError (503)
├── RateLimitedError (429)
├── CaptchaRequiredError (403)
├── DataExtractionError (500)
├── InvalidTokenError (401)
├── UserNotFoundError (401)
└── LinkNotFoundError (404)
```

---

## Blueprint System

### JSON Blueprints

Files in `/connectors/*.json` are loaded by the engine when a matching site is requested.

Current schema (v1):
```json
{
  "name": "Human-readable name",
  "login_url": "https://...",
  "fields": {
    "username": "#css-selector",
    "password": "#css-selector",
    "submit": "#css-selector"
  },
  "post_login": [
    { "wait": "#selector" },
    { "extract": { "key": "#selector" } }
  ]
}
```

### Python Connectors

Files matching `/connectors/*_connector.py` are auto-discovered. Any class inheriting from `BaseConnector` is loaded.

```python
from src.core.connector_base import BaseConnector

class MyConnector(BaseConnector):
    def connect(self, username: str, password: str) -> dict:
        return {"status": "connected", "data": {...}}
```

### Resolution Order

1. Check for a Python connector matching `{site}_connector.py`
2. Fall back to JSON blueprint matching `{site}.json`
3. Raise `BlueprintNotFoundError` if neither found

---

## Testing

```bash
# Run all tests
pytest tests/ -v

# With coverage
pytest tests/ --cov=src --cov-report=term-missing

# Single test file
pytest tests/test_auth.py -v
```

### Test Structure

| File | Tests | Coverage |
|------|-------|----------|
| `tests/conftest.py` | Fixtures: client, auth_headers, test DB setup/teardown | — |
| `tests/test_system.py` | Root, health, status, connect, disconnect | System endpoints |
| `tests/test_auth.py` | Register, login, profile, OAuth2, edge cases | Auth flow |
| `tests/test_links.py` | Link flow, instructions, CRUD, user isolation | Core business logic |
| `tests/test_core.py` | Encryption, exceptions, config | Utilities |
| `tests/test_main.py` | Legacy basic tests | Backward compat |
| `tests/test_example.py` | Mock site connection | Blueprint loading |

---

## Docker

### Development

```bash
docker compose up --build
# Mounts local code, auto-reloads on changes
```

### Production

```bash
docker build -t plaidify:latest .
docker run -d \
  -p 8000:8000 \
  -e ENCRYPTION_KEY="your-key" \
  -e JWT_SECRET_KEY="your-secret" \
  -e DATABASE_URL="postgresql://..." \
  -e LOG_FORMAT="json" \
  plaidify:latest
```

The Dockerfile uses a multi-stage build (builder + runtime), runs as a non-root user, and includes a health check.

---

## CI Pipeline

GitHub Actions runs on every push/PR to `main`:

1. **Lint** — ruff check + format validation
2. **Test** — pytest on Python 3.9, 3.10, 3.11, 3.12 with coverage threshold (70%)
3. **Security** — pip-audit for dependency vulnerabilities
4. **Docker** — Build image and check size
