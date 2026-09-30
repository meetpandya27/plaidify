"""
Consent engine endpoints: request, approve, deny, list, revoke.

An agent (an agent's API key) asks for scoped, time-limited access to one
access token's data; the account owner approves or denies. An approved grant
is bound to that agent: ``/fetch_data`` with the agent's key needs a grant
bound to it, and the grant's scopes limit the fields returned. Agents cannot
approve or deny consent — only the owner can (a login, or an ordinary API key
of the owner's backend).
"""

import json as json_mod
import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from src.audit import record_audit_event
from src.crypto import token_fingerprint
from src.database import AccessToken, Agent, ConsentGrant, ConsentRequest, Link, User, as_utc, get_db, utcnow
from src.dependencies import (
    constrain_requested_scopes,
    ensure_site_allowed_for_request,
    get_auth_context,
    get_current_user_or_api_key,
    reject_agent_caller,
)
from src.logging_config import get_logger
from src.models import ConsentRequestCreate

logger = get_logger("api.consent")

router = APIRouter(prefix="/consent", tags=["consent"])


def _caller_agent_id(request: Request):
    auth_context = get_auth_context(request)
    return auth_context.agent_id if auth_context else None


@router.post("/request")
async def consent_request(
    request: Request,
    body: ConsentRequestCreate,
    user: User = Depends(get_current_user_or_api_key),
    db: Session = Depends(get_db),
):
    """Request user consent for scoped, time-limited data access.

    An AI agent calls this with its API key to ask for permission to read
    specific fields of one access token; the grant, once approved, is bound to
    that agent. The owner may also create requests (``agent_name`` is then
    required). The returned request_id is used by the owner to approve or deny.
    """
    agent_id = _caller_agent_id(request)

    # Verify the access token belongs to this user
    token_record = db.query(AccessToken).filter_by(token=body.access_token, user_id=user.id).first()
    if not token_record:
        raise HTTPException(status_code=401, detail="Invalid access token.")
    link = db.query(Link).filter_by(link_token=token_record.link_token).first()
    ensure_site_allowed_for_request(request, link.site if link else "")
    # An agent can only ask for fields it is allowed to read at all.
    constrain_requested_scopes(request, body.scopes)

    if agent_id:
        agent = db.get(Agent, agent_id)
        agent_name = agent.name if agent else body.agent_name
    else:
        agent_name = body.agent_name
    if not agent_name:
        raise HTTPException(status_code=422, detail="'agent_name' is required.")

    request_id = f"creq-{uuid.uuid4()}"
    cr = ConsentRequest(
        id=request_id,
        agent_name=agent_name,
        agent_description=body.agent_description or "",
        scopes=json_mod.dumps(body.scopes),
        duration_seconds=body.duration_seconds,
        access_token=body.access_token,
        user_id=user.id,
        agent_id=agent_id,
    )
    db.add(cr)
    db.commit()

    logger.info(
        "Consent requested",
        extra={"extra_data": {"request_id": request_id, "agent": agent_name, "agent_id": agent_id}},
    )
    record_audit_event(
        db,
        "consent",
        "request",
        user_id=user.id,
        agent_id=agent_id,
        resource=request_id,
        metadata={"scopes": body.scopes, "access_token": token_fingerprint(body.access_token)},
    )
    return {
        "request_id": request_id,
        "agent_name": agent_name,
        "agent_id": agent_id,
        "scopes": body.scopes,
        "duration_seconds": body.duration_seconds,
        "status": "pending",
    }


