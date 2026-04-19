"""
Agent registration and management endpoints.
"""

import hashlib
import json
import secrets
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from src.audit import record_audit_event
from src.database import Agent, ApiKey, User, get_db
from src.dependencies import get_current_user
from src.logging_config import get_logger

logger = get_logger("api.agents")

router = APIRouter(prefix="/agents", tags=["agents"])


@router.post("")
async def register_agent(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Register a new AI agent.

    Creates an agent identity with its own API key, allowed scopes,
    and permitted sites. The agent's API key is returned once — store it
    securely.

    Body:
        name: Agent display name (required).
        description: What the agent does (optional).
        allowed_scopes: JSON list of scope strings the agent can request (optional, null = all).
        allowed_sites: JSON list of site identifiers the agent can connect to (optional, null = all).
        rate_limit: Custom rate limit string e.g. "30/minute" (optional).
    """
    body = await request.json()
    name = body.get("name")
    if not name:
        raise HTTPException(status_code=422, detail="'name' is required.")

    agent_id = f"agent-{uuid.uuid4()}"

    # Create a dedicated API key for this agent
    raw_key = f"pk_agent_{secrets.token_urlsafe(32)}"
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    key_prefix = raw_key[:16]

    api_key_id = str(uuid.uuid4())
    db_key = ApiKey(
        id=api_key_id,
        name=f"Agent: {name}",
        key_hash=key_hash,
        key_prefix=key_prefix,
        user_id=user.id,
        scopes=(json.dumps(body.get("allowed_scopes")) if body.get("allowed_scopes") else None),
    )
    db.add(db_key)

    agent = Agent(
        id=agent_id,
        name=name,
        description=body.get("description", ""),
        owner_id=user.id,
        api_key_id=api_key_id,
        allowed_scopes=(json.dumps(body.get("allowed_scopes")) if body.get("allowed_scopes") else None),
        allowed_sites=(json.dumps(body.get("allowed_sites")) if body.get("allowed_sites") else None),
        rate_limit=body.get("rate_limit"),
    )
    db.add(agent)
    db.commit()

    record_audit_event(
        db,
        "agent",
        "register",
        user_id=user.id,
        resource=agent_id,
        metadata={"name": name},
    )
    logger.info(
        "Agent registered",
        extra={"extra_data": {"agent_id": agent_id, "owner": user.id}},
    )

    return {
        "agent_id": agent_id,
        "name": name,
        "api_key": raw_key,  # Only time the raw key is exposed
        "api_key_prefix": key_prefix,
        "allowed_scopes": body.get("allowed_scopes"),
        "allowed_sites": body.get("allowed_sites"),
    }


@router.get("")
async def list_agents(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List all agents owned by the current user."""
    agents = db.query(Agent).filter_by(owner_id=user.id, is_active=True).all()
    return {
        "agents": [
            {
                "agent_id": a.id,
                "name": a.name,
                "description": a.description,
                "allowed_scopes": (json.loads(a.allowed_scopes) if a.allowed_scopes else None),
                "allowed_sites": (json.loads(a.allowed_sites) if a.allowed_sites else None),
                "rate_limit": a.rate_limit,
                "last_active_at": (a.last_active_at.isoformat() if a.last_active_at else None),
                "created_at": (a.created_at.isoformat() if a.created_at else None),
            }
            for a in agents
        ],
        "count": len(agents),
    }


@router.get("/{agent_id}")
async def get_agent(
    agent_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get details of a specific agent."""
    agent = db.query(Agent).filter_by(id=agent_id, owner_id=user.id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found.")
    return {
        "agent_id": agent.id,
        "name": agent.name,
        "description": agent.description,
        "allowed_scopes": (json.loads(agent.allowed_scopes) if agent.allowed_scopes else None),
        "allowed_sites": (json.loads(agent.allowed_sites) if agent.allowed_sites else None),
        "rate_limit": agent.rate_limit,
        "is_active": agent.is_active,
        "last_active_at": (agent.last_active_at.isoformat() if agent.last_active_at else None),
        "created_at": (agent.created_at.isoformat() if agent.created_at else None),
    }


@router.patch("/{agent_id}")
async def update_agent(
    agent_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update an agent's configuration."""
    agent = db.query(Agent).filter_by(id=agent_id, owner_id=user.id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found.")

    body = await request.json()
    if "name" in body:
        agent.name = body["name"]
    if "description" in body:
        agent.description = body["description"]
    if "allowed_scopes" in body:
        agent.allowed_scopes = json.dumps(body["allowed_scopes"]) if body["allowed_scopes"] else None
    if "allowed_sites" in body:
        agent.allowed_sites = json.dumps(body["allowed_sites"]) if body["allowed_sites"] else None
    if "rate_limit" in body:
        agent.rate_limit = body["rate_limit"]

    db.commit()
    record_audit_event(
        db,
        "agent",
        "update",
        user_id=user.id,
        resource=agent_id,
        metadata={"fields": list(body.keys())},
    )
    return {"status": "updated", "agent_id": agent_id}


@router.delete("/{agent_id}")
async def deactivate_agent(
    agent_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Deactivate an agent and revoke its API key."""
    agent = db.query(Agent).filter_by(id=agent_id, owner_id=user.id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found.")

    agent.is_active = False
    # Also revoke the agent's API key
    if agent.api_key_id:
        api_key = db.query(ApiKey).filter_by(id=agent.api_key_id).first()
        if api_key:
            api_key.is_active = False

    db.commit()
    record_audit_event(
        db,
        "agent",
        "deactivate",
        user_id=user.id,
        resource=agent_id,
    )
    logger.info(
        "Agent deactivated",
        extra={"extra_data": {"agent_id": agent_id}},
    )
    return {"status": "deactivated", "agent_id": agent_id}
