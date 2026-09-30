"""
Plaidify MCP Server — exposes Plaidify as tools for AI agent frameworks.

Runs as a stdio server (for Claude Desktop, etc.) or HTTP SSE server.
Provides tools for:
  - list_available_sites(): list connectable blueprints
  - connect_site(site, username, password): connect and extract data directly
  - connect_utility_account(site): create a link session, return link URL
  - check_connection_status(link_token): check link session progress
  - fetch_data(access_token): retrieve extracted data
  - submit_mfa(session_id, code): submit MFA verification
  - get_job_status(job_id): progress, MFA prompt or outcome of an access job
  - request_consent(access_token, scopes): request scoped data access
  - list_connections(): list active connections

Usage (stdio):
    python -m src.mcp_server

Usage (HTTP, SSE or streamable HTTP):
    python -m src.mcp_server --transport sse --port 3001 [--host 127.0.0.1]
    python -m src.mcp_server --transport streamable-http --port 3001

The HTTP transports listen on 127.0.0.1 by default. Anyone who can reach the
port acts with PLAIDIFY_API_KEY, so only bind another interface behind your
own authentication, and list the host names clients use in
PLAIDIFY_MCP_ALLOWED_HOSTS (DNS-rebinding protection).

Environment variables:
    PLAIDIFY_SERVER_URL        — Base URL of the Plaidify API (default: http://localhost:8000)
    PLAIDIFY_API_KEY           — API key (pk_…, sent as X-API-Key) or a user JWT (sent as a Bearer token)
    PLAIDIFY_MCP_ALLOWED_HOSTS — Comma-separated Host values accepted when bound to a non-loopback host
"""

from __future__ import annotations

import argparse
import os
from typing import Any, Optional

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

# ── Server Setup ──────────────────────────────────────────────────────────────

mcp = FastMCP(
    "Plaidify",
    instructions=(
        "Plaidify is an open-source API for authenticated web data extraction. "
        "Use these tools to connect to utility/energy/bank portals, authenticate "
        "users through a hosted link flow, and extract structured data.\n\n"
        "Typical flow:\n"
        "1. list_available_sites() — see what's connectable\n"
        "2. connect_site(site, username, password) — direct extraction\n"
        "   OR connect_utility_account(site) → user opens link → check_connection_status()\n"
        "3. For scoped access: request_consent() before fetch_data()\n"
        "4. If MFA required: submit_mfa(session_id, code), then get_job_status(job_id)\n\n"
        "Sandbox: when the server runs with DEMO_MODE=true, list_available_sites() "
        "returns connectable demo sites — demo_utility (OTP, code 123456), "
        "demo_bank (security question, answer 'plaidify'), and demo_saas (no MFA). "
        "Demo credentials are shown on each site's sign-in page."
    ),
)

# Default Plaidify server URL (override via PLAIDIFY_SERVER_URL env var)
PLAIDIFY_SERVER_URL = os.environ.get("PLAIDIFY_SERVER_URL", "http://localhost:8000")
PLAIDIFY_API_KEY = os.environ.get("PLAIDIFY_API_KEY", "")


def _headers() -> dict[str, str]:
    h: dict[str, str] = {"Content-Type": "application/json"}
    if PLAIDIFY_API_KEY:
        # API keys (pk_…, agents pk_agent_…) travel only in X-API-Key; a user
        # JWT is a Bearer token.
        if PLAIDIFY_API_KEY.startswith("pk_"):
            h["X-API-Key"] = PLAIDIFY_API_KEY
        else:
            h["Authorization"] = f"Bearer {PLAIDIFY_API_KEY}"
    return h


# Reuse a single httpx.AsyncClient for connection pooling
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            base_url=PLAIDIFY_SERVER_URL,
            headers=_headers(),
            timeout=60.0,
        )
    return _client


async def _api(
    method: str,
    path: str,
    json: dict | None = None,
    params: dict | None = None,
) -> dict[str, Any]:
    """Make an API call to the Plaidify server."""
    client = _get_client()
    if method == "GET":
        resp = await client.get(path, params=params)
    else:
        resp = await client.post(path, json=json, params=params)
    resp.raise_for_status()
    return resp.json()


