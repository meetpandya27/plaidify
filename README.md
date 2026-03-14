<p align="center">
  <h1 align="center">Plaidify</h1>
  <p align="center">
    Open-source infrastructure for authenticated web data — for developers and AI agents.
    <br />
    <a href="#-quick-start">Quick Start</a> · <a href="https://github.com/meetpandya27/plaidify/issues">Report Bug</a> · <a href="https://github.com/meetpandya27/plaidify/issues">Request Feature</a>
  </p>
</p>

<p align="center">
  <a href="https://github.com/meetpandya27/plaidify/actions/workflows/ci.yml"><img src="https://github.com/meetpandya27/plaidify/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green.svg" alt="MIT License"></a>
  <img src="https://img.shields.io/badge/python-3.9%2B-blue.svg" alt="Python 3.9+">
  <img src="https://img.shields.io/badge/PRs-welcome-brightgreen.svg" alt="PRs Welcome">
  <img src="https://img.shields.io/badge/status-alpha-orange.svg" alt="Alpha">
</p>

---

> **Honest status:** Plaidify is in **early alpha**. The API, auth system, database layer, and blueprint architecture are built and tested. The browser automation engine (the part that actually logs into real websites) **is not yet implemented** — it currently returns simulated responses. We're building in public and contributions are welcome. See [What's Built vs. What's Not](#-whats-built-vs-whats-not) for the full picture.

---

## What is Plaidify?

Plaidify lets developers programmatically **connect to any website protected by a login form** and extract structured data through a unified API.

The world's data is locked behind login forms — bank balances, utility bills, insurance policies, academic transcripts — and most of these sites have no API. Plaidify aims to be the open-source infrastructure layer that solves this: define a JSON "blueprint" for any site, and get a REST API that authenticates and returns clean data.

**The long-term vision:** Plaidify becomes the standard way both **developers** and **AI agents** safely access authenticated web data — with user consent, scoped permissions, and full audit trails.

### How It Works

```text
Your App                              Plaidify
────────                              ────────

POST /connect ──────────────────────► Load Blueprint (JSON or Python)
  { site, username, password }                │
                                              ▼
                                       Execute Login Flow
                                       (Playwright — coming in Phase 1)
                                              │
                                              ▼
                                       Extract Structured Data
                                              │
◄──────────────────────────────────── Return JSON Response
  { status: "connected",
    data: { balance: 4521.30, ... } }
```

### Who Is This For?

| Persona | Use Case |
|---------|----------|
| **App Developer** | Pull data from sites with no API (banks, portals, utilities) into your product |
| **AI Agent Builder** | Give your agents safe, scoped, auditable access to authenticated web data |
| **Data Startup** | Build "Plaid for X" (utilities, insurance, healthcare) on top of Plaidify |
| **Enterprise** | Self-host for internal data aggregation with compliance controls |

---

## ✅ What's Built vs. What's Not

We believe in being transparent about where this project stands.

### Built and Working

| Component | Details |
|-----------|---------|
| **REST API** | FastAPI with full Swagger docs at `/docs` |
| **User Authentication** | Registration, login, JWT tokens, OAuth2 placeholder |
| **Link Token Flow** | Multi-step Plaid-style: `create_link` → `submit_credentials` → `fetch_data` |
| **Credential Encryption** | Fernet symmetric encryption at rest. No hardcoded keys. |
| **Blueprint System** | JSON blueprints + Python connector plugins (`BaseConnector`) |
| **Database** | SQLAlchemy ORM with Alembic migrations. SQLite (dev) / PostgreSQL (prod) |
| **Configuration** | Pydantic Settings — all config via env vars. Fails fast if secrets missing. |
| **Error Handling** | Custom exception hierarchy → structured JSON error responses |
| **Structured Logging** | JSON format (production) and colored text (development) |
| **Health Check** | `GET /health` — DB connectivity, version, system status |
| **CI/CD** | GitHub Actions: lint (ruff), test (Python 3.9–3.12), security audit, Docker build |
| **Docker** | Multi-stage build, non-root user, container health check |
| **Test Suite** | 53 tests, 80% code coverage, user isolation verified |
| **Link/Token Management** | Full CRUD — list, create, delete links and tokens per user |

### Not Built Yet

