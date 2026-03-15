"""
Plaidify MCP Server — exposes Plaidify as tools for AI agent frameworks.

Runs as a stdio server (for Claude Desktop, etc.) or HTTP SSE server.
Provides four tools:
  - list_available_sites(): list connectable blueprints
  - connect_utility_account(site): create a link session, return link URL
  - check_connection_status(link_token): check link session progress
  - fetch_data(access_token): retrieve extracted data

Usage (stdio):
    python -m src.mcp_server

Usage (HTTP SSE):
    python -m src.mcp_server --transport sse --port 3001
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

# ── Server Setup ──────────────────────────────────────────────────────────────

mcp = FastMCP(
    "Plaidify",
    instructions=(
        "Plaidify is an open-source API for authenticated web data extraction. "
        "Use these tools to connect to utility/energy/bank portals, authenticate "
        "users through a hosted link flow, and extract structured data."
    ),
)

# Default Plaidify server URL (override via PLAIDIFY_SERVER_URL env var)
import os

PLAIDIFY_SERVER_URL = os.environ.get("PLAIDIFY_SERVER_URL", "http://localhost:8000")
PLAIDIFY_API_KEY = os.environ.get("PLAIDIFY_API_KEY", "")


def _headers() -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    if PLAIDIFY_API_KEY:
        h["Authorization"] = f"Bearer {PLAIDIFY_API_KEY}"
    return h


async def _api(method: str, path: str, json: dict | None = None) -> dict[str, Any]:
    """Make an API call to the Plaidify server."""
    async with httpx.AsyncClient(
        base_url=PLAIDIFY_SERVER_URL,
        headers=_headers(),
        timeout=30.0,
    ) as client:
        if method == "GET":
            resp = await client.get(path)
        else:
            resp = await client.post(path, json=json)
        resp.raise_for_status()
        return resp.json()


# ── Tools ─────────────────────────────────────────────────────────────────────


@mcp.tool()
async def list_available_sites() -> str:
    """List all available site blueprints that can be connected.

    Returns a formatted list of sites with names, domains, and supported features.
    Each site has:
    - site: identifier used for connect_utility_account()
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
async def connect_utility_account(site: str) -> str:
    """Create a link session for a user to authenticate with a site.

    This generates a hosted link URL that the user should open in their browser.
    The user will select their provider, enter credentials, and handle MFA
    in a secure hosted page (credentials never leave that page).

    Args:
        site: Site identifier from list_available_sites() (e.g. "greengrid_energy").

    Returns:
        Link URL for the user, plus the link_token for tracking status.
    """
    try:
        data = await _api("POST", "/link/sessions", json=None)
    except httpx.HTTPStatusError:
        # Try without auth (creates encryption session instead)
        data = await _api("POST", "/encryption/session")
        link_token = data.get("link_token", "")
        return (
            f"Link session created.\n"
            f"Link token: {link_token}\n"
            f"Link URL: {PLAIDIFY_SERVER_URL}/link?token={link_token}\n\n"
            f"Ask the user to open this URL to authenticate."
        )

    link_token = data.get("link_token", "")
    link_url = data.get("link_url", f"/link?token={link_token}")
    full_url = f"{PLAIDIFY_SERVER_URL}{link_url}" if link_url.startswith("/") else link_url

    return (
        f"Link session created.\n"
        f"Link token: {link_token}\n"
        f"Link URL: {full_url}\n\n"
        f"Ask the user to open this URL to connect their {site} account.\n"
        f"Use check_connection_status(link_token) to monitor progress."
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

    return "\n".join(lines)


@mcp.tool()
async def fetch_data(access_token: str) -> str:
    """Fetch extracted data using an access token from a completed link session.

    Call this after check_connection_status() shows 'completed'.

    Args:
        access_token: The access token received after successful authentication.

    Returns:
        Extracted data from the connected site in a readable format.
    """
    try:
        data = await _api("GET", f"/fetch_data?access_token={access_token}")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            return "Invalid access token. The user may need to re-authenticate."
        return f"Error fetching data: {e.response.text}"

    if isinstance(data, dict) and "data" in data:
        extracted = data["data"]
    else:
        extracted = data

    if not extracted:
        return "No data was extracted. The connection may still be in progress."

    # Format extracted data for readability
    lines = ["Extracted data:\n"]
    if isinstance(extracted, dict):
        for key, value in extracted.items():
            if isinstance(value, (dict, list)):
                import json
                lines.append(f"  {key}: {json.dumps(value, indent=2)}")
            else:
                lines.append(f"  {key}: {value}")
    else:
        lines.append(str(extracted))

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