@router.post("/{request_id}/approve")
async def consent_approve(
    request_id: str,
    request: Request,
    user: User = Depends(get_current_user_or_api_key),
    db: Session = Depends(get_db),
):
    """Approve a consent request (the owner only). Creates a time-limited consent grant token."""
    reject_agent_caller(request, "approve consent requests")
    cr = db.query(ConsentRequest).filter_by(id=request_id, user_id=user.id).first()
    if not cr:
        raise HTTPException(status_code=404, detail="Consent request not found.")
    if cr.status != "pending":
        raise HTTPException(
            status_code=409,
            detail=f"Consent request is already '{cr.status}'.",
        )

    cr.status = "approved"
    consent_token = f"consent-{uuid.uuid4()}"
    grant = ConsentGrant(
        token=consent_token,
        consent_request_id=request_id,
        scopes=cr.scopes,
        access_token=cr.access_token,
        user_id=user.id,
        agent_id=cr.agent_id,
        expires_at=utcnow() + timedelta(seconds=cr.duration_seconds),
    )
    db.add(grant)
    db.commit()

    logger.info(
        "Consent approved",
        extra={
            "extra_data": {
                "request_id": request_id,
                "consent_token": token_fingerprint(consent_token),
                "agent_id": cr.agent_id,
            }
        },
    )
    record_audit_event(
        db,
        "consent",
        "approve",
        user_id=user.id,
        agent_id=cr.agent_id,
        resource=token_fingerprint(consent_token),
        metadata={"request_id": request_id},
    )
    return {
        "consent_token": consent_token,
        "agent_id": cr.agent_id,
        "scopes": json_mod.loads(cr.scopes),
        "expires_at": grant.expires_at.isoformat(),
        "status": "approved",
    }


@router.post("/{request_id}/deny")
async def consent_deny(
    request_id: str,
    request: Request,
    user: User = Depends(get_current_user_or_api_key),
    db: Session = Depends(get_db),
):
    """Deny a consent request (the owner only)."""
    reject_agent_caller(request, "deny consent requests")
    cr = db.query(ConsentRequest).filter_by(id=request_id, user_id=user.id).first()
    if not cr:
        raise HTTPException(status_code=404, detail="Consent request not found.")
    if cr.status != "pending":
        raise HTTPException(
            status_code=409,
            detail=f"Consent request is already '{cr.status}'.",
        )

    cr.status = "denied"
    db.commit()

    logger.info("Consent denied", extra={"extra_data": {"request_id": request_id}})
    record_audit_event(db, "consent", "deny", user_id=user.id, agent_id=cr.agent_id, resource=request_id)
    return {"request_id": request_id, "status": "denied"}


@router.get("")
async def list_consents(
    request: Request,
    user: User = Depends(get_current_user_or_api_key),
    db: Session = Depends(get_db),
):
    """List the active consent grants of the current user (an agent sees only its own)."""
    query = db.query(ConsentGrant).filter_by(user_id=user.id, revoked=False)
    agent_id = _caller_agent_id(request)
    if agent_id:
        query = query.filter(ConsentGrant.agent_id == agent_id)
    now = utcnow()
    results = []
    for g in query.order_by(ConsentGrant.created_at, ConsentGrant.token).all():
        expires = as_utc(g.expires_at)
        if now > expires:
            continue  # Skip expired grants
        req = db.query(ConsentRequest).filter_by(id=g.consent_request_id).first()
        results.append(
            {
                "consent_token": g.token,
                "agent_name": req.agent_name if req else "unknown",
                "agent_id": g.agent_id,
                "scopes": json_mod.loads(g.scopes),
                "access_token": g.access_token,
                "expires_at": expires.isoformat(),
                "created_at": (g.created_at.isoformat() if g.created_at else None),
            }
        )
    return {"grants": results, "count": len(results)}


@router.delete("/{consent_token}")
async def revoke_consent(
    consent_token: str,
    request: Request,
    user: User = Depends(get_current_user_or_api_key),
    db: Session = Depends(get_db),
):
    """Revoke a consent grant immediately (an agent may give up its own grants)."""
    query = db.query(ConsentGrant).filter_by(token=consent_token, user_id=user.id)
    agent_id = _caller_agent_id(request)
    if agent_id:
        query = query.filter(ConsentGrant.agent_id == agent_id)
    grant = query.first()
    if not grant:
        raise HTTPException(status_code=404, detail="Consent grant not found.")
    if grant.revoked:
        raise HTTPException(status_code=409, detail="Consent already revoked.")

    grant.revoked = True
    db.commit()

    logger.info(
        "Consent revoked",
        extra={"extra_data": {"consent_token": token_fingerprint(consent_token)}},
    )
    record_audit_event(
        db,
        "consent",
        "revoke",
        user_id=user.id,
        agent_id=agent_id,
        resource=token_fingerprint(consent_token),
    )
    return {"status": "revoked", "consent_token": consent_token}
