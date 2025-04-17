# 🌐 Plaidify  
*Connect to any website that requires a username + password — securely, seamlessly, and consistently.*

![MIT License](https://img.shields.io/badge/license-MIT-green)
![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen)

---

## 📚 Table of Contents
1. [What is Plaidify?](#-what-is-plaidify)
2. [Key Features](#-key-features)
3. [How Plaidify Works](#-how-plaidify-works)
4. [Quick Start](#-quick-start)
5. [Prerequisites](#-prerequisites)
6. [Connector Blueprints](#-connector-blueprints)
7. [Project Structure](#-project-structure)
8. [Security by Design](#-security-by-design)
9. [Roadmap](#-roadmap)
10. [Contributing](#-contributing)
11. [License](#-license)
12. [About](#-about)

---

## 🔗 What is Plaidify?
Plaidify is an open‑source platform that lets developers programmatically **connect to any website protected by a standard login form**.  
It abstracts away the quirks of individual sites and exposes a **unified API** to:

* Establish a user‑authorized session  
* Execute the site‑specific login flow defined in a _blueprint_  
* Return structured data selected by the author of that blueprint  

If a site has no public API, Plaidify gives you a repeatable way to obtain the data your users already have permission to access — with **zero changes** to the target site.

---

## 🎯 Key Features
| Capability | Description |
|------------|-------------|
| **Universal Login** | Works with any site that uses basic username / email + password authentication. |
| **Blueprint‑Driven** | Each site’s flow is defined in a small JSON file. No custom code per site required. |
| **Unified API** | `/connect`, `/status`, and `/disconnect` endpoints keep integration simple. |
| **Session Isolation** | Every connection runs in its own sandbox, ensuring data separation and safety. |
| **Extensible** | Written in Python 3.9 +, ready to integrate secret vaults, OTP handlers, or AI helpers. |
| **Open License** | MIT licensed — use it in personal projects, SaaS products, or enterprise tooling. |

---

## ⚙️ How Plaidify Works
```text
┌────────────┐    1. /connect                 ┌────────────────────────┐
│  Your App  │  ───────────────────────────▶  │  Plaidify API Server   │
└────────────┘                                └─────────┬──────────────┘
                                                       ▼
                                             2. Load Blueprint
                                                       ▼
                                             3. Execute Login Flow
                                                       ▼
                                             4. Extract Structured Data
                                                       ▼
┌────────────┐    5. JSON Response          ┌────────────────────────┐
│  Your App  │  ◀──────────────────────────  │  Plaidify API Server   │
└────────────┘                                └────────────────────────┘
Input site, username, password

Plaidify reads the matching JSON blueprint in /connectors/.

The connection engine performs the site‑specific steps (fields, buttons, waits).

Defined data points are captured and normalized.

Your app receives a clean JSON payload.

📦 Quick Start
# 1. Clone
git clone https://github.com/meetpandya/plaidify.git
cd plaidify

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run the API
uvicorn main:app --reload

# 4. Explore
# Open http://127.0.0.1:8000/docs for the interactive Swagger UI.
Sample POST /connect
{
  "site": "demo_site",
  "username": "demo_user",
  "password": "secret123"
}
Sample Response
{
  "status": "connected",
  "data": {
    "profile_status": "active",
    "last_synced": "2025-04-17T12:00:00Z"
  }
}

## 🔗 Plaidify "Link Token" Flow
As an alternative to the single-step /connect, Plaidify supports a flow inspired by Plaid:
1. Create a link token for the desired site:
   ```
   curl -X POST "http://127.0.0.1:8000/create_link?site=mock_site"
   ```
   This returns a JSON object with a `link_token`.

2. Submit credentials for that link token:
   ```
   curl -X POST "http://127.0.0.1:8000/submit_credentials?link_token=<link_token>&username=mock_user&password=mock_password"
   ```
   This returns an `access_token`, which you can store or use immediately.

3. Fetch data with your access token:
   ```
   curl "http://127.0.0.1:8000/fetch_data?access_token=<access_token>"
   ```
   This triggers the site’s login flow according to the JSON blueprint in `/connectors`, then returns extracted data.

This approach provides a more granular, multi‑step process that mirrors how Plaid handles linking and storing financial accounts.
🛠️ Prerequisites

Requirement	Notes
Python 3.9 +	Confirm with python --version.
pip / poetry	Any modern Python package manager.
(Optional) Docker 20.10 +	A docker-compose.yml is included for containerised runs.
🧩 Connector Blueprints
Each file in /connectors/ fully describes how Plaidify should interact with a given site.

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
    { "extract": {
        "profile_status": "#status",
        "last_synced":    "#last-sync"
    }}
  ]
}
Add a new site by dropping in a JSON file and restarting Plaidify — no code changes needed.

📁 Project Structure
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
🔐 Security by Design
Ephemeral Credentials – Used only for the active session and then discarded.

No Logging of Secrets – Sensitive fields are never written to disk or stdout.

Vault Ready – Planned integrations for HashiCorp Vault, Azure Key Vault, GCP Secret Manager.

Blueprint Isolation – Each connector’s scope is limited to its own sandbox, preventing cross‑site data leakage.

🗺️ Roadmap
 OTP / email code workflows

 Secure vault plugins

 Admin dashboard for monitoring connections

 Automated blueprint generator (AI‑assisted)

 Connection health analytics & alerting

🤝 Contributing
We love pull requests!
See CONTRIBUTING.md for setup, coding style, and PR guidelines.

Good first issues:

Create a connector for a public demo site.

Add integration tests using pytest.

Improve error handling in src/core/session.py.

📄 License
Plaidify is released under the MIT License.

🧠 About
Created & maintained by @meetpandya.
Our mission is to give everyone a secure, unified way to access their own data — wherever it lives.