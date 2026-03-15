# Plaidify Python SDK

> **The open-source API for authenticated web data — for developers and AI agents.**

[![PyPI](https://img.shields.io/pypi/v/plaidify)](https://pypi.org/project/plaidify/)
[![Python](https://img.shields.io/pypi/pyversions/plaidify)](https://pypi.org/project/plaidify/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)

## Install

```bash
pip install plaidify
```

## Quick Start

### Python SDK (async)

```python
from plaidify import Plaidify

async with Plaidify(server_url="http://localhost:8000") as pfy:
    # One-call connect + extract
    result = await pfy.connect(
        "greengrid_energy",
        username="demo_user",
        password="demo_pass",
    )
    print(result.data["current_bill"])   # "$142.57"
    print(result.data["account_number"]) # "GGE-2024-78432"
```

### Python SDK (sync)

```python
from plaidify import PlaidifySync

with PlaidifySync(server_url="http://localhost:8000") as pfy:
    result = pfy.connect("greengrid_energy", username="demo_user", password="demo_pass")
    print(result.data)
```

### With MFA Handling

```python
async def handle_mfa(challenge):
    return input(f"Enter {challenge.mfa_type} code: ")

result = await pfy.connect(
    "greengrid_energy",
    username="mfa_user",
    password="mfa_pass",
    mfa_handler=handle_mfa,
)
```

### Multi-Step Link Flow (Plaid-style)

```python
async with Plaidify(server_url="http://localhost:8000", api_key="your-jwt") as pfy:
    # Step 1: Create link
    link = await pfy.create_link("greengrid_energy")

    # Step 2: Submit credentials
    link = await pfy.submit_credentials(link.link_token, "user", "pass")

    # Step 3: Fetch data
    result = await pfy.fetch_data(link.access_token)
    print(result.data)
```

## CLI

```bash
# Launch the full demo (servers + browser)
plaidify demo

# Start just the API server
plaidify serve --port 8000

# Connect to a site from the terminal
plaidify connect greengrid_energy -u demo_user -p demo_pass

# Browse available blueprints
plaidify blueprint list
plaidify blueprint info greengrid_energy

# Validate a blueprint file
plaidify blueprint validate ./connectors/my_site.json

# Test a blueprint against a live site
plaidify blueprint test ./connectors/greengrid_energy.json -u demo_user -p demo_pass

# Check server health
plaidify health
```

## API Reference

### `Plaidify` (async client)

| Method | Description |
|--------|-------------|
| `connect(site, *, username, password, extract_fields, mfa_handler)` | Connect + extract in one call |
| `submit_mfa(session_id, code)` | Submit MFA code |
| `mfa_status(session_id)` | Check MFA session status |
| `create_link(site)` | Create a link token (step 1) |
| `submit_credentials(link_token, username, password)` | Submit creds (step 2) |
| `fetch_data(access_token)` | Fetch extracted data (step 3) |
| `list_blueprints()` | List all available blueprints |
| `get_blueprint(site)` | Get blueprint details |
| `health()` | Server health check |
| `register(username, email, password)` | Register a new user |
| `login(username, password)` | Log in and get JWT |
| `me()` | Get current user profile |

### `PlaidifySync` (sync client)

Same API as above, but blocking. Use `with` instead of `async with`.

### Models

- **`ConnectResult`** — `status`, `data`, `session_id`, `mfa_type`, `metadata`
- **`BlueprintInfo`** — `site`, `name`, `domain`, `tags`, `has_mfa`, `extract_fields`
- **`LinkResult`** — `link_token`, `access_token`, `site`
- **`MFAChallenge`** — `session_id`, `site`, `mfa_type`, `metadata`

### Exceptions

All inherit from `PlaidifyError`:

| Exception | When |
|-----------|------|
| `ConnectionError` | Server unreachable |
| `AuthenticationError` | Bad credentials |
| `MFARequiredError` | MFA needed (no handler provided) |
| `BlueprintNotFoundError` | Unknown site |
| `RateLimitedError` | Too many requests |
| `ServerError` | Server 5xx |
| `InvalidTokenError` | Bad JWT/API key |

## Configuration

| Env Variable | Default | Description |
|-------------|---------|-------------|
| `PLAIDIFY_SERVER_URL` | `http://localhost:8000` | Server URL for CLI |
| `PLAIDIFY_API_KEY` | — | JWT token for authenticated endpoints |

## License

MIT — see [LICENSE](../LICENSE).
