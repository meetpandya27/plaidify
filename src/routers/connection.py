"""
Connection endpoints: connect, disconnect, encryption sessions, MFA.
"""

import asyncio
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from src import session_store
from src.access_jobs import start_access_job, wait_for_mfa_session
from src.config import get_settings
from src.core.engine import connect_to_site, submit_mfa_code
from src.core.mfa_manager import get_mfa_manager
from src.crypto import generate_keypair, get_public_key
from src.database import AccessToken, Link, User, encrypt_credential_for_user, get_current_key_version, get_db
from src.dependencies import limiter, resolve_credentials
from src.exceptions import MFARequiredError
from src.models import ConnectRequest, ConnectResponse
from src.routers.link_sessions import _push_link_session_event, reconcile_link_session

settings = get_settings()
_CONNECT_COMPLETION_WAIT_SECONDS = 1.5
_CONNECT_MFA_DISCOVERY_WAIT_SECONDS = 0.75

router = APIRouter(tags=["connection"])


def _ensure_hosted_link_access_token(
    db: Session,
    *,
    link_token: str,
    site: str,
    username: str,
    password: str,
) -> tuple[Optional[dict], Optional[str], Optional[int]]:
    session = session_store.get_link_session(link_token)
    if not session:
        return None, None, None

    user_id = session.get("user_id")
    if user_id is None:
        return session, None, None

    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        return None, None, None

    link = db.query(Link).filter_by(link_token=link_token).first()
    if link is None:
        link = Link(link_token=link_token, site=site, user_id=user_id)
        db.add(link)
    elif link.site != site:
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
    return session, token.token, user_id


@router.get("/encryption/public_key/{link_token}")
async def get_encryption_key(link_token: str):
    """Get the ephemeral public key for a link session.

    Clients use this for the one-shot /connect flow: create a temporary
    link_token just to get the public key, then encrypt credentials before
    calling /connect.
    """
    pub_key = get_public_key(link_token)
    if not pub_key:
        raise HTTPException(
            status_code=404,
            detail="No encryption key found for this link token.",
        )
    return {"link_token": link_token, "public_key": pub_key}


@router.post("/encryption/session")
async def create_encryption_session():
    """Create a temporary encryption session for one-shot /connect usage.

    Returns a link_token and public key without requiring authentication.
    The link_token is only used for credential encryption — not stored in DB.
    """
    link_token = str(uuid.uuid4())
    public_key_pem = generate_keypair(link_token)
    return {"link_token": link_token, "public_key": public_key_pem}


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
    """
    username, password = resolve_credentials(body)
    hosted_session = None
    hosted_access_token = None
    hosted_user_id = None
    if body.link_token:
        hosted_session, hosted_access_token, hosted_user_id = _ensure_hosted_link_access_token(
            db,
            link_token=body.link_token,
            site=body.site,
            username=username,
            password=password,
        )

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
                "extract_fields": body.extract_fields,
            },
            principal_hint=username,
            metadata={
                "extract_fields": body.extract_fields or [],
                "link_token": body.link_token,
            },
            user_id=hosted_user_id,
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
            return response_data
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
                return response_data

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


@router.post("/disconnect")
async def disconnect():
    """Disconnect / end a session."""
    return {"status": "disconnected"}


# ── MFA Endpoints ─────────────────────────────────────────────────────────────


@router.post("/mfa/submit")
async def mfa_submit(session_id: str, code: str):
    """
    Submit an MFA code for a pending session.

    After a connection returns status 'mfa_required', the client retrieves
    the code from the user and submits it here.
    """
    result = await submit_mfa_code(session_id, code)
    return result


@router.get("/mfa/status/{session_id}")
async def mfa_status(session_id: str):
    """
    Check the status of an MFA session.

    Returns session metadata (type, question text, etc.) or 404 if expired.
    """
    mfa_manager = get_mfa_manager()
    session = await mfa_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="MFA session not found or expired.")
    return {
        "session_id": session.session_id,
        "site": session.site,
        "mfa_type": session.mfa_type,
        "metadata": session.metadata,
    }
