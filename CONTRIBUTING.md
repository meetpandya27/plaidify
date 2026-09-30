# Contributing to Plaidify

Plaidify is the open-source layer between apps, AI agents and the authenticated
web. Contributions of every size are welcome: a blueprint for a new site, a
failing test, a bug fix, a clearer doc. For anything large, open an issue first
so the approach can be discussed.

Report security problems privately, as [SECURITY.md](SECURITY.md) describes —
never in a public issue.

---

## Setup

The server supports Python 3.11–3.13 (CI tests all three; the image runs
3.11). Dependencies are installed from hash-locked files, the same way CI and
the image install them.

```bash
git clone https://github.com/YOUR_USERNAME/plaidify.git
cd plaidify

python -m venv .venv && source .venv/bin/activate
pip install --require-hashes -r requirements-dev.lock   # server, tests, locust, ruff 0.14.6
python -m playwright install --only-shell chromium       # add --with-deps on a fresh Linux host

pip install pre-commit && pre-commit install              # ruff, ruff-format, gitleaks and file checks on commit

cp .env.example .env    # set ENCRYPTION_KEY and JWT_SECRET_KEY (commands in the file)
alembic upgrade head
(cd frontend-next && npm ci && npm run build)            # the hosted Link page, served at /link
uvicorn src.main:app --reload                            # http://127.0.0.1:8000/docs
```

The settings also read a `.env` in the working directory, tests included:
keep production values out of it.

Changing a dependency: edit `requirements*.txt`, regenerate the matching
`.lock` with the `pip-compile` command printed by
`python scripts/check_requirements_locks.py` (also in `requirements.txt`), and
commit both. CI fails when a lock no longer matches its source.

---

## Checks to run before a pull request

### Lint (whole repository, as CI does)

```bash
ruff check .
ruff format --check .    # `ruff format <files>` to fix
```

`pyproject.toml` pins ruff 0.14.6 (`required-version`); the pre-commit hook,
`requirements-dev.lock` and CI use the same version.

### Python server

```bash
PYTHONPATH=$PWD python -m pytest tests/ -q
PYTHONPATH=$PWD python -m pytest tests/ -q --cov=src --cov-fail-under=73   # CI's coverage floor
```

Without `DATABASE_URL` each run uses a fresh SQLite file and test keys. Some
tests need real services and skip without them:

| Variable | Enables |
| --- | --- |
| `DATABASE_URL=postgresql://…/plaidify_test` | the whole suite on PostgreSQL (the database name must contain `test`, or set `PLAIDIFY_TEST_DB_DISPOSABLE=1`: every table is emptied) |
| `PLAIDIFY_TEST_REDIS_URL=redis://127.0.0.1:6379/15` | the Redis-backed worker, event-stream, session-store and lease tests |
| `PLAIDIFY_TEST_POSTGRES_ADMIN_URL=postgresql://…` | `tests/test_provision_app_db_role.py` (the least-privilege role) |

Migrations, on a throwaway PostgreSQL database:

```bash
export DATABASE_URL=postgresql://…/plaidify_test
alembic upgrade head && alembic check && alembic downgrade base
```

### Browser tests and the demo

These drive a real Chromium and bind local ports:

```bash
PYTHONPATH=$PWD python -m pytest tests/test_hosted_link_e2e.py -q -m playwright   # needs frontend-next/dist
SKIP_BROWSER_TESTS=0 PYTHONPATH=$PWD python -m pytest tests/test_engine_integration.py -q
python scripts/demo.py --all
```

Chromium runs with its sandbox. On Ubuntu 23.10 and later it needs
unprivileged user namespaces: `sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0`
(CI does the same).

### Clients

```bash
PYTHONPATH=$PWD/sdk python -m pytest sdk/tests -q                          # Python SDK
(cd frontend-next && npm ci && npm run typecheck && npm test && npm run build)
(cd sdk-js && npm ci && npm run lint && npm run typecheck && npm test && npm run build)
(cd sdk-swift && swift test)                                              # macOS / Swift 5.9+
(cd sdk-android && ./gradlew :core:test)                                  # JDK 17
```

CI runs these checks except the `sdk-js` lint and the whole suite on
PostgreSQL: its test jobs use SQLite with a real Redis, and PostgreSQL gets the
migrations and the least-privilege role test (see [README](README.md#what-ci-runs)).

---

## Writing blueprints

A blueprint is a JSON file in `connectors/` that tells the engine how to log in
to a site, what to read, and how to log out. Start from a bundled one —
[`connectors/demo_saas.json`](connectors/demo_saas.json) is the smallest — and
keep to the schema the engine enforces:

- `schema_version` (`"2.0"` or `"3.0"`) is required, and unknown keys are rejected.
- `auth.submit_targets` lists where the login form may post; `mfa.submit_targets`
  and `logout_targets` do the same for MFA and logout. Any other form
  submission is refused (see the read-only rules in [SECURITY.md](SECURITY.md#runtime-safety-model)).
- Navigation stays on the blueprint's `domain` and its `allowed_domains`.
- `execute_js` steps run only for trusted connectors: the bundled ones and
  those an operator lists in `ENGINE_TRUSTED_CONNECTORS`. A new file is
  untrusted until it is added to `BUNDLED_CONNECTOR_SITES` in
  `src/core/blueprint.py`.
- Mark fields that hold personal data `"sensitive": true`.

Validate a file with the engine's own loader:

```bash
PYTHONPATH=$PWD python -c "from src.core.blueprint import load_blueprint; print(load_blueprint('connectors/my_site.json').name)"
```

Try it against the running server with an access token (or an API key in
`X-API-Key`):

```bash
curl -s -X POST http://127.0.0.1:8000/connect \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"site": "my_site", "username": "test", "password": "test"}'
```

Only write blueprints for sites whose terms allow automated access by the
account holder, and never commit real credentials, cookies or captured pages
with personal data.

---

## Code standards

- Python 3.11+, type hints on function signatures, docstrings on public functions.
- No secrets in code: every sensitive value comes from the environment.
- Never log, print or persist credentials, MFA codes or tokens. Log tokens as
  fingerprints (`src.crypto.token_fingerprint`), never in full.
- Every behavior change comes with a test; match the surrounding code's style.

## Pull request process

1. Branch from `main` (`feature/…`, `fix/…`, `docs/…`).
2. Write the change with its tests.
3. Run the checks above that cover what you touched.
4. Open the PR and fill in the template; CI must pass.

Commit messages: a short summary line, then what changed and why.

## Questions?

Open a [GitHub issue](https://github.com/meetpandya27/plaidify/issues). No question is too small.
