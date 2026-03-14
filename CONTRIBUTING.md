# Contributing to Plaidify

Thanks for your interest in contributing! Plaidify is in early alpha and we welcome help at every level — from fixing typos to building core features.

---

## Development Setup

```bash
# 1. Fork and clone
git clone https://github.com/YOUR_USERNAME/plaidify.git
cd plaidify

# 2. Install dependencies
pip install -r requirements.txt

# 3. Set up environment
cp .env.example .env
# Edit .env — you MUST set ENCRYPTION_KEY and JWT_SECRET_KEY
# Generation commands are in the file

# 4. Run migrations
alembic upgrade head

# 5. Run the server
uvicorn src.main:app --reload
# → http://127.0.0.1:8000/docs

# 6. Run tests
pytest tests/ -v

# 7. Run linter
ruff check src/ tests/
```

---

## Code Standards

- **Python 3.9+** — use modern syntax but stay compatible
- **Type hints** on all function signatures
- **Docstrings** on all public functions and classes (Google style)
- **Ruff** for linting and formatting — config in `pyproject.toml`
- **No secrets in code** — all sensitive values via environment variables
- **No logging of credentials** — never print, log, or persist passwords/tokens in plaintext

---

## Making Changes

### Branch naming

```
feature/short-description
fix/short-description
docs/short-description
```

### Commit messages

Write clear, descriptive commit messages. First line should be a concise summary (< 72 chars), followed by details if needed.

```
Add MFA detection to JSON blueprint parser

- Add "mfa" field to blueprint schema
- Engine checks for MFA selector after login
- Returns mfa_required status with session ID
```

### Pull Requests

1. Create a branch from `main`
2. Make your changes with tests
3. Run `pytest tests/ -v` — all tests must pass
4. Run `ruff check src/ tests/` — no lint errors
5. Open a PR and fill out the template
6. Wait for CI to pass and a maintainer to review

---

## Testing

We use **pytest** with the following conventions:

- Tests go in `tests/`
- Use the fixtures from `tests/conftest.py` (`client`, `auth_headers`, etc.)
- Each test file covers one area: `test_auth.py`, `test_links.py`, `test_system.py`, `test_core.py`
- Class-based test grouping: `class TestRegistration:`, `class TestLinkManagement:`
- Minimum 70% coverage required (enforced in CI)

```bash
# Run with coverage
pytest tests/ --cov=src --cov-report=term-missing

# Run a specific test
pytest tests/test_auth.py::TestRegistration::test_register_success -v
```

---

## Good First Issues

If you're new to the project, these are great starting points:

- **Write a JSON blueprint** for a public demo/test site
- **Add unit tests** for edge cases (empty strings, very long inputs, special characters)
- **Improve error messages** in the engine — make them more actionable
- **Add correlation IDs** — log a unique request ID across the lifecycle of each API call
- **Create a minimal CLI** — `python -m plaidify test-blueprint connectors/demo_site.json`

---

## Where Help Is Needed Most

These areas have the highest impact:

1. **Phase 1 — Playwright Engine** (the big one)
   - Replace the stub in `src/core/engine.py` with real Playwright browser automation
   - Write a browser pool manager for concurrent connections
   - Build the `fill`, `click`, `wait`, `extract` step executor

2. **Blueprint Quality**
   - Write blueprints for real public-facing test sites
   - Build a blueprint validator (check selectors, test connectivity)

3. **Security Review**
   - Review credential handling for vulnerabilities
   - Suggest improvements to the encryption/auth approach

---

## Project Structure

```
src/
├── main.py              # Endpoints — start here to understand the API
├── config.py            # Configuration — how env vars are loaded
├── database.py          # Models & encryption — how data is stored
├── models.py            # Schemas — request/response formats
├── exceptions.py        # Errors — custom exception types
├── logging_config.py    # Logging — structured log setup
└── core/
    ├── engine.py        # Engine — where connections happen (stub today)
    └── connector_base.py # Base class for Python connectors
```

---

## Questions?

Open a [GitHub issue](https://github.com/meetpandya27/plaidify/issues) — we're happy to help you get oriented.

Thank you for contributing!
