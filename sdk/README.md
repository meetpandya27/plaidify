# Plaidify Python SDK

Python client (async `Plaidify`, blocking `PlaidifySync`) and the `plaidify`
command-line tool for the Plaidify service.

## Install

The package is not on PyPI yet. Install it from a checkout of this repository
(Python 3.11+):

```bash
pip install ./sdk
# or: pip install "git+https://github.com/meetpandya27/plaidify.git#subdirectory=sdk"
```

## Connect Flow

```python
from plaidify import Plaidify

async with Plaidify(server_url="http://localhost:8000", api_key="pk_...") as pfy:
    result = await pfy.connect(
        "hydro_one",
        username="your_username",
        password="your_password",
    )
    print(result.status, result.data)
```

`POST /connect` needs a credential: an API key or a user access token.
Credentials are encrypted to a one-time server key before they are sent.
If that key cannot be obtained, `connect()` raises instead of falling back
to plaintext; pass `encrypt=False` to send them as plain JSON over TLS.

If the site asks for MFA, pass `mfa_handler=` (an async function that takes
the challenge and returns the code) and `connect()` answers it and follows the
job to the end; without one it raises `MFARequiredError`. A `pending` result
carries a `job_id` for `wait_for_access_job()`.

## Authentication

`api_key=` takes either credential the server understands; the SDK sends
each one where the server reads it:

```python
Plaidify(api_key="pk_...")   # API key or agent key -> X-API-Key header
Plaidify(api_key=jwt)        # user access token     -> Authorization: Bearer

async with Plaidify(server_url="http://localhost:8000") as pfy:
    await pfy.login("alice", "your-password")   # OAuth2 form at /auth/token; keeps the token
    key = await pfy.create_api_key("ci", scopes=["read:current_balance"], expires_days=30)
    print(key.raw_key)  # shown once
```

Managing API keys, agents, webhooks and refresh schedules needs a user access
token (`login()`), not an API key.

## Hosted Link Flow

Preferred production pattern:

1. Your backend creates a signed hosted link bootstrap token (`POST /link/bootstrap`).
2. The client redeems that token for a live hosted link session (`POST /link/sessions/bootstrap`).
3. The client opens the hosted link URL.

The Python SDK covers backend-created sessions (`create_link_session()`,
`get_link_url()`, `poll_link_status()`, `stream_link_events()`,
`register_webhook()`); the bootstrap helpers are in the JavaScript SDK.

## CLI

```bash
plaidify serve --port 8000                     # from anywhere inside the server checkout
export PLAIDIFY_API_KEY=pk_...                 # the credential for the commands below
plaidify connect hydro_one -u your_username    # prompts for the password (hidden)
echo "$SITE_PASSWORD" | plaidify connect hydro_one -u your_username --password-stdin
plaidify blueprint list
plaidify blueprint info hydro_one
plaidify blueprint validate ./connectors/your_site.json
plaidify registry search utility
plaidify health
```

Site passwords are never taken as command-line arguments (they would land
in shell history and the process list). Pass the Plaidify credential with
the `PLAIDIFY_API_KEY` environment variable rather than `--api-key`.

Server-side tools: `plaidify rotate-key --re-encrypt` re-keys stored data
from inside a server checkout once `ENCRYPTION_KEY` has changed
([SECURITY.md](../SECURITY.md#key-rotation-procedure)), and
`plaidify audit verify` checks the audit chain. The audit commands take a
user's access token, not an API key; `plaidify login` prints one (the
password is prompted for, hidden), and `audit verify` needs an administrator's:

```bash
export PLAIDIFY_API_KEY="$(plaidify login -u admin)"
plaidify audit verify
```

## Tests

```bash
PYTHONPATH=$PWD/sdk python -m pytest sdk/tests -q   # from the repository root
```

## License

MIT.
