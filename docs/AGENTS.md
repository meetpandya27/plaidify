# Plaidify for AI Agents

Plaidify gives AI agents constrained, auditable access to websites behind login flows. The agent decides when to fetch data; Plaidify owns browser execution, MFA state, credential handling, and the resulting access controls.

## What Agents Get

- Structured JSON instead of raw HTML scraping
- Hosted-link handoff when a human needs to authenticate directly
- Access jobs with polling and stored (encrypted) results
- MFA continuation without exposing browser state to the agent
- Agent identities with their own site and scope limits, and consent grants bound to the agent
- MCP tools for assistants that speak the Model Context Protocol

## Recommended Boundary

1. The agent identifies the target site or data request.
2. Plaidify performs the login and extraction workflow.
3. If a user interaction is required, Plaidify hands off through hosted link or MFA continuation.
4. The agent receives structured data, access-job status, or approved artifacts.

That keeps privileged browser execution inside Plaidify and leaves planning, summarization, and user interaction orchestration in the agent.

For the longer-term executor isolation model, see [ISOLATED_ACCESS_RUNTIME.md](ISOLATED_ACCESS_RUNTIME.md).

## Agent identities and consent

An account owner (signed in with a user access token) registers an agent with
`POST /agents {name, allowed_sites?, allowed_scopes?, rate_limit?}`; the reply
carries the agent's API key (`pk_agent_…`) once. The agent sends it as
`X-API-Key`. Omitted lists mean no restriction, `[]` means nothing is allowed,
and `rate_limit` (`N/period`, e.g. `60/minute`) is enforced per agent.

To read a stored connection's data with `POST /fetch_data`, an agent needs a
consent grant bound to it:

1. The agent calls `POST /consent/request {access_token, scopes, duration_seconds?}`.
2. The owner approves it (`POST /consent/{request_id}/approve`, with the owner's
   token or own API key — an agent cannot approve its own request).
3. The agent sends the returned `consent_token` with
   `POST /fetch_data {access_token, consent_token}`. The fields returned are the
   intersection of the grant's, the agent's and the access token's scopes.

## Integration Options

| Option | Best for | Primary entry points |
| --- | --- | --- |
| REST API | Server-side agents in any language | `/blueprints`, `/connect`, `/access_jobs/{id}`, `/mfa/submit`, `/consent/request`, `/fetch_data` |
| Python SDK | Python agents and background workers | `Plaidify.connect()`, `wait_for_access_job()`, `submit_mfa()`, `request_consent()`, `fetch_data()` |
| TypeScript SDK | Browser or mobile shells around agent flows | `createHostedLinkBootstrap()`, `exchangeHostedLinkBootstrap()`, `getLinkUrl()` |
| MCP server | MCP-capable assistants and tool hosts | `python -m src.mcp_server` |

The SDKs are not published yet: install them from this repository
(`pip install ./sdk`; `sdk-js/README.md`).

## Direct Connect with the Python SDK

```python
import asyncio

from plaidify import Plaidify


async def prompt_for_code(challenge):
    return input(f"Enter {challenge.mfa_type} code for {challenge.site}: ")


async def main():
    async with Plaidify(server_url="http://localhost:8000", api_key="pk_your_key") as client:
        blueprints = await client.list_blueprints()
        print([bp.site for bp in blueprints.blueprints])

        result = await client.connect(
            "hydro_one",
            username="your_username",
            password="your_password",
            mfa_handler=prompt_for_code,
        )
        if result.connected:
            print(result.data)
        else:
            print(result.status, result.job_id)


asyncio.run(main())
```

With an `mfa_handler`, `connect()` answers the challenge and follows the job
to its end. Without one it raises `MFARequiredError` (carrying `session_id` for
`submit_mfa()`) when the site asks for MFA; a `pending` result carries a
`job_id` to follow with `wait_for_access_job()` or `get_access_job()`.
Credentials are encrypted to a one-time server key before they are sent.

## Hosted Link for Human-in-the-Loop Flows

When an agent needs the user to authenticate in their own browser or mobile shell, use the hosted-link bootstrap flow instead of pushing raw credentials through the agent.

