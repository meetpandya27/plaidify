"""
Connection endpoints: connect, disconnect, encryption sessions, MFA.
"""

import asyncio
import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from src import session_store
from src.access_jobs import start_access_job, wait_for_mfa_session
from src.audit import record_audit_event
from src.config import get_settings
from src.core.engine import connect_to_site, submit_mfa_code
from src.core.mfa_manager import get_mfa_manager
from src.crypto import generate_keypair, get_public_key, token_fingerprint
from src.database import AccessToken, Link, User, encrypt_credential_for_user, get_current_key_version, get_db
from src.dependencies import (
    constrain_requested_scopes,
    ensure_site_allowed_for_request,
    get_auth_context,
    get_current_user_or_api_key,
    has_credentials,
    limiter,
    resolve_credentials,
)
from src.exceptions import MFARequiredError
from src.logging_config import get_logger
from src.models import (
    ConnectRequest,
    ConnectResponse,
    DisconnectRequest,
    DisconnectResponse,
    MFAStatusResponse,
    MFASubmitRequest,
)
from src.routers.link_sessions import _push_link_session_event, reconcile_link_session
from src.routers.links import delete_access_tokens, end_link_session

settings = get_settings()
logger = get_logger("api.connection")
_CONNECT_COMPLETION_WAIT_SECONDS = 1.5
_CONNECT_MFA_DISCOVERY_WAIT_SECONDS = 0.75

# Hosted-link session states in which the page may (re)submit credentials:
# before the first attempt, and after a failed one ("Try again").
_CONNECTABLE_STATES = frozenset({"awaiting_institution", "awaiting_credentials", "error"})
# States of an attempt still under way; the job may have ended since the
# session was last updated, so they are reconciled before deciding.
_IN_PROGRESS_STATES = frozenset({"connecting", "mfa_required", "verifying_mfa"})

router = APIRouter(tags=["connection"])