| Component | Phase | What's Missing |
|-----------|-------|----------------|
| **Browser Engine** | 1 | The core engine uses stub responses. Playwright integration needed for real site login. |
| **Real Blueprints** | 1 | Only demo/mock test blueprints exist. No real-world site connectors yet. |
| **MFA Support** | 1 | No OTP, push notification, or security question handling. |
| **Data Type System** | 1 | No currency/date/table parsing or pagination. |
| **Python SDK** | 2 | No `pip install plaidify` yet. |
| **JavaScript SDK** | 2 | No `npm install plaidify` yet. |
| **Plaidify Link UI** | 2 | No embeddable frontend component. |
| **Blueprint Registry** | 2 | No community-contributed searchable registry. |
| **CLI Tool** | 2 | No `plaidify` command-line tool. |
| **Webhooks** | 2 | No real-time event notifications. |
| **AI Agent Protocol** | 3 | No MCP server, consent model, or agent SDK. |
| **Write Actions** | 4 | No form filling, bill payment, or submissions. |
| **Enterprise** | 5 | No multi-tenant, SSO, SOC 2, or admin dashboard. |

---

## 🚀 Quick Start

### Prerequisites

- Python 3.9+
- pip

### Setup

```bash
# Clone
git clone https://github.com/meetpandya27/plaidify.git
cd plaidify

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env and set ENCRYPTION_KEY and JWT_SECRET_KEY
# (instructions are in the file)

# Run database migrations
alembic upgrade head

# Start the server
uvicorn src.main:app --reload

# Open Swagger docs
open http://127.0.0.1:8000/docs
```

### Docker

```bash
cp .env.example .env
# Fill in ENCRYPTION_KEY and JWT_SECRET_KEY
docker compose up --build
```

---

## 📖 API Reference

### Quick Test

```bash
# Simple connection (no auth required)
curl -X POST http://localhost:8000/connect \
  -H "Content-Type: application/json" \
  -d '{"site": "demo_site", "username": "demo_user", "password": "secret123"}'
```

```json
{
  "status": "connected",
  "data": {
    "profile_status": "active",
    "last_synced": "2025-04-17T12:00:00Z"
  }
}
```

> **Note:** This returns simulated data. Real browser-driven extraction comes in Phase 1.

### Link Token Flow (Plaid-style)

```bash
# 1. Register & get JWT
TOKEN=$(curl -s -X POST http://localhost:8000/auth/register \
  -H "Content-Type: application/json" \
  -d '{"username":"myuser","email":"me@example.com","password":"securepass123"}' \
  | jq -r '.access_token')

# 2. Create a link
LINK=$(curl -s -X POST "http://localhost:8000/create_link?site=demo_site" \
  -H "Authorization: Bearer $TOKEN" | jq -r '.link_token')

# 3. Submit credentials (encrypted at rest)
ACCESS=$(curl -s -X POST \
  "http://localhost:8000/submit_credentials?link_token=$LINK&username=demo_user&password=secret123" \
  -H "Authorization: Bearer $TOKEN" | jq -r '.access_token')

# 4. Fetch data
curl -s "http://localhost:8000/fetch_data?access_token=$ACCESS" \
  -H "Authorization: Bearer $TOKEN" | jq
```

### All Endpoints

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/` | GET | — | Welcome + version |
| `/health` | GET | — | System health + DB check |
| `/status` | GET | — | Alive check |
| `/connect` | POST | — | One-step connect + extract |
| `/disconnect` | POST | — | End session |
| `/create_link` | POST | JWT | Create link token for a site |
| `/submit_credentials` | POST | JWT | Submit encrypted credentials |
| `/submit_instructions` | POST | JWT | Attach processing instructions |
| `/fetch_data` | GET | JWT | Fetch data via access token |
| `/links` | GET | JWT | List your links |
| `/links/{token}` | DELETE | JWT | Delete a link |
| `/tokens` | GET | JWT | List your access tokens |
| `/tokens/{token}` | DELETE | JWT | Delete an access token |
| `/auth/register` | POST | — | Create account |
| `/auth/token` | POST | — | Login → JWT |
| `/auth/me` | GET | JWT | Your profile |
| `/auth/oauth2` | POST | — | OAuth2 login (placeholder) |

Interactive docs: `http://localhost:8000/docs`