def _format_data(extracted: dict | list) -> str:
    """Format extracted data for readable tool output."""
    import json as json_mod

    lines: list[str] = []
    if isinstance(extracted, dict):
        for key, value in extracted.items():
            if isinstance(value, (dict, list)):
                lines.append(f"  {key}: {json_mod.dumps(value, indent=2)}")
            else:
                lines.append(f"  {key}: {value}")
    else:
        lines.append(str(extracted))
    return "\n".join(lines)


# ── Tools ─────────────────────────────────────────────────────────────────────


@mcp.tool()
async def list_available_sites() -> str:
    """List all available site blueprints that can be connected.

    Returns a formatted list of sites with names, domains, and supported features.
    Each site has:
    - site: identifier used for connect_site() or connect_utility_account()
    - name: human-readable name
    - domain: target website
    - has_mfa: whether MFA may be required
    """
    data = await _api("GET", "/blueprints")
    blueprints = data.get("blueprints", [])
    if not blueprints:
        return "No sites available."

    lines = [f"Available sites ({data.get('count', len(blueprints))}):\n"]
    for bp in blueprints:
        mfa_flag = " [MFA]" if bp.get("has_mfa") else ""
        tags = ", ".join(bp.get("tags", []))
        lines.append(f"  • {bp['name']} ({bp['site']}){mfa_flag}")
        lines.append(f"    Domain: {bp.get('domain', 'N/A')}")
        if tags:
            lines.append(f"    Tags: {tags}")
    return "\n".join(lines)


@mcp.tool()
async def connect_site(site: str, username: str, password: str) -> str:
    """Connect to a site and extract data directly with credentials.

    This is the simplest integration — provide credentials and get data back.
    If MFA is required, you'll receive a session_id to use with submit_mfa();
    a slow connection answers with a job_id to follow with get_job_status().

    Args:
        site: Site identifier from list_available_sites() (e.g. "hydro_one").
        username: Login username for the target site.
        password: Login password for the target site.

    Returns:
        Extracted data or MFA challenge details.
    """
    try:
        data = await _api(
            "POST",
            "/connect",
            json={
                "site": site,
                "username": username,
                "password": password,
            },
        )
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return f"Site '{site}' not found. Use list_available_sites() to see options."
        if e.response.status_code == 429:
            return "Rate limited. Please wait before trying again."
        return f"Connection error: {e.response.text}"

    status = data.get("status", "unknown")

    job_id = data.get("job_id")
    if status == "mfa_required":
        session_id = data.get("session_id", "")
        mfa_type = data.get("mfa_type", "unknown")
        follow_up = f"\nThen follow the connection with get_job_status('{job_id}')." if job_id else ""
        return (
            f"MFA required ({mfa_type}).\n"
            f"Session ID: {session_id}\n\n"
            f"Ask the user for their verification code, then call:\n"
            f"  submit_mfa(session_id='{session_id}', code='<user_code>')" + follow_up
        )

    if status == "pending" and job_id:
        return (
            f"The connection to {site} is still running (job {job_id}).\n"
            f"Call get_job_status('{job_id}') to follow it; it may ask for an MFA code."
        )

    if status == "connected":
        extracted = data.get("data", {})
        field_count = len(extracted)
        method = data.get("extraction_method", "unknown")
        lines = [
            f"Connected to {site}! Extracted {field_count} fields (method: {method}).\n",
            _format_data(extracted),
        ]
        return "\n".join(lines)

    return f"Unexpected status: {status}. Response: {data}"


@mcp.tool()
async def connect_utility_account(site: str) -> str:
    """Create a hosted link session for a user to authenticate securely.

    This generates a URL the user opens in their browser to enter credentials.
    The agent never sees raw credentials — they stay in the hosted page.
    Use check_connection_status() to monitor when the user completes.

    Args:
        site: Site identifier from list_available_sites() (e.g. "hydro_one").

    Returns:
        Link URL for the user, plus the link_token for tracking status.
    """
    try:
        data = await _api("POST", "/link/sessions", params={"site": site})
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            return "Authentication required. Set PLAIDIFY_API_KEY to an API key (pk_...) or a user access token."
        if e.response.status_code == 403:
            return f"This API key or agent is not allowed to connect '{site}'."
        return f"Error creating link session: {e.response.text}"

    link_token = data.get("link_token", "")
    link_url = data.get("link_url", f"/link?token={link_token}")
    full_url = f"{PLAIDIFY_SERVER_URL}{link_url}" if link_url.startswith("/") else link_url

    return (
        f"Link session created for {site}.\n"
        f"Link token: {link_token}\n"
        f"Link URL: {full_url}\n"
        f"Expires in: {data.get('expires_in', 1800)} seconds\n\n"
        f"Ask the user to open this URL to connect their {site} account.\n"
        f"Use check_connection_status('{link_token}') to monitor progress."
    )