def _check_live_session(session: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Refuse a hosted-link session that no longer accepts credentials."""
    if not session:
        raise HTTPException(status_code=404, detail="Link session not found.")
    status = session.get("status")
    if status == "expired":
        raise HTTPException(status_code=410, detail="Link session has expired.")
    if status == "completed":
        raise HTTPException(status_code=409, detail="This link session has already been completed.")
    if status in _IN_PROGRESS_STATES:
        if session.get("current_job_id"):
            raise HTTPException(
                status_code=409, detail="A connection attempt is already in progress for this link session."
            )
        return session  # only a page event said so; no attempt is running
    if status not in _CONNECTABLE_STATES:
        raise HTTPException(status_code=409, detail="This link session no longer accepts credentials.")
    return session


async def _live_hosted_session(db: Session, link_token: str) -> Optional[Dict[str, Any]]:
    """The hosted-link session of ``link_token`` (None if it is not one), refreshed from its job."""
    session = session_store.get_link_session(link_token)
    if session and session.get("status") in _IN_PROGRESS_STATES:
        session = await reconcile_link_session(db, link_token=link_token)
    return session


def _ensure_hosted_link_access_token(
    db: Session,
    *,
    session: Dict[str, Any],
    link_token: str,
    site: str,
    username: str,
    password: str,
) -> tuple[Optional[str], Optional[int]]:
    """Store the credentials submitted through a live hosted-link session.

    Called only after the session was found to accept credentials, so a
    finished link's stored credentials are never replaced.
    """
    user_id = session.get("user_id")
    if user_id is None:
        return None, None

    user = db.query(User).filter(User.id == user_id).first()
    if user is None or not user.is_active:
        raise HTTPException(status_code=410, detail="Link session is no longer valid.")

    link = db.query(Link).filter_by(link_token=link_token).first()
    if link is None:
        link = Link(link_token=link_token, site=site, user_id=user_id)
        db.add(link)
    elif link.site != site:
        # The user went back to the picker after a failed attempt.
        link.site = site

    token = (
        db.query(AccessToken)
        .filter_by(link_token=link_token, user_id=user_id)
        .order_by(AccessToken.updated_at.desc())
        .first()
    )
    if token is None:
        token = AccessToken(
            token=str(uuid.uuid4()),
            link_token=link_token,
            username_encrypted=encrypt_credential_for_user(user, username),
            password_encrypted=encrypt_credential_for_user(user, password),
            scopes=session_store.pop_link_scopes(link_token),
            user_id=user_id,
            key_version=get_current_key_version(),
        )
        db.add(token)
    else:
        token.username_encrypted = encrypt_credential_for_user(user, username)
        token.password_encrypted = encrypt_credential_for_user(user, password)
        token.key_version = get_current_key_version()

    db.commit()
    return token.token, user_id


@router.get("/encryption/public_key/{link_token}")
@limiter.limit(settings.rate_limit_encryption)
async def get_encryption_key(request: Request, link_token: str, db: Session = Depends(get_db)):
    """Get the public key to encrypt credentials for a link.

    For a live hosted-link session every call issues a fresh one-time key
    (each attempt, including "Try again", encrypts to its own key; a key is
    destroyed once it has decrypted a submission). For other link tokens
    (``/create_link``, ``/encryption/session``) the key issued with the
    token is returned.
    """
    session = await _live_hosted_session(db, link_token)
    if session is not None:
        _check_live_session(session)
        public_key_pem = await asyncio.to_thread(generate_keypair, link_token)
        return {"link_token": link_token, "public_key": public_key_pem}

    pub_key = get_public_key(link_token)
    if not pub_key:
        raise HTTPException(
            status_code=404,
            detail="No encryption key found for this link token.",
        )
    return {"link_token": link_token, "public_key": pub_key}


@router.post("/encryption/session")
@limiter.limit(settings.rate_limit_encryption)
async def create_encryption_session(request: Request):
    """Create a temporary encryption session for one-shot /connect usage.

    Returns a link_token and public key without requiring authentication.
    The link_token is only used for credential encryption — not stored in DB.
    RSA key generation is CPU-heavy, so it runs off the event loop and the
    endpoint has its own, tighter rate limit.
    """
    link_token = str(uuid.uuid4())
    public_key_pem = await asyncio.to_thread(generate_keypair, link_token)
    return {"link_token": link_token, "public_key": public_key_pem}


def _within_allowed_fields(request: Request, response_data: Dict[str, Any]) -> Dict[str, Any]:
    """Drop extracted fields the caller's API key or agent may not read."""
    auth_context = get_auth_context(request)
    allowed = auth_context.allowed_scopes if auth_context else None
    if allowed is not None and isinstance(response_data.get("data"), dict):
        response_data["data"] = {k: v for k, v in response_data["data"].items() if k in allowed}
    return response_data


def _unauthenticated_connect() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail=(
            "Authentication required: send an X-API-Key, a Bearer access token, "
            "or the link_token of a live hosted-link session."
        ),
    )


