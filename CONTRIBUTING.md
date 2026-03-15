# Contributing to Plaidify

> **We're building the open-source infrastructure layer between AI agents and the authenticated web. That's a big mission, and we need your help.**

Every contribution matters — whether it's writing a JSON blueprint for a new site, fixing a bug, or building the Playwright browser engine. You don't need to be a senior engineer. You just need to care about the problem.

---

## 🔥 Highest-Impact Work Right Now

These are the things that move the needle most. Pick one and become a hero.

| Priority | Task | Difficulty | Good First Issue? |
|:--------:|------|:----------:|:-----------------:|
| 🔥 | [**Build the Playwright engine**](#the-big-one-playwright-engine) — replace `engine.py` stub with real browser automation | Hard | No |
| 🔥 | [**Write a real blueprint**](#writing-blueprints) — pick a public site, write the JSON | Easy | **Yes** ✅ |
| 🟡 | **MFA detection** — detect and handle 2FA flows in blueprints | Medium | No |
| 🟡 | **Blueprint validator CLI** — `python -m plaidify test connectors/my_site.json` | Medium | Yes |
| 🟢 | **Add tests** — edge cases, error paths, special characters | Easy | **Yes** ✅ |
| 🟢 | **Improve error messages** — make failures actionable for AI agents | Easy | **Yes** ✅ |
| 🟢 | **Request logging middleware** — correlation IDs across each API call | Easy | Yes |

---

## ⚡ Setup (60 seconds)

```bash
# 1. Fork and clone
git clone https://github.com/YOUR_USERNAME/plaidify.git
cd plaidify

# 2. Install
pip install -r requirements.txt

# 3. Configure
cp .env.example .env
# Open .env and set ENCRYPTION_KEY and JWT_SECRET_KEY
# Generation commands are inside the file

# 4. Database
alembic upgrade head

# 5. Verify everything works
pytest tests/ -v        # All 53 tests should pass
ruff check src/ tests/  # No lint errors

# 6. Start the server
uvicorn src.main:app --reload
# → http://127.0.0.1:8000/docs
```

---

## Writing Blueprints

**This is the #1 way to contribute.** Every new blueprint makes Plaidify useful for more people.

A blueprint is a JSON file that teaches Plaidify how to log into a site:

```json
{
  "name": "Example Portal",
  "login_url": "https://example.com/login",
  "fields": {
    "username": "#email-input",
    "password": "#password-input",
    "submit": "#login-btn"
  },
  "post_login": [
    { "wait": "#dashboard" },
    {
      "extract": {
        "account_name": ".account-header",
        "balance": ".balance-amount"
      }
    }
  ]
}
```

### Steps

1. Pick a public-facing website with a login form (test/demo sites preferred while we're in alpha)
2. Open DevTools → identify the CSS selectors for username, password, and submit button
3. Identify the post-login data you want to extract
4. Save as `connectors/site_name.json`
5. Test: `curl -X POST http://localhost:8000/connect -d '{"site":"site_name","username":"test","password":"test"}'`
6. Submit a PR!

### Blueprint Ideas

- Banking demo sites (e.g., Parabank, OWASP WebGoat)
- University portals (public demo instances)
- Utility company demos
- Test e-commerce sites
- Government portal test environments

---

## The Big One: Playwright Engine

The current engine in `src/core/engine.py` returns **simulated responses**. The most impactful contribution is replacing it with real Playwright browser automation.

### What Needs to Happen

1. **Browser pool manager** — launch and reuse Playwright browser instances
2. **Step executor** — read a JSON blueprint and execute `fill`, `click`, `wait`, `extract` steps
3. **Data extraction** — parse the authenticated page and return structured JSON
4. **Error detection** — detect login failures, MFA prompts, CAPTCHAs, rate limits
5. **Session cleanup** — close browsers, clear state after each connection

### Architecture Hint

```python
# The current engine interface (don't change this — it's what the API calls)
class PlaidifyEngine:
    async def connect(self, site: str, username: str, password: str) -> dict:
        # Currently returns simulated data
        # Replace with: load blueprint → launch browser → execute → extract
        pass
```

If you want to tackle this, open an issue first so we can discuss the approach.

---

## Code Standards

- **Python 3.9+** — use modern syntax but stay compatible
- **Type hints** on all function signatures
- **Docstrings** on all public functions (Google style)
- **Ruff** for linting — config in `pyproject.toml`, run: `ruff check src/ tests/`
- **No secrets in code** — all sensitive values via environment variables
- **No credential logging** — never print, log, or persist passwords in plaintext

---

## Pull Request Process

```bash
# Branch naming
feature/short-description
fix/short-description
docs/short-description
```

1. Create a branch from `main`
2. Write your code with tests
3. `pytest tests/ -v` — all tests pass
4. `ruff check src/ tests/` — no lint errors
5. Open a PR → fill out the template → wait for CI

### Commit Style

```
Add MFA detection to JSON blueprint parser

- Add "mfa" field to blueprint schema
- Engine checks for MFA selector after login
- Returns mfa_required status with session ID
```

---

## Testing

```bash
# Run all tests
pytest tests/ -v

# With coverage
pytest tests/ --cov=src --cov-report=term-missing

# Specific suite
pytest tests/test_auth.py -v
pytest tests/test_links.py -v
pytest tests/test_system.py -v
pytest tests/test_core.py -v
```

We use **pytest** with shared fixtures in `tests/conftest.py`. Tests are grouped by area in classes: `class TestRegistration:`, `class TestLinkManagement:`, etc.

**Minimum 70% coverage required** (enforced in CI).

---

## Questions?

Open a [GitHub issue](https://github.com/meetpandya27/plaidify/issues). No question is too small.

---

<p align="center">
  <strong>Every blueprint, every test, every fix makes Plaidify more useful for developers and AI agents everywhere.</strong>
  <br />
  Thank you for contributing. 🙌
</p>
