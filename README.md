<p align="center">
  <br />
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://img.shields.io/badge/🔓_Plaidify-Open_Source_Web_Auth_Gateway-8B5CF6?style=for-the-badge&labelColor=1a1a2e">
    <img src="https://img.shields.io/badge/🔓_Plaidify-Open_Source_Web_Auth_Gateway-8B5CF6?style=for-the-badge&labelColor=1a1a2e" alt="Plaidify">
  </picture>
  <br /><br />
  <strong>The missing infrastructure between AI agents and the authenticated web.</strong>
  <br />
  Give any app or agent a REST API to log in, read data, and take actions on <em>any</em> website — <br />
  banks, utilities, portals, government sites — without writing a single scraper.
  <br /><br />
  <a href="#-try-the-demo">Try the Demo</a> &nbsp;·&nbsp;
  <a href="#-30-second-quickstart">Quickstart</a> &nbsp;·&nbsp;
  <a href="docs/AGENTS.md">Agent Integration</a> &nbsp;·&nbsp;
  <a href="#-api-reference">API Docs</a> &nbsp;·&nbsp;
  <a href="docs/PRODUCT_PLAN.md">Roadmap</a> &nbsp;·&nbsp;
  <a href="https://github.com/meetpandya27/plaidify/issues">Report a Bug</a>
</p>

<p align="center">
  <a href="https://github.com/meetpandya27/plaidify/actions/workflows/ci.yml"><img src="https://github.com/meetpandya27/plaidify/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/meetpandya27/plaidify/stargazers"><img src="https://img.shields.io/github/stars/meetpandya27/plaidify?style=social" alt="Stars"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green.svg" alt="MIT License"></a>
  <img src="https://img.shields.io/badge/python-3.9%2B-3776AB.svg?logo=python&logoColor=white" alt="Python 3.9+">
  <img src="https://img.shields.io/badge/MCP-coming_soon-blueviolet?logo=data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIyNCIgaGVpZ2h0PSIyNCIgdmlld0JveD0iMCAwIDI0IDI0IiBmaWxsPSJ3aGl0ZSI+PGNpcmNsZSBjeD0iMTIiIGN5PSIxMiIgcj0iMTAiLz48L3N2Zz4=" alt="MCP">
  <img src="https://img.shields.io/badge/PRs-welcome-brightgreen.svg" alt="PRs Welcome">
</p>

---

## The Problem

Bank balances, utility bills, insurance claims, medical records, academic transcripts, government portals — the most useful data on the web sits behind login forms with no APIs.

Services like Plaid cover banking and charge $500+/month. Everything else? You’re writing brittle Selenium scripts or paying per-connection fees to closed-source vendors.

Plaidify is an attempt to fix this: one JSON blueprint per site, one REST API for everything.

---

## How It Works

```
┌──────────────────┐        ┌─────────────────────────────────────┐
│                  │        │            Plaidify                  │
│   Your App /     │  POST  │                                     │
│   AI Agent /     ├───────►│  1. Load Blueprint (JSON or Python) │
│   MCP Client     │        │  2. Launch Browser (Playwright)     │
│                  │◄───────┤  3. Authenticate & Extract Data     │
│                  │  JSON  │  4. Return Structured Response      │
│                  │        │                                     │
└──────────────────┘        └─────────────────────────────────────┘
```

**One call. Structured JSON out. Any website.**

```bash
curl -X POST http://localhost:8000/connect \
  -H "Content-Type: application/json" \
  -d '{"site": "greengrid_energy", "username": "demo_user", "password": "demo_pass"}'
```

```json
{
  "status": "connected",
  "data": {
    "current_bill": "$142.57",
    "usage_kwh": "1,247 kWh",
    "account_status": "Active",
    "service_address": "742 Evergreen Terrace, Springfield, IL 62704",
    "plan_name": "Green Choice 100",
    "usage_history": [
      { "month": "March 2026", "kwh": "1,247", "cost": "$142.57" },
      { "month": "February 2026", "kwh": "1,389", "cost": "$158.83" }
    ]
  }
}
```

---

## Why Not Just Use Plaid?

| | **Plaid** | **Plaidify** |
|---|---|---|
| **Cost** | $500+/mo, per-connection fees | **Free forever** (MIT) |
| **Coverage** | Banks & financial only | **Any website with a login form** |
| **Self-hosted** | No | **Yes** — your infra, your data |
| **AI Agent Ready** | Not designed for agents | **MCP server, agent SDK, consent model** (Phase 3) |
| **Open Source** | No | **Yes** — audit, extend, contribute |
| **Custom Sites** | Wait for Plaid to support it | **Write a JSON blueprint in 5 minutes** |
| **Data Residency** | Their servers | **Your servers, your country** |