@mcp.tool()
async def check_connection_status(link_token: str) -> str:
    """Check the current status of a link session.

    Use this after asking the user to open the link URL, to see if they've
    completed the authentication flow.

    Args:
        link_token: The link token from connect_utility_account().

    Returns:
        Current session status and events that have occurred.
    """
    try:
        data = await _api("GET", f"/link/sessions/{link_token}/status")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return f"Link session '{link_token}' not found. It may have expired."
        return f"Error checking status: {e.response.text}"

    status = data.get("status", "unknown")
    events = data.get("events", [])
    site = data.get("site", "unknown")

    lines = [
        f"Link session status: {status}",
        f"Site: {site}",
    ]
    if events:
        lines.append(f"Events: {' → '.join(events)}")

    status_messages = {
        "awaiting_institution": "User has not yet selected a provider.",
        "awaiting_credentials": "User has selected a provider but hasn't entered credentials.",
        "connecting": "User has submitted credentials. Connecting...",
        "mfa_required": "MFA is required. Waiting for user to enter verification code.",
        "verifying_mfa": "Verifying MFA code...",
        "completed": "Connection successful! The user has been authenticated.",
        "error": "An error occurred during the connection.",
        "exited": "The user closed the link before finishing.",
        "expired": "This session has expired. Create a new one with connect_utility_account().",
    }
    lines.append(f"\n{status_messages.get(status, 'Unknown status.')}")

    if status == "error" and data.get("error_message"):
        code = data.get("error_code")
        lines.append(f"Error{f' ({code})' if code else ''}: {data['error_message']}")

    # If completed, include public token for exchange
    if status == "completed":
        public_token = data.get("public_token")
        if public_token:
            lines.append(f"\nPublic token: {public_token}")
            lines.append("Exchange this for an access token with exchange_public_token().")

    return "\n".join(lines)


@mcp.tool()
async def exchange_public_token(public_token: str) -> str:
    """Exchange a one-time public token for a permanent access token.

    Call this after check_connection_status() shows 'completed' and returns
    a public_token. The public token can only be used once.

    Args:
        public_token: The public token from a completed link session.

    Returns:
        The permanent access_token for data retrieval.
    """
    try:
        data = await _api(
            "POST",
            "/exchange/public_token",
            json={
                "public_token": public_token,
            },
        )
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 410:
            return "This public token has already been exchanged or has expired."
        if e.response.status_code in (401, 403):
            return "Authentication required. Set PLAIDIFY_API_KEY."
        return f"Error exchanging token: {e.response.text}"

    access_token = data.get("access_token", "")
    return (
        f"Access token obtained: {access_token}\n\n"
        f"Use fetch_data('{access_token}') to retrieve extracted data.\n"
        f"Store this token securely — it provides ongoing access."
    )


@mcp.tool()
async def fetch_data(access_token: str, consent_token: Optional[str] = None) -> str:
    """Fetch extracted data using an access token.

    Call this after connect_site() returns data, or after exchanging a
    public_token for an access_token via exchange_public_token().

    If a consent_token is provided, returned data will be filtered to only
    the scopes granted by that consent.

    Args:
        access_token: The access token from connect_site or exchange_public_token.
        consent_token: Optional consent token for scoped data access.

    Returns:
        Extracted data from the connected site in a readable format.
    """
    # In the body, never the URL: a query string lands in access logs.
    body: dict[str, str] = {"access_token": access_token}
    if consent_token:
        body["consent_token"] = consent_token

    try:
        data = await _api("POST", "/fetch_data", json=body)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            return "Invalid access token. The user may need to re-authenticate."
        if e.response.status_code == 403:
            return "Access denied. Consent may have been revoked or expired."
        return f"Error fetching data: {e.response.text}"

    extracted = data.get("data", data) if isinstance(data, dict) else data

    if not extracted:
        return "No data was extracted. The connection may still be in progress."

    lines = ["Extracted data:\n", _format_data(extracted)]
    if isinstance(data, dict):
        if data.get("scopes_applied"):
            lines.append(f"\nScopes applied: {', '.join(data['scopes_applied'])}")
        if data.get("extraction_method"):
            lines.append(f"Extraction method: {data['extraction_method']}")
    return "\n".join(lines)


