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
  - request_consent(access_token, scopes): request scoped data access
  - list_connections(): list active connections

Usage (stdio):
    python -m src.mcp_server

Usage (HTTP SSE):
    python -m src.mcp_server --transport sse --port 3001

Environment variables:
    PLAIDIFY_SERVER_URL  — Base URL of the Plaidify API (default: http://localhost:8000)
    PLAIDIFY_API_KEY     — JWT token or API key for authenticated endpoints
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any, Optional

import httpx
from mcp.server.fastmcp import FastMCP

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
        "4. If MFA required: submit_mfa(session_id, code)"
    ),
)

# Default Plaidify server URL (override via PLAIDIFY_SERVER_URL env var)
import os

PLAIDIFY_SERVER_URL = os.environ.get("PLAIDIFY_SERVER_URL", "http://localhost:8000")
PLAIDIFY_API_KEY = os.environ.get("PLAIDIFY_API_KEY", "")


def _headers() -> dict[str, str]:
    h: dict[str, str] = {"Content-Type": "application/json"}
    if PLAIDIFY_API_KEY:
        # Support both API keys (pk_...) and JWT tokens
        if PLAIDIFY_API_KEY.startswith("pk_"):
            h["X-API-Key"] = PLAIDIFY_API_KEY
        else:
            h["Authorization"] = f"Bearer {PLAIDIFY_API_KEY}"
    return h


async def _api(
    method: str,
    path: str,
    json: dict | None = None,
    params: dict | None = None,
) -> dict[str, Any]:
    """Make an API call to the Plaidify server."""
    async with httpx.AsyncClient(
        base_url=PLAIDIFY_SERVER_URL,
        headers=_headers(),
        timeout=60.0,
    ) as client:
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
    If MFA is required, you'll receive a session_id to use with submit_mfa().

    Args:
        site: Site identifier from list_available_sites() (e.g. "greengrid_energy").
        username: Login username for the target site.
        password: Login password for the target site.

    Returns:
        Extracted data or MFA challenge details.
    """
    try:
        data = await _api("POST", "/connect", json={
            "site": site,
            "username": username,
            "password": password,
        })
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return f"Site '{site}' not found. Use list_available_sites() to see options."
        if e.response.status_code == 429:
            return "Rate limited. Please wait before trying again."
        return f"Connection error: {e.response.text}"

    status = data.get("status", "unknown")

    if status == "mfa_required":
        session_id = data.get("session_id", "")
        mfa_type = data.get("mfa_type", "unknown")
        return (
            f"MFA required ({mfa_type}).\n"
            f"Session ID: {session_id}\n\n"
            f"Ask the user for their verification code, then call:\n"
            f"  submit_mfa(session_id='{session_id}', code='<user_code>')"
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
        site: Site identifier from list_available_sites() (e.g. "greengrid_energy").

    Returns:
        Link URL for the user, plus the link_token for tracking status.
    """
    try:
        data = await _api("POST", "/link/sessions", params={"site": site})
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (401, 403):
            # Fall back to unauthenticated encryption session
            data = await _api("POST", "/encryption/session")
            link_token = data.get("link_token", "")
            return (
                f"Link session created (unauthenticated mode).\n"
                f"Link token: {link_token}\n"
                f"Link URL: {PLAIDIFY_SERVER_URL}/link?token={link_token}&site={site}\n\n"
                f"Ask the user to open this URL to connect their {site} account."
            )
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
        "expired": "This session has expired. Create a new one with connect_utility_account().",
    }
    lines.append(f"\n{status_messages.get(status, 'Unknown status.')}")

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
        data = await _api("POST", "/exchange/public_token", json={
            "public_token": public_token,
        })
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
    params: dict[str, str] = {"access_token": access_token}
    if consent_token:
        params["consent_token"] = consent_token

    try:
        data = await _api("GET", "/fetch_data", params=params)
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
        # The API expects query params, not JSON body
        data = await _api("POST", "/mfa/submit", params={
            "session_id": session_id,
            "code": code,
        })
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return f"MFA session '{session_id}' not found. It may have expired."
        return f"Error submitting MFA code: {e.response.text}"

    status = data.get("status", "unknown")
    if status == "mfa_submitted":
        return (
            "MFA code submitted successfully. The connection will resume.\n"
            "Check the connection status or try connect_site() again."
        )

    return f"MFA submission result: {status}"


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
        data = await _api("POST", "/consent/request", json={
            "access_token": access_token,
            "scopes": scopes,
            "agent_name": agent_name,
            "duration_seconds": duration_seconds,
        })
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

    if isinstance(data, list):
        links = data
    else:
        links = data if isinstance(data, list) else []

    if not links:
        return "No active connections found."

    lines = [f"Active connections ({len(links)}):\n"]
    for link in links:
        lines.append(f"  • {link.get('site', 'unknown')} — token: {link.get('link_token', 'N/A')}")
    return "\n".join(lines)


# ── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    transport = "stdio"
    port = 3001

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--transport" and i + 1 < len(args):
            transport = args[i + 1]
            i += 2
        elif args[i] == "--port" and i + 1 < len(args):
            port = int(args[i + 1])
            i += 2
        elif args[i] == "--server-url" and i + 1 < len(args):
            PLAIDIFY_SERVER_URL = args[i + 1]
            i += 2
        else:
            i += 1

    if transport == "sse":
        mcp.run(transport="sse", port=port)
    else:
        mcp.run(transport="stdio")