@router.post("/connect", response_model=ConnectResponse)
@limiter.limit(settings.rate_limit_connect)
async def connect(
    request: Request,
    body: ConnectRequest,
    db: Session = Depends(get_db),
):
    """
    Connect to a site and extract data in a single step.

    This is the simplest integration path — send credentials, get data back.
    Credentials can be sent encrypted (recommended) or plaintext.
    If MFA is required, returns status='mfa_required' with a session_id.
    The client then calls POST /mfa/submit with the code.

    The caller needs an API key (``X-API-Key``), a Bearer access token, or the
    ``link_token`` of a live hosted-link session (the hosted page's path).
    A hosted session accepts credentials only before its first attempt or
    after a failed one, and only for its own site.
    """
    user: Optional[User] = None
    extract_fields = body.extract_fields
    if has_credentials(request):
        user = get_current_user_or_api_key(request, db)
        ensure_site_allowed_for_request(request, body.site)
        extract_fields = constrain_requested_scopes(request, body.extract_fields)

    hosted_session = await _live_hosted_session(db, body.link_token) if body.link_token else None
    if hosted_session is not None:
        _check_live_session(hosted_session)
        session_site = hosted_session.get("site")
        if session_site and session_site != body.site:
            raise HTTPException(status_code=400, detail="'site' does not match this link session.")
        # A session an agent created carries that agent's site allow-list
        # (None = unrestricted); the anonymous page can't widen it.
        allowed_sites = hosted_session.get("allowed_sites")
        if allowed_sites is not None and body.site.lower() not in allowed_sites:
            raise HTTPException(status_code=403, detail="This link session may not connect to that site.")
        owner_id = hosted_session.get("user_id")
        if user is not None and owner_id is not None and owner_id != user.id:
            raise HTTPException(status_code=403, detail="This link session belongs to another account.")
    elif user is None:
        raise _unauthenticated_connect()

    username, password = resolve_credentials(body)
    hosted_access_token = None
    # The caller owns a one-shot connect's job, so its result is stored
    # (encrypted under the owner's key) and readable back. Concurrency is
    # unaffected: the job lock is per site credential, not per owner, so one
    # key may still connect many end users to the same site at once.
    job_user_id = user.id if user is not None else None
    if hosted_session is not None:
        hosted_access_token, job_user_id = _ensure_hosted_link_access_token(
            db,
            session=hosted_session,
            link_token=body.link_token,
            site=body.site,
            username=username,
            password=password,
        )

    auth_context = get_auth_context(request)
    try:
        job, task = await start_access_job(
            db,
            site=body.site,
            job_type="connect",
            executor=connect_to_site,
            executor_name="connect_to_site",
            executor_kwargs={
                "site": body.site,
                "username": username,
                "password": password,
                "extract_fields": extract_fields,
            },
            principal_hint=username,
            metadata={
                "extract_fields": extract_fields or [],
                "link_token": token_fingerprint(body.link_token) if body.link_token else None,
                "auth_method": auth_context.auth_method if auth_context else "hosted_link",
                "agent_id": auth_context.agent_id if auth_context else None,
            },
            user_id=job_user_id,
        )

        if body.link_token and hosted_session is not None:
            session_store.update_link_session(
                body.link_token,
                {
                    "access_token": hosted_access_token,
                    "current_job_id": job.id,
                    "error_message": None,
                    "message": None,
                    "metadata": None,
                    "mfa_type": None,
                    "public_token": None,
                    "result": None,
                    "session_id": job.session_id,
                    "site": body.site,
                    "status": "connecting",
                },
            )

        try:
            completed_job, response_data = await asyncio.wait_for(
                asyncio.shield(task),
                timeout=_CONNECT_COMPLETION_WAIT_SECONDS,
            )
            response_data["job_id"] = completed_job.id
            if body.link_token and hosted_session is not None:
                await reconcile_link_session(db, link_token=body.link_token, job_id=completed_job.id)
            return _within_allowed_fields(request, response_data)
        except asyncio.TimeoutError:
            mfa_session = await wait_for_mfa_session(
                job.session_id,
                timeout=_CONNECT_MFA_DISCOVERY_WAIT_SECONDS,
            )
            if mfa_session:
                if body.link_token and hosted_session is not None:
                    await _push_link_session_event(
                        body.link_token,
                        "MFA_REQUIRED",
                        data={
                            "mfa_type": mfa_session["mfa_type"],
                            "session_id": mfa_session["session_id"],
                            "site": body.site,
                        },
                        updates={
                            "access_token": hosted_access_token,
                            "current_job_id": job.id,
                            "message": (mfa_session.get("metadata") or {}).get("message"),
                            "metadata": mfa_session.get("metadata") or {},
                            "mfa_type": mfa_session["mfa_type"],
                            "session_id": mfa_session["session_id"],
                            "site": body.site,
                            "status": "mfa_required",
                        },
                    )
                return ConnectResponse(
                    status="mfa_required",
                    job_id=job.id,
                    session_id=mfa_session["session_id"],
                    mfa_type=mfa_session["mfa_type"],
                    metadata=mfa_session.get("metadata") or {},
                )

            if task.done():
                completed_job, response_data = await task
                response_data["job_id"] = completed_job.id
                if body.link_token and hosted_session is not None:
                    await reconcile_link_session(db, link_token=body.link_token, job_id=completed_job.id)
                return _within_allowed_fields(request, response_data)

            return ConnectResponse(
                status="pending",
                job_id=job.id,
                session_id=job.session_id,
                metadata={
                    "message": (
                        "Connection is still running in the background. Poll /access_jobs/{job_id} for status updates."
                    )
                },
            )
    except MFARequiredError as e:
        if body.link_token and hosted_session is not None:
            await _push_link_session_event(
                body.link_token,
                "MFA_REQUIRED",
                data={
                    "mfa_type": e.mfa_type,
                    "session_id": e.session_id,
                    "site": body.site,
                },
                updates={
                    "access_token": hosted_access_token,
                    "current_job_id": getattr(e, "job_id", None),
                    "message": e.message,
                    "metadata": {"message": e.message},
                    "mfa_type": e.mfa_type,
                    "session_id": e.session_id,
                    "site": body.site,
                    "status": "mfa_required",
                },
            )
        return ConnectResponse(
            status="mfa_required",
            job_id=getattr(e, "job_id", None),
            mfa_type=e.mfa_type,
            session_id=e.session_id,
            metadata={"message": e.message},
        )


