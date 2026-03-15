# Changelog

All notable changes to Plaidify will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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