```typescript
import { Plaidify } from "@plaidify/client";

// Server side, with the agent's (or backend's) API key:
const serverClient = new Plaidify({
  serverUrl: "https://api.example.com",
  apiKey: "pk_your_key",
});

const bootstrap = await serverClient.createHostedLinkBootstrap({
  site: "hydro_one",
  allowedOrigin: "https://app.example.com",
  scopes: ["current_balance", "due_date"],
});

// In the browser at https://app.example.com:
const publicClient = new Plaidify({ serverUrl: "https://api.example.com" });
const session = await publicClient.exchangeHostedLinkBootstrap(bootstrap.launch_token);
const hostedUrl = publicClient.getLinkUrl(session.link_token, {
  origin: "https://app.example.com",
});
```

A session created with an agent's key keeps that agent's site limits. The page
hands the host a one-time `public_token` on `CONNECTED`; exchange it on the
server with `POST /exchange/public_token`. This pattern is the preferred
production entrypoint for browser, iframe, and native-webview clients.

## MCP Server

Plaidify ships an MCP server in `src/mcp_server.py`. `PLAIDIFY_API_KEY` may be
an API key (`pk_…`, sent as `X-API-Key`) or a user access token (sent as a
bearer token).

Run it over stdio:

```bash
PLAIDIFY_SERVER_URL=http://localhost:8000 \
PLAIDIFY_API_KEY=pk_agent_your_key \
python -m src.mcp_server
```

Run it over HTTP (SSE, or `--transport streamable-http`):

```bash
PLAIDIFY_SERVER_URL=http://localhost:8000 \
PLAIDIFY_API_KEY=pk_agent_your_key \
python -m src.mcp_server --transport sse --port 3001
```

The HTTP transports listen on `127.0.0.1` by default. Anyone who can reach the
port acts with `PLAIDIFY_API_KEY`, so bind another interface (`--host`) only
behind your own authentication, and list the host names clients use in
`PLAIDIFY_MCP_ALLOWED_HOSTS` (or `--allowed-host`), which guards against DNS
rebinding.

Tools:

- `list_available_sites`
- `connect_site`
- `connect_utility_account` (creates a hosted-link session and returns its URL)
- `check_connection_status`
- `exchange_public_token`
- `get_job_status`
- `submit_mfa`
- `request_consent`
- `fetch_data`
- `list_connections`

## Consent, Scoping, and Safety

- Give each agent its own identity (`POST /agents`) with the narrowest `allowed_sites` and `allowed_scopes`; revoke it with `DELETE /agents/{id}`.
- Agent keys read stored data only through consent grants bound to them.
- Prefer hosted-link handoff when a human should own the login step.
- Keep `STRICT_READ_ONLY_MODE=true` unless you have a deliberate reason to broaden browser behavior.
- In production, leave anonymous hosted-link sessions disabled unless you explicitly need them and have origin restrictions in place.

## Operational Notes for Agent Workloads

- Poll `GET /access_jobs/{job_id}` for long-running jobs; terminal statuses are `completed`, `failed`, `blocked`, `cancelled` and `mfa_timeout`.
- Treat `mfa_required` and `pending` as normal control-flow states, not hard failures.
- In production, run jobs in the separate executor (`ACCESS_JOB_EXECUTION_MODE=redis-worker`): API restarts don't touch them, and an executor restart drains or fails them instead of leaving them hanging ([DEPLOYMENT.md](DEPLOYMENT.md#process-management)).
- Keep the agent focused on planning and result handling; avoid granting arbitrary browser access when a bounded Plaidify flow will do.

## Related Docs

- [README.md](../README.md) for the product overview
- [README.md](README.md) for the technical architecture guide and the API surface
- [MOBILE_LINK_INTEGRATION.md](MOBILE_LINK_INTEGRATION.md) for native hosted-link embeds
- [ISOLATED_ACCESS_RUNTIME.md](ISOLATED_ACCESS_RUNTIME.md) for executor isolation design
- [RUNBOOK.md](RUNBOOK.md) for operational response procedures
