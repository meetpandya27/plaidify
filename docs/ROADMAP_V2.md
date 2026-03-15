# Plaidify v2 Roadmap — LLM Extraction, Security Hardening & Agent Integration

> **Created:** March 15, 2026
> **Status:** Active
> **Objective:** Transform Plaidify from a manual-blueprint automation tool into an intelligent, secure, agent-ready data extraction platform.

---

## Overview

This roadmap addresses three major areas:

1. **LLM-Powered Adaptive Extraction** — Eliminate per-site CSS selector maintenance
2. **Security Hardening** — Close gaps between Plaidify and Plaid's production security model
3. **Agent Integration (Plaidify Link)** — Embeddable widget + webhooks + MCP server for AI agents

Each section includes a problem statement, proposed solution, and implementation plan.

---

## 1. LLM-Powered Adaptive Data Extraction

### Problem

Every new website requires a hand-authored JSON blueprint with exact CSS selectors (e.g., `span.sm-banner-acc-number`, `.h1-db-row-padding .col-md-4:first-child p.h1-widget-bTitle`). If a website redesigns its portal, all selectors break silently. The Hydro One connector is a prime example — we needed fallback selectors, a 5-second JS sleep for page stabilization, and conditional cleanup logic.

This doesn't scale for a general-purpose product.

### Solution: Hybrid LLM + Selector Caching

**Key insight:** Login forms are stable (username/password fields rarely change), but dashboard layouts change frequently. So we keep blueprint auth steps as-is, and replace the `extract` section with an LLM-powered adaptive layer.

#### Architecture

```
Site Connection Request
    │
    ▼
[Auth Steps] ← Still use blueprint (login forms are stable)
    │
    ▼
[Page Loaded — Dashboard]
    │
    ├─► [Cached Selectors Exist?]
    │       │
    │       ├─ YES → Try cached selectors
    │       │         │
    │       │         ├─ Success → Return data ✅
    │       │         └─ Failure → Fall through to LLM ↓
    │       │
    │       └─ NO → Fall through to LLM ↓
    │
    ▼
[LLM Extraction]
    ├─ Capture simplified DOM (strip scripts/styles, keep structure + text)
    ├─ Optionally capture screenshot (GPT-4o / Claude vision)
    ├─ Send prompt: "Extract {fields} from this page"
    ├─ LLM returns: { data: {...}, selectors: {...}, confidence: 0.95 }
    │
    ▼
[Verify + Cache]
    ├─ Re-extract using returned selectors (deterministic check)
    ├─ If verified → Cache selectors for this site+page
    └─ Return data ✅
```

#### New Blueprint Format

The `extract` section becomes **declarative intent** instead of hardcoded selectors:

```json
{
  "extract": {
    "strategy": "llm_adaptive",
    "fields": {
      "account_number": { "type": "text", "description": "The customer's account or ID number", "sensitive": true },
      "current_balance": { "type": "currency", "description": "Amount currently owed" },
      "due_date": { "type": "date", "description": "When the next payment is due" },
      "usage_history": {
        "type": "list",
        "description": "Monthly usage records",
        "fields": {
          "month": { "type": "text", "description": "The billing month" },
          "kwh": { "type": "number", "description": "Kilowatt-hours consumed" },
          "cost": { "type": "currency", "description": "Dollar amount for that month" }
        }
      }
    },
    "fallback_selectors": {
      "account_number": "span.sm-banner-acc-number"
    }
  }
}
```

#### Implementation Tasks

- [ ] **DOM simplification module** — Strip scripts/styles/SVG, add element IDs, reduce token count
- [ ] **LLM extraction provider** — Pluggable interface for OpenAI/Anthropic/local models
- [ ] **Prompt engineering** — Structured extraction prompt with field definitions + output schema
- [ ] **Selector caching layer** — Store extracted selectors per site+page hash, with TTL and failure tracking
- [ ] **Self-healing logic** — If cached selectors fail N times, invalidate and re-run LLM
- [ ] **Blueprint v3 schema** — Add `strategy: "llm_adaptive"` and `description` fields
- [ ] **Backward compatibility** — V2 blueprints with hardcoded selectors continue to work as-is
- [ ] **Multimodal fallback** — Screenshot-based extraction for JS-heavy SPAs
- [ ] **Cost controls** — Token budgets, model fallback chain (mini → full), usage metering

---

## 2. Security Hardening — Plaid vs Plaidify Gap Analysis

### Current State vs Plaid

| Layer | Plaid | Plaidify Today | Gap |
|-------|-------|---------------|-----|
| **Credentials in transit** | Captured in Plaid-hosted iframe; developer server never sees them | Plaintext JSON in POST body | **CRITICAL** |
| **Encryption key management** | HSM-managed, per-customer keys | Single env var `$ENCRYPTION_KEY` for all users | **HIGH** |
| **Key rotation** | Automated, zero-downtime rotation | No rotation mechanism | **HIGH** |
| **Rate limiting** | Built-in on all endpoints | Not implemented (defined in blueprints but not enforced) | **HIGH** |
| **CORS** | Strict per-environment | Default `*` (wildcard) | **HIGH** |
| **JWT lifetime** | Short-lived + refresh tokens | 7-day access token, no refresh | **MODERATE** |
| **Data isolation** | Per-customer encryption keys (envelope encryption) | Single key, queries filtered by user_id | **HIGH** |
| **Token architecture** | 3-token: link → public → access (one-time exchange) | 2-token: link → access (no exchange step) | **MODERATE** |
| **Audit logging** | Tamper-evident, retained for compliance | Structured JSON logging, no tamper detection | **MODERATE** |
| **Access scoping** | Per-product token scopes | All-or-nothing access | **LOW** |
| **Compliance** | SOC 2 Type II, ISO 27001 | None | Expected for early stage |

