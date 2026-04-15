# Plaidify — Product Plan

**Version:** 2.1
**Last Updated:** April 15, 2026
**Vision:** Plaidify is the open-source infrastructure layer that lets any developer turn their product into a data accessibility platform — for human users and AI agents alike.

---

## Table of Contents

1. [Product Vision & Positioning](#1-product-vision--positioning)
2. [Target Users & Use Cases](#2-target-users--use-cases)
3. [Architecture Overview](#3-architecture-overview)
4. [✅ Phase 0 — Foundation (COMPLETE)](#4--phase-0--foundation-complete)
5. [✅ Phase 1 — Browser Engine (COMPLETE)](#5--phase-1--browser-engine-complete)
6. [🔥 Phase 2 — Developer SDK & Platform (Weeks 1-3)](#6--phase-2--developer-sdk--platform-weeks-1-3)
7. [Phase 3 — AI Agent Protocol (Weeks 3-5)](#7-phase-3--ai-agent-protocol-weeks-3-5)
8. [Phase 4 — Browser Actions / Write Ops (Weeks 5-7)](#8-phase-4--browser-actions--write-ops-weeks-5-7)
9. [Phase 5 — Enterprise & Scale (Weeks 7-10)](#9-phase-5--enterprise--scale-weeks-7-10)
10. [Phase 6 — Data Strategy & Intelligence (Weeks 11-14)](#10-phase-6--data-strategy--intelligence-weeks-11-14)
11. [Week-by-Week Execution Calendar](#11-week-by-week-execution-calendar)
12. [Risk Matrix](#12-risk-matrix)
13. [Success Metrics](#13-success-metrics)
14. [Open Source & Community Strategy](#14-open-source--community-strategy)

---

## 1. Product Vision & Positioning

### The Problem
The world's data is locked behind login forms. Millions of websites hold user data — bank balances, medical records, utility bills, academic transcripts, insurance policies — and provide no APIs. Today, if a developer wants to build an app that accesses this data on behalf of a user, they have two options: (1) pay Plaid/MX for financial data only, or (2) build fragile, one-off scrapers.

AI agents face an even worse version of this problem. They need authenticated access to websites to act on behalf of users, but there's no standard protocol for an AI to safely log in, read data, and — eventually — take actions.

### The Solution
Plaidify is **open-source infrastructure** that:

1. **For Developers:** Drop a JSON blueprint into your project → get a REST API that authenticates to any site and returns structured data.

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
**Goal:** Building a personal finance app. Needs to pull transaction data from 5 regional banks with no API.

**How she uses Plaidify:**
```python
from plaidify import Plaidify

pfy = Plaidify()
result = await pfy.connect(
    blueprint="us_regional_bank",
    credentials={"username": "user@bank.com", "password": "***"},
    extract=["transactions", "balance"]
)
# result = {"status": "connected", "data": {"balance": 4521.30, "transactions": [...]}}
```

### User Persona 2: The AI Agent Builder
**Name:** Marcus, AI Engineer at an agent startup
**Goal:** Building an AI assistant that manages insurance. Agent needs to read the user's policy from their insurance portal.

**How he uses Plaidify:**
```python
from plaidify.agent import PlaidifyTool

tool = PlaidifyTool(
    consent_mode="explicit",
    data_scope=["policy_summary", "premium_amount"],
    session_ttl=300
)
data = await tool.fetch(blueprint="state_farm_insurance", user_token="usr_abc123")
```

### User Persona 3: The Data Accessibility Startup
**Name:** Priya, CTO of a utility bill aggregation startup
**Goal:** Build "Plaid for utilities" without building scraping infrastructure from scratch.

**How she uses Plaidify:** Self-hosts Plaidify, writes blueprints for 50 utility companies, exposes a white-labeled API to her customers.

---

## 3. Architecture Overview

### Current Architecture (Phase 1 Complete)

```
┌──────────────────┐        ┌─────────────────────────────────────┐
│                  │        │            Plaidify v0.2.0           │
│   Your App /     │  POST  │                                     │
│   AI Agent /     ├───────►│  1. Load Blueprint (JSON V2)        │
│   MCP Client     │        │  2. Launch Browser (Playwright)     │
│                  │◄───────┤  3. Authenticate + Handle MFA       │
│                  │  JSON  │  4. Extract Typed Data               │
│                  │        │  5. Return Structured Response       │
└──────────────────┘        └─────────────────────────────────────┘
```

### Target Architecture (End of Phase 6)

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          PLAIDIFY PLATFORM                              │
│                                                                         │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌────────────┐ │
│  │  REST API     │  │  Python SDK  │  │  MCP Server  │  │  Link UI   │ │
│  │  (FastAPI)    │  │  (pip pkg)   │  │  (Agents)    │  │  (React)   │ │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘  └─────┬──────┘ │
│         └─────────────────┼─────────────────┼─────────────────┘         │
│                           ▼                                             │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │                     ORCHESTRATION LAYER                          │   │
│  │  • Session Manager    • Consent Engine    • Rate Limiter         │   │
│  │  • Retry/Circuit Breaker   • Queue (Redis)                      │   │
│  └──────────────────────────────┬──────────────────────────────────┘   │
│                                 ▼                                       │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │                    PROVIDER ROUTER (Phase 6)                     │   │
│  │  • Intelligent routing: API → Browser fallback                  │   │
│  │  • Third-party aggregator support (Teller, MX, Akoya)           │   │
│  │  • Blueprint Auto-Healer (LLM-powered)                          │   │
│  └──────────┬─────────────────────────────────┬────────────────────┘   │
│             ▼                                 ▼                         │
│  ┌─────────────────────────┐  ┌───────────────────────────────────┐   │
│  │   BROWSER ENGINE  ✅    │  │       API CONNECTOR (Phase 6)     │   │
│  │  ┌────────────────────┐ │  │  ┌─────────────────────────────┐  │   │
│  │  │ Playwright Driver ✅│ │  │  │ OAuth2 / API Key / Bearer   │  │   │
│  │  │ Step Executor ✅    │ │  │  │ JSONPath Extraction          │  │   │
│  │  │ Browser Pool ✅     │ │  │  │ Open Banking (FDX/PSD2/UK)  │  │   │
│  │  └────────────────────┘ │  │  └─────────────────────────────┘  │   │
│  └──────────┬──────────────┘  └───────────────┬───────────────────┘   │
│             └─────────────────────────────────┘                         │
│                                 ▼                                       │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │                    DATA & SECURITY LAYER  ✅                     │   │
│  │  • PostgreSQL/SQLite ✅  • AES-256-GCM ✅  • JWT Auth ✅       │   │
│  │  • Alembic Migrations ✅  • Audit Log       • Consent Records   │   │
│  └─────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 4. ✅ Phase 0 — Foundation (COMPLETE)

**Status:** ✅ Shipped in v0.1.0

| Component | Status | Details |
|-----------|--------|---------|
| Security — no hardcoded secrets | ✅ | App fails to start without `ENCRYPTION_KEY` + `JWT_SECRET_KEY` |
| AES-256-GCM encryption | ✅ | Upgraded from Fernet/AES-128-CBC to OWASP-recommended AEAD |
| Input validation | ✅ | Pydantic models on all endpoints, password min length |
| Custom exception hierarchy | ✅ | 15 exception types (`PlaidifyError`, `BlueprintNotFoundError`, etc.) |
| Structured logging | ✅ | JSON (prod) / colored text (dev), correlation IDs |
| Pydantic Settings | ✅ | All config from env vars, fail-fast on missing |
| 98 unit tests | ✅ | 8 test suites, auth isolation verified |
| CI pipeline | ✅ | GitHub Actions: lint, test (3.9–3.12), security audit, Docker |
| Alembic migrations | ✅ | Initial schema: users, links, access_tokens |
| Multi-stage Docker | ✅ | Non-root user, health check, <200MB image |
| Health endpoint | ✅ | `GET /health` with DB connectivity check |
| 19 API endpoints | ✅ | Full Swagger docs at `/docs` |

---

## 5. ✅ Phase 1 — Browser Engine (COMPLETE)

**Status:** ✅ Shipped in v0.2.0

| Component | Status | Details |
|-----------|--------|---------|
| Playwright integration | ✅ | Headless Chromium, real browser automation |
| Browser Pool Manager | ✅ | Configurable concurrency, idle timeout, cleanup |
| Blueprint V2 schema | ✅ | Auth steps, MFA detection, typed extraction, cleanup |
| Step Executor | ✅ | `goto`, `fill`, `click`, `wait`, variable interpolation |
| Data Extractor | ✅ | Typed fields (text, currency, date, sensitive), list/table extraction |
| MFA Manager | ✅ | Async event-based, detects challenges, pauses for user input |
| GreenGrid Energy demo | ✅ | Full utility portal + interactive demo UI + one-command launcher |
| 13-field extraction | ✅ | Account info, bills, usage history, payments — one API call |
| Error handling | ✅ | Auth failures, timeouts, MFA required, blueprint errors |

**Try it now:** `python run_demo.py` → http://localhost:8000/ui/demo.html

---

## 6. 🔥 Phase 2 — Developer SDK & Platform (Weeks 1-3)

**Goal:** Make Plaidify embeddable. Developer installs SDK → first successful extraction in <30 minutes.

**Start date:** March 15, 2026

**Progress:** Python SDK + CLI shipped (v0.3.0a1)

---

### Week 1: Python SDK + CLI Foundation (Mar 17-21)

#### Monday-Tuesday: Python SDK Core

| Task | Details | Output |
|------|---------|--------|
| Package scaffold | `plaidify/` package: `__init__.py`, `client.py`, `config.py`, `exceptions.py` | PyPI-ready structure |
| `PlaidifyClient` class | Async client wrapping all API endpoints | Singleton client |
| `connect()` method | One-call connect + extract, wraps `POST /connect` | Returns typed `ConnectResult` |
| MFA callback | Accept `mfa_handler` callback for inline MFA resolution | Async callback pattern |
| Link flow methods | `create_link()`, `submit_credentials()`, `fetch_data()` | Full Plaid-style flow |
| Type stubs | Full type annotations + `py.typed` marker | IDE autocomplete works |

```python
# Target API by Tuesday:
from plaidify import Plaidify

pfy = Plaidify(server_url="http://localhost:8000")

# Simple one-call
result = await pfy.connect("greengrid_energy", username="demo_user", password="demo_pass")
print(result.data["current_bill"])  # "$142.57"

# With MFA
async def handle_mfa(challenge):
    return input(f"Enter {challenge.type} code: ")

result = await pfy.connect("greengrid_energy", username="mfa_user", password="mfa_pass", mfa_handler=handle_mfa)
```

#### Wednesday-Thursday: CLI Tool

| Task | Details | Output |
|------|---------|--------|
| CLI scaffold | Click-based CLI in `plaidify/cli.py` | `plaidify --help` |
| `plaidify connect` | Test a blueprint from the terminal | Quick testing |
| `plaidify blueprint validate` | Validate JSON schema + required fields | CI-friendly |
| `plaidify blueprint test` | Run a blueprint against a live site, show results | Developer feedback loop |
| `plaidify serve` | Start the Plaidify server | Replaces `uvicorn` command |
| `plaidify demo` | Start both servers + open browser | One-word demo |

```bash
# Target by Thursday:
pip install plaidify
plaidify demo                                          # launches everything
plaidify connect greengrid_energy -u demo_user -p demo_pass
plaidify blueprint validate ./connectors/my_site.json
plaidify blueprint test ./connectors/my_site.json -u user -p pass
```

#### Friday: Tests + Publish to PyPI

| Task | Details |
|------|---------|
| Unit tests for SDK client | Mock server responses, test connect, link flow, error handling |
| Integration test | SDK → live API → example site → data returned |
| `pyproject.toml` for SDK | Package metadata, dependencies, entry points (`plaidify` CLI) |
| Publish to PyPI | `pip install plaidify` works globally |
| SDK README | Quickstart, full API reference, examples |

---

### Week 2: JavaScript SDK + Plaidify Link UI (Mar 24-28)

#### Monday-Tuesday: JavaScript / TypeScript SDK

| Task | Details | Output |
|------|---------|--------|
| TypeScript project | `tsconfig.json`, `rollup` bundler, ESM + CJS output | `npm install plaidify` |
| `PlaidifyClient` class | `fetch`-based client matching Python API surface | Cross-platform |
| `connect()` method | One-call connect + extract | Returns typed `ConnectResult` |
| Link flow methods | `createLink()`, `submitCredentials()`, `fetchData()` | Mirror Python |
| Browser + Node support | Works in both environments | Universal package |
| Tests + publish to npm | Jest tests, publish to npm registry | `npm install plaidify` works |

```typescript
// Target by Tuesday:
import { Plaidify } from 'plaidify';
const pfy = new Plaidify({ serverUrl: 'http://localhost:8000' });
const result = await pfy.connect('greengrid_energy', { username: 'demo_user', password: 'demo_pass' });
console.log(result.data.current_bill); // "$142.57"
```

#### Wednesday-Thursday: Plaidify Link UI Component

| Task | Details | Output |
|------|---------|--------|
| React component | `<PlaidifyLink>` embeddable widget | Drop-in for React apps |
| Vanilla JS version | `PlaidifyLink.open({ ... })` for non-React apps | Universal |
| Multi-step flow | Provider search → credentials → progress → MFA → success | Full UX |
| Theming API | CSS custom properties for brand customization | White-labelable |
| Event callbacks | `onSuccess`, `onError`, `onMFA`, `onClose` | Developer hooks |
| Iframe security | Credentials go directly to Plaidify, never touch developer backend | Secure by default |

```jsx
// Target by Thursday:
<PlaidifyLink
  serverUrl="http://localhost:8000"
  onSuccess={(result) => console.log(result.data)}
  onError={(err) => console.error(err)}
  theme={{ primaryColor: '#22c55e' }}
/>
```

#### Friday: Cross-platform Testing + Polish

| Task | Details |
|------|---------|
| E2E test | JS SDK → API → example site → Link UI renders results |
| Cross-browser | Chrome, Firefox, Safari verification |
| Bundle size | Tree-shake to <20KB gzipped |
| Storybook | Interactive component docs for Link UI |
| npm README | Quickstart, API reference, Storybook link |

---

### Week 3: Blueprint Registry + Webhooks + Ship v0.3.0 (Mar 31 - Apr 4)

#### Monday-Tuesday: Blueprint Registry

| Task | Details | Output |
|------|---------|--------|
| Registry data model | Blueprint metadata table: name, domain, version, author, tags, quality tier | DB migration |
| `POST /registry/publish` | Upload + validate a blueprint | API endpoint |
| `GET /registry/search` | Search by name, domain, tag | API endpoint |
| `GET /registry/{name}` | Download blueprint JSON | API endpoint |
| CLI integration | `plaidify registry search "utility"`, `plaidify registry install greengrid_energy` | CLI commands |
| Quality tiers | `community` → `tested` (CI-validated) → `certified` (reviewed) | Trust levels |

#### Wednesday-Thursday: Webhook System

| Task | Details | Output |
|------|---------|--------|
| `POST /webhooks` | Register webhook URL + events + HMAC secret | Registration |
| Event types | `connection.success`, `connection.failed`, `connection.mfa_required`, `data.updated` | 4 events |
| HMAC-signed delivery | Payloads signed with shared secret, retry 3x with backoff | Reliable delivery |
| Webhook CRUD | `GET /webhooks`, `DELETE /webhooks/{id}`, `POST /webhooks/{id}/test` | Management |
| SDK integration | `pfy.on("connection.success", handler)` in Python and JS | Developer-friendly |

#### Friday: Scheduled Refresh + v0.3.0 Release

| Task | Details |
|------|---------|
| Scheduled refresh | `refresh_schedule` param on `create_link` (hourly/daily/weekly/cron) |
| Background worker | Celery/Redis or APScheduler for scheduled jobs |
| `data.updated` webhook | Fires when scheduled refresh finds new data |
| **Tag v0.3.0** | Python SDK + JS SDK + Link UI + Registry + Webhooks |
| CHANGELOG | Full release notes |

### Phase 2 Deliverables

- [ ] Python SDK on PyPI (`pip install plaidify`)
- [ ] CLI tool (`plaidify connect`, `plaidify demo`, `plaidify blueprint`)
- [ ] JavaScript/TypeScript SDK on npm (`npm install plaidify`)
- [ ] `<PlaidifyLink>` React component + vanilla JS
- [ ] Blueprint Registry with search, publish, install
- [ ] Webhook system with HMAC signing + retries
- [ ] Scheduled data refresh
- [ ] v0.3.0 released

---

## 7. Phase 3 — AI Agent Protocol (Weeks 3-5)

**Goal:** Make Plaidify the standard way AI agents access authenticated web data. Scoped, consented, auditable.

**Overlaps with Phase 2 Week 3 — MCP work starts Thursday of Week 3.**

---

### Week 3-4: MCP Server + Consent Engine (Mar 31 - Apr 11)

#### Week 3 Thursday-Friday: MCP Server Scaffold

| Task | Details | Output |
|------|---------|--------|
| FastMCP server | MCP server exposing Plaidify as tools | `plaidify mcp serve` |
| `plaidify_connect` tool | Wraps `POST /connect` | Agents can extract data |
| `plaidify_list_blueprints` tool | Wraps `GET /blueprints` | Agents discover available sites |
| `plaidify_submit_mfa` tool | Wraps `POST /mfa/submit` | Agents handle MFA |

#### Week 4 Monday-Tuesday: Full MCP Implementation

| Task | Details | Output |
|------|---------|--------|
| `plaidify_fetch_data` tool | Fetch from existing connection | Read without re-auth |
| `plaidify_list_connections` tool | List user's active connections | Connection management |
| Scope enforcement | Agent only accesses fields it was granted | Server-side enforcement |
| Session management | Agent connections auto-expire, configurable TTL | Cleanup |
| Error mapping | MCP-compatible error responses | Agent-friendly errors |

```json
{
  "name": "plaidify",
  "version": "1.0",
  "tools": [
    {
      "name": "plaidify_connect",
      "description": "Connect to an authenticated website and extract data",
      "parameters": {
        "site": "string — Blueprint name",
        "username": "string",
        "password": "string",
        "fields": "string[] — Specific fields to extract (optional)"
      }
    },
    {
      "name": "plaidify_list_blueprints",
      "description": "List available site blueprints",
      "parameters": { "search": "string (optional)" }
    },
    {
      "name": "plaidify_submit_mfa",
      "description": "Submit MFA code for a pending connection",
      "parameters": { "session_id": "string", "code": "string" }
    },
    {
      "name": "plaidify_fetch_data",
      "description": "Fetch data from an existing connection",
      "parameters": { "connection_id": "string", "fields": "string[]" }
    },
    {
      "name": "plaidify_list_connections",
      "description": "List active connections for current user",
      "parameters": {}
    }
  ]
}
```

#### Week 4 Wednesday-Thursday: Consent Engine

| Task | Details | Output |
|------|---------|--------|
| Consent data model | `ConsentRequest` → user approves → `ConsentToken` with scoped fields | DB migration |
| `POST /consent/request` | Agent requests access to specific fields | API endpoint |
| Consent approval UI | User sees agent name, requested fields, duration → approve/deny | Web page |
| Token expiry | All consent tokens auto-expire (configurable, max 30 days) | Auto-cleanup |
| `DELETE /consent/{token}` | Instant revocation by user | User control |
| Scope system | Field-level: `read:current_bill`, `read:usage_history`, etc. | Granular |

```
┌──────────────────────────────────────────┐
│  "BudgetBot" wants to access:            │
│                                          │
│  ✅ Current bill                          │
│  ✅ Usage history (last 6 months)         │
│  ❌ Account number (not requested)        │
│                                          │
│  Access expires: 24 hours                │
│                                          │
│  [ Deny ]        [ Allow ]               │
└──────────────────────────────────────────┘
```

#### Week 4 Friday: Agent SDK

| Task | Details | Output |
|------|---------|--------|
| `PlaidifyAgent` class | Python SDK for agent developers | `from plaidify.agent import PlaidifyAgent` |
| `request_access()` | Triggers consent flow, returns consent request | Async |
| `fetch()` with consent token | Fetch data within approved scope | Scope-enforced |
| `ScopeViolationError` | Raised when agent requests unauthorized fields | Fail-safe |
| Agent registration | `POST /agents/register` with name, description, default scopes | Identity |

```python
from plaidify.agent import PlaidifyAgent

agent = PlaidifyAgent(agent_id="budgetbot", api_key="pk_agent_...")

# Request scoped access
consent = await agent.request_access(
    user_id="usr_123",
    site="greengrid_energy",
    scopes=["current_bill", "usage_history"],
    duration="24h",
    reason="To analyze your energy spending"
)

if consent.approved:
    data = await agent.fetch(consent_token=consent.token)
    # data.current_bill = "$142.57"
    # data.usage_history = [...]
```

---

### Week 5: Audit Trail + Safety Guardrails + Ship v0.4.0 (Apr 14-18)

#### Monday-Tuesday: Audit Trail

| Task | Details | Output |
|------|---------|--------|
| Audit log model | agent_id, user_id, site, fields_accessed, timestamp, status | DB table |
| `GET /audit-log` | Users view who accessed their data and when | API endpoint |
| Audit dashboard | Simple web UI showing access history | `/audit` page |
| Agent access log | `GET /agents/{id}/log` — what did this agent access? | API endpoint |

```json
{
  "event_id": "evt_abc123",
  "timestamp": "2026-04-15T12:00:00Z",
  "agent_id": "budgetbot",
  "agent_name": "BudgetBot",
  "user_id": "usr_123",
  "action": "fetch_data",
  "site": "greengrid_energy",
  "fields_accessed": ["current_bill", "usage_history"],
  "duration_ms": 3400,
  "status": "success"
}
```

#### Wednesday-Thursday: Safety Guardrails

| Task | Details | Output |
|------|---------|--------|
| Rate limiting | Per-agent, per-user token bucket (configurable) | Server-side |
| Kill switch | `POST /consent/revoke-all` — user kills all agent access instantly | Emergency stop |
| Anomaly flags | Log warnings for bulk access, off-hours, unusual patterns | Alerting |
| Data redaction | Sensitive fields (SSN, full account #) require elevated consent | Consent tiers |
| Agent verification | Verified vs unverified agents, different rate limits | Trust levels |

#### Friday: Framework Integrations + v0.4.0 Release

| Task | Details |
|------|---------|
| LangChain tool | Plaidify as a LangChain `Tool` with working example |
| CrewAI integration | Plaidify as a CrewAI tool with working example |
| E2E test | Agent SDK → MCP → consent → fetch → audit logged |
| **Tag v0.4.0** | MCP server + Consent engine + Agent SDK + Audit trail |
| CHANGELOG + announcement | Release notes |

### Phase 3 Deliverables

- [ ] MCP server with 5 tools
- [ ] Consent engine with user-facing approval UI
- [ ] Field-level scope enforcement
- [ ] Agent SDK (Python) with consent flow
- [ ] Agent registration system
- [ ] Audit trail with user dashboard
- [ ] Rate limiting per agent
- [ ] LangChain + CrewAI integration examples
- [ ] `plaidify mcp serve` CLI command
- [ ] v0.4.0 released

---

## 8. Phase 4 — Browser Actions / Write Ops (Weeks 5-7)

**Goal:** Go beyond reading. Let apps and agents fill forms, make payments, upload documents — with explicit user authorization and safety rails.

---

### Week 5-6: Action Framework (Apr 14 - Apr 25)

#### Week 5 Thursday-Friday: Action Schema + Engine Foundation

| Task | Details | Output |
|------|---------|--------|
| Blueprint v3 schema | Add `actions` block alongside existing `extract` | Schema extension |
| New step types | `select` (dropdown), `upload` (file), `confirm` (pre-submit check) | Step executor |
| Action parameters | Typed, validated inputs per action (amount, method, etc.) | Validation engine |

```json
{
  "schema_version": "3.0",
  "name": "Pay GreenGrid Bill",
  "type": "action",
  "actions": {
    "pay_bill": {
      "description": "Pay the current utility bill",
      "risk_level": "high",
      "requires_consent": "explicit_per_action",
      "parameters": {
        "amount": { "type": "currency", "required": true, "max": 10000 }
      },
      "steps": [
        { "action": "goto", "url": "{{base_url}}/pay" },
        { "action": "fill", "selector": "#amount", "value": "{{amount}}" },
        { "action": "click", "selector": "#pay-now" },
        { "action": "wait", "selector": "#confirmation" },
        { "action": "extract", "fields": { "confirmation": { "selector": "#conf-num" } } }
      ],
      "confirmation": {
        "require_user_approval": true,
        "screenshot_before_submit": true
      }
    }
  }
}
```

#### Week 6 Monday-Tuesday: Action Execution Engine

| Task | Details | Output |
|------|---------|--------|
| `POST /actions/execute` | Execute an action from a blueprint | API endpoint |
| Dry-run mode | Fill fields, don't submit — return screenshot | `?dry_run=true` |
| Pre-flight screenshot | Capture page before irreversible click, show to user | Safety net |
| Parameter validation | Type-check, range-check all inputs before execution | Fail-fast |
| Action result model | Success with confirmation data, or failure with reason | Typed responses |

#### Week 6 Wednesday-Thursday: Action Consent + Safety

| Task | Details | Output |
|------|---------|--------|
| Action consent UI | Shows action details, amount, screenshot — stricter than read consent | Web page |
| Risk classification | `low` (read), `medium` (form fill), `high` (payment), `critical` (delete) | Per-action |
| Spending limits | Per-agent, per-user caps on financial actions | Config |
| Cooldown periods | Min time between repeated actions (prevent rapid-fire) | Config |
| Transaction logging | Every action: params, screenshot, outcome, timestamp | Audit |

```
┌──────────────────────────────────────────┐
│  🔴 ACTION REQUIRED                      │
│                                          │
│  "BudgetBot" wants to:                   │
│  Pay GreenGrid Energy bill               │
│                                          │
│  Amount: $142.57                         │
│  [Screenshot of payment page]            │
│                                          │
│  ⚠️ This will submit a real payment.     │
│                                          │
│  [ Cancel ]     [ Approve & Pay ]        │
└──────────────────────────────────────────┘
```

#### Week 6 Friday: Action SDK + MCP Extension

| Task | Details |
|------|---------|
| SDK `execute_action()` | Python + JS SDK methods for triggering actions |
| `pfy.execute_action("pay_bill", ..., dry_run=True)` | Dry-run from SDK |
| MCP `plaidify_execute_action` tool | Agents can request actions via MCP |
| MCP `plaidify_list_actions` tool | Agents discover available actions |

---

### Week 7: Action Blueprints + Polish + Ship v0.5.0 (Apr 28 - May 2)

#### Monday-Tuesday: Write Action Blueprints

| Task | Details |
|------|---------|
| Pay bill action | Demo action for GreenGrid Energy portal |
| Update account info | Change email, phone, address on demo site |
| Download statement | Export PDF/CSV from demo site |
| Extend demo site | Add payment page, settings page, download page to example_site |

#### Wednesday: Rollback + Error Recovery

| Task | Details |
|------|---------|
| Rollback support | `undo_steps` in action schema for reversible actions |
| Error detection | Detect failed actions (payment declined, form validation error) |
| Structured errors | `ActionFailedError` with reason, screenshot, suggested fix |
| Retry logic | Configurable retries for transient failures |

#### Thursday-Friday: v0.5.0 Release

| Task | Details |
|------|---------|
| E2E test | Agent → request action → consent → dry-run → execute → confirm → audit |
| 5 action blueprints working | Pay, update, download, cancel, reschedule |
| **Tag v0.5.0** | Action framework + consent + SDK + MCP |
| CHANGELOG | Release notes |

### Phase 4 Deliverables

- [ ] Action blueprint schema v3
- [ ] `POST /actions/execute` with dry-run mode
- [ ] Pre-flight screenshots
- [ ] Action consent UI with risk levels
- [ ] Spending limits + cooldown periods
- [ ] Transaction logging
- [ ] Python + JS SDK `execute_action()` methods
- [ ] MCP `plaidify_execute_action` tool
- [ ] 5 action blueprints for demo site
- [ ] Rollback support
- [ ] v0.5.0 released

---

## 9. Phase 5 — Enterprise & Scale (Weeks 7-10)

**Goal:** Make Plaidify production-grade for teams deploying at scale. Ship v1.0.

---

### Week 7-8: Infrastructure + Multi-Tenancy (Apr 28 - May 9)

#### Week 7 Thursday-Friday: Redis + Background Workers

| Task | Details |
|------|---------|
| Redis integration | Session cache, rate limit counters, job queue backend |
| Celery worker setup | Async connection jobs, scheduled refreshes |
| Connection pooling | Redis + PostgreSQL connection pool tuning |

#### Week 8 Monday-Tuesday: Kubernetes + Scaling

| Task | Details | Output |
|------|---------|--------|
| Helm chart | API pods + browser workers + Redis + PostgreSQL | `helm install plaidify` |
| Browser worker HPA | Auto-scale based on queue depth | Elastic capacity |
| Health probes | Liveness, readiness, startup probes | K8s-native |
| Resource limits | CPU/memory per pod, tested under load | Predictable costs |

#### Week 8 Wednesday-Thursday: Multi-Tenancy

| Task | Details | Output |
|------|---------|--------|
| Tenant model | Org → Users → API Keys → Connections | DB migration |
| Row-level isolation | `tenant_id` on all tables, enforced in queries | Data isolation |
| API key management | Create, rotate, revoke per tenant | `POST /org/api-keys` |
| Usage tracking | Per-tenant: connection counts, API calls, bandwidth | Billing foundation |

#### Week 8 Friday: Monitoring

| Task | Details |
|------|---------|
| Prometheus metrics | Connection latency, success rate, queue depth, browser pool utilization |
| Grafana dashboard template | Pre-built dashboard JSON |
| Status page | Blueprint health, API uptime |

---

### Week 9: Admin Console + Compliance (May 12-16)

#### Monday-Tuesday: Admin Console

| Task | Details | Output |
|------|---------|--------|
| Admin web UI | React SPA at `/admin` | Management dashboard |
| Connection manager | View, retry, cancel connections | Operations |
| Blueprint manager | Upload, edit, enable/disable blueprints | CRUD |
| User + tenant management | View users, connections, audit logs per tenant | Admin tools |
| API key dashboard | Create, view, rotate keys (self-service) | Dev portal |

#### Wednesday-Thursday: Compliance

| Task | Details | Output |
|------|---------|--------|
| `DELETE /user/{id}/data` | GDPR — purge all user data | Right to deletion |
| `GET /user/{id}/export` | GDPR — download user data as JSON | Data portability |
| Retention policies | Auto-delete connection data after configurable period | Background job |
| Key rotation | Rotate AES-256-GCM keys without downtime | Zero-downtime |
| SOC 2 prep | Document security controls, access policies, incident response | Compliance docs |

#### Friday: SSO / SAML

| Task | Details |
|------|---------|
| SAML 2.0 | Enterprise SSO via Okta, Azure AD |
| OIDC support | OpenID Connect for modern IdPs |
| Role-based access | Admin, Developer, Viewer roles per org |

---

### Week 10: Load Test + Docs + v1.0 Launch 🚀 (May 19-23)

#### Monday-Tuesday: Load Testing

| Task | Target |
|------|--------|
| Locust load test — concurrent connections | 1,000+ concurrent |
| Browser pool stress test — max Playwright instances | 50 per node |
| API latency — P50, P95, P99 | P95 < 500ms (API), <10s (connection) |
| Memory profiling — sustained load | Stable at 1GB per worker |
| Fix bottlenecks | Optimize based on profiling results |

#### Wednesday: Documentation Site

| Task | Details |
|------|---------|
| MkDocs / Docusaurus site | Hosted at docs.plaidify.dev |
| Sections | Quickstart, Blueprint guide, SDK reference, Agent guide, Self-hosting |
| API reference | Auto-generated from OpenAPI schema |
| Video walkthrough | 5-minute demo video embedded in README |
| Architecture deep-dive | Technical blog post |

#### Thursday-Friday: 🚀 v1.0.0 Launch

| Task | Details |
|------|---------|
| Version bump | Semantic versioning commitment: v1.0.0 |
| CHANGELOG | Full history: v0.1.0 → v0.2.0 → v0.3.0 → v0.4.0 → v0.5.0 → v1.0.0 |
| GitHub Release | Detailed release notes with highlights |
| Launch post | Blog + Twitter + Reddit + Hacker News |
| Product Hunt | Launch listing |
| Discord | Open community server |

### Phase 5 Deliverables

- [ ] Kubernetes Helm chart with auto-scaling
- [ ] Redis + Celery background workers
- [ ] Multi-tenant data isolation
- [ ] Admin console web UI
- [ ] Prometheus + Grafana monitoring
- [ ] GDPR data deletion + export
- [ ] SSO / SAML / OIDC
- [ ] Load tested to 1,000 concurrent connections
- [ ] Documentation site
- [ ] **v1.0.0 released and announced** 🚀

---

## 10. Phase 6 — Data Strategy & Intelligence (Weeks 11-14)

**Goal:** Make Plaidify the universal data access layer that intelligently routes to the best data source — API when available, browser when necessary — while self-healing broken blueprints.

| Week | Dates | Focus | Ship |
|------|-------|-------|------|
| **11** | May 25-29 | API Connector blueprint type | `connector_type: "api"` working |
| **12** | Jun 1-5 | Open Banking integration (FDX, PSD2, UK) | Bank API templates |
| **13** | Jun 8-12 | Blueprint Auto-Healer (LLM-powered) | `plaidify blueprint doctor/heal` |
| **14** | Jun 15-19 | Provider aggregation layer + v1.1.0 | Intelligent routing + ship |

### Week 11: API Connector Blueprint Type (May 25-29)

#### Monday-Tuesday: API Connector Engine

| Task | Details | Output |
|------|---------|--------|
| API connector blueprint type | `connector_type: "api"` alongside existing `"browser"` type | New blueprint schema |
| OAuth2 auth flow | Authorization code, client credentials, PKCE support | Standardized auth |
| API key / Bearer token auth | Static API keys, Bearer tokens for simpler APIs | Auth options |
| JSONPath extraction | Extract data from JSON API responses using JSONPath expressions | Structured output |

#### Wednesday-Thursday: API ↔ Browser Fallback

| Task | Details | Output |
|------|---------|--------|
| Automatic fallback chain | Try API first, fall back to browser if API unavailable | Resilient connections |
| Provider config | Per-blueprint `preferred_method: "api"` / `"browser"` / `"auto"` | Developer control |
| Unified response format | API and browser connectors return identical response shapes | Consistent SDK |
| Connection latency optimization | API connections skip browser overhead (sub-second) | Performance gain |

#### Friday: Testing + Polish

| Task | Details |
|------|---------|
| API connector test suite | End-to-end tests with mock API endpoints |
| Blueprint validator update | `plaidify blueprint validate` supports `connector_type: "api"` |
| Documentation | Blueprint authoring guide for API connectors |

---

### Week 12: Open Banking Integration (Jun 1-5)

#### Monday-Tuesday: FDX 6.0 (US/Canada)

| Task | Details | Output |
|------|---------|--------|
| FDX 6.0 connector template | Financial Data Exchange standard for US/Canada banks | Blueprint template |
| Account discovery | `/accounts` endpoint mapping | Account listing |
| Transaction extraction | `/transactions` with date range filtering | Transaction data |
| Balance retrieval | `/accounts/{id}/balances` mapping | Real-time balances |

#### Wednesday-Thursday: PSD2 + UK Open Banking

| Task | Details | Output |
|------|---------|--------|
| PSD2 / Berlin Group template | EU bank API standard connector | Blueprint template |
| UK Open Banking v3.1 template | UK bank API standard connector | Blueprint template |
| Consent management | Open Banking consent flows mapped to Plaidify consent engine | Compliant access |
| Certificate management | eIDAS / OBIE certificate handling for production use | TLS client certs |

#### Friday: Aggregator Proxies

| Task | Details |
|------|---------|
| Teller integration | API connector template for Teller (US banks) |
| Akoya integration | API connector template for Akoya (FDX-based) |
| MX integration | API connector template for MX (financial data) |

---

### Week 13: Blueprint Auto-Healer (Jun 8-12)

#### Monday-Tuesday: Health Monitoring

| Task | Details | Output |
|------|---------|--------|
| Blueprint health checks | Scheduled probes to detect broken selectors | Health status per blueprint |
| Failure classification | Categorize failures: auth changed, layout changed, site down | Actionable alerts |
| Health monitoring dashboard | UI showing blueprint health across all connectors | Operations visibility |
| Alert system | Webhook/email alerts when blueprints break | Proactive maintenance |

#### Wednesday-Thursday: LLM Auto-Repair

| Task | Details | Output |
|------|---------|--------|
| `plaidify blueprint doctor` | CLI command to diagnose broken blueprints | Diagnostic report |
| `plaidify blueprint heal` | LLM-powered automatic selector repair | Auto-fixed blueprints |
| Repair verification | Run healed blueprint against live site to verify fix | Confidence score |
| Repair caching | Cache successful repairs to avoid repeated LLM calls | Cost control |

#### Friday: Regression Testing

| Task | Details |
|------|---------|
| Automated regression suite | Healed blueprints run through full extraction test |
| Repair history | Track all auto-repairs with before/after diffs |
| Manual review queue | Flag low-confidence repairs for human review |

---

### Week 14: Provider Aggregation + v1.1.0 (Jun 15-19)

#### Monday-Tuesday: Provider Router

| Task | Details | Output |
|------|---------|--------|
| Intelligent routing layer | Route connections to best available provider | Optimal data source |
| Provider priority chain | API → Aggregator → Browser, configurable per blueprint | Flexibility |
| Latency-aware routing | Choose fastest healthy provider | Performance |
| Cost-aware routing | Prefer free paths (direct API, browser) over paid aggregators | Cost optimization |

#### Wednesday: Integration Testing

| Task | Details |
|------|---------|
| End-to-end routing tests | API → Browser fallback, aggregator failover |
| Load test routing layer | Verify routing under concurrent connections |
| Provider health monitoring | Real-time provider availability tracking |

#### Thursday-Friday: 🚀 v1.1.0 Release

| Task | Details |
|------|---------|
| Version bump | v1.1.0 — Universal data access layer |
| CHANGELOG | Full release notes for Phase 6 |
| GitHub Release | Detailed release notes with highlights |
| Updated documentation | API connector guide, Open Banking guide, auto-healer guide |
| Announcement | Blog post + community update |

### Phase 6 Deliverables

- [ ] API Connector blueprint type with OAuth2, API key, Bearer token auth
- [ ] JSONPath-based API response extraction
- [ ] Automatic fallback: API → browser
- [ ] FDX 6.0 connector template (US/Canada banks)
- [ ] PSD2/Berlin Group connector template (EU banks)
- [ ] Open Banking UK v3.1 connector template
- [ ] LLM-powered blueprint auto-healer
- [ ] Blueprint health monitoring dashboard
- [ ] Provider aggregation layer with intelligent routing
- [ ] Third-party aggregator support (Teller, MX, Akoya)
- [ ] **v1.1.0 released** 🚀

---

## 11. Week-by-Week Execution Calendar

Starting **March 17, 2026:**

| Week | Dates | Focus | Ship |
|------|-------|-------|------|
| **1** | Mar 17-21 | Python SDK + CLI tool | SDK on PyPI |
| **2** | Mar 24-28 | JavaScript SDK + Plaidify Link UI | npm package + React component |
| **3** | Mar 31 - Apr 4 | Blueprint Registry + Webhooks + MCP scaffold | **v0.3.0** |
| **4** | Apr 7-11 | Full MCP Server + Consent Engine + Agent SDK | MCP live |
| **5** | Apr 14-18 | Audit trail + Safety guardrails + Action scaffold | **v0.4.0** |
| **6** | Apr 21-25 | Action engine + Action consent + Action SDK | Actions working |
| **7** | Apr 28 - May 2 | Action blueprints + Redis + K8s start | **v0.5.0** |
| **8** | May 5-9 | Multi-tenancy + Monitoring + API keys | Enterprise infra |
| **9** | May 12-16 | Admin console + Compliance + SSO | Admin live |
| **10** | May 19-23 | Load testing + Docs site + Launch | **🚀 v1.0.0** |
| **11** | May 25-29 | API Connector blueprint type | `connector_type: api` |
| **12** | Jun 1-5 | Open Banking (FDX, PSD2, UK OB) | Bank API templates |
| **13** | Jun 8-12 | Blueprint Auto-Healer | `plaidify blueprint doctor` |
| **14** | Jun 15-19 | Provider aggregation + Polish | **🚀 v1.1.0** |

### Key Milestones

| Date | Milestone |
|------|-----------|
| **Mar 21** | `pip install plaidify` works |
| **Mar 28** | `npm install plaidify` works, Link UI embeddable |
| **Apr 4** | Blueprint Registry live, **v0.3.0 shipped** |
| **Apr 18** | MCP server + Agent SDK, **v0.4.0 shipped** |
| **May 2** | Write operations working, **v0.5.0 shipped** |
| **May 23** | **🚀 v1.0.0 launched** |
| **May 29** | API connectors work alongside browser connectors |
| **Jun 5** | Connect to banks via Open Banking APIs |
| **Jun 12** | Broken blueprints auto-detected and self-healed |
| **Jun 19** | **🚀 v1.1.0 — Universal data access layer** |

---

## 12. Risk Matrix

| Risk | Prob. | Impact | Mitigation |
|------|-------|--------|------------|
| **Legal: ToS violations** | High | High | Focus on user-consented access, legal review, open-source transparency |
| **Technical: Anti-bot detection** | High | High | Playwright stealth, residential proxies, fingerprint rotation |
| **Technical: Blueprint maintenance** | High | Medium | Automated health checks, community maintenance, AI-assisted repair |
| **Security: Credential breach** | Low | Critical | AES-256-GCM, zero-knowledge architecture, external security audit |
| **Business: Plaid/MX compete** | Medium | Medium | Stay open-source, focus on non-financial verticals, community moat |
| **Timeline: Scope creep** | High | Medium | **Strict weekly milestones. Cut scope before slipping dates.** |
| **Community: Low adoption** | Medium | High | Great docs, easy quickstart, Discord, blueprint bounties |
| **Open Banking certification requirements** | Medium | High | Start with aggregator proxies (Akoya/Teller), get certified later |
| **LLM costs for auto-healing** | Low | Medium | Cache repairs, only trigger on failure, use small models |
| **Third-party aggregator dependencies** | Medium | Medium | Multiple providers, graceful fallback to browser |

---

## 13. Success Metrics

### Phase 2 — Developer SDK (End of Week 3)
| Metric | Target |
|--------|--------|
| PyPI downloads (first week) | 100+ |
| npm downloads (first week) | 50+ |
| Install → first connection | < 30 minutes |
| Blueprints in registry | 10+ |
| SDK test coverage | 90%+ |

### Phase 3 — AI Agent Protocol (End of Week 5)
| Metric | Target |
|--------|--------|
| MCP tools available | 5+ |
| Agent framework integrations | 2+ (LangChain, CrewAI) |
| Consent completion rate | > 80% |
| Unauthorized data access incidents | 0 |

### Phase 4 — Browser Actions (End of Week 7)
| Metric | Target |
|--------|--------|
| Action blueprints | 5+ |
| Action failure rate | < 2% |
| Unauthorized actions | 0 |
| Dry-run usage | > 50% of first-time actions |

### Phase 5 — Enterprise & v1.0 (End of Week 10)
| Metric | Target |
|--------|--------|
| Concurrent connections (load test) | 1,000+ |
| API uptime | 99.9% |
| GitHub stars | 500+ |
| Documentation pages | 30+ |
| Community members (Discord) | 50+ |

### Phase 6 — Data Strategy & Intelligence (End of Week 14)
| Metric | Target |
|--------|--------|
| API connector blueprints | 10+ |
| Open Banking standards supported | 3 (FDX, PSD2, UK OB) |
| Blueprint auto-heal success rate | > 70% |
| Provider routing fallback rate | < 5% |

---

## 14. Open Source & Community Strategy

### Community Building

| Activity | When | Purpose |
|----------|------|---------|
| Discord server | Week 1 | Real-time help, blueprint sharing |
| "Good first issue" labels | Week 1 | Onboard new contributors |
| Blueprint bounties ($50-200) | Week 3+ | Incentivize community blueprints |
| Weekly office hours | Week 4+ | Live coding, Q&A |
| Launch blog post | Week 10 | Tell the story, share the vision |
| HN / Reddit / Product Hunt | Week 10 | Visibility + early adopters |

### Contribution Model

```
Contributor writes blueprint → PR → Automated validation → Human review → Merge → Published to registry
```

### Governance

- **Benevolent Dictator (Founder)** through v1.0
- **Core maintainer team** (3-5 people) post-launch
- All RFCs (design proposals) are public and open for comment

### Documentation Priority (in order)

1. **Quickstart** — First connection in 5 minutes
2. **Blueprint guide** — Write a blueprint from scratch
3. **SDK reference** — Auto-generated from docstrings
4. **Agent integration guide** — Plaidify + LangChain/CrewAI/MCP
5. **Self-hosting guide** — Deploy on your infrastructure
6. **Security whitepaper** — How credentials are handled

---

## Appendix: What's Built vs What's Next

```
✅ DONE (v0.2.0)                          🔥 NEXT (Weeks 1-10 → v1.0.0)
─────────────────                          ──────────────────────────────
✅ FastAPI REST API (19 endpoints)         🔥 Python SDK (pip install plaidify)
✅ Playwright browser engine               🔥 JavaScript SDK (npm install plaidify)
✅ Blueprint V2 schema                     🔥 CLI tool (plaidify connect/demo)
✅ MFA Manager (async, event-based)        🔥 Plaidify Link UI (React + vanilla)
✅ Data Extractor (typed fields + lists)   🔥 Blueprint Registry (search/install)
✅ Browser Pool Manager                    🔥 MCP Server (AI agent protocol)
✅ AES-256-GCM encryption                  🔥 Consent Engine (scoped access)
✅ JWT auth + user isolation               🔥 Agent SDK + audit trail
✅ 98 tests across 8 suites               🔥 Write operations (actions/payments)
✅ GreenGrid Energy demo                   🔥 Kubernetes + auto-scaling
✅ Interactive demo UI                     🔥 Admin Console + multi-tenancy
✅ CI/CD pipeline                          🔥 Documentation site
✅ Docker multi-stage build                🚀 v1.0.0 launch — May 23, 2026

                                           🔮 FUTURE (Weeks 11-14 → v1.1.0)
                                           ──────────────────────────────────
                                           🔮 API Connector blueprint type
                                           🔮 Open Banking (FDX, PSD2, UK OB)
                                           🔮 Blueprint Auto-Healer (LLM)
                                           🔮 Provider aggregation + routing
                                           🔮 Third-party aggregators (Teller, MX)
                                           🔮 Blueprint health monitoring
                                           🚀 v1.1.0 launch — Jun 19, 2026
```

---

*This plan is aggressive by design. Ship weekly. Cut scope before slipping dates. Every Friday is a potential release.*
