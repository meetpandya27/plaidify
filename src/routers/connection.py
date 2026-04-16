"""
Connection endpoints: connect, disconnect, encryption sessions, MFA.
"""

import uuid

from fastapi import APIRouter, HTTPException, Request

from src.config import get_settings
from src.core.engine import connect_to_site, submit_mfa_code
from src.core.mfa_manager import get_mfa_manager
from src.crypto import generate_keypair, get_public_key
from src.dependencies import limiter, resolve_credentials
from src.exceptions import MFARequiredError
from src.models import ConnectRequest, ConnectResponse

settings = get_settings()

router = APIRouter(tags=["connection"])


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
async def connect(request: Request, body: ConnectRequest):
    """
    Connect to a site and extract data in a single step.

    This is the simplest integration path — send credentials, get data back.
    Credentials can be sent encrypted (recommended) or plaintext.
    If MFA is required, returns status='mfa_required' with a session_id.
    The client then calls POST /mfa/submit with the code.
    """
    username, password = resolve_credentials(body)
    try:
        response_data = await connect_to_site(
            site=body.site,
            username=username,
            password=password,
            extract_fields=body.extract_fields,
        )
        return response_data
    except MFARequiredError as e:
        return ConnectResponse(
            status="mfa_required",
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
        raise HTTPException(
            status_code=404, detail="MFA session not found or expired."
        )
    return {
        "session_id": session.session_id,
        "site": session.site,
        "mfa_type": session.mfa_type,
        "metadata": session.metadata,
    }
