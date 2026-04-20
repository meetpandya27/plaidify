# Plaidify for AI Agents

Plaidify gives AI agents constrained, auditable access to websites behind login flows. The agent decides when to fetch data; Plaidify owns browser execution, MFA state, credential handling, and the resulting access controls.

## What Agents Get

- Structured JSON instead of raw HTML scraping
- Hosted-link handoff when a human needs to authenticate directly
- Detached access jobs with polling and persisted results
- MFA continuation without exposing browser state to the agent
- Scoped consent grants, API keys, and agent identities
- MCP tools for assistants that speak the Model Context Protocol

## Recommended Boundary

1. The agent identifies the target site or data request.
2. Plaidify performs the login and extraction workflow.
3. If a user interaction is required, Plaidify hands off through hosted link or MFA continuation.
4. The agent receives structured data, access-job status, or approved artifacts.

That keeps privileged browser execution inside Plaidify and leaves planning, summarization, and user interaction orchestration in the agent.

For the longer-term executor isolation model, see [ISOLATED_ACCESS_RUNTIME.md](ISOLATED_ACCESS_RUNTIME.md).

## Integration Options

| Option | Best for | Primary entry points |
| --- | --- | --- |
| REST API | Server-side agents in any language | `/blueprints`, `/connect`, `/access_jobs`, `/mfa/submit`, `/fetch_data` |
| Python SDK | Python agents and background workers | `Plaidify.connect()`, `get_access_job()`, `submit_mfa()` |
| TypeScript SDK | Browser or mobile shells around agent flows | `createHostedLinkBootstrap()`, `exchangeHostedLinkBootstrap()`, `getLinkUrl()` |
| MCP server | MCP-capable assistants and tool hosts | `python -m src.mcp_server` |

## Direct Connect with the Python SDK

The Python SDK is the easiest way to wire Plaidify into an agent loop without hand-rolling HTTP calls.

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
            return

        if result.job_id:
            job = await client.get_access_job(result.job_id)
            print(job.status, job.result)


asyncio.run(main())
```

If you omit `mfa_handler`, treat `mfa_required` or `pending` as expected intermediate states and continue through `submit_mfa()` or `get_access_job()`.

## Hosted Link for Human-in-the-Loop Flows

When an agent needs the user to authenticate in their own browser or mobile shell, use the hosted-link bootstrap flow instead of pushing raw credentials through the agent.

```typescript
import { Plaidify } from "@plaidify/client";

const serverClient = new Plaidify({
  serverUrl: "https://api.example.com",
  apiKey: "pk_your_key",
});

const bootstrap = await serverClient.createHostedLinkBootstrap({
  site: "hydro_one",
  allowedOrigin: "https://app.example.com",
  scopes: ["read_bill"],
});

const publicClient = new Plaidify({ serverUrl: "https://api.example.com" });
const session = await publicClient.exchangeHostedLinkBootstrap(bootstrap.launch_token);
const hostedUrl = publicClient.getLinkUrl(session.link_token, {
  origin: "https://app.example.com",
});
```

This pattern is the preferred production entrypoint for browser, iframe, and native-webview clients.

## MCP Server

Plaidify already ships an MCP server in `src/mcp_server.py`.

Run it over stdio:

```bash
PLAIDIFY_SERVER_URL=http://localhost:8000 \
PLAIDIFY_API_KEY=pk_your_key \
python -m src.mcp_server
```

Run it as an SSE server:

```bash
PLAIDIFY_SERVER_URL=http://localhost:8000 \
PLAIDIFY_API_KEY=pk_your_key \
python -m src.mcp_server --transport sse --port 3001
```

The shipped MCP tools cover the current Plaidify flow surface, including:

- `list_available_sites`
- `connect_site`
- `connect_utility_account`
- `check_connection_status`
- `exchange_public_token`
- `fetch_data`
- `submit_mfa`
- `request_consent`

## Consent, Scoping, and Safety

- Use JWTs or API keys for authenticated agent access; API keys can be scoped, expired, and revoked.
- Consent grants can narrow the fields returned by `fetch_data` and related read paths.
- Prefer hosted-link handoff when a human should own the login step.
- Keep `STRICT_READ_ONLY_MODE=true` unless you have a deliberate reason to broaden browser behavior.
- In production, leave anonymous hosted-link sessions disabled unless you explicitly need them and have origin restrictions in place.

## Operational Notes for Agent Workloads

- Poll `GET /access_jobs/{job_id}` for long-running or detached jobs.
- Treat `mfa_required` and `pending` as normal control-flow states, not hard failures.
- Use Redis-backed worker execution in production so detached jobs can survive web-process restarts.
- Keep the agent focused on planning and result handling; avoid granting arbitrary browser access when a bounded Plaidify flow will do.

## Related Docs

- [README.md](../README.md) for the product overview
- [README.md](README.md) for the technical architecture guide
- [MOBILE_LINK_INTEGRATION.md](MOBILE_LINK_INTEGRATION.md) for native hosted-link embeds
- [ISOLATED_ACCESS_RUNTIME.md](ISOLATED_ACCESS_RUNTIME.md) for executor isolation design
- [RUNBOOK.md](RUNBOOK.md) for operational response procedures
