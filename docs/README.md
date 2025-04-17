# 🌐 Plaidify  
*Connect to any website that requires a username + password — securely, seamlessly, and consistently.*

![MIT License](https://img.shields.io/badge/license-MIT-green)
![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen)

---

## 📚 Table of Contents
1. [What is Plaidify?](#-what-is-plaidify)
2. [Key Features](#-key-features)
3. [How Plaidify Works](#-how-plaidify-works)
4. [Quick Start](#-quick-start)
5. [Prerequisites](#-prerequisites)
6. [Connector Blueprints](#-connector-blueprints)
7. [Project Structure](#-project-structure)
8. [Security by Design](#-security-by-design)
9. [Roadmap](#-roadmap)
10. [Contributing](#-contributing)
11. [License](#-license)
12. [About](#-about)

---

## 🔗 What is Plaidify?
Plaidify is an open‑source platform that lets developers programmatically **connect to any website protected by a login form**, using a secure and unified API.

It abstracts away the quirks of individual sites and exposes a reusable connection layer that lets you:
- Establish user-authorized sessions
- **Extract structured data**
- **Perform user-authorized actions** (e.g., submit forms, click buttons, acknowledge alerts)

This means you can read data *and* take meaningful actions — even if the site doesn’t offer an API.

---

## 🎯 Key Features
| Capability | Description |
|------------|-------------|
| **Universal Login** | Works with any site using standard username/password authentication. |
| **Blueprint‑Driven** | Site flows (fields, clicks, waits, data, actions) are defined in a JSON file. |
| **Structured Data + Actions** | Extract data, submit forms, toggle switches — all inside the same session. |
| **Unified API** | `/connect`, `/status`, and `/disconnect` endpoints keep integration simple. |
| **Session Isolation** | Each request runs securely in isolation. |
| **Extensible** | Ready for vaults, OTP handling, AI-driven flows, and more. |
| **MIT Licensed** | Free to use in open-source and commercial products. |

---

## ⚙️ How Plaidify Works
```text
┌────────────┐    1. /connect                 ┌────────────────────────┐
│  Your App  │  ───────────────────────────▶  │  Plaidify API Server   │
└────────────┘                                └─────────┬──────────────┘
                                                       ▼
                                             2. Load Blueprint
                                                       ▼
                                             3. Login + Flow Execution
                                                   ├──── Extract data
                                                   └──── Take actions
                                                       ▼
                                               4. Return JSON result
                                                       ▼
┌────────────┐    5. JSON Response          ┌────────────────────────┐
│  Your App  │  ◀──────────────────────────  │  Plaidify API Server   │
└────────────┘                                └────────────────────────┘
```

---

## 📦 Quick Start

```bash
# 1. Clone
git clone https://github.com/meetpandya/plaidify.git
cd plaidify

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run the API
uvicorn src.main:app --reload

# 4. Explore
# Open http://127.0.0.1:8000/docs for the interactive Swagger UI.
```

**Sample POST /connect**

```json
{
  "site": "demo_site",
  "username": "demo_user",
  "password": "secret123"
}
```

**Sample Response**

```json
{
  "status": "connected",
  "data": {
    "profile_status": "active",
    "last_synced": "2025-04-17T12:00:00Z"
  },
  "actions_performed": ["clicked_#acknowledge"]
}
```

---

## 🛠️ Prerequisites

| Requirement         | Notes                                         |
|---------------------|----------------------------------------------|
| Python 3.9 +        | Confirm with `python --version`.             |
| pip / poetry        | Any modern Python package manager.           |
| (Optional) Docker 20.10 + | A docker-compose.yml is included for containerised runs. |

---

## 🧩 Connector Blueprints

Each connector JSON defines:
- login fields
- what to extract
- **what actions to perform**

Example: `connectors/demo_site.json`
```json
{
  "name": "Demo Site",
  "login_url": "https://demo.example.com/login",
  "fields": {
    "username": "#user",
    "password": "#pass",
    "submit":   "#login-btn"
  },
  "post_login": [
    { "wait": "#dashboard" },
    { "click": "#acknowledge" },
    { "extract": {
        "profile_status": "#status",
        "last_synced":    "#last-sync"
    }}
  ]
}
```

Supported actions:
- `"click": "#selector"`
- `"fill": { "selector": "#input", "value": "some text" }`
- `"submit"` (submit form)
- `"extract": { ... }`
- `"wait": "#selector"`

---

## 📁 Project Structure

```bash
plaidify/
├─ src/
│  ├─ main.py          # FastAPI entry‑point
│  ├─ core/            # connection engine & helpers
│  └─ models.py        # Pydantic response models
├─ connectors/         # JSON blueprints (one per site)
├─ docs/               # Diagrams, guides, badges
├─ tests/              # Unit + blueprint tests
├─ docker-compose.yml
├─ requirements.txt
└─ README.md
```

### Codebase Overview

- **src/main.py**  
  FastAPI application entry point. Defines API endpoints (`/connect`, `/status`, `/disconnect`) and handles request routing.

- **src/core/**  
  Contains the core connection engine, browser/session management, blueprint execution logic, and utility helpers.  
  - `engine.py`: Orchestrates the login flow, executes blueprint actions, manages browser automation (e.g., with Playwright or Selenium).
  - `blueprint.py`: Loads and validates connector blueprints, parses supported actions.
  - `actions.py`: Implements supported actions (click, fill, extract, wait, submit, etc.) as Python functions.
  - `session.py`: Handles session isolation, ephemeral credential storage, and cleanup.

- **src/models.py**  
  Defines Pydantic models for request/response validation and OpenAPI docs.

- **connectors/**  
  JSON files describing each site's login flow and post-login actions.  
  To add a new site, create a new JSON blueprint here.

- **tests/**  
  Unit and integration tests for the engine, actions, and blueprint validation.  
  Includes sample blueprints and test credentials for local testing.

- **docker-compose.yml**  
  For running Plaidify and its dependencies in containers.

- **requirements.txt**  
  Python dependencies.

### Extending Plaidify

#### Adding a New Connector

1. **Create a Blueprint:**  
   Add a new JSON file in `connectors/` describing the site's login form, fields, and post-login actions.
2. **Test Locally:**  
   Use the `/connect` endpoint with test credentials and verify the flow.
3. **Write Tests:**  
   Add tests in `tests/` to ensure the connector works and is robust to site changes.

#### Adding a New Action

1. **Implement the Action:**  
   Add a new function in `src/core/actions.py` to handle the action logic.
2. **Update Blueprint Schema:**  
   Ensure `src/core/blueprint.py` recognizes the new action type.
3. **Document Usage:**  
   Update the README and provide an example in the connector blueprint section.
4. **Test Thoroughly:**  
   Add unit and integration tests for the new action.

#### Security & Best Practices

- Never log or persist credentials.
- Use ephemeral browser sessions.
- Validate all blueprint inputs.
- Review new blueprints and actions for security implications.

---

## 🔐 Security by Design

- **Ephemeral Credentials** – Used only for the active session and then discarded.
- **No Logging of Secrets** – Sensitive fields are never written to disk or stdout.
- **Vault Ready** – Planned integrations for HashiCorp Vault, Azure Key Vault, GCP Secret Manager.
- **Blueprint Isolation** – Each connector’s logic is sandboxed to prevent cross‑site interference.

---

## 🗺️ Roadmap

- [ ] OTP / email verification workflows  
- [ ] Secure vault plugins  
- [ ] Admin dashboard for session monitoring  
- [ ] Real-time connector tester UI  
- [ ] AI assistant to auto-generate blueprints  
- [ ] User-level permissions / audit trail support  

---

## 🤝 Contributing

We **love** pull requests!  
See **[`CONTRIBUTING.md`](CONTRIBUTING.md)** for setup, coding style, and PR guidelines.

**Good first issues:**
1. Add action examples to a demo connector  
2. Build integration test coverage for post-login actions  
3. Add validation rules for blueprint structure  

---

## 📄 License

Plaidify is released under the [MIT License](LICENSE).

---

## 🧠 About

Created & maintained by **[@meetpandya](https://github.com/meetpandya27)**.  
Plaidify helps developers connect to any web platform securely — to **extract data and take action** — all through a unified interface.