@mcp.tool()
async def submit_mfa(session_id: str, code: str) -> str:
    """Submit an MFA verification code for a pending connection.

    Use this when connect_site() or check_connection_status() indicates
    MFA is required. The user must provide the code from their authenticator
    app, SMS, or email.

    Args:
        session_id: The session ID from the MFA challenge.
        code: The MFA verification code entered by the user.

    Returns:
        Result of the MFA submission (success or error).
    """
    try:
        data = await _api(
            "POST",
            "/mfa/submit",
            json={
                "session_id": session_id,
                "code": code,
            },
        )
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return f"MFA session '{session_id}' not found. It may have expired."
        return f"Error submitting MFA code: {e.response.text}"

    status = data.get("status", "unknown")
    if status == "mfa_submitted":
        return (
            "MFA code submitted successfully. The connection will resume.\n"
            "Follow it with get_job_status(job_id) or check_connection_status(link_token)."
        )

    return f"MFA submission result: {status}"


@mcp.tool()
async def get_job_status(job_id: str) -> str:
    """Check the progress of an access job (a connection started by connect_site()).

    Use this after connect_site() answers "pending", or after submit_mfa(), to
    see whether the job is still running, waiting for an MFA code, finished,
    or failed.

    Args:
        job_id: The job_id returned by connect_site().

    Returns:
        The job's status; the MFA prompt when one is waiting; the extracted
        fields when it completed; the error when it failed.
    """
    try:
        data = await _api("GET", f"/access_jobs/{job_id}")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return f"Access job '{job_id}' not found."
        if e.response.status_code == 401:
            return "Authentication required. Set PLAIDIFY_API_KEY."
        return f"Error checking job status: {e.response.text}"

    status = data.get("status", "unknown")
    site = data.get("site", "unknown")
    lines = [f"Job {job_id} ({site}): {status}"]
    metadata = data.get("metadata") or {}

    if status == "mfa_required":
        session_id = data.get("session_id", "")
        prompt = metadata.get("question") or metadata.get("message") or "Ask the user for their verification code."
        if metadata.get("mfa_error") == "invalid_code":
            remaining = metadata.get("attempts_remaining")
            lines.append(
                "The site rejected the last code." + (f" {remaining} attempt(s) left." if remaining is not None else "")
            )
        lines.append(f"MFA required ({data.get('mfa_type', 'unknown')}): {prompt}")
        lines.append(f"Then call: submit_mfa(session_id='{session_id}', code='<user_code>')")
    elif status in ("pending", "running"):
        if data.get("mfa_state") == "verifying":
            lines.append("The verification code was received; the site is checking it.")
        else:
            lines.append("Still running. Check again in a few seconds.")
    elif status == "completed":
        result = data.get("result") or {}
        extracted = result.get("data") if isinstance(result, dict) else None
        if extracted:
            lines.append(f"Extracted {len(extracted)} fields:\n")
            lines.append(_format_data(extracted))
        else:
            fields = metadata.get("result_fields") or []
            lines.append("Completed." + (f" Fields: {', '.join(fields)}" if fields else ""))
    else:
        error = data.get("error_message") or "The connection could not be completed."
        code = data.get("error_code")
        lines.append(f"Error{f' ({code})' if code else ''}: {error}")
    return "\n".join(lines)