---

## 🤖 Built for the AI Agent Era

Plaidify isn't just another Plaid alternative. It's **infrastructure for the next generation of AI agents** that need to interact with the authenticated web.

<table>
<tr>
<td width="50%">

### For AI Agent Builders

```python
# Coming in Phase 3 — MCP Server
# Your agent connects to any site
# through a standardized protocol

# Claude, GPT, or any MCP client:
# "What's my electricity bill this month?"
# → Plaidify logs into GreenGrid Energy
# → Returns $142.57 bill + 1,247 kWh usage
# → Agent summarizes and responds
```

**Why agents need this:**
- Structured data from any authenticated site
- User consent & scoped permissions
- Credential encryption at rest (AES-256-GCM)
- Full audit trail per agent action
- Built-in rate limiting & error recovery

</td>
<td width="50%">

### For App Developers

```python
# Today — works right now
import requests

# Connect to any site with a blueprint
resp = requests.post(
    "http://localhost:8000/connect",
    json={
        "site": "greengrid_energy",
        "username": "demo_user",
        "password": "demo_pass"
    }
)
print(resp.json()["data"])
# → {"current_bill": "$142.57", "usage_kwh": "1,247 kWh", ...}
```

**Why devs love this:**
- Drop a JSON blueprint → get an API
- No Selenium/Playwright code to write
- Credential encryption handled for you (AES-256-GCM)
- Swagger docs at `/docs` out of the box
- Docker-ready, CI included

</td>
</tr>
</table>

> **📖 Full agent integration guide → [docs/AGENTS.md](docs/AGENTS.md)**

---

## 🎮 Try the Demo

See Plaidify in action with our built-in **GreenGrid Energy** demo — a fully functional utility company portal that showcases the complete extraction pipeline.

```bash
git clone https://github.com/meetpandya27/plaidify.git && cd plaidify
pip install -r requirements.txt
python run_demo.py
# → Open http://localhost:8000/ui/demo.html
```

The demo launches two servers:
- **GreenGrid Energy** portal (port 8080) — a realistic utility company site with login, dashboard, billing, and account pages
- **Plaidify API** (port 8000) — the extraction engine with an interactive demo UI

**Demo credentials:**
| Username | Password | Flow |
|----------|----------|------|
| `demo_user` | `demo_pass` | Standard login → full data extraction |
| `mfa_user` | `mfa_pass` | MFA challenge (code: `123456`) → data extraction |

**What gets extracted:** Account info, current bill, energy usage (kWh), 6 months of usage history, payment records, service address, meter ID, plan details, and customer profile — all from a single API call.

<p align="center">
  <img src="https://img.shields.io/badge/13_fields_extracted-in_one_call-22c55e?style=for-the-badge" alt="13 fields">
  <img src="https://img.shields.io/badge/MFA_flow-fully_supported-8B5CF6?style=for-the-badge" alt="MFA">
  <img src="https://img.shields.io/badge/zero_config-just_run_it-3B82F6?style=for-the-badge" alt="Zero config">
</p>

---

## ⚡ 30-Second Quickstart

### Option A: Docker (recommended)

```bash
git clone https://github.com/meetpandya27/plaidify.git && cd plaidify
cp .env.example .env     # Edit and set ENCRYPTION_KEY + JWT_SECRET_KEY
docker compose up --build
# → API live at http://localhost:8000
# → Swagger docs at http://localhost:8000/docs
```

### Option B: Local

```bash
git clone https://github.com/meetpandya27/plaidify.git && cd plaidify
pip install -r requirements.txt
cp .env.example .env     # Edit and set ENCRYPTION_KEY + JWT_SECRET_KEY
alembic upgrade head
uvicorn src.main:app --reload
```

### Try it

```bash
# Quickest way — run the interactive demo
python run_demo.py
# → Open http://localhost:8000/ui/demo.html

# Or use the API directly
curl -s http://localhost:8000/connect \
  -H "Content-Type: application/json" \
  -d '{"site": "greengrid_energy", "username": "demo_user", "password": "demo_pass"}' | jq

# Full Plaid-style link flow
TOKEN=$(curl -s -X POST http://localhost:8000/auth/register \
  -H "Content-Type: application/json" \
  -d '{"username":"dev","email":"dev@test.com","password":"securepass123"}' \
  | jq -r '.access_token')

curl -s -X POST "http://localhost:8000/create_link?site=demo_site" \
  -H "Authorization: Bearer $TOKEN" | jq
```

