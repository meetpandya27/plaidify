# Plaidify — Product Plan

**Version:** 1.0  
**Date:** March 14, 2026  
**Vision:** Plaidify is the open-source infrastructure layer that lets any developer turn their product into a data accessibility platform — for human users and AI agents alike.

---

## Table of Contents

1. [Product Vision & Positioning](#1-product-vision--positioning)
2. [Target Users & Use Cases](#2-target-users--use-cases)
3. [Architecture Overview](#3-architecture-overview)
4. [Phase 0 — Foundation Hardening](#4-phase-0--foundation-hardening-weeks-1-6)
5. [Phase 1 — Real Browser Engine](#5-phase-1--real-browser-engine-weeks-7-14)
6. [Phase 2 — Developer SDK & Platform](#6-phase-2--developer-sdk--platform-weeks-15-24)
7. [Phase 3 — AI Agent Protocol](#7-phase-3--ai-agent-protocol-weeks-25-34)
8. [Phase 4 — Browser Actions (Write)](#8-phase-4--browser-actions-write-operations-weeks-35-44)
9. [Phase 5 — Enterprise & Scale](#9-phase-5--enterprise--scale-weeks-45-56)
10. [Team Structure](#10-team-structure)
11. [Risk Matrix](#11-risk-matrix)
12. [Success Metrics per Phase](#12-success-metrics-per-phase)
13. [Open Source & Community Strategy](#13-open-source--community-strategy)

---

## 1. Product Vision & Positioning

### The Problem
The world's data is locked behind login forms. Millions of websites hold user data — bank balances, medical records, utility bills, academic transcripts, insurance policies — and provide no APIs. Today, if a developer wants to build an app that accesses this data on behalf of a user, they have two options: (1) pay Plaid/MX for financial data only, or (2) build fragile, one-off scrapers.

AI agents face an even worse version of this problem. They need authenticated access to websites to act on behalf of users, but there's no standard protocol for an AI to safely log in, read data, and — eventually — take actions.

### The Solution
Plaidify is **open-source infrastructure** that:

1. **For Developers:** Drop a JSON blueprint into your project → get a REST API that authenticates to any site and returns structured data. Any developer can turn their app into a data aggregation platform.

2. **For AI Agents:** A standardized protocol (MCP-compatible) that allows AI agents to safely request, receive, and act on authenticated web data with user consent.

3. **For End Users:** Their data, their choice. Plaidify is the secure pipe that lets users share their own data with the apps and agents they trust.

### Competitive Edge
| Competitor | Weakness | Plaidify's Edge |
|-----------|----------|-----------------|
| Plaid | Closed-source, expensive, financial-only | Open-source, any site, free |
| Woob | Complex Python modules, no REST API, EU-centric | JSON blueprints, API-first, global |
| Playwright/Selenium | Raw tools, no abstraction, no data model | Blueprint-driven, structured output |
| Huginn | Ruby, agent-focused, not embeddable as SDK | Python SDK, embeddable, API-first |

### One-liner
> **"Plaidify: The open-source API for authenticated web data — for developers and AI agents."**

---

## 2. Target Users & Use Cases

### User Persona 1: The App Developer
**Name:** Sarah, Full-Stack Developer  
**Goal:** She's building a personal finance app and needs to pull transaction data from 5 regional banks that have no API.

**How she uses Plaidify:**
```bash
pip install plaidify
```
```python
from plaidify import Plaidify

pfy = Plaidify()

# Her app's backend calls this when a user connects their bank
result = await pfy.connect(
    blueprint="us_regional_bank",
    credentials={"username": "user@bank.com", "password": "***"},
    extract=["transactions", "balance"]
)
# result = {"status": "connected", "data": {"balance": 4521.30, "transactions": [...]}}
```

She never writes a scraper. She just picks a blueprint from the community registry (or writes a 20-line JSON file for her specific bank) and ships.

---

### User Persona 2: The AI Agent Builder
**Name:** Marcus, AI Engineer at an agent startup  
**Goal:** He's building an AI assistant that helps users manage their insurance. The agent needs to log into the user's insurance portal, read their policy, and summarize it.

**How he uses Plaidify:**
```python
# Marcus's AI agent uses Plaidify as a tool
from plaidify.agent import PlaidifyTool

tool = PlaidifyTool(
    consent_mode="explicit",      # User must approve each access
    data_scope=["policy_summary", "premium_amount"],  # Agent can only see these fields
    session_ttl=300               # Session expires in 5 minutes
)

# The agent calls this during a conversation
data = await tool.fetch(
    blueprint="state_farm_insurance",
    user_token="usr_abc123"       # Pre-authorized by user via Plaidify Link
)
# Agent receives ONLY the scoped fields, nothing else
```

The agent never sees raw credentials. The user authorized access via a Plaidify Link flow (like Plaid Link), and the agent only sees the fields it was scoped to.

---

### User Persona 3: The Data Accessibility Startup
**Name:** Priya, CTO of a startup that aggregates utility bills  
**Goal:** She wants to build "Plaid for utilities" without building scraping infrastructure from scratch.

**How she uses Plaidify:**
```yaml
# Her docker-compose.yml
services:
  plaidify:
    image: plaidify/server:latest
    environment:
      - VAULT_URL=https://vault.company.com
      - DATABASE_URL=postgres://...
      - REDIS_URL=redis://...
      - LICENSE_KEY=...  # Optional commercial license for enterprise features
    volumes:
      - ./blueprints:/connectors  # Her proprietary blueprints
```

She self-hosts Plaidify, writes blueprints for 50 utility companies, and exposes a white-labeled API to her customers. Her entire scraping infrastructure is Plaidify.

---

### User Persona 4: The Enterprise
**Name:** A large bank's innovation team  
**Goal:** They need to aggregate customer data from 200 external sources for an open-banking product.

**Requirements:** SOC 2, audit logs, credential vaulting, rate limiting, multi-region deployment.

**How they use Plaidify:** Plaidify Enterprise (commercial license on top of the open-source core) with Vault integration, audit logging, and a management dashboard.

---

## 3. Architecture Overview

### Target Architecture (End of Phase 5)

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          PLAIDIFY PLATFORM                              │
│                                                                         │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌────────────┐ │
│  │  REST API     │  │  Python SDK  │  │  Agent Proto │  │  Link UI   │ │
│  │  (FastAPI)    │  │  (pip pkg)   │  │  (MCP/A2A)   │  │  (React)   │ │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘  └─────┬──────┘ │
│         │                 │                 │                 │         │
│         └─────────────────┼─────────────────┼─────────────────┘         │
│                           ▼                                             │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │                     ORCHESTRATION LAYER                          │   │
│  │  • Session Manager    • Consent Engine    • Rate Limiter         │   │
│  │  • Retry/Circuit Breaker   • Queue (Celery/Redis)               │   │
│  └──────────────────────────────┬──────────────────────────────────┘   │
│                                 ▼                                       │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │                      BROWSER ENGINE                              │   │
│  │  ┌──────────────┐  ┌──────────────┐  ┌───────────────────┐     │   │
│  │  │  Playwright   │  │  HTTP Client │  │  Headless Pool    │     │   │
│  │  │  Driver       │  │  Driver      │  │  (Browser Mgmt)   │     │   │
│  │  └──────────────┘  └──────────────┘  └───────────────────┘     │   │
│  └──────────────────────────────┬──────────────────────────────────┘   │
│                                 ▼                                       │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │                    BLUEPRINT REGISTRY                            │   │
│  │  • JSON Blueprints   • Python Connectors   • Community Registry │   │
│  │  • Blueprint Validator   • Auto-Generator (AI)                  │   │
│  └─────────────────────────────────────────────────────────────────┘   │
│                                                                         │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │                     DATA & SECURITY LAYER                        │   │
│  │  • PostgreSQL/SQLite  • Credential Vault  • Encryption (Fernet) │   │
│  │  • Audit Log          • Consent Records   • Session Store       │   │
│  └─────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 4. Phase 0 — Foundation Hardening (Weeks 1-6)

**Goal:** Take what exists today and make it production-quality. No new features — just make the current code rock-solid.

### 4.1 Security Overhaul
| Task | Details | Priority |
|------|---------|----------|
| Remove hardcoded secrets | `ENCRYPTION_KEY` and `JWT_SECRET_KEY` must ONLY come from env vars. Fail-fast if not set. | P0 |
| Rotate default Fernet key | Remove the default key entirely. Require explicit configuration. | P0 |
| Credential encryption at rest | Verify Fernet encryption is applied correctly. Add key rotation support. | P0 |
| Input validation | Sanitize all user inputs. Add rate limiting on auth endpoints. | P0 |
| HTTPS enforcement | Add HSTS headers, secure cookie flags. | P1 |
| Dependency audit | Run `pip-audit` and `safety check`. Pin all dependency versions. | P1 |
| CORS configuration | Lock down CORS to specific origins (currently wide open). | P1 |

### 4.2 Code Quality
| Task | Details |
|------|---------|
| Type hints everywhere | Add full type annotations to all functions and methods. |
| Docstrings | Google-style docstrings on every public function/class. |
| Remove dead code | Unreachable `return response_data` after `raise` in `/connect`. Duplicate `from fastapi import HTTPException` imports inside functions. |
| Error handling | Create custom exception hierarchy: `PlaidifyError`, `BlueprintNotFoundError`, `ConnectionFailedError`, `AuthenticationError`, etc. |
| Logging | Replace all `print` statements with structured logging (JSON format). Add correlation IDs per request. |
| Configuration | Move all config to a Pydantic `Settings` class with env var support. |

### 4.3 Testing
| Task | Details | Target |
|------|---------|--------|
| Unit tests | Test every endpoint, every model, every utility function. | 90%+ coverage |
| Integration tests | Test full `/create_link` → `/submit_credentials` → `/fetch_data` flow. | All flows covered |
| Blueprint validation tests | Test loading, parsing, and error handling for malformed blueprints. | Edge cases |
| Auth tests | Test registration, login, token expiry, invalid tokens, OAuth2 flow. | All auth paths |
| CI pipeline | GitHub Actions: lint (ruff), type check (mypy), test (pytest), security (pip-audit). | Green on every PR |

### 4.4 Database
| Task | Details |
|------|---------|
| Alembic migrations | Add Alembic for database migrations. Never use `create_all()` in production. |
| PostgreSQL support | Test and document PostgreSQL as the production database. SQLite for dev only. |
| Connection pooling | Configure SQLAlchemy connection pool size, overflow, timeout. |
| Indexes | Add indexes on frequently queried columns (`user_id`, `link_token`). |

### 4.5 DevOps
| Task | Details |
|------|---------|
| Multi-stage Dockerfile | Separate build and runtime stages. Minimize image size. |
| Health check endpoint | `GET /health` that checks DB connectivity, returns version info. |
| Graceful shutdown | Handle SIGTERM properly, drain connections. |
| Environment configs | `.env.example` with all required variables documented. |

### Deliverables
- [ ] All hardcoded secrets removed
- [ ] 90%+ test coverage
- [ ] CI pipeline passing (lint, type check, test, security)
- [ ] Alembic migrations working
- [ ] Docker image < 200MB
- [ ] `GET /health` endpoint returning system status

---

## 5. Phase 1 — Real Browser Engine (Weeks 7-14)

**Goal:** Replace the stub engine with a real browser automation layer that can actually log into websites.

### 5.1 Engine Architecture
```
Blueprint (JSON) ──▶ Step Compiler ──▶ Execution Plan ──▶ Driver (Playwright)
                                                              │
                                                              ▼
                                                        Browser Pool
                                                     (managed instances)
```

### 5.2 Playwright Integration
| Task | Details |
|------|---------|
| Install Playwright | Add `playwright` to requirements. Include browser install in Docker. |
| Browser Pool Manager | Pool of reusable browser contexts. Configure max concurrency, idle timeout, cleanup. |
| Session isolation | Each connection gets its own `BrowserContext` (cookies, storage isolated). |
| Resource blocking | Block images, fonts, analytics scripts for speed. Configurable per blueprint. |
| Proxy support | SOCKS5/HTTP proxy per connection. Rotate proxies from a pool. |
| Stealth mode | Anti-detection: randomize viewport, user-agent, WebGL fingerprint. Use `playwright-stealth`. |

### 5.3 Blueprint V2 Schema
```json
{
  "schema_version": "2.0",
  "name": "Example Bank",
  "domain": "example-bank.com",
  "tags": ["banking", "us", "regional"],
  
  "auth": {
    "type": "form",
    "steps": [
      {
        "action": "goto",
        "url": "https://example-bank.com/login"
      },
      {
        "action": "fill",
        "selector": "#username",
        "value": "{{username}}"
      },
      {
        "action": "fill",
        "selector": "#password",
        "value": "{{password}}"
      },
      {
        "action": "click",
        "selector": "#login-btn"
      },
      {
        "action": "wait",
        "selector": "#dashboard",
        "timeout": 10000
      }
    ]
  },

  "mfa": {
    "detection": {
      "selector": "#otp-input",
      "timeout": 3000
    },
    "type": "otp_input",
    "handler": "user_prompt"
  },

  "extract": {
    "balance": {
      "selector": "#account-balance",
      "type": "currency",
      "transform": "strip_dollar_sign"
    },
    "transactions": {
      "selector": "#transaction-table tbody tr",
      "type": "list",
      "fields": {
        "date": { "selector": "td:nth-child(1)", "type": "date" },
        "description": { "selector": "td:nth-child(2)", "type": "text" },
        "amount": { "selector": "td:nth-child(3)", "type": "currency" }
      }
    },
    "account_number": {
      "selector": "#account-num",
      "type": "text",
      "sensitive": true
    }
  },

  "cleanup": {
    "steps": [
      { "action": "click", "selector": "#logout-btn" }
    ]
  },

  "rate_limit": {
    "max_requests_per_hour": 10,
    "min_interval_seconds": 30
  },

  "health_check": {
    "url": "https://example-bank.com",
    "expected_status": 200
  }
}
```

### 5.4 Step Executor
| Step Type | Description |
|-----------|-------------|
| `goto` | Navigate to URL. Wait for network idle. |
| `fill` | Type text into input. Support `{{variable}}` interpolation. |
| `click` | Click element. Wait for navigation if needed. |
| `wait` | Wait for selector to appear. Configurable timeout. |
| `screenshot` | Capture page state (for debugging, never stored in production). |
| `extract` | Pull data from the page using selectors. Apply type transforms. |
| `conditional` | If selector exists, branch to different steps (for MFA, error pages). |
| `scroll` | Scroll to element or position (for lazy-loaded content). |
| `select` | Choose option from dropdown. |
| `iframe` | Switch context into an iframe. |
| `wait_for_navigation` | Wait for page load after form submission. |
| `execute_js` | Run JavaScript in page context (escape hatch, use sparingly). |

### 5.5 Data Extraction & Normalization
| Feature | Details |
|---------|---------|
| Type system | `text`, `currency`, `date`, `number`, `email`, `phone`, `list`, `table` |
| Transforms | `strip_whitespace`, `strip_dollar_sign`, `parse_date(format)`, `to_lowercase`, `regex_extract(pattern)` |
| Sensitive fields | Fields marked `sensitive: true` are encrypted in transit and never logged. |
| Pagination | Support `next_page` action to extract across multiple pages. |

### 5.6 Error Handling
| Scenario | Response |
|----------|----------|
| Login failed (wrong creds) | `{"status": "auth_failed", "error": "Invalid credentials"}` |
| MFA required | `{"status": "mfa_required", "mfa_type": "otp", "session_id": "..."}` |
| Site unreachable | `{"status": "site_error", "error": "Connection timeout"}` |
| Blueprint broken | `{"status": "blueprint_error", "error": "Selector #dashboard not found"}` |
| Rate limited by site | `{"status": "rate_limited", "retry_after": 60}` |
| CAPTCHA detected | `{"status": "captcha_required", "captcha_type": "recaptcha"}` |

### 5.7 MFA Support (Basic)
| MFA Type | Handling |
|----------|----------|
| OTP (SMS/Authenticator) | Return `mfa_required` status. Client submits OTP via `POST /mfa/submit`. Engine enters it and continues. |
| Email code | Same as OTP, user retrieves code from email and submits. |
| Security questions | Blueprint defines question selectors. Engine returns question text. Client submits answer. |
| Push notification | Return `mfa_required` with `type: push`. Engine polls for page change (user approves on phone). |

### Deliverables
- [ ] Playwright engine replaces stub logic
- [ ] Browser pool manager with configurable concurrency
- [ ] Blueprint V2 schema with full step vocabulary
- [ ] At least 5 working blueprints for real public sites (demo/test sites)
- [ ] MFA flow working for OTP
- [ ] Data extraction with type system and transforms
- [ ] Error handling for all failure scenarios
- [ ] Performance: < 10s average connection time, < 500MB memory per 10 concurrent sessions

---

## 6. Phase 2 — Developer SDK & Platform (Weeks 15-24)

**Goal:** Make Plaidify embeddable. A developer installs our SDK and integrates in under 30 minutes.

### 6.1 Python SDK (`pip install plaidify`)
```python
from plaidify import Plaidify, PlaidifyConfig

# Initialize
pfy = Plaidify(
    config=PlaidifyConfig(
        server_url="https://your-plaidify-instance.com",  # Or self-hosted
        api_key="pk_live_...",
        # OR run embedded (no server needed):
        mode="embedded",  # Runs Playwright locally
    )
)

# Simple connection
result = await pfy.connect(
    blueprint="chase_bank",
    credentials={"username": "user", "password": "pass"}
)

# With MFA callback
async def handle_mfa(mfa_request):
    code = input(f"Enter your {mfa_request.type} code: ")
    return code

result = await pfy.connect(
    blueprint="chase_bank",
    credentials={"username": "user", "password": "pass"},
    mfa_handler=handle_mfa
)

# Link flow (Plaid-style)
link = await pfy.create_link(site="chase_bank", user_id="usr_123")
# → Returns link_url that you embed in your frontend

# After user completes the link:
data = await pfy.fetch(link_token=link.token, extract=["balance", "transactions"])
```

### 6.2 JavaScript/TypeScript SDK (`npm install plaidify`)
```typescript
import { Plaidify } from 'plaidify';

const pfy = new Plaidify({
  serverUrl: 'https://your-plaidify-instance.com',
  apiKey: 'pk_live_...',
});

// Create a link for frontend embedding
const link = await pfy.createLink({
  site: 'chase_bank',
  userId: 'usr_123',
  redirectUrl: 'https://yourapp.com/callback',
});

// In your frontend:
// <PlaidifyLink token={link.token} onSuccess={handleSuccess} />
```

### 6.3 Plaidify Link (Frontend Component)
Embeddable UI component (like Plaid Link) that handles the credential flow securely:

```
┌──────────────────────────────────────────┐
│          Connect Your Account            │
│                                          │
│  ┌────────────────────────────────────┐  │
│  │  Search for your bank or site...   │  │
│  └────────────────────────────────────┘  │
│                                          │
│  🏦 Chase Bank                           │
│  🏦 Bank of America                      │
│  🏦 Wells Fargo                          │
│  📱 T-Mobile                             │
│  ⚡ PG&E                                 │
│                                          │
│  ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─   │
│                                          │
│  Username: [___________________]         │
│  Password: [___________________]         │
│                                          │
│         [ Connect Securely ]             │
│                                          │
│  🔒 Powered by Plaidify (open source)   │
│  Your credentials are encrypted and      │
│  never stored on the developer's server. │
└──────────────────────────────────────────┘
```

**Key properties:**
- Renders in an iframe/modal on the developer's site
- Credentials go directly to the Plaidify server, never touch the developer's backend
- Shows real-time connection progress
- Handles MFA inline
- Returns a `link_token` to the developer's callback

### 6.4 Blueprint Registry
| Feature | Details |
|---------|---------|
| Community registry | Public registry at `registry.plaidify.dev` where anyone can publish blueprints |
| CLI tool | `plaidify registry search "bank"`, `plaidify registry install chase_bank` |
| Blueprint validation | Automated testing: does the blueprint's login URL resolve? Are selectors valid? |
| Versioning | Blueprints have semver versions. Breaking changes = major version bump. |
| Auto-update | SDK can auto-update blueprints (with pinning support). |
| Quality tiers | `community` (unverified), `tested` (CI-validated), `certified` (manually reviewed) |

### 6.5 CLI Tool
```bash
# Initialize a new Plaidify project
plaidify init

# Create a new blueprint interactively
plaidify blueprint create
# → Opens browser, you click elements, it generates the JSON

# Test a blueprint locally
plaidify blueprint test chase_bank --username test --password test

# Validate blueprint syntax
plaidify blueprint validate ./connectors/chase_bank.json

# Start local server
plaidify serve --port 8000

# Publish blueprint to registry
plaidify registry publish ./connectors/chase_bank.json
```

### 6.6 Webhook System
```python
# Developer registers webhooks
POST /webhooks/register
{
  "url": "https://yourapp.com/plaidify-webhook",
  "events": ["connection.success", "connection.failed", "connection.mfa_required", "data.updated"],
  "secret": "whsec_..."
}
```

Events:
| Event | When |
|-------|------|
| `connection.success` | Login succeeded, data extracted |
| `connection.failed` | Login failed (bad creds, site down, etc.) |
| `connection.mfa_required` | MFA challenge detected, awaiting user input |
| `connection.expired` | Session expired, re-auth needed |
| `data.updated` | Scheduled refresh found new data |
| `blueprint.deprecated` | A blueprint the developer uses is being retired |

### 6.7 Scheduled Data Refresh
```python
# Developer sets up recurring data pulls
link = await pfy.create_link(
    site="chase_bank",
    user_id="usr_123",
    refresh_schedule="daily",  # or "hourly", "weekly", cron expression
    webhook_url="https://yourapp.com/webhook"
)
```

### Deliverables
- [ ] Python SDK published to PyPI
- [ ] JavaScript SDK published to npm
- [ ] Plaidify Link frontend component (React, vanilla JS)
- [ ] Blueprint Registry with search, install, publish
- [ ] CLI tool for blueprint development and testing
- [ ] Webhook system with retry logic
- [ ] Scheduled refresh with configurable intervals
- [ ] Documentation site (Docusaurus/MkDocs) with quickstarts, guides, API reference
- [ ] 25+ working blueprints in the registry

---

## 7. Phase 3 — AI Agent Protocol (Weeks 25-34)

**Goal:** Make Plaidify the standard way AI agents access authenticated web data. Safe, scoped, auditable.

### 7.1 The Problem for AI Agents
AI agents today have no safe way to access user data behind login walls. Current approaches:
- **Give the agent your password** — catastrophic security risk
- **Screen sharing / computer use** — slow, brittle, no data structure
- **Custom API integrations** — doesn't scale, most sites have no API

Plaidify solves this: the user authorizes access once via Plaidify Link, and the agent uses a scoped, time-limited token to fetch specific data fields.

### 7.2 Consent & Scoping Model
```
┌─────────────────────────────────────────────────────────────┐
│                    USER CONSENT FLOW                         │
│                                                              │
│  1. Agent requests: "I need your bank balance and           │
│     last 30 days of transactions from Chase"                │
│                              │                               │
│                              ▼                               │
│  2. Plaidify shows consent screen to user:                  │
│     ┌──────────────────────────────────────┐                │
│     │  "BudgetBot" wants to access:        │                │
│     │                                       │                │
│     │  ✅ Account balance                   │                │
│     │  ✅ Transactions (last 30 days)       │                │
│     │  ❌ Account number (not requested)    │                │
│     │  ❌ Personal info (not requested)     │                │
│     │                                       │                │
│     │  Access expires: 24 hours             │                │
│     │                                       │                │
│     │  [ Deny ]     [ Allow ]               │                │
│     └──────────────────────────────────────┘                │
│                              │                               │
│  3. User approves → Agent receives scoped token             │
│  4. Agent can ONLY read approved fields                     │
│  5. Token expires automatically                              │
└─────────────────────────────────────────────────────────────┘
```

### 7.3 Agent SDK
```python
from plaidify.agent import PlaidifyAgent

agent_client = PlaidifyAgent(
    agent_id="agent_budgetbot_123",
    agent_name="BudgetBot",
    api_key="pk_agent_...",
    consent_mode="explicit",  # Always ask user
)

# Request access (triggers user consent flow)
consent = await agent_client.request_access(
    user_id="usr_123",
    site="chase_bank",
    scopes=["balance", "transactions"],
    duration="24h",
    reason="To analyze your spending patterns"  # Shown to user
)

# After user approves:
if consent.approved:
    data = await agent_client.fetch(
        consent_token=consent.token,
        extract=["balance", "transactions"]
    )
    # data.balance = 4521.30
    # data.transactions = [{"date": "...", "description": "...", "amount": ...}, ...]
    
    # Attempting to access a field not in scope raises ScopeViolationError
    data = await agent_client.fetch(
        consent_token=consent.token,
        extract=["account_number"]  # ❌ Not in approved scope
    )
    # → raises plaidify.ScopeViolationError
```

### 7.4 MCP (Model Context Protocol) Server
Plaidify runs as an MCP server that any MCP-compatible AI agent can use:

```json
{
  "name": "plaidify",
  "version": "1.0",
  "tools": [
    {
      "name": "plaidify_request_access",
      "description": "Request user consent to access data from an authenticated website",
      "parameters": {
        "site": "string — The site blueprint name",
        "scopes": "string[] — Data fields to request access to",
        "duration": "string — How long access should last",
        "reason": "string — Why the agent needs this data (shown to user)"
      }
    },
    {
      "name": "plaidify_fetch_data",
      "description": "Fetch authorized data from a connected site",
      "parameters": {
        "consent_token": "string — Token from approved access request",
        "fields": "string[] — Which approved fields to retrieve"
      }
    },
    {
      "name": "plaidify_list_connections",
      "description": "List the user's active Plaidify connections",
      "parameters": {
        "user_id": "string"
      }
    }
  ]
}
```

### 7.5 Google A2A (Agent-to-Agent) Support
```json
{
  "name": "Plaidify Data Agent",
  "description": "Securely access authenticated web data on behalf of users",
  "capabilities": ["data_retrieval", "authenticated_browsing"],
  "authentication": {
    "type": "oauth2",
    "flows": ["authorization_code"]
  },
  "skills": [
    {
      "name": "fetch_financial_data",
      "description": "Retrieve bank balances, transactions from connected accounts"
    },
    {
      "name": "fetch_utility_data", 
      "description": "Retrieve utility bills, usage data from connected providers"
    }
  ]
}
```

### 7.6 Audit Trail
Every agent access is logged:
```json
{
  "event_id": "evt_abc123",
  "timestamp": "2026-03-14T12:00:00Z",
  "agent_id": "agent_budgetbot_123",
  "agent_name": "BudgetBot",
  "user_id": "usr_123",
  "action": "fetch_data",
  "site": "chase_bank",
  "fields_requested": ["balance", "transactions"],
  "fields_returned": ["balance", "transactions"],
  "consent_token": "ct_xyz789",
  "consent_expires": "2026-03-15T12:00:00Z",
  "ip_address": "203.0.113.1",
  "duration_ms": 3400,
  "status": "success"
}
```

Users can view their audit trail:
```
GET /user/audit-log?user_id=usr_123

[
  "BudgetBot accessed your Chase balance 2 hours ago",
  "BudgetBot accessed your Chase transactions 2 hours ago",
  "TaxHelper requested access to your ADP payroll — DENIED by you"
]
```

### 7.7 Agent Safety Guardrails
| Guardrail | Details |
|-----------|---------|
| Scope enforcement | Agents can ONLY access fields they were approved for. Server-side enforcement. |
| Time-limited tokens | All consent tokens expire. Max 30 days. |
| Rate limiting | Per-agent, per-user rate limits. Prevent abuse. |
| Data redaction | Sensitive fields (SSN, account numbers) require elevated consent. |
| Anomaly detection | Flag unusual patterns: agent requesting all users' data, accessing at 3am, etc. |
| Kill switch | User can revoke all agent access instantly via dashboard. |
| Agent verification | Agents must register and verify identity before accessing production data. |

### Deliverables
- [ ] Agent SDK (Python) with consent flow
- [ ] MCP server implementation
- [ ] A2A agent card
- [ ] Consent UI (user-facing approval screen)
- [ ] Audit trail with user-facing dashboard
- [ ] Scope enforcement engine
- [ ] Agent registration and verification
- [ ] Rate limiting per agent
- [ ] 3 demo agents (finance assistant, tax helper, insurance analyzer)
- [ ] Security audit by external firm

---

## 8. Phase 4 — Browser Actions / Write Operations (Weeks 35-44)

**Goal:** Go beyond reading data. Let developers and agents take actions on websites — fill forms, submit payments, upload documents — with user authorization.

### 8.1 Action Blueprint Schema
```json
{
  "schema_version": "3.0",
  "name": "Pay Utility Bill",
  "domain": "pge.com",
  "type": "action",
  
  "auth": { "...same as v2..." },
  
  "actions": {
    "pay_bill": {
      "description": "Pay the current utility bill",
      "requires_consent": "explicit_per_action",
      "parameters": {
        "payment_method": {
          "type": "enum",
          "values": ["bank_account", "credit_card"],
          "required": true
        },
        "amount": {
          "type": "currency",
          "required": true,
          "max": 10000
        }
      },
      "steps": [
        { "action": "goto", "url": "https://pge.com/pay" },
        { "action": "select", "selector": "#payment-method", "value": "{{payment_method}}" },
        { "action": "fill", "selector": "#amount", "value": "{{amount}}" },
        { "action": "click", "selector": "#pay-now" },
        { "action": "wait", "selector": "#confirmation" },
        { "action": "extract", "fields": {
            "confirmation_number": { "selector": "#conf-num" },
            "amount_paid": { "selector": "#amount-paid" }
          }
        }
      ],
      "confirmation": {
        "require_user_approval_before_submit": true,
        "screenshot_before_submit": true,
        "fields_to_show_user": ["amount", "payment_method"]
      }
    }
  }
}
```

### 8.2 Action Consent Model (Stricter than Read)
```
┌─────────────────────────────────────────────────────────────┐
│                  ACTION CONSENT FLOW                         │
│                                                              │
│  Agent: "I'd like to pay your PG&E bill of $142.50"        │
│                              │                               │
│  ┌──────────────────────────────────────────┐               │
│  │  🔴 ACTION REQUIRED                      │               │
│  │                                           │               │
│  │  "BudgetBot" wants to TAKE AN ACTION:    │               │
│  │                                           │               │
│  │  Action: Pay utility bill                 │               │
│  │  Site: PG&E (pge.com)                    │               │
│  │  Amount: $142.50                          │               │
│  │  Payment: Bank account ending in 4521    │               │
│  │                                           │               │
│  │  [Screenshot of payment page]            │               │
│  │                                           │               │
│  │  ⚠️ This will submit a real payment.     │               │
│  │  This action cannot be undone.            │               │
│  │                                           │               │
│  │  [ Cancel ]     [ Approve & Pay ]         │               │
│  └──────────────────────────────────────────┘               │
│                              │                               │
│  User approves → Action executes → Confirmation returned    │
└─────────────────────────────────────────────────────────────┘
```

### 8.3 Safety Architecture for Actions
| Layer | Description |
|-------|-------------|
| **Parameter validation** | Every action parameter has a type, range, and validation rule. Amount can't exceed max. |
| **Pre-flight screenshot** | Before any irreversible action, capture a screenshot and show the user exactly what will happen. |
| **Dry-run mode** | Fill all fields but DON'T click submit. Return screenshot for user review. |
| **Rollback support** | Where possible, define undo steps (e.g., cancel a reservation). |
| **Transaction logging** | Every action is logged with full parameters, screenshots, and outcome. |
| **Spending limits** | Per-agent, per-user spending limits for financial actions. |
| **Cooldown periods** | Configurable delay between actions (prevent rapid-fire submissions). |
| **Two-party approval** | For high-value actions, require approval from both user and a second party. |

### 8.4 Action Categories
| Category | Examples | Risk Level |
|----------|----------|------------|
| **Read** | Check balance, view policy, download statement | Low |
| **Fill** | Pre-fill a form (don't submit) | Low |
| **Submit** | Submit a form, file a claim, send a message | Medium |
| **Financial** | Make a payment, transfer funds, change plan | High |
| **Destructive** | Cancel account, delete data, close position | Critical |

Each category has increasing consent requirements and safety checks.

### Deliverables
- [ ] Action blueprint schema (v3)
- [ ] Pre-flight screenshot and dry-run mode
- [ ] Action consent UI with risk-level indicators
- [ ] Parameter validation engine
- [ ] Transaction logging with screenshots
- [ ] Spending limits and cooldown periods
- [ ] 10 action blueprints (bill pay, form fill, document download)
- [ ] Agent action SDK
- [ ] Rollback support for reversible actions

---

## 9. Phase 5 — Enterprise & Scale (Weeks 45-56)

**Goal:** Make Plaidify enterprise-ready and scalable to millions of connections.

### 9.1 Infrastructure
| Component | Technology | Purpose |
|-----------|-----------|---------|
| API Gateway | Kong / AWS API Gateway | Rate limiting, API key management, request routing |
| Browser Farm | Kubernetes + Playwright containers | Horizontally scalable browser instances |
| Job Queue | Celery + Redis (or BullMQ) | Async connection jobs, scheduled refreshes |
| Cache | Redis | Session cache, blueprint cache, rate limit counters |
| Database | PostgreSQL (primary) + Redis (sessions) | Persistent storage with connection pooling |
| Object Storage | S3 / MinIO | Screenshots, audit logs, exported data |
| Secrets | HashiCorp Vault / AWS Secrets Manager | Credential encryption keys, API keys |
| Monitoring | Prometheus + Grafana | Metrics, alerts, dashboards |
| Logging | ELK Stack / Loki | Centralized structured logging |
| CDN | CloudFront / Cloudflare | Plaidify Link UI, documentation site |

### 9.2 Scaling Architecture
```
                    ┌──────────────┐
                    │   CDN/LB     │
                    └──────┬───────┘
                           │
              ┌────────────┼────────────┐
              ▼            ▼            ▼
        ┌──────────┐ ┌──────────┐ ┌──────────┐
        │ API Pod 1│ │ API Pod 2│ │ API Pod N│
        └────┬─────┘ └────┬─────┘ └────┬─────┘
             │             │             │
             └─────────────┼─────────────┘
                           ▼
                    ┌──────────────┐
                    │  Job Queue   │
                    │  (Redis)     │
                    └──────┬───────┘
                           │
              ┌────────────┼────────────┐
              ▼            ▼            ▼
        ┌──────────┐ ┌──────────┐ ┌──────────┐
        │ Browser  │ │ Browser  │ │ Browser  │
        │ Worker 1 │ │ Worker 2 │ │ Worker N │
        │(Playwright│ │(Playwright│ │(Playwright│
        └──────────┘ └──────────┘ └──────────┘
```

### 9.3 Enterprise Features
| Feature | Details |
|---------|---------|
| **SSO / SAML** | Enterprise customers authenticate via their identity provider. |
| **Tenant isolation** | Multi-tenant architecture. Each customer's data is fully isolated. |
| **Custom blueprints** | Private blueprint registry per tenant. |
| **SLA dashboard** | Real-time connection success rates, latency percentiles, uptime. |
| **Admin console** | Manage connections, view audit logs, configure policies, manage API keys. |
| **Compliance** | SOC 2 Type II audit, GDPR data deletion, CCPA compliance. |
| **Data residency** | Choose where data is stored (US, EU, APAC). |
| **IP allowlisting** | Restrict API access to specific IP ranges. |
| **Custom encryption** | Bring your own encryption keys (BYOK). |
| **Priority support** | Dedicated support channel, SLA guarantees. |

### 9.4 Commercial Model
| Tier | Price | Features |
|------|-------|----------|
| **Open Source** | Free forever | Core engine, JSON blueprints, REST API, community blueprints, self-hosted |
| **Cloud** | Pay-per-connection | Hosted Plaidify, managed browser farm, 99.9% SLA, email support |
| **Enterprise** | Custom pricing | SSO, tenant isolation, compliance, SLA dashboard, dedicated support, BYOK |

Open-source core stays MIT licensed forever. Commercial features are additive, not restrictive.

### Deliverables
- [ ] Kubernetes deployment manifests (Helm chart)
- [ ] Browser worker auto-scaling
- [ ] Multi-tenant data isolation
- [ ] Admin console (web UI)
- [ ] SOC 2 Type II readiness (policies, controls, evidence)
- [ ] SLA dashboard with real-time metrics
- [ ] SSO/SAML integration
- [ ] Data residency support (multi-region)
- [ ] Commercial licensing framework
- [ ] Load testing: 10,000 concurrent connections

---

## 10. Team Structure

### Phase 0-1 (1-5 people — You + early contributors)
| Role | Responsibility |
|------|---------------|
| **You (Founder/Lead)** | Architecture, core engine, blueprint design, community |
| **Backend Engineer** | FastAPI, database, auth, security |
| **Browser Automation Engineer** | Playwright integration, stealth, anti-detection |
| **DevOps** | CI/CD, Docker, testing infrastructure |
| **Community Manager** | Docs, issues, Discord, blueprint contributions |

### Phase 2-3 (10-25 people)
| Team | Size | Focus |
|------|------|-------|
| **Core Engine** | 4 | Browser engine, step executor, error handling |
| **SDK & Developer Experience** | 4 | Python SDK, JS SDK, CLI, docs |
| **AI Agent Platform** | 4 | MCP server, consent engine, agent SDK |
| **Blueprint Engineering** | 3 | Building + maintaining blueprints, registry |
| **Security** | 2 | Encryption, audit, penetration testing |
| **Frontend** | 2 | Plaidify Link, consent UI, admin dashboard |
| **QA** | 2 | Test automation, blueprint validation |
| **DevOps** | 2 | Infrastructure, monitoring, deployment |
| **Product/Design** | 2 | UX, product strategy, user research |

### Phase 4-5 (50-100 people)
Add: Sales, Customer Success, Legal/Compliance, Data Science (anomaly detection), dedicated teams per vertical (financial, healthcare, utilities, government).

---

## 11. Risk Matrix

| Risk | Probability | Impact | Mitigation |
|------|------------|--------|------------|
| **Legal: ToS violations** | High | High | Partner with sites, focus on user-consented access, legal review per vertical |
| **Technical: Anti-bot detection** | High | High | Playwright stealth, residential proxies, browser fingerprint rotation, headless detection evasion |
| **Technical: Blueprint maintenance** | High | Medium | Automated health checks, community maintenance, AI-assisted blueprint repair |
| **Security: Credential breach** | Low | Critical | HSM-backed encryption, zero-knowledge architecture, SOC 2, bug bounty program |
| **Business: Plaid/MX compete** | Medium | Medium | Stay open-source, focus on non-financial verticals, build community moat |
| **Technical: Scale limits** | Medium | Medium | Kubernetes auto-scaling, browser pool optimization, connection queuing |
| **Community: Low adoption** | Medium | High | Great docs, easy quickstart, active Discord, blueprint bounties |
| **Regulatory: New data laws** | Medium | Medium | Modular compliance engine, per-region consent flows |

---

## 12. Success Metrics per Phase

### Phase 0 — Foundation
- 90%+ test coverage
- 0 critical security findings
- CI pipeline < 5 min
- Docker image builds and runs cleanly

### Phase 1 — Browser Engine
- 5+ working blueprints for real sites
- < 10s average connection time
- < 5% connection failure rate (on healthy sites)
- MFA flow works end-to-end

### Phase 2 — Developer SDK
- 100+ GitHub stars
- 25+ blueprints in registry
- 10+ developers using SDK in projects
- SDK install → first successful connection in < 30 minutes
- Documentation NPS > 40

### Phase 3 — AI Agent Protocol
- 3+ AI agent frameworks integrated (LangChain, CrewAI, AutoGen)
- 100+ agents registered
- < 1 security incident
- User consent completion rate > 80%

### Phase 4 — Browser Actions
- 10+ action blueprints
- 0 unauthorized actions (safety record)
- < 2% action failure rate

### Phase 5 — Enterprise
- First paying enterprise customer
- 99.9% API uptime
- SOC 2 Type II certified
- 10,000 concurrent connections supported
- 1,000+ GitHub stars

---

## 13. Open Source & Community Strategy

### Community Building
| Activity | Cadence | Purpose |
|----------|---------|---------|
| Discord server | Always on | Real-time help, blueprint sharing, feature discussion |
| Weekly office hours | Weekly | Live coding, Q&A, blueprint building |
| Blueprint bounties | Ongoing | Pay contributors $50-200 per verified blueprint |
| "Good first issue" labels | Ongoing | Onboard new contributors |
| Monthly blog post | Monthly | Progress updates, technical deep-dives |
| Conference talks | Quarterly | Visibility at PyCon, API World, AI conferences |

### Contribution Model
```
Contributor writes blueprint → PR → Automated validation → Human review → Merge → Published to registry
```

### Governance
- **Benevolent Dictator (You)** for Phase 0-2
- **Core maintainer team** (3-5 people) by Phase 3
- **Technical Steering Committee** by Phase 5
- All RFCs (design proposals) are public and open for comment

### Documentation Priority
1. **Quickstart** — First connection in 5 minutes
2. **Blueprint guide** — How to write a blueprint from scratch
3. **SDK reference** — Auto-generated from docstrings
4. **Security whitepaper** — How credentials are handled
5. **Agent integration guide** — How to use Plaidify with AI agents
6. **Self-hosting guide** — Deploy Plaidify on your infrastructure

---

## Appendix: Immediate Next Steps (This Week)

1. **Commit and push** the current uncommitted changes to GitHub
2. **Remove hardcoded secrets** (Fernet key, JWT secret)
3. **Add CI pipeline** (GitHub Actions: ruff + pytest)
4. **Fix the dead code** (unreachable return, duplicate imports)
5. **Add Alembic** for database migrations
6. **Write 3 unit tests** for the Link Token flow
7. **Create a GitHub Project board** with Phase 0 tasks
8. **Set up Discord** for early contributors

---

*This plan is a living document. Update it as priorities shift and learnings emerge.*
