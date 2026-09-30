"""
Link token flow endpoints: create_link, submit_credentials, submit_instructions,
fetch_data, link/token CRUD, public token exchange.

Every endpoint accepts a user's access token or an API key (X-API-Key); an
API key's or agent's site and scope restrictions apply. Tokens and
credentials travel only in JSON bodies, never in the URL, and only their
fingerprints reach logs and the audit trail.
"""

import asyncio
import base64
import binascii
import json as json_mod
import uuid
from typing import Iterable, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, update
from sqlalchemy.orm import Session

from src import session_store
from src.access_jobs import run_access_job
from src.audit import record_audit_event
from src.config import get_settings
from src.core.engine import connect_to_site
from src.crypto import decrypt_with_session_key, destroy_session_key, generate_keypair, token_fingerprint
from src.database import (
    AccessToken,
    ConsentGrant,
    ConsentRequest,
    Link,
    PublicToken,
    ScheduledRefreshJob,
    User,
    as_utc,
    decrypt_credential_for_user,
    encrypt_credential_for_user,
    get_current_key_version,
    get_db,
    utcnow,
)
from src.dependencies import (
    constrain_requested_scopes,
    ensure_site_allowed_for_request,
    get_auth_context,
    get_current_user_or_api_key,
    get_principal_allowed_scopes,
    load_scope_set,
)
from src.logging_config import get_logger
from src.models import (
    CreateLinkRequest,
    FetchDataRequest,
    PublicTokenExchangeRequest,
    SubmitCredentialsRequest,
    SubmitInstructionsRequest,
)

settings = get_settings()
logger = get_logger("api.links")

router = APIRouter(tags=["links"])

# Duration for which a public_token is valid (10 minutes).
_PUBLIC_TOKEN_TTL_MINUTES = 10


# ── Cleanup helpers (also used by /disconnect and account deletion) ──────────


def unschedule_refresh_jobs(access_tokens: Iterable[str] = (), *, user_id: Optional[int] = None) -> None:
    """Drop refresh jobs from this process's scheduler, so it stops running (and re-saving) them.

    ``access_tokens`` are the tokens being deleted; ``user_id`` drops every job
    of that user (account deletion).
    """
    from src.routers.refresh import _get_refresh_scheduler

    try:
        scheduler = _get_refresh_scheduler()
        tokens = set(access_tokens)
        if user_id is not None:
            tokens.update(job.access_token for job in scheduler.jobs_for_user(user_id))
        for token in tokens:
            scheduler.unschedule(token)
    except Exception:
        logger.exception("Could not unschedule refresh jobs")


def delete_access_tokens(db: Session, tokens: list[str]) -> int:
    """Delete access tokens, with their stored credentials and everything that hangs off them.

    Consents, public tokens and scheduled refreshes of the tokens go too (the
    database cascades on PostgreSQL; SQLite does not enforce foreign keys),
    and their refresh jobs are unscheduled. Does not commit; returns the
    number of access tokens deleted.
    """
    if not tokens:
        return 0
    unschedule_refresh_jobs(tokens)
    for column in (
        ConsentGrant.access_token,
        ConsentRequest.access_token,
        PublicToken.access_token,
        ScheduledRefreshJob.access_token,
    ):
        db.query(column.class_).filter(column.in_(tokens)).delete(synchronize_session=False)
    return db.query(AccessToken).filter(AccessToken.token.in_(tokens)).delete(synchronize_session=False)


def end_link_session(link_token: str) -> None:
    """Forget a link's hosted session and ephemeral key, so the link cannot be reconnected."""
    try:
        session_store.delete_link_session(link_token)
        destroy_session_key(link_token)
    except Exception as exc:
        logger.warning(
            "Could not clear the hosted session of a deleted link",
            extra={"extra_data": {"link_token": token_fingerprint(link_token), "error": type(exc).__name__}},
        )


# ── Link flow ─────────────────────────────────────────────────────────────────