---

## 🧩 Blueprints — The Core Idea

A **blueprint** is a tiny JSON file that teaches Plaidify how to log into a specific website. No code required.

```json
{
  "name": "My Bank",
  "login_url": "https://mybank.com/login",
  "fields": {
    "username": "#email-input",
    "password": "#password-input",
    "submit": "#login-button"
  },
  "post_login": [
    { "wait": "#dashboard-loaded" },
    {
      "extract": {
        "balance": "#account-balance",
        "last_transaction": "#recent-activity .first"
      }
    }
  ]
}
```

**Drop it in `/connectors/` → restart → call the API.** That's it.

Need custom logic? Use a Python connector instead:

```python
from src.core.connector_base import BaseConnector

class MyBankConnector(BaseConnector):
    def connect(self, username: str, password: str) -> dict:
        # Custom Playwright logic, API calls, anything
        return {"status": "connected", "data": {"balance": 4521.30}}
```

> **Want to contribute a blueprint?** That's the #1 way to help. See [CONTRIBUTING.md](CONTRIBUTING.md).

---

## 📖 API Reference

### Core Endpoints

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/connect` | POST | — | One-step connect & extract data |
| `/disconnect` | POST | — | End a session |
| `/health` | GET | — | System health + DB status |

### Link Token Flow (Plaid-style)

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/create_link` | POST | JWT | Create a link token for a site |
| `/submit_credentials` | POST | JWT | Submit credentials (encrypted at rest) |
| `/submit_instructions` | POST | JWT | Attach processing instructions |
| `/fetch_data` | GET | JWT | Fetch extracted data |
| `/links` | GET | JWT | List your links |
| `/links/{token}` | DELETE | JWT | Delete a link |
| `/tokens` | GET | JWT | List your access tokens |
| `/tokens/{token}` | DELETE | JWT | Delete an access token |

### Auth

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/auth/register` | POST | Create account |
| `/auth/token` | POST | Login → JWT |
| `/auth/me` | GET | Your profile |
| `/auth/oauth2` | POST | OAuth2 login (placeholder) |

**Interactive Swagger docs:** `http://localhost:8000/docs`

---

## 🔐 Security Model

We treat credential handling as the #1 priority.

| Practice | Status |
|----------|--------|
| AES-256-GCM encryption at rest | ✅ |
| No hardcoded secrets — app fails to start without env vars | ✅ |
| JWT auth with signed tokens + expiry | ✅ |
| User data isolation (tested & verified) | ✅ |
| Non-root Docker container | ✅ |
| Dependency auditing in CI (pip-audit) | ✅ |
| Input validation (Pydantic, password min length) | ✅ |
| Rate limiting | 🔜 Phase 2 |
| Credential vaulting (HashiCorp Vault) | 🔜 Phase 3 |
| SOC 2 compliance | 🔜 Phase 5 |

---

## 📊 Current Status — Honest & Transparent

> **We believe in building in public.** Here's exactly what works and what doesn't.

### ✅ Production-Ready

| Component | What it does |
|-----------|-------------|
| **REST API** | FastAPI with 19 endpoints, full Swagger docs |
| **Auth System** | Register, login, JWT tokens, OAuth2 placeholder |
| **Link Token Flow** | Plaid-style multi-step: create_link → submit_credentials → fetch_data |
| **Credential Encryption** | AES-256-GCM authenticated encryption, no plaintext storage |
| **Blueprint System V2** | JSON blueprints with typed extraction, list extractors, cleanup steps |
| **Browser Engine** | Real Playwright automation — headless Chromium, browser pooling, step execution |
| **MFA Handling** | Async event-based MFA manager — detects challenges, pauses for user input |
| **Data Extraction** | Typed field extraction (text, currency, date, sensitive), list/table extraction |
| **Database** | SQLAlchemy ORM, Alembic migrations, SQLite/PostgreSQL |
| **Security** | AES-256-GCM (OWASP-recommended AEAD), no plaintext credentials |
| **Configuration** | Pydantic Settings, env vars, fails fast if misconfigured |
| **CI/CD** | GitHub Actions: lint, test (3.9–3.12 matrix), security audit, Docker build |
| **Test Suite** | 98 tests across 8 suites, covering engine, blueprints, MFA, API, auth |
| **Docker** | Multi-stage build, non-root user, health check |
| **Interactive Demo** | GreenGrid Energy utility portal + dark-themed demo UI |

