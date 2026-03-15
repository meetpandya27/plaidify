# Plaidify for AI Agents

> **Give your AI agent secure, auditable access to any website behind a login form.**

This guide covers how to integrate Plaidify with AI agents, agentic frameworks, and MCP-compatible clients. Whether you're building with LangChain, CrewAI, AutoGen, OpenAI function calling, or the Model Context Protocol — Plaidify is designed to be the data layer your agents are missing.

---

## Table of Contents

- [Why Agents Need Plaidify](#why-agents-need-plaidify)
- [Architecture for Agents](#architecture-for-agents)
- [Integration Patterns](#integration-patterns)
  - [Direct REST API](#1-direct-rest-api-works-today)
  - [Python Tool Wrapper](#2-python-tool-wrapper-works-today)
  - [LangChain Tool](#3-langchain-tool-works-today)
  - [CrewAI Tool](#4-crewai-tool-works-today)
  - [OpenAI Function Calling](#5-openai-function-calling-works-today)
  - [MCP Server](#6-mcp-server-coming-phase-3)
- [Security & Consent Model](#security--consent-model)
- [Blueprint System for Agents](#blueprint-system-for-agents)
- [FAQ](#faq)

---

## Why Agents Need Plaidify

Every AI agent eventually needs to access real-world data that's locked behind authentication. Today, this is the hardest problem in agentic AI:

| Problem | Without Plaidify | With Plaidify |
|---------|-----------------|---------------|
| "What's my bank balance?" | Agent can't access bank portals | Agent calls Plaidify API → gets structured JSON |
| "How much is my electricity bill?" | Agent has no utility portal access | Blueprint for utility site → structured bill data |
| "Download my insurance EOB" | Agent can't authenticate to insurer | Blueprint handles login → returns document data |
| "What grades did I get this semester?" | Agent can't scrape university portals | Blueprint for university → structured transcript |

**The value proposition is simple:** Plaidify turns the authenticated web into a structured API that your agent can call.

### What Makes This Agent-Ready

- **Structured JSON responses** — no HTML parsing in your agent
- **Credential encryption** — Fernet AES-128-CBC at rest, never logged
- **User isolation** — each user's data is scoped and separate
- **Error hierarchy** — agents get typed errors (`mfa_required`, `captcha_required`, `site_unavailable`) they can reason about
- **Stateless API** — no session management needed in your agent
- **Self-hosted** — credentials never leave your infrastructure

---

## Architecture for Agents

```
┌───────────────────────┐
│   User / Chat UI      │
│   "What's my balance?" │
└──────────┬────────────┘
           │
           ▼
┌───────────────────────┐
│   AI Agent            │
│   (LangChain / CrewAI │
│    / AutoGen / GPT)   │
│                       │
│   Tool: PlaidifyTool  │───── Decides to call Plaidify
└──────────┬────────────┘
           │  POST /connect
           ▼
┌───────────────────────┐
│   Plaidify Server     │
│                       │
│   1. Load blueprint   │
│   2. Launch browser   │
│   3. Authenticate     │
│   4. Extract data     │
│   5. Return JSON      │
└──────────┬────────────┘
           │
           ▼
┌───────────────────────┐
│   Target Website      │
│   (bank, utility,     │
│    portal, etc.)      │
└───────────────────────┘
```

---

## Integration Patterns

### 1. Direct REST API (works today)

The simplest integration. Your agent makes HTTP calls to Plaidify.

```python
import requests

PLAIDIFY_URL = "http://localhost:8000"

def fetch_site_data(site: str, username: str, password: str) -> dict:
    """Agent tool: connect to a site and extract data."""
    response = requests.post(
        f"{PLAIDIFY_URL}/connect",
        json={"site": site, "username": username, "password": password}
    )
    response.raise_for_status()
    return response.json()

# Your agent calls this when it needs authenticated web data
result = fetch_site_data("demo_site", "user", "pass")
print(result["data"])
```

### 2. Python Tool Wrapper (works today)

A reusable tool class your agent framework can discover:

```python
import requests
from dataclasses import dataclass

@dataclass
class PlaidifyTool:
    """Give your AI agent access to authenticated web data."""
    
    base_url: str = "http://localhost:8000"
    jwt_token: str | None = None  # Set after register/login
    
    @property
    def _headers(self) -> dict:
        if self.jwt_token:
            return {"Authorization": f"Bearer {self.jwt_token}"}
        return {}
    
    def connect(self, site: str, username: str, password: str) -> dict:
        """One-step: log into a site and extract data."""
        resp = requests.post(
            f"{self.base_url}/connect",
            json={"site": site, "username": username, "password": password}
        )
        resp.raise_for_status()
        return resp.json()
    
    def create_link(self, site: str) -> str:
        """Create a reusable link token for a site."""
        resp = requests.post(
            f"{self.base_url}/create_link?site={site}",
            headers=self._headers
        )
        resp.raise_for_status()
        return resp.json()["link_token"]
    
    def submit_credentials(self, link_token: str, username: str, password: str) -> str:
        """Submit credentials for a link (encrypted at rest)."""
        resp = requests.post(
            f"{self.base_url}/submit_credentials",
            params={"link_token": link_token, "username": username, "password": password},
            headers=self._headers
        )
        resp.raise_for_status()
        return resp.json()["access_token"]
    
    def fetch_data(self, access_token: str) -> dict:
        """Fetch extracted data using an access token."""
        resp = requests.get(
            f"{self.base_url}/fetch_data?access_token={access_token}",
            headers=self._headers
        )
        resp.raise_for_status()
        return resp.json()
    
    def list_available_sites(self) -> list[str]:
        """List all available blueprints/connectors."""
        # Reads the connectors directory
        import os
        connectors_dir = os.path.join(os.path.dirname(__file__), "..", "connectors")
        return [f.replace(".json", "") for f in os.listdir(connectors_dir) if f.endswith(".json")]

# Usage
tool = PlaidifyTool(base_url="http://localhost:8000")
data = tool.connect("demo_site", "user", "pass")
```

### 3. LangChain Tool (works today)

```python
from langchain.tools import tool
import requests

PLAIDIFY_URL = "http://localhost:8000"

@tool
def plaidify_connect(site: str, username: str, password: str) -> str:
    """Connect to a website and extract authenticated data.
    
    Use this tool when you need to access data that requires logging into a website,
    such as bank portals, utility companies, insurance sites, or any login-protected page.
    
    Args:
        site: The blueprint name (e.g., 'chase_bank', 'electric_company')
        username: The user's login username for that site
        password: The user's login password for that site
    
    Returns:
        JSON string with extracted data from the authenticated session
    """
    response = requests.post(
        f"{PLAIDIFY_URL}/connect",
        json={"site": site, "username": username, "password": password}
    )
    return response.json()

@tool  
def plaidify_health() -> str:
    """Check if the Plaidify server is running and healthy."""
    response = requests.get(f"{PLAIDIFY_URL}/health")
    return response.json()

# Use in a LangChain agent
from langchain.agents import create_tool_calling_agent
from langchain_openai import ChatOpenAI

llm = ChatOpenAI(model="gpt-4o")
tools = [plaidify_connect, plaidify_health]
agent = create_tool_calling_agent(llm, tools, prompt=your_prompt)
```

### 4. CrewAI Tool (works today)

```python
from crewai.tools import tool
import requests

PLAIDIFY_URL = "http://localhost:8000"

@tool("Plaidify Web Data Extractor")
def plaidify_extract(site: str, username: str, password: str) -> dict:
    """Connect to an authenticated website and extract structured data.
    Useful for accessing bank accounts, utility portals, insurance sites,
    and any website that requires login credentials."""
    response = requests.post(
        f"{PLAIDIFY_URL}/connect",
        json={"site": site, "username": username, "password": password}
    )
    return response.json()

# Use in a CrewAI agent
from crewai import Agent

financial_agent = Agent(
    role="Financial Data Analyst",
    goal="Access and analyze the user's financial data from banking portals",
    tools=[plaidify_extract],
    backstory="You help users understand their finances by accessing their bank portals securely."
)
```

### 5. OpenAI Function Calling (works today)

```python
import openai
import requests
import json

PLAIDIFY_URL = "http://localhost:8000"

# Define the function schema
tools = [{
    "type": "function",
    "function": {
        "name": "plaidify_connect",
        "description": "Connect to an authenticated website and extract data. Use when the user asks about data locked behind a login form (bank balance, utility bill, etc.)",
        "parameters": {
            "type": "object",
            "properties": {
                "site": {
                    "type": "string",
                    "description": "The blueprint name for the target site (e.g., 'chase_bank')"
                },
                "username": {
                    "type": "string", 
                    "description": "The user's login username"
                },
                "password": {
                    "type": "string",
                    "description": "The user's login password"  
                }
            },
            "required": ["site", "username", "password"]
        }
    }
}]

def execute_plaidify_call(args: dict) -> dict:
    resp = requests.post(f"{PLAIDIFY_URL}/connect", json=args)
    return resp.json()

# In your agent loop
response = openai.chat.completions.create(
    model="gpt-4o",
    messages=[{"role": "user", "content": "Check my bank balance on demo_site"}],
    tools=tools
)

# Handle tool calls
for tool_call in response.choices[0].message.tool_calls:
    if tool_call.function.name == "plaidify_connect":
        args = json.loads(tool_call.function.arguments)
        result = execute_plaidify_call(args)
```

### 6. MCP Server (coming Phase 3)

> **This is the big one.** We're building Plaidify as an MCP (Model Context Protocol) server so any compatible AI client — Claude, ChatGPT, and others — can use it as a tool natively.

```yaml
# ~/.config/claude/mcp_servers.yaml (planned)
plaidify:
  command: plaidify
  args: ["serve", "--mcp"]
  env:
    PLAIDIFY_URL: "http://localhost:8000"
```

Once configured, your AI assistant can:
- `"What's my bank balance?"` → Plaidify logs in, extracts balance
- `"How much is my electric bill?"` → Plaidify reads utility portal
- `"Download my latest insurance statement"` → Plaidify fetches document

**With built-in consent:** The agent always asks the user before accessing a new site, and all actions are logged.

> **Want to help build the MCP server?** This is one of our highest-impact open issues. See [CONTRIBUTING.md](../CONTRIBUTING.md).

---

## Security & Consent Model

### How Credentials Are Protected

```
User provides credentials
        │
        ▼
┌─────────────────────┐
│  Pydantic validates  │  ← Input validation, min lengths
└─────────┬───────────┘
          │
          ▼
┌─────────────────────┐
│  Fernet encrypts     │  ← AES-128-CBC, key from env var
└─────────┬───────────┘
          │
          ▼
┌─────────────────────┐
│  SQLAlchemy stores   │  ← Only ciphertext in DB
└─────────┬───────────┘
          │
          ▼
┌─────────────────────┐
│  Decrypt on use only │  ← Plaintext never persisted
└─────────────────────┘
```

**What we guarantee today:**
- Credentials are encrypted at rest (Fernet/AES-128-CBC)
- Encryption key is never hardcoded (required env var)
- Credentials are never logged or printed
- Users can only access their own links and tokens (tested)
- JWT tokens have configurable expiry

**What's coming:**
- **Consent model** — agents must request permission per-site, per-action
- **Scoped permissions** — read vs. write, per-site granularity
- **Audit trails** — every agent action logged with timestamp and context
- **Credential vaulting** — HashiCorp Vault integration
- **Rate limiting** — per-user, per-agent throttling

---

## Blueprint System for Agents

### How Agents Discover Available Sites

Your agent can discover which sites have blueprints:

```python
import os

def list_blueprints():
    """List all available site blueprints."""
    connectors_dir = "connectors/"
    sites = []
    for f in os.listdir(connectors_dir):
        if f.endswith(".json"):
            sites.append(f.replace(".json", ""))
    return sites

# Returns: ["demo_site", "mock_site", ...]
```

### How to Add a New Site

1. Figure out the CSS selectors for the login form
2. Write a JSON blueprint:

```json
{
  "name": "Electric Company",
  "login_url": "https://electricco.com/login",
  "fields": {
    "username": "#account-email",
    "password": "#account-password", 
    "submit": "button[type='submit']"
  },
  "post_login": [
    { "wait": ".dashboard-content" },
    {
      "extract": {
        "current_bill": ".bill-amount",
        "due_date": ".due-date",
        "kwh_used": ".usage-amount"
      }
    }
  ]
}
```

3. Save to `connectors/electric_company.json`
4. Your agent can now call: `plaidify_connect("electric_company", user, pass)`

### Error Handling for Agents

Plaidify returns typed errors that agents can reason about:

```python
# Errors your agent might receive:
{
    "error": "mfa_required",
    "detail": "Multi-factor authentication required",
    "session_id": "abc123"  # Agent can prompt user for OTP
}

{
    "error": "captcha_required", 
    "detail": "CAPTCHA challenge detected"  # Agent should escalate to user
}

{
    "error": "site_unavailable",
    "detail": "Target site returned 503"  # Agent should retry later
}

{
    "error": "credentials_invalid",
    "detail": "Login failed — check username/password"  # Agent should ask user
}
```

Your agent can use these typed errors to make intelligent decisions about retries, escalation, or alternative approaches.

---

## FAQ

### Is this production-ready?

The API, auth, encryption, and database layers are production-quality with 53 tests and 80% coverage. The browser engine (Playwright) is **not yet implemented** — it currently returns simulated data. We're building it in Phase 1.

### Can my agent actually log into real websites today?

Not yet. The engine returns simulated responses. Once Phase 1 (Playwright integration) ships, yes.

### How is this different from just running Playwright myself?

Plaidify adds the abstraction layer: blueprint-driven login flows, credential encryption, user isolation, structured data extraction, error typing, and a REST API. You don't write browser automation code — you write a JSON config.

### Is it safe to pass user credentials through this?

Credentials are Fernet-encrypted at rest and never logged. The server is designed to be self-hosted on your infrastructure. In Phase 3, we're adding consent models and audit trails specifically for AI agent use cases.

### Can I use this with Claude / ChatGPT / other AI assistants?

Today, through the REST API (your agent calls it as a tool). In Phase 3, we're building an MCP server so compatible clients can use Plaidify natively.

### How do I contribute a blueprint?

Write a JSON file describing the login flow for a site and submit a PR. This is the #1 way to contribute. See [CONTRIBUTING.md](../CONTRIBUTING.md).

---

<p align="center">
  <strong>Ready to give your agent access to the authenticated web?</strong>
  <br /><br />
  <a href="../README.md#-30-second-quickstart">⚡ Quickstart</a> &nbsp;·&nbsp;
  <a href="https://github.com/meetpandya27/plaidify/issues">💬 Questions</a> &nbsp;·&nbsp;
  <a href="../CONTRIBUTING.md">🤝 Contribute</a>
</p>