@router.post("/disconnect", response_model=DisconnectResponse)
async def disconnect(
    request: Request,
    body: DisconnectRequest,
    user: User = Depends(get_current_user_or_api_key),
    db: Session = Depends(get_db),
):
    """Disconnect a link: revoke its access tokens and erase the credentials stored with them.

    The link itself stays listed (``DELETE /links/{link_token}`` removes it);
    its consents, public tokens and refresh jobs go with the tokens, and its
    hosted-link session ends.
    """
    link = db.query(Link).filter_by(link_token=body.link_token, user_id=user.id).first()
    if not link:
        raise HTTPException(status_code=404, detail="Link not found.")
    ensure_site_allowed_for_request(request, link.site)
    tokens = [token for (token,) in db.query(AccessToken.token).filter_by(link_token=link.link_token, user_id=user.id)]
    revoked = delete_access_tokens(db, tokens)
    db.commit()
    end_link_session(link.link_token)

    auth_context = get_auth_context(request)
    record_audit_event(
        db,
        "token",
        "disconnect",
        user_id=user.id,
        agent_id=auth_context.agent_id if auth_context else None,
        resource=token_fingerprint(link.link_token),
        metadata={"site": link.site, "tokens_deleted": revoked},
    )
    return DisconnectResponse(
        status="disconnected",
        link_token=link.link_token,
        revoked_tokens=revoked,
        message="Access tokens and stored credentials for this link were deleted.",
    )


# ── MFA Endpoints ─────────────────────────────────────────────────────────────


@router.post("/mfa/submit")
@limiter.limit(settings.rate_limit_mfa)
async def mfa_submit(request: Request, body: MFASubmitRequest):
    """
    Submit an MFA code for a pending session.

    After a connection returns status 'mfa_required', the client retrieves
    the code from the user and submits it here as a JSON body
    (``{"session_id": ..., "code": ...}``); a code in the URL would land in
    every access log on the way. Rate-limited per IP to deter brute-forcing
    short numeric codes.
    """
    result = await submit_mfa_code(body.session_id, body.code)
    return result


@router.get("/mfa/status/{session_id}", response_model=MFAStatusResponse)
async def mfa_status(session_id: str):
    """
    Check the status of an MFA session.

    Returns session metadata (type, question text, etc.) or 404 if expired.
    """
    mfa_manager = get_mfa_manager()
    session = await mfa_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="MFA session not found or expired.")
    return MFAStatusResponse(
        session_id=session.session_id,
        site=session.site,
        mfa_type=session.mfa_type,
        metadata=session.metadata,
    )