### 🚧 In Progress

| Component | What's Missing | Help Wanted? |
|-----------|---------------|:---:|
| **Real-World Blueprints** | Only demo blueprints exist — need community-contributed blueprints for real sites | **🔥 Yes** |
| **Python & JS SDKs** | Client libraries for easier integration | Yes |
| **Plaidify Link UI** | Embeddable drop-in widget (like Plaid Link) | Yes |
| **Blueprint Registry** | Searchable catalog of community blueprints | Yes |

### 🗺️ Planned

| Phase | Focus | Timeline |
|-------|-------|----------|
| **0** | ✅ ~~Foundation hardening, security, CI/CD, tests~~ | **Complete** |
| **1** | ✅ ~~Real browser engine (Playwright), MFA, blueprints, demo~~ | **Complete** |
| **2** | Python & JS SDKs, Plaidify Link UI, CLI, blueprint registry | **Weeks 1-3** (Mar 17 – Apr 4) |
| **3** | MCP server, AI agent SDK, consent engine, audit trails | **Weeks 3-5** (Mar 31 – Apr 18) |
| **4** | Write operations — pay bills, fill forms, action framework | **Weeks 5-7** (Apr 14 – May 2) |
| **5** | Enterprise — multi-tenant, K8s, SSO, admin console, **v1.0** 🚀 | **Weeks 7-10** (Apr 28 – May 23) |

> 📋 **Full 10-week execution plan → [docs/PRODUCT_PLAN.md](docs/PRODUCT_PLAN.md)**

---

## 🏗️ Architecture

```
plaidify/
├── src/
│   ├── main.py              # FastAPI app — all 19 endpoints
│   ├── config.py            # Pydantic Settings — env var config
│   ├── database.py          # SQLAlchemy + AES-256-GCM encryption
│   ├── models.py            # Request/response Pydantic schemas
│   ├── exceptions.py        # Custom error hierarchy (15 types)
│   ├── logging_config.py    # JSON (prod) / colored text (dev) logging
│   └── core/
│       ├── engine.py        # Playwright browser engine + blueprint executor
│       └── connector_base.py # Base class for Python connectors
├── connectors/              # Drop JSON blueprints here
│   ├── greengrid_energy.json # GreenGrid Energy demo blueprint
│   └── test_bank.json       # Legacy test blueprint
├── example_site/            # GreenGrid Energy fake utility portal
│   └── server.py            # FastAPI app simulating a utility company
├── frontend/                # Demo UI assets
│   ├── demo.html            # Interactive demo widget
│   ├── demo.css             # Dark theme styles
│   └── demo.js              # Client-side connection flow logic
├── alembic/                 # Database migrations
├── tests/                   # 98 tests across 8 suites
├── run_demo.py              # One-command demo launcher
├── .github/workflows/       # CI: lint → test → audit → docker
├── Dockerfile               # Multi-stage, non-root
├── docker-compose.yml       # One-command dev environment
└── .env.example             # All config documented here
```

> 📖 **Full technical docs → [docs/README.md](docs/README.md)**

---

## 🤝 Contributing

We’re building open-source infrastructure for authenticated web data. Contributions welcome — especially blueprints for real sites.

### Highest-Impact Contributions Right Now

| Priority | Task | Difficulty |
|:--------:|------|:----------:|
| 🔥 | **Write real-world blueprints** — pick a public site, write the JSON | Easy |
| 🔥 | **Build the blueprint registry CLI** — search, validate, share blueprints | Medium |
| 🟡 | **Build Python/JS SDKs** — client libraries for easier integration | Medium |
| 🟡 | **Add push notification MFA** — extend MFA beyond OTP codes | Medium |
| 🟢 | **Add unit tests** — edge cases, error paths | Easy |
| 🟢 | **Improve error messages** — make failures actionable | Easy |

```bash
# Get started in 60 seconds
git clone https://github.com/YOUR_USERNAME/plaidify.git && cd plaidify
pip install -r requirements.txt
cp .env.example .env               # Set ENCRYPTION_KEY + JWT_SECRET_KEY
alembic upgrade head && pytest -v  # All 98 tests should pass
```