@mcp.tool()
async def request_consent(
    access_token: str,
    scopes: list[str],
    agent_name: str = "MCP Agent",
    duration_seconds: int = 3600,
) -> str:
    """Request user consent for scoped, time-limited data access.

    Before accessing user data, agents should request consent specifying
    exactly what fields they need and for how long. The user must approve.

    Args:
        access_token: The access token for the connected site.
        scopes: List of data fields to request (e.g. ["read:current_bill", "read:usage_history"]).
        agent_name: Display name for this agent (shown to user).
        duration_seconds: How long the consent should last (default: 1 hour, max: 30 days).

    Returns:
        Consent request status and instructions for the user.
    """
    try:
        data = await _api(
            "POST",
            "/consent/request",
            json={
                "access_token": access_token,
                "scopes": scopes,
                "agent_name": agent_name,
                "duration_seconds": duration_seconds,
            },
        )
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (401, 403):
            return "Authentication required. Set PLAIDIFY_API_KEY."
        return f"Error requesting consent: {e.response.text}"

    request_id = data.get("request_id", "")
    return (
        f"Consent request created.\n"
        f"Request ID: {request_id}\n"
        f"Agent: {agent_name}\n"
        f"Scopes: {', '.join(scopes)}\n"
        f"Duration: {duration_seconds} seconds\n"
        f"Status: {data.get('status', 'pending')}\n\n"
        f"The user must approve this request at:\n"
        f"  POST /consent/{request_id}/approve\n\n"
        f"Once approved, use the returned consent_token with fetch_data()."
    )


@mcp.tool()
async def list_connections() -> str:
    """List all active connections (links) for the current user.

    Returns a list of connected sites with their link tokens and status.
    Requires authentication via PLAIDIFY_API_KEY.
    """
    try:
        data = await _api("GET", "/links")
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (401, 403):
            return "Authentication required. Set PLAIDIFY_API_KEY to a valid JWT or API key."
        return f"Error listing connections: {e.response.text}"

    links = data if isinstance(data, list) else []

    if not links:
        return "No active connections found."

    lines = [f"Active connections ({len(links)}):\n"]
    for link in links:
        lines.append(f"  • {link.get('site', 'unknown')} — token: {link.get('link_token', 'N/A')}")
    return "\n".join(lines)


# ── Entry Point ───────────────────────────────────────────────────────────────

_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def configure_http_transport(host: str, port: int, allowed_hosts: Optional[list[str]] = None) -> None:
    """Point the SSE / streamable-HTTP transports at ``host:port``.

    FastMCP reads the bind address from ``mcp.settings`` (``run()`` takes no
    port), and sets its DNS-rebinding protection for a loopback host when it is
    constructed, so both are set here.
    """
    mcp.settings.host = host
    mcp.settings.port = port
    if host in _LOOPBACK_HOSTS:
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*"],
            allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"],
        )
        return
    hosts = [entry.strip() for entry in (allowed_hosts or []) if entry.strip()]
    if not hosts:
        raise SystemExit(
            f"Binding the MCP server to {host} exposes PLAIDIFY_API_KEY to everyone who can reach it. "
            "Put it behind your own authentication and set PLAIDIFY_MCP_ALLOWED_HOSTS (or --allowed-host) "
            "to the host names clients use."
        )
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[entry if ":" in entry else f"{entry}:*" for entry in hosts],
        allowed_origins=[f"https://{entry}" for entry in hosts] + [f"http://{entry}" for entry in hosts],
    )


def main(argv: Optional[list[str]] = None) -> None:
    global PLAIDIFY_SERVER_URL, _client

    parser = argparse.ArgumentParser(description="Plaidify MCP server")
    parser.add_argument("--transport", choices=("stdio", "sse", "streamable-http"), default="stdio")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address for the HTTP transports.")
    parser.add_argument("--port", type=int, default=3001, help="Port for the HTTP transports.")
    parser.add_argument("--server-url", help="Base URL of the Plaidify API (overrides PLAIDIFY_SERVER_URL).")
    parser.add_argument(
        "--allowed-host",
        action="append",
        default=None,
        help="Host header value accepted on a non-loopback bind (repeatable).",
    )
    args = parser.parse_args(argv)

    if args.server_url:
        PLAIDIFY_SERVER_URL = args.server_url
        _client = None

    if args.transport == "stdio":
        mcp.run(transport="stdio")
        return

    allowed_hosts = args.allowed_host or [
        entry for entry in os.environ.get("PLAIDIFY_MCP_ALLOWED_HOSTS", "").split(",") if entry.strip()
    ]
    configure_http_transport(args.host, args.port, allowed_hosts)
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