@router.post("/create_link")
async def create_link(
    request: Request,
    site: str = Query(..., min_length=1, max_length=64),
    body: Optional[CreateLinkRequest] = None,
    user: User = Depends(get_current_user_or_api_key),
    db: Session = Depends(get_db),
):
    """
    Create a link token for a specific site.

    Step 1 of the Plaid-style multi-step flow.
    Optionally accepts a JSON body with ``scopes`` — a list of field names
    or scope strings (e.g. ``["balance", "transactions"]``) that will be
    enforced on data retrieval (omit for all fields, ``[]`` for none) — and a
    ``refresh_schedule`` registered when credentials are submitted.
    Everything is validated before the link is created.
    """
    ensure_site_allowed_for_request(request, site)
    effective_scopes = constrain_requested_scopes(request, body.scopes if body else None)

    directive = None
    if body is not None and body.refresh_schedule is not None:
        # Validate the directive eagerly so /create_link rejects bad input
        # rather than silently failing later in /submit_credentials.
        from src.scheduled_refresh import MIN_INTERVAL_SECONDS, resolve_schedule

        try:
            fmt, resolved_interval = resolve_schedule(
                schedule_format=body.refresh_schedule.schedule_format,
                interval_seconds=body.refresh_schedule.interval_seconds,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if resolved_interval < MIN_INTERVAL_SECONDS:
            raise HTTPException(
                status_code=400,
                detail=f"Minimum interval is {MIN_INTERVAL_SECONDS} seconds (5 minutes).",
            )
        directive = {"schedule_format": fmt, "interval_seconds": resolved_interval}

    link_token = str(uuid.uuid4())
    db.add(Link(link_token=link_token, site=site, user_id=user.id))
    db.commit()

    # Ephemeral RSA keypair for client-side encryption (CPU-heavy: off the event loop)
    public_key_pem = await asyncio.to_thread(generate_keypair, link_token)

    logger.info("Link created", extra={"extra_data": {"site": site, "user_id": user.id}})
    result = {"link_token": link_token, "public_key": public_key_pem}
    if effective_scopes is not None:
        session_store.set_link_scopes(link_token, json_mod.dumps(effective_scopes))
        result["scopes"] = effective_scopes
    if directive is not None:
        session_store.set_link_refresh_schedule(link_token, json_mod.dumps(directive))
        result["refresh_schedule"] = directive
    return result


@router.post("/submit_credentials")
async def submit_credentials(
    request: Request,
    body: SubmitCredentialsRequest,
    user: User = Depends(get_current_user_or_api_key),
    db: Session = Depends(get_db),
):
    """
    Submit credentials for a link token (JSON body; never the query string).

    Step 2 of the multi-step flow. Credentials are encrypted at rest.
    Accepts plaintext or RSA-OAEP encrypted credentials; the link's one-time
    key is destroyed once it has decrypted them.
    """
    link_token = body.link_token
    existing_link = db.query(Link).filter_by(link_token=link_token, user_id=user.id).first()
    if not existing_link:
        raise HTTPException(status_code=404, detail="Invalid link token.")
    ensure_site_allowed_for_request(request, existing_link.site)

    # Resolve credentials — encrypted takes precedence
    if body.encrypted_username and body.encrypted_password:
        try:
            plain_user = decrypt_with_session_key(link_token, base64.b64decode(body.encrypted_username))
            plain_pass = decrypt_with_session_key(link_token, base64.b64decode(body.encrypted_password))
        except (ValueError, binascii.Error) as exc:
            raise HTTPException(status_code=400, detail=str(exc) or "Failed to decrypt credentials.")
        destroy_session_key(link_token)
    elif body.username and body.password:
        plain_user, plain_pass = body.username, body.password
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
    auth_context = get_auth_context(request)
    logger.info(
        "Credentials submitted",
        extra={"extra_data": {"link_token": token_fingerprint(link_token), "user_id": user.id}},
    )
    record_audit_event(
        db,
        "token",
        "create",
        user_id=user.id,
        agent_id=auth_context.agent_id if auth_context else None,
        resource=token_fingerprint(access_token),
        metadata={"link_token": token_fingerprint(link_token), "site": existing_link.site},
    )
    result = {"access_token": access_token}
    if token_scopes is not None:
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
            logger.exception("Failed to apply deferred refresh_schedule for %s", token_fingerprint(link_token))
    return result


def _owned_token_with_site(db: Session, user: User, access_token: str) -> tuple[AccessToken, Link]:
    token_record = db.query(AccessToken).filter_by(token=access_token, user_id=user.id).first()
    if not token_record:
        raise HTTPException(status_code=401, detail="Invalid access token.")
    link = db.query(Link).filter_by(link_token=token_record.link_token, user_id=user.id).first()
    if not link:
        raise HTTPException(status_code=401, detail="Linked data not found.")
    return token_record, link


@router.post("/submit_instructions")
async def submit_instructions(
    request: Request,
    body: SubmitInstructionsRequest,
    user: User = Depends(get_current_user_or_api_key),
    db: Session = Depends(get_db),
):
    """Store processing instructions for an access token."""
    token_record, link = _owned_token_with_site(db, user, body.access_token)
    ensure_site_allowed_for_request(request, link.site)
    token_record.instructions = body.instructions
    db.commit()
    return {"status": "Instructions stored successfully"}


def _consented_fields(db: Session, user: User, body: FetchDataRequest, agent_id: Optional[str]) -> Optional[set[str]]:
    """The fields a consent grant allows, after checking it may be used here.

    An agent's API key must present a grant bound to that agent and to this
    access token; the owner (a login or an ordinary API key) may present any
    of their grants to narrow the result, or none.
    """
    if not body.consent_token:
        if agent_id:
            raise HTTPException(
                status_code=403,
                detail=(
                    "An agent needs the user's consent to read this data: request it with POST "
                    "/consent/request and send the approved consent_token."
                ),
            )
        return None

    grant = db.query(ConsentGrant).filter_by(token=body.consent_token, user_id=user.id).first()
    if not grant:
        raise HTTPException(status_code=401, detail="Invalid consent token.")
    if grant.revoked:
        raise HTTPException(status_code=403, detail="Consent has been revoked.")
    if utcnow() > as_utc(grant.expires_at):
        raise HTTPException(status_code=403, detail="Consent token has expired.")
    if grant.access_token != body.access_token:
        raise HTTPException(
            status_code=403,
            detail="Consent token does not match the access token.",
        )
    if agent_id and grant.agent_id != agent_id:
        raise HTTPException(status_code=403, detail="This consent was not granted to this agent.")
    fields = load_scope_set(grant.scopes)
    return fields if fields is not None else set()


@router.post("/fetch_data")
async def fetch_data(
    request: Request,
    body: FetchDataRequest,
    user: User = Depends(get_current_user_or_api_key),
    db: Session = Depends(get_db),
):
    """
    Fetch data using a previously submitted access token (JSON body; never the query string).

    Step 3 of the multi-step flow. Decrypts credentials, connects to the site,
    and returns extracted data.

    The returned fields are the intersection of the access token's scopes,
    the caller's (API key or agent) scopes and, when a ``consent_token`` is
    sent, the scopes of that consent. An agent's API key must send a consent
    token granted to that agent.
    """
    token_record, link = _owned_token_with_site(db, user, body.access_token)
    auth_context = ensure_site_allowed_for_request(request, link.site)
    agent_id = auth_context.agent_id if auth_context else None

    allowed_fields = _consented_fields(db, user, body, agent_id)

    # Also check access token scopes; merge to the most restrictive set.
    token_allowed = load_scope_set(token_record.scopes)
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
        site=link.site,
        job_type="fetch_data",
        executor=connect_to_site,
        executor_kwargs={
            "site": link.site,
            "username": username,
            "password": password,
            "extract_fields": sorted(allowed_fields) if allowed_fields is not None else None,
        },
        user_id=user.id,
        metadata={
            "access_token_fingerprint": token_fingerprint(body.access_token),
            "auth_method": auth_context.auth_method if auth_context else "jwt",
            "agent_id": agent_id,
            "api_key_id": auth_context.api_key_id if auth_context else None,
            "consent_token_provided": body.consent_token is not None,
            "extract_fields": sorted(allowed_fields) if allowed_fields is not None else [],
            "instructions_present": bool(user_instructions),
        },
    )
    response_data["job_id"] = job.id
    if user_instructions:
        response_data["instructions_applied"] = user_instructions

    # Filter data by scopes if applicable (consent + access token + caller)
    if allowed_fields is not None and "data" in response_data:
        response_data["data"] = {k: v for k, v in (response_data["data"] or {}).items() if k in allowed_fields}
        response_data["scopes_applied"] = sorted(allowed_fields)

    record_audit_event(
        db,
        "data_access",
        "fetch_data",
        user_id=user.id,
        agent_id=agent_id,
        resource=token_fingerprint(body.access_token),
        metadata={
            "site": link.site,
            "consent_token": token_fingerprint(body.consent_token) if body.consent_token else None,
        },
    )

    return response_data