> 📋 **Full contributor guide → [CONTRIBUTING.md](CONTRIBUTING.md)**

---

## 🌍 Use Cases

<details>
<summary><strong>💰 Personal Finance App</strong> — Aggregate bank data without Plaid</summary>

Write blueprints for each bank your users need. Plaidify handles login, session management, and data extraction. You get structured JSON with balances, transactions, and account details.

</details>

<details>
<summary><strong>🤖 AI Financial Assistant</strong> — Let your agent check bank balances</summary>

Your agent calls the Plaidify API to securely access the user's bank portal, extract current balances, and answer questions like "Can I afford this purchase?" — with full audit trails and user consent.

</details>

<details>
<summary><strong>⚡ Utility Bill Tracker</strong> — Monitor bills across providers</summary>

Create blueprints for utility company portals. Schedule periodic data fetches. Get structured billing data without waiting for each company to build an API.

</details>

<details>
<summary><strong>🏥 Insurance & Healthcare Aggregator</strong> — Unified patient/policyholder portal</summary>

Access insurance claims, EOBs, and coverage details from provider portals. Self-hosted means full data residency compliance.

</details>

<details>
<summary><strong>🎓 Student Data Platform</strong> — Transcripts, grades, financial aid</summary>

Build integrations with university portals. Pull transcripts, grades, and financial aid information through a unified API.

</details>

<details>
<summary><strong>🏢 Enterprise Data Aggregation</strong> — Internal tool integration</summary>

Connect to internal portals, vendor dashboards, and legacy systems that lack APIs. Self-host with compliance controls and SSO.

</details>

---

## Comparison with Other Tools

| Tool | Type | Websites Supported | AI Agent Ready | Self-Hosted | Cost |
|------|------|-------------------|:--------------:|:-----------:|------|
| **Plaidify** | Infrastructure layer | **Any login-protected site** | ✅ (Phase 3) | ✅ | Free |
| Plaid | Managed service | Banks & financial only | ❌ | ❌ | $500+/mo |
| Woob | Python scrapers | ~80 French/EU sites | ❌ | ✅ | Free |
| Selenium/Playwright | Raw tools | Any (you write everything) | ❌ | ✅ | Free |
| Huginn | Ruby agents | Any (complex setup) | ❌ | ✅ | Free |

**Plaidify's sweet spot:** The abstraction of Plaid + the flexibility of Playwright + the openness of Woob, designed with AI agents in mind.

---

## Star History

If Plaidify is useful, a ⭐ helps others find it.

[![Star History Chart](https://api.star-history.com/svg?repos=meetpandya27/plaidify&type=Date)](https://star-history.com/#meetpandya27/plaidify&Date)

---

## ⚠️ Legal Disclaimer

**Plaidify is a general-purpose browser automation infrastructure tool.** It is your responsibility to ensure that your use of Plaidify complies with the Terms of Service of any website you interact with, as well as all applicable local, state, and federal laws.

- Many websites prohibit automated access in their Terms of Service. Using Plaidify with such sites may violate those terms and could result in account suspension or legal action.
- Plaidify is **not** a licensed financial data aggregator. If you use it to access banking or financial sites, you do so at your own risk. Your financial institution may not cover losses related to credentials shared with third-party tools.
- The authors and contributors of Plaidify accept **no liability** for misuse, data loss, account lockouts, or any other damages arising from use of this software.
- Always obtain explicit user consent before accessing any account on their behalf.

> **tl;dr** — This is a power tool. Use it responsibly, read the TOS of target sites, and don't do anything you wouldn't want done to your own accounts.

---

## 📄 License

[MIT](LICENSE) — use it in personal projects, startups, or enterprise. No restrictions.

---

<p align="center">
  <strong>Built by <a href="https://github.com/meetpandya27">@meetpandya27</a> and contributors</strong>
  <br />
  <sub>The open-source gateway between AI agents and the authenticated web.</sub>
  <br /><br />
  <a href="https://github.com/meetpandya27/plaidify/stargazers">⭐ Star</a> &nbsp;·&nbsp;
  <a href="https://github.com/meetpandya27/plaidify/fork">🍴 Fork</a> &nbsp;·&nbsp;
  <a href="https://github.com/meetpandya27/plaidify/issues">🐛 Issues</a> &nbsp;·&nbsp;
  <a href="docs/AGENTS.md">🤖 Agent Docs</a>
</p>
