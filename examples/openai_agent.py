"""
OpenAI function-calling + Plaidify example.

Lightweight example using OpenAI's function calling directly (no LangChain).

Requirements:
    pip install openai httpx

Usage:
    export OPENAI_API_KEY=sk-...
    python examples/openai_agent.py
"""

from __future__ import annotations

import json
import httpx
from openai import OpenAI

PLAIDIFY_SERVER = "http://localhost:8000"

# ── Tool definitions (OpenAI function-calling schema) ─────────────────────────

tools = [
    {
        "type": "function",
        "function": {
            "name": "list_available_sites",
            "description": "List all utility/energy/bank sites available for connection.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "connect_utility_account",
            "description": "Create a link session for a user to authenticate. Returns a URL to open.",
            "parameters": {
                "type": "object",
                "properties": {
                    "site": {"type": "string", "description": "Site identifier (e.g. 'greengrid_energy')"},
                },
                "required": ["site"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_connection_status",
            "description": "Check if a user has completed authentication for a link session.",
            "parameters": {
                "type": "object",
                "properties": {
                    "link_token": {"type": "string", "description": "The link token to check."},
                },
                "required": ["link_token"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_data",
            "description": "Retrieve extracted data after successful authentication.",
            "parameters": {
                "type": "object",
                "properties": {
                    "access_token": {"type": "string", "description": "Access token from completed session."},
                },
                "required": ["access_token"],
            },
        },
    },
]


# ── Tool implementations ──────────────────────────────────────────────────────


def call_tool(name: str, args: dict) -> str:
    """Execute a tool and return the result as a string."""
    if name == "list_available_sites":
        resp = httpx.get(f"{PLAIDIFY_SERVER}/blueprints")
        data = resp.json()
        return json.dumps(data.get("blueprints", []), indent=2)

    elif name == "connect_utility_account":
        resp = httpx.post(f"{PLAIDIFY_SERVER}/encryption/session")
        data = resp.json()
        token = data["link_token"]
        return json.dumps({
            "link_token": token,
            "link_url": f"{PLAIDIFY_SERVER}/link?token={token}",
            "message": "Ask the user to open this URL.",
        })

    elif name == "check_connection_status":
        token = args["link_token"]
        resp = httpx.get(f"{PLAIDIFY_SERVER}/link/sessions/{token}/status")
        return resp.text

    elif name == "fetch_data":
        token = args["access_token"]
        resp = httpx.get(f"{PLAIDIFY_SERVER}/fetch_data", params={"access_token": token})
        return resp.text

    return json.dumps({"error": f"Unknown tool: {name}"})


# ── Agent loop ────────────────────────────────────────────────────────────────


def run_agent(user_message: str):
    """Run a simple function-calling agent loop."""
    client = OpenAI()

    messages = [
        {
            "role": "system",
            "content": (
                "You help users connect utility accounts and retrieve data using Plaidify. "
                "Use the available tools to list sites, create connection links, check status, "
                "and fetch extracted data."
            ),
        },
        {"role": "user", "content": user_message},
    ]

    while True:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            tools=tools,
        )

        choice = response.choices[0]

        if choice.finish_reason == "tool_calls":
            messages.append(choice.message)
            for tool_call in choice.message.tool_calls:
                fn_name = tool_call.function.name
                fn_args = json.loads(tool_call.function.arguments)
                result = call_tool(fn_name, fn_args)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result,
                })
        else:
            print(choice.message.content)
            return choice.message.content


if __name__ == "__main__":
    run_agent("What sites can I connect to?")