### Implementation Priority

#### Phase 1 — Do Now (before any production use)
- [ ] **Rate limiting on auth endpoints** — 5 attempts/min/IP on `/auth/token`, respect blueprint limits on `/connect`
- [ ] **Enforce CORS per environment** — Remove `*` default, require explicit origins in production
- [ ] **TLS enforcement** — Redirect HTTP → HTTPS, add HSTS headers
- [ ] **Short JWT lifetime + refresh tokens** — 15-min access tokens, 7-day refresh tokens stored in DB

#### Phase 2 — Before Production
- [ ] **Client-side credential encryption** — RSA/X25519 ephemeral keypair per session; credentials encrypted before leaving the browser
- [ ] **Envelope encryption** — Per-user Data Encryption Keys (DEKs) wrapped by a master key
- [ ] **Key rotation mechanism** — `key_version` column, support multiple active keys, background re-encryption job
- [ ] **3-token exchange flow** — Add `public_token` one-time exchange step between link and access tokens

#### Phase 3 — Enterprise Readiness
- [ ] **HSM/KMS integration** — AWS KMS, Azure Key Vault, or HashiCorp Vault for master key storage
- [ ] **Tamper-evident audit logging** — Hash chain or append-only log with integrity verification
- [ ] **Access token scoping** — Per-data-type permissions on access tokens
- [ ] **SOC 2 preparation** — Policies, controls, evidence collection

---

## 3. Agent Integration — Plaidify Link

### Vision

An AI agent should be able to trigger a Plaid-style popup where the user selects a website, enters credentials, completes MFA — and then the agent takes over with the extracted data. The agent never sees raw credentials.

### Architecture

```
┌──────────────────────────────────────────────────────┐
│                   AI Agent App                        │
│                                                       │
│  Agent: "I need your utility bill data."              │
│  Agent: [Opens Plaidify Link]                         │
│                                                       │
│  ┌──────────────────────────────────────────────┐    │
│  │         Plaidify Link (iframe/popup)          │    │
│  │                                               │    │
│  │  Step 1: Select your provider                 │    │
│  │  ┌──────────┐ ┌──────────┐ ┌──────────┐     │    │
│  │  │ Hydro One│ │GreenGrid │ │ Enbridge │     │    │
│  │  └──────────┘ └──────────┘ └──────────┘     │    │
│  │                                               │    │
│  │  Step 2: Enter credentials                    │    │
│  │  [Username: ________]                         │    │
│  │  [Password: ________]                         │    │
│  │  [Connect →]                                  │    │
│  │                                               │    │
│  │  Step 3: MFA (if needed)                      │    │
│  │  [Enter code: ______]                         │    │
│  │                                               │    │
│  │  Step 4: ✅ Connected! Data extracted.        │    │
│  └──────────────────────────────────────────────┘    │
│                                                       │
│  → Agent receives access_token via callback/webhook   │
│  → Agent calls GET /fetch_data to get the data        │
│  → Agent processes data autonomously                  │
└──────────────────────────────────────────────────────┘
```

### Three Integration Modes

#### Mode 1: Embeddable JavaScript Widget (web-based agents)

```javascript
const link = PlaidifyLink.create({
  serverUrl: 'https://your-plaidify-server',
  token: linkToken,
  onSuccess: (accessToken, metadata) => {
    agent.processData(accessToken);
  },
  onMFA: (challenge) => { /* optional custom MFA UI */ },
  onExit: (error) => { /* user closed without completing */ },
  onEvent: (event) => { /* telemetry */ }
});

link.open(); // Agent triggers the popup
```

#### Mode 2: SDK with Webhooks (server-side/Python agents)

```python
async with Plaidify(server_url="https://your-server") as pfy:
    link = await pfy.create_link()
    link_url = pfy.get_link_url(link.link_token)
    # Agent shows this URL to user in chat

    await pfy.register_webhook(link.link_token, url="https://agent/webhook")
    # Agent gets notified when user completes
```

#### Mode 3: MCP Server (LangChain, CrewAI, OpenAI function calling)

```python
@tool
def connect_utility_account(site: str) -> str:
    link = plaidify.create_link(site)
    return f"Please open this link: {link.url}"

@tool
def fetch_utility_data(access_token: str) -> dict:
    return plaidify.fetch_data(access_token)
```

### Implementation Tasks

- [ ] **Plaidify Link widget** (`link.js`) — Standalone embeddable script, iframe-based credential isolation, callback API
- [ ] **Hosted Link page** (`GET /link?token=`) — Self-contained institution picker → creds → MFA → success flow
- [ ] **Webhook system** — Register webhook URLs, fire events on LINK_COMPLETE/ERROR/MFA_REQUIRED
- [ ] **SSE event stream** — `GET /link/events/{link_token}` for real-time agent subscriptions
- [ ] **SDK link helpers** — `get_link_url()`, `register_webhook()`, link event polling
- [ ] **MCP server** — Plaidify as a tool provider for AI agent frameworks
- [ ] **Theming** — Configurable colors, logo, branding for the Link widget
- [ ] **postMessage API** — Secure cross-origin communication between iframe and parent

---

## Task Tracking

All tasks from this roadmap are tracked as GitHub Issues with the labels:
- `llm-extraction` — LLM adaptive extraction tasks
- `security` — Security hardening tasks
- `agent-integration` — Plaidify Link and agent UX tasks
- `priority:critical` / `priority:high` / `priority:medium`

See the [GitHub Issues](https://github.com/meetpandya27/plaidify/issues) for the full backlog.