# ── Link & Token Management ──────────────────────────────────────────────────


def _restrict_to_allowed_sites(request: Request, query, site_column):
    """Limit a query to the sites the caller's API key or agent may reach."""
    auth_context = get_auth_context(request)
    if auth_context is None or auth_context.allowed_sites is None:
        return query
    return query.filter(func.lower(site_column).in_(sorted(auth_context.allowed_sites)))


@router.get("/links")
async def list_links(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user_or_api_key),
    db: Session = Depends(get_db),
):
    """List the current user's links (oldest first), limited to the caller's allowed sites."""
    query = _restrict_to_allowed_sites(request, db.query(Link).filter_by(user_id=user.id), Link.site)
    links = query.order_by(Link.created_at, Link.link_token).offset(offset).limit(limit).all()
    return [{"link_token": link.link_token, "site": link.site} for link in links]


@router.delete("/links/{link_token}")
async def delete_link(
    request: Request,
    link_token: str,
    user: User = Depends(get_current_user_or_api_key),
    db: Session = Depends(get_db),
):
    """Delete a link and all its associated access tokens (and their credentials, consents and refresh jobs)."""
    link = db.query(Link).filter_by(link_token=link_token, user_id=user.id).first()
    if not link:
        raise HTTPException(status_code=404, detail="Link not found.")
    ensure_site_allowed_for_request(request, link.site)
    site = link.site
    tokens = [token for (token,) in db.query(AccessToken.token).filter_by(link_token=link_token, user_id=user.id)]
    revoked = delete_access_tokens(db, tokens)
    db.delete(link)
    db.commit()
    end_link_session(link_token)
    auth_context = get_auth_context(request)
    record_audit_event(
        db,
        "token",
        "link_deleted",
        user_id=user.id,
        agent_id=auth_context.agent_id if auth_context else None,
        resource=token_fingerprint(link_token),
        metadata={"site": site, "tokens_deleted": revoked},
    )
    return {"status": "Link and associated tokens deleted."}


