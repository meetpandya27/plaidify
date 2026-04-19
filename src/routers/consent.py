"""
Consent engine endpoints: request, approve, deny, list, revoke.
"""

import json as json_mod
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from src.audit import record_audit_event
from src.database import AccessToken, ConsentGrant, ConsentRequest, User, get_db
from src.dependencies import get_current_user
from src.logging_config import get_logger

logger = get_logger("api.consent")

_MAX_CONSENT_DURATION = 30 * 24 * 3600  # 30 days in seconds

router = APIRouter(prefix="/consent", tags=["consent"])


@router.post("/request")
async def consent_request(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Request user consent for scoped, time-limited data access.

    An AI agent calls this endpoint to ask for permission to read specific fields.
    The returned request_id is used by the user to approve or deny.
    """
    body = await request.json()
    agent_name = body.get("agent_name")
    scopes = body.get("scopes")
    access_token_str = body.get("access_token")
    duration = body.get("duration_seconds", 3600)

    if not agent_name:
        raise HTTPException(status_code=422, detail="'agent_name' is required.")
    if not scopes or not isinstance(scopes, list):
        raise HTTPException(
            status_code=422,
            detail="'scopes' must be a non-empty list of scope strings.",
        )
    if not access_token_str:
        raise HTTPException(status_code=422, detail="'access_token' is required.")
    if duration > _MAX_CONSENT_DURATION:
        raise HTTPException(
            status_code=422,
            detail=f"Duration cannot exceed {_MAX_CONSENT_DURATION} seconds (30 days).",
        )
    if duration < 60:
        raise HTTPException(status_code=422, detail="Duration must be at least 60 seconds.")

    # Verify the access token belongs to this user
    token_record = db.query(AccessToken).filter_by(token=access_token_str, user_id=user.id).first()
    if not token_record:
        raise HTTPException(status_code=401, detail="Invalid access token.")

    request_id = f"creq-{uuid.uuid4()}"
    cr = ConsentRequest(
        id=request_id,
        agent_name=agent_name,
        agent_description=body.get("agent_description", ""),
        scopes=json_mod.dumps(scopes),
        duration_seconds=duration,
        access_token=access_token_str,
        user_id=user.id,
    )
    db.add(cr)
    db.commit()

    logger.info(
        "Consent requested",
        extra={"extra_data": {"request_id": request_id, "agent": agent_name}},
    )
    return {
        "request_id": request_id,
        "agent_name": agent_name,
        "scopes": scopes,
        "duration_seconds": duration,
        "status": "pending",
    }


@router.post("/{request_id}/approve")
async def consent_approve(
    request_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Approve a consent request. Creates a time-limited consent grant token."""
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
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=cr.duration_seconds),
    )
    db.add(grant)
    db.commit()

    logger.info(
        "Consent approved",
        extra={
            "extra_data": {
                "request_id": request_id,
                "consent_token": consent_token,
            }
        },
    )
    return {
        "consent_token": consent_token,
        "scopes": json_mod.loads(cr.scopes),
        "expires_at": grant.expires_at.isoformat(),
        "status": "approved",
    }


@router.post("/{request_id}/deny")
async def consent_deny(
    request_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Deny a consent request."""
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
    return {"request_id": request_id, "status": "denied"}


@router.get("")
async def list_consents(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List all active consent grants for the current user."""
    grants = db.query(ConsentGrant).filter_by(user_id=user.id, revoked=False).all()
    now = datetime.now(timezone.utc)
    results = []
    for g in grants:
        expires = g.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if now > expires:
            continue  # Skip expired grants
        req = db.query(ConsentRequest).filter_by(id=g.consent_request_id).first()
        results.append(
            {
                "consent_token": g.token,
                "agent_name": req.agent_name if req else "unknown",
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
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Revoke a consent grant immediately."""
    grant = db.query(ConsentGrant).filter_by(token=consent_token, user_id=user.id).first()
    if not grant:
        raise HTTPException(status_code=404, detail="Consent grant not found.")
    if grant.revoked:
        raise HTTPException(status_code=409, detail="Consent already revoked.")

    grant.revoked = True
    db.commit()

    logger.info(
        "Consent revoked",
        extra={"extra_data": {"consent_token": consent_token}},
    )
    record_audit_event(
        db,
        "consent",
        "revoke",
        user_id=user.id,
        resource=consent_token,
    )
    return {"status": "revoked", "consent_token": consent_token}
