"""
LangChain + Plaidify agent example.

Uses the Plaidify Python SDK to create an agent that can connect to
utility portals and extract data.

Requirements:
    pip install langchain langchain-openai plaidify

Usage:
    export OPENAI_API_KEY=sk-...
    python examples/langchain_agent.py
"""

from __future__ import annotations

import asyncio

from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langchain.agents import AgentExecutor, create_openai_tools_agent
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder


# ── Plaidify Tools ────────────────────────────────────────────────────────────

PLAIDIFY_SERVER = "http://localhost:8000"


@tool
def list_sites() -> str:
    """List all available sites that can be connected through Plaidify."""
    import httpx

    resp = httpx.get(f"{PLAIDIFY_SERVER}/blueprints")
    data = resp.json()
    return "\n".join(
        f"- {bp['name']} ({bp['site']}): {bp.get('domain', '')}"
        for bp in data.get("blueprints", [])
    )


@tool
def connect_account(site: str) -> str:
    """Create a link session so the user can authenticate with a site.

    Args:
        site: The site identifier (e.g. 'greengrid_energy').

    Returns:
        A URL the user should open to authenticate.
    """
    import httpx

    resp = httpx.post(f"{PLAIDIFY_SERVER}/encryption/session")
    data = resp.json()
    token = data["link_token"]
    return f"Please open: {PLAIDIFY_SERVER}/link?token={token}"


@tool
def check_status(link_token: str) -> str:
    """Check the status of a link session.

    Args:
        link_token: The link token from connect_account.
    """
    import httpx

    resp = httpx.get(f"{PLAIDIFY_SERVER}/link/sessions/{link_token}/status")
    if resp.status_code == 404:
        return "Session not found or expired."
    data = resp.json()
    return f"Status: {data['status']}, Events: {data.get('events', [])}"


@tool
def get_data(access_token: str) -> str:
    """Fetch extracted data after successful authentication.

    Args:
        access_token: The access token from a completed link session.
    """
    import httpx

    resp = httpx.get(f"{PLAIDIFY_SERVER}/fetch_data", params={"access_token": access_token})
    if resp.status_code != 200:
        return f"Error: {resp.text}"
    return str(resp.json())


# ── Agent Setup ───────────────────────────────────────────────────────────────


def create_plaidify_agent():
    """Create a LangChain agent with Plaidify tools."""
    llm = ChatOpenAI(model="gpt-4o", temperature=0)

    tools = [list_sites, connect_account, check_status, get_data]

    prompt = ChatPromptTemplate.from_messages([
        (
            "system",
            "You are a helpful assistant that can connect to utility accounts "
            "and retrieve billing data using Plaidify. When a user asks to connect "
            "an account, use the tools to: 1) list available sites, 2) create a "
            "connection link, 3) check if the user has authenticated, 4) fetch data.",
        ),
        MessagesPlaceholder("chat_history", optional=True),
        ("human", "{input}"),
        MessagesPlaceholder("agent_scratchpad"),
    ])

    agent = create_openai_tools_agent(llm, tools, prompt)
    return AgentExecutor(agent=agent, tools=tools, verbose=True)


if __name__ == "__main__":
    agent = create_plaidify_agent()
    result = agent.invoke({"input": "What utility sites can I connect to?"})
    print(result["output"])