---

## 🧩 Blueprints

### JSON Blueprint

Drop a `.json` file in `/connectors/` — no code changes needed:

```json
{
  "name": "Demo Site",
  "login_url": "https://demo.example.com/login",
  "fields": {
    "username": "#user",
    "password": "#pass",
    "submit": "#login-btn"
  },
  "post_login": [
    { "wait": "#dashboard" },
    {
      "extract": {
        "profile_status": "#status",
        "last_synced": "#last-sync"
      }
    }
  ]
}
```

### Python Connector

For custom logic beyond JSON:

```python
from src.core.connector_base import BaseConnector

class MySiteConnector(BaseConnector):
    def connect(self, username: str, password: str) -> dict:
        return {"status": "connected", "data": {"balance": 4521.30}}
```

Save as `connectors/my_site_connector.py` — auto-discovered on startup.

---

## 📁 Project Structure

```
plaidify/
├── src/
│   ├── main.py              # FastAPI app & all endpoints
│   ├── config.py            # Pydantic Settings (env var config)
│   ├── database.py          # SQLAlchemy models + Fernet encryption
│   ├── models.py            # Request/response schemas
│   ├── exceptions.py        # Custom exception hierarchy
│   ├── logging_config.py    # JSON/text structured logging
│   └── core/
│       ├── engine.py        # Connection engine (stub → Playwright)
│       └── connector_base.py # Base class for Python connectors
├── connectors/              # JSON blueprints + Python connectors
├── alembic/                 # Database migrations
├── tests/                   # 53 tests, 80% coverage
├── .github/workflows/       # CI pipeline
├── .env.example             # Required env vars template
├── Dockerfile               # Multi-stage, non-root
├── docker-compose.yml
├── requirements.txt
└── pyproject.toml           # Linter + test config
```

---

## 🔐 Security

| Practice | Status |
|----------|--------|
| No hardcoded secrets | ✅ App fails to start without env vars |
| Credential encryption at rest | ✅ Fernet symmetric encryption |
| JWT authentication | ✅ Signed tokens with expiry |
| User data isolation | ✅ Tested — users can't access others' data |
| Non-root Docker | ✅ Runs as unprivileged user |
| Dependency auditing | ✅ pip-audit in CI |
| Input validation | ✅ Pydantic with password min length |
| Credential vaulting | ❌ Planned (HashiCorp Vault, etc.) |
| Rate limiting | ❌ Planned |
| SOC 2 compliance | ❌ Planned |

---

## 🗺️ Roadmap

See [docs/PRODUCT_PLAN.md](docs/PRODUCT_PLAN.md) for the full 56-week plan with architecture diagrams.

| Phase | Focus | Status |
|-------|-------|--------|
| **0** | Foundation — security, config, testing, CI, migrations | ✅ Complete |
| **1** | Browser engine (Playwright), MFA, real blueprints | 🔲 Next |
| **2** | SDKs, Plaidify Link UI, blueprint registry, CLI | 🔲 Planned |
| **3** | AI agent protocol (MCP), consent/scoping, audit trails | 🔲 Planned |
| **4** | Write operations — pay bills, fill forms, submit apps | 🔲 Planned |
| **5** | Enterprise — multi-tenant, SSO, SOC 2, admin console | 🔲 Planned |

---

## 🤝 Contributing

Contributions welcome — from typo fixes to building the browser engine.

```bash
# Setup
git clone https://github.com/YOUR_USERNAME/plaidify.git && cd plaidify
pip install -r requirements.txt
cp .env.example .env  # Fill in secrets

# Test
pytest tests/ -v

# Lint
ruff check src/ tests/
```

### Good First Issues

- Write a JSON blueprint for a public test site
- Add unit tests for edge cases in auth endpoints
- Improve error messages in the engine
- Add request logging middleware with correlation IDs
- Create a CLI to test blueprints locally

See [CONTRIBUTING.md](CONTRIBUTING.md) for full guidelines.

---

## 📄 License

[MIT](LICENSE) — use it anywhere.

---

<p align="center">
  Built by <a href="https://github.com/meetpandya27">@meetpandya27</a>
  <br />
  Giving every developer and AI agent a secure, unified way to access data behind login forms.
</p>
