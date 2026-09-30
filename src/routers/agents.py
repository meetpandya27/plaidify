"""
Agent registration and management endpoints.
"""

import json
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from src.audit import record_audit_event
from src.database import Agent, ApiKey, User, get_db
from src.dependencies import generate_api_key, get_current_user
from src.logging_config import get_logger
from src.models import AgentCreateRequest, AgentUpdateRequest

logger = get_logger("api.agents")

router = APIRouter(prefix="/agents", tags=["agents"])


def _dump_list(values: Optional[list[str]]) -> Optional[str]:
    """Store a restriction list as JSON. ``None`` (no restriction) stays NULL; ``[]`` stays ``[]`` (allow nothing)."""
    return json.dumps(values) if values is not None else None


def _load_list(stored: Optional[str]) -> Optional[list]:
    """A stored restriction list for the API; unreadable values read as ``[]`` (they allow nothing)."""
    if stored is None:
        return None
    try:
        values = json.loads(stored)
    except (TypeError, ValueError):
        return []
    return values if isinstance(values, list) else []


def _serialize_agent(agent: Agent, *, include_status: bool = False) -> dict:
    payload = {
        "agent_id": agent.id,
        "name": agent.name,
        "description": agent.description,
        "allowed_scopes": _load_list(agent.allowed_scopes),
        "allowed_sites": _load_list(agent.allowed_sites),
        "rate_limit": agent.rate_limit,
        "last_active_at": (agent.last_active_at.isoformat() if agent.last_active_at else None),
        "created_at": (agent.created_at.isoformat() if agent.created_at else None),
    }
    if include_status:
        payload["is_active"] = agent.is_active
    return payload


@router.post("")
async def register_agent(
    body: AgentCreateRequest,
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
        allowed_scopes: List of scope strings the agent can request (omit for all, [] for none).
        allowed_sites: List of site identifiers the agent can connect to (omit for all, [] for none).
        rate_limit: The agent's own request limit, e.g. "30/minute" (optional).
    """
    agent_id = f"agent-{uuid.uuid4()}"

    # Create a dedicated API key for this agent
    raw_key, key_hash = generate_api_key("pk_agent_")
    key_prefix = raw_key[:16]

    api_key_id = str(uuid.uuid4())
    db_key = ApiKey(
        id=api_key_id,
        name=f"Agent: {body.name}",
        key_hash=key_hash,
        key_prefix=key_prefix,
        user_id=user.id,
        scopes=_dump_list(body.allowed_scopes),
    )
    db.add(db_key)

    agent = Agent(
        id=agent_id,
        name=body.name,
        description=body.description or "",
        owner_id=user.id,
        api_key_id=api_key_id,
        allowed_scopes=_dump_list(body.allowed_scopes),
        allowed_sites=_dump_list(body.allowed_sites),
        rate_limit=body.rate_limit,
    )
    db.add(agent)
    db.commit()

    record_audit_event(
        db,
        "agent",
        "register",
        user_id=user.id,
        resource=agent_id,
        metadata={"name": body.name},
    )
    logger.info(
        "Agent registered",
        extra={"extra_data": {"agent_id": agent_id, "owner": user.id}},
    )

    return {
        "agent_id": agent_id,
        "name": body.name,
        "api_key": raw_key,  # Only time the raw key is exposed
        "api_key_prefix": key_prefix,
        "allowed_scopes": body.allowed_scopes,
        "allowed_sites": body.allowed_sites,
        "rate_limit": body.rate_limit,
    }


@router.get("")
async def list_agents(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List the active agents owned by the current user, oldest first."""
    agents = (
        db.query(Agent)
        .filter_by(owner_id=user.id, is_active=True)
        .order_by(Agent.created_at, Agent.id)
        .offset(offset)
        .limit(limit)
        .all()
    )
    return {
        "agents": [_serialize_agent(a) for a in agents],
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
    return _serialize_agent(agent, include_status=True)


@router.patch("/{agent_id}")
async def update_agent(
    agent_id: str,
    body: AgentUpdateRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update an agent's configuration; only the fields sent change.

    Changing ``allowed_scopes`` changes what the agent's API key may read too.
    """
    agent = db.query(Agent).filter_by(id=agent_id, owner_id=user.id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found.")

    fields = body.model_fields_set
    if "name" in fields:
        if body.name is None:
            raise HTTPException(status_code=422, detail="'name' cannot be null.")
        agent.name = body.name
    if "description" in fields:
        agent.description = body.description or ""
    if "allowed_scopes" in fields:
        agent.allowed_scopes = _dump_list(body.allowed_scopes)
        # The key's own scope list is intersected with the agent's on every
        # request; keep them equal, or a widened agent stays at the old list.
        if agent.api_key_id:
            db.query(ApiKey).filter_by(id=agent.api_key_id).update(
                {"scopes": agent.allowed_scopes}, synchronize_session=False
            )
    if "allowed_sites" in fields:
        agent.allowed_sites = _dump_list(body.allowed_sites)
    if "rate_limit" in fields:
        agent.rate_limit = body.rate_limit

    db.commit()
    record_audit_event(
        db,
        "agent",
        "update",
        user_id=user.id,
        resource=agent_id,
        metadata={"fields": sorted(fields)},
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