@router.get("/tokens")
async def list_tokens(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user_or_api_key),
    db: Session = Depends(get_db),
):
    """List the current user's access tokens (oldest first), limited to the caller's allowed sites."""
    query = (
        db.query(AccessToken)
        .join(Link, AccessToken.link_token == Link.link_token)
        .filter(AccessToken.user_id == user.id)
    )
    query = _restrict_to_allowed_sites(request, query, Link.site)
    tokens = query.order_by(AccessToken.created_at, AccessToken.token).offset(offset).limit(limit).all()
    return [{"token": t.token, "link_token": t.link_token} for t in tokens]


@router.delete("/tokens/{token}")
async def delete_token(
    request: Request,
    token: str,
    user: User = Depends(get_current_user_or_api_key),
    db: Session = Depends(get_db),
):
    """Delete a specific access token (its stored credentials, consents and refresh job go with it)."""
    token_obj = db.query(AccessToken).filter_by(token=token, user_id=user.id).first()
    if not token_obj:
        raise HTTPException(status_code=404, detail="Token not found.")
    link = db.query(Link).filter_by(link_token=token_obj.link_token).first()
    ensure_site_allowed_for_request(request, link.site if link else "")
    delete_access_tokens(db, [token])
    db.commit()
    auth_context = get_auth_context(request)
    record_audit_event(
        db,
        "token",
        "revoke",
        user_id=user.id,
        agent_id=auth_context.agent_id if auth_context else None,
        resource=token_fingerprint(token),
    )
    return {"status": "Token deleted."}


# ── Public Token Exchange (3-Token Flow) ──────────────────────────────────────


@router.post("/exchange/public_token")
async def exchange_public_token(
    request: Request,
    body: PublicTokenExchangeRequest,
    user: User = Depends(get_current_user_or_api_key),
    db: Session = Depends(get_db),
):
    """Exchange a one-time public_token for a permanent access_token.

    This implements the Plaid-style 3-token exchange flow:
      link_token → public_token (short-lived, client-safe) → access_token (permanent, server-only)

    The public_token can only be exchanged once and expires after 10 minutes.
    The exchange is one conditional UPDATE, so of concurrent requests with
    the same token exactly one gets the access token.
    """
    pt = db.query(PublicToken).filter_by(token=body.public_token).first()
    if not pt:
        raise HTTPException(status_code=404, detail="Invalid public_token.")
    if pt.user_id != user.id:
        raise HTTPException(status_code=403, detail="Not authorized to exchange this token.")
    link = db.query(Link).filter_by(link_token=pt.link_token).first()
    ensure_site_allowed_for_request(request, link.site if link else "")

    claimed = db.execute(
        update(PublicToken)
        .where(
            PublicToken.token == body.public_token,
            PublicToken.exchanged.isnot(True),
            PublicToken.expires_at > utcnow(),
        )
        .values(exchanged=True)
        .execution_options(synchronize_session=False)
    ).rowcount
    if claimed != 1:
        db.rollback()
        db.refresh(pt)
        if pt.exchanged:
            raise HTTPException(status_code=410, detail="public_token has already been exchanged.")
        raise HTTPException(status_code=410, detail="public_token has expired.")
    db.commit()

    logger.info(
        "Public token exchanged",
        extra={
            "extra_data": {"link_token": token_fingerprint(pt.link_token), "user_id": user.id},
        },
    )
    return {"access_token": pt.access_token}
