"""
Link token flow endpoints: create_link, submit_credentials, submit_instructions,
fetch_data, link/token CRUD, public token exchange.
"""

import base64
import json as json_mod
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from src import session_store
from src.access_jobs import run_access_job
from src.audit import record_audit_event
from src.config import get_settings
from src.core.engine import connect_to_site
from src.crypto import decrypt_with_session_key, destroy_session_key, generate_keypair
from src.database import (
    AccessToken,
    ConsentGrant,
    Link,
    PublicToken,
    User,
    decrypt_credential_for_user,
    encrypt_credential_for_user,
    get_current_key_version,
    get_db,
)
from src.dependencies import (
    constrain_requested_scopes,
    ensure_site_allowed_for_request,
    get_current_user,
    get_current_user_or_api_key,
    get_principal_allowed_scopes,
)
from src.logging_config import get_logger

settings = get_settings()
logger = get_logger("api.links")

router = APIRouter(tags=["links"])

# Duration for which a public_token is valid (10 minutes).
_PUBLIC_TOKEN_TTL_MINUTES = 10


@router.post("/create_link")
async def create_link(
    request: Request,
    site: str,
    user: User = Depends(get_current_user_or_api_key),
    db: Session = Depends(get_db),
):
    """
    Create a link token for a specific site.

    Step 1 of the Plaid-style multi-step flow.
    Optionally accepts a JSON body with ``scopes`` — a list of field names
    or scope strings (e.g. ``["balance", "transactions"]``) that will be
    enforced on data retrieval. If omitted, all fields are allowed.
    """
    # Parse optional scopes / refresh_schedule from JSON body
    scopes = None
    refresh_schedule = None
    try:
        body = await request.json()
        if body:
            scopes = body.get("scopes")
            refresh_schedule = body.get("refresh_schedule")
    except Exception:
        pass  # No body or non-JSON body is fine

    ensure_site_allowed_for_request(request, site)
    effective_scopes = constrain_requested_scopes(request, scopes)

    link_token = str(uuid.uuid4())
    new_link = Link(link_token=link_token, site=site, user_id=user.id)
    db.add(new_link)
    db.commit()

    # Generate ephemeral RSA keypair for client-side encryption
    public_key_pem = generate_keypair(link_token)

    logger.info("Link created", extra={"extra_data": {"site": site, "user_id": user.id}})
    result = {"link_token": link_token, "public_key": public_key_pem}
    if effective_scopes is not None:
        session_store.set_link_scopes(link_token, json_mod.dumps(effective_scopes))
        result["scopes"] = effective_scopes
    if refresh_schedule is not None:
        # Validate the directive eagerly so /create_link rejects bad input
        # rather than silently failing later in /submit_credentials.
        from src.scheduled_refresh import resolve_schedule

        if not isinstance(refresh_schedule, dict):
            raise HTTPException(status_code=400, detail="refresh_schedule must be an object.")
        try:
            fmt, resolved_interval = resolve_schedule(
                schedule_format=refresh_schedule.get("schedule_format") or refresh_schedule.get("format"),
                interval_seconds=refresh_schedule.get("interval_seconds", 3600),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        from src.scheduled_refresh import MIN_INTERVAL_SECONDS

        if resolved_interval < MIN_INTERVAL_SECONDS:
            raise HTTPException(
                status_code=400,
                detail=f"Minimum interval is {MIN_INTERVAL_SECONDS} seconds (5 minutes).",
            )
        directive = {"schedule_format": fmt, "interval_seconds": resolved_interval}
        session_store.set_link_refresh_schedule(link_token, json_mod.dumps(directive))
        result["refresh_schedule"] = directive
    return result


@router.post("/submit_credentials")
async def submit_credentials(
    link_token: str,
    username: Optional[str] = None,
    password: Optional[str] = None,
    encrypted_username: Optional[str] = None,
    encrypted_password: Optional[str] = None,
    user: User = Depends(get_current_user_or_api_key),
    db: Session = Depends(get_db),
):
    """
    Submit credentials for a link token.

    Step 2 of the multi-step flow. Credentials are encrypted at rest.
    Accepts plaintext or RSA-OAEP encrypted credentials.
    """
    existing_link = db.query(Link).filter_by(link_token=link_token, user_id=user.id).first()
    if not existing_link:
        raise HTTPException(status_code=404, detail="Invalid link token.")

    # Resolve credentials — encrypted takes precedence
    if encrypted_username and encrypted_password:
        try:
            plain_user = decrypt_with_session_key(link_token, base64.b64decode(encrypted_username))
            plain_pass = decrypt_with_session_key(link_token, base64.b64decode(encrypted_password))
            destroy_session_key(link_token)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    elif username and password:
        plain_user, plain_pass = username, password
    else:
        raise HTTPException(
            status_code=422,
            detail="Provide either (username + password) or (encrypted_username + encrypted_password).",
        )

    encrypted_username_stored = encrypt_credential_for_user(user, plain_user)
    encrypted_password_stored = encrypt_credential_for_user(user, plain_pass)
    access_token = str(uuid.uuid4())

    # Inherit scopes from the link creation step (if any)
    token_scopes = session_store.pop_link_scopes(link_token)

    new_token = AccessToken(
        token=access_token,
        link_token=link_token,
        username_encrypted=encrypted_username_stored,
        password_encrypted=encrypted_password_stored,
        scopes=token_scopes,
        user_id=user.id,
        key_version=get_current_key_version(),
    )
    db.add(new_token)
    db.commit()
    logger.info(
        "Credentials submitted",
        extra={"extra_data": {"link_token": link_token, "user_id": user.id}},
    )
    record_audit_event(
        db,
        "token",
        "create",
        user_id=user.id,
        resource=access_token,
        metadata={"link_token": link_token},
    )
    result = {"access_token": access_token}
    if token_scopes:
        result["scopes"] = json_mod.loads(token_scopes)

    # If /create_link attached a refresh_schedule directive, register it now
    # that we have an access_token. Failures here are non-fatal; the caller
    # can always re-register via POST /refresh/schedule.
    deferred = session_store.pop_link_refresh_schedule(link_token)
    if deferred:
        try:
            from src.routers.refresh import _get_refresh_scheduler

            directive = json_mod.loads(deferred)
            scheduler = _get_refresh_scheduler()
            if not scheduler.running:
                scheduler.start()
            scheduler.schedule(
                access_token,
                user.id,
                interval_seconds=directive.get("interval_seconds"),
                schedule_format=directive.get("schedule_format"),
            )
            result["refresh_schedule"] = {
                "interval_seconds": directive.get("interval_seconds"),
                "schedule_format": directive.get("schedule_format"),
            }
        except Exception:
            logger.exception("Failed to apply deferred refresh_schedule for %s", link_token)
    return result


@router.post("/submit_instructions")
async def submit_instructions(
    access_token: str,
    instructions: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Store processing instructions for an access token."""
    token_record = db.query(AccessToken).filter_by(token=access_token, user_id=user.id).first()
    if not token_record:
        raise HTTPException(status_code=401, detail="Invalid access token.")
    token_record.instructions = instructions
    db.commit()
    return {"status": "Instructions stored successfully"}


@router.get("/fetch_data")
async def fetch_data(
    request: Request,
    access_token: str,
    consent_token: Optional[str] = None,
    user: User = Depends(get_current_user_or_api_key),
    db: Session = Depends(get_db),
):
    """
    Fetch data using a previously submitted access token.

    Step 3 of the multi-step flow. Decrypts credentials, connects to the site,
    and returns extracted data.

    If a consent_token is provided, the returned data is filtered to only the
    scopes granted by that consent.
    """
    token_record = db.query(AccessToken).filter_by(token=access_token, user_id=user.id).first()
    if not token_record:
        raise HTTPException(status_code=401, detail="Invalid access token.")

    site = db.query(Link).filter_by(link_token=token_record.link_token, user_id=user.id).first()
    if not site:
        raise HTTPException(status_code=401, detail="Linked data not found.")

    auth_context = ensure_site_allowed_for_request(request, site.site)

    # Validate consent token if provided
    allowed_fields = None
    if consent_token:
        grant = db.query(ConsentGrant).filter_by(token=consent_token, user_id=user.id).first()
        if not grant:
            raise HTTPException(status_code=401, detail="Invalid consent token.")
        if grant.revoked:
            raise HTTPException(status_code=403, detail="Consent has been revoked.")
        grant_expires = grant.expires_at
        if grant_expires.tzinfo is None:
            grant_expires = grant_expires.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > grant_expires:
            raise HTTPException(status_code=403, detail="Consent token has expired.")
        if grant.access_token != access_token:
            raise HTTPException(
                status_code=403,
                detail="Consent token does not match the access token.",
            )
        scopes = json_mod.loads(grant.scopes)
        # Extract field names from scopes like "read:current_bill" -> "current_bill"
        allowed_fields = set()
        for scope in scopes:
            if ":" in scope:
                allowed_fields.add(scope.split(":", 1)[1])
            else:
                allowed_fields.add(scope)

    # Also check access token scopes
    token_allowed = None
    if token_record.scopes:
        token_scopes_list = json_mod.loads(token_record.scopes)
        token_allowed = set()
        for scope in token_scopes_list:
            if ":" in scope:
                token_allowed.add(scope.split(":", 1)[1])
            else:
                token_allowed.add(scope)

    # Merge: use the most restrictive set of allowed fields
    if allowed_fields is not None and token_allowed is not None:
        allowed_fields = allowed_fields & token_allowed
    elif token_allowed is not None:
        allowed_fields = token_allowed

    principal_allowed = get_principal_allowed_scopes(request)
    if principal_allowed is not None and allowed_fields is not None:
        allowed_fields = allowed_fields & principal_allowed
    elif principal_allowed is not None:
        allowed_fields = set(principal_allowed)

    username = decrypt_credential_for_user(user, token_record.username_encrypted)
    password = decrypt_credential_for_user(user, token_record.password_encrypted)
    user_instructions = token_record.instructions

    job, response_data = await run_access_job(
        db,
        site=site.site,
        job_type="fetch_data",
        executor=connect_to_site,
        executor_kwargs={
            "site": site.site,
            "username": username,
            "password": password,
            "extract_fields": sorted(allowed_fields) if allowed_fields is not None else None,
        },
        user_id=user.id,
        metadata={
            "access_token_prefix": access_token[:12],
            "auth_method": auth_context.auth_method if auth_context else "jwt",
            "agent_id": auth_context.agent_id if auth_context else None,
            "api_key_id": auth_context.api_key_id if auth_context else None,
            "consent_token_provided": consent_token is not None,
            "extract_fields": sorted(allowed_fields) if allowed_fields is not None else [],
            "instructions_present": bool(user_instructions),
        },
    )
    response_data["job_id"] = job.id
    if user_instructions:
        response_data["instructions_applied"] = user_instructions

    # Filter data by scopes if applicable (consent + access token)
    if allowed_fields is not None and "data" in response_data:
        response_data["data"] = {k: v for k, v in response_data["data"].items() if k in allowed_fields}
        response_data["scopes_applied"] = sorted(allowed_fields)

    record_audit_event(
        db,
        "data_access",
        "fetch_data",
        user_id=user.id,
        resource=access_token,
        metadata={"site": site.site},
    )

    return response_data


# ── Link & Token Management ──────────────────────────────────────────────────


@router.get("/links")
async def list_links(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """List all links for the current user."""
    links = db.query(Link).filter_by(user_id=user.id).all()
    return [{"link_token": link.link_token, "site": link.site} for link in links]


@router.delete("/links/{link_token}")
async def delete_link(
    link_token: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete a link and all its associated access tokens."""
    link = db.query(Link).filter_by(link_token=link_token, user_id=user.id).first()
    if not link:
        raise HTTPException(status_code=404, detail="Link not found.")
    db.query(AccessToken).filter_by(link_token=link_token, user_id=user.id).delete()
    db.delete(link)
    db.commit()
    record_audit_event(
        db,
        "token",
        "link_deleted",
        user_id=user.id,
        resource=link_token,
        metadata={"site": link.site},
    )
    return {"status": "Link and associated tokens deleted."}


@router.get("/tokens")
async def list_tokens(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """List all access tokens for the current user."""
    tokens = db.query(AccessToken).filter_by(user_id=user.id).all()
    return [{"token": t.token, "link_token": t.link_token} for t in tokens]


@router.delete("/tokens/{token}")
async def delete_token(
    token: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete a specific access token."""
    token_obj = db.query(AccessToken).filter_by(token=token, user_id=user.id).first()
    if not token_obj:
        raise HTTPException(status_code=404, detail="Token not found.")
    db.delete(token_obj)
    db.commit()
    record_audit_event(db, "token", "revoke", user_id=user.id, resource=token)
    return {"status": "Token deleted."}


# ── Public Token Exchange (3-Token Flow) ──────────────────────────────────────


@router.post("/exchange/public_token")
async def exchange_public_token(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Exchange a one-time public_token for a permanent access_token.

    This implements the Plaid-style 3-token exchange flow:
      link_token → public_token (short-lived, client-safe) → access_token (permanent, server-only)

    The public_token can only be exchanged once and expires after 10 minutes.
    """
    body = await request.json()
    public_token = body.get("public_token")
    if not public_token:
        raise HTTPException(status_code=422, detail="public_token is required.")

    pt = db.query(PublicToken).filter_by(token=public_token).first()
    if not pt:
        raise HTTPException(status_code=404, detail="Invalid public_token.")
    if pt.user_id != user.id:
        raise HTTPException(status_code=403, detail="Not authorized to exchange this token.")
    if pt.exchanged:
        raise HTTPException(status_code=410, detail="public_token has already been exchanged.")
    expires_at = pt.expires_at.replace(tzinfo=timezone.utc) if pt.expires_at.tzinfo is None else pt.expires_at
    if datetime.now(timezone.utc) > expires_at:
        raise HTTPException(status_code=410, detail="public_token has expired.")

    # Mark as exchanged (single-use)
    pt.exchanged = True
    db.commit()

    logger.info(
        "Public token exchanged",
        extra={
            "extra_data": {"link_token": pt.link_token, "user_id": user.id},
        },
    )
    return {"access_token": pt.access_token}
