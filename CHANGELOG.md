# Changelog

All notable changes to Plaidify will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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
