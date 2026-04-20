"""
Hosted link page, link session management, and SSE event streaming.
"""

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from sse_starlette.sse import EventSourceResponse

from src import session_store
from src.auth_utils import create_link_launch_token, decode_link_launch_token
from src.config import get_settings
from src.crypto import generate_keypair
from src.database import Link, PublicToken, User, get_db
from src.dependencies import (
    constrain_requested_scopes,
    ensure_site_allowed_for_request,
    get_auth_context,
    get_current_user_or_api_key,
)
from src.logging_config import get_logger
from src.models import (
    HostedLinkBootstrapExchangeRequest,
    HostedLinkBootstrapRequest,
    HostedLinkBootstrapResponse,
)

settings = get_settings()
logger = get_logger("api.link_sessions")
FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"

router = APIRouter(tags=["link_sessions"])

# TTL for link sessions (30 minutes)
_LINK_SESSION_TTL = session_store.LINK_SESSION_TTL

# Duration for which a public_token is valid (10 minutes).
_PUBLIC_TOKEN_TTL_MINUTES = 10

# Local SSE subscribers — asyncio.Queues can't be serialized to Redis,
# so we keep a local map of link_token -> list[asyncio.Queue].
# In multi-worker deployments, each worker only serves its own SSE connections.
_sse_subscribers: Dict[str, list] = {}
_sse_lock = asyncio.Lock()


def _extract_request_origin(request: Request) -> Optional[str]:
    """Return the caller origin from Origin or Referer headers."""
    origin = request.headers.get("origin")
    if origin:
        return origin.rstrip("/")

    referer = request.headers.get("referer")
    if not referer:
        return None

    try:
        from urllib.parse import urlparse

        parsed = urlparse(referer)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
    except Exception:
        return None

    return None


def _configured_public_link_allowed_origins() -> set[str]:
    return {
        origin.strip().rstrip("/")
        for origin in settings.public_link_allowed_origins.split(",")
        if origin.strip()
    }


def _enforce_public_link_session_policy(request: Request) -> None:
    """Enforce production safeguards around anonymous public link sessions."""
    if settings.env == "production" and not settings.public_link_sessions_enabled:
        raise HTTPException(
            status_code=403,
            detail="Anonymous public link sessions are disabled in production.",
        )

    allowed_origins = _configured_public_link_allowed_origins()
    if not allowed_origins:
        return

    request_origin = _extract_request_origin(request)
    if not request_origin or request_origin not in allowed_origins:
        raise HTTPException(
            status_code=403,
            detail="This origin is not allowed to create anonymous public link sessions.",
        )


def _get_link_session(token: str) -> Optional[Dict[str, Any]]:
    """Return a link session if it exists and hasn't expired."""
    return session_store.get_link_session(token)


def _create_ephemeral_link_session(
    *,
    site: Optional[str],
    user_id: Optional[int],
    db: Optional[Session],
    scopes: Optional[list[str]],
) -> Dict[str, Any]:
    """Create a hosted link session and its ephemeral encryption material."""
    link_token = str(uuid.uuid4())

    if site and user_id is not None and db is not None:
        new_link = Link(link_token=link_token, site=site, user_id=user_id)
        db.add(new_link)
        db.commit()

    public_key_pem = generate_keypair(link_token)

    if scopes is not None:
        session_store.set_link_scopes(link_token, json.dumps(scopes))

    session_store.create_link_session(
        link_token,
        {
            "status": "awaiting_institution",
            "site": site,
            "user_id": user_id,
            "events": [],
            "access_token": None,
        },
    )

    return {
        "link_token": link_token,
        "link_url": f"/link?token={link_token}",
        "public_key": public_key_pem,
        "expires_in": _LINK_SESSION_TTL,
        "scopes": scopes,
    }


# ── Hosted Link Page ──────────────────────────────────────────────────────────


@router.get("/link", response_class=HTMLResponse)
async def hosted_link_page(token: Optional[str] = None):
    """Serve the hosted Link page.

    The page validates the token client-side via the /link/sessions API.
    """
    link_html = FRONTEND_DIR / "link.html"
    if not link_html.exists():
        raise HTTPException(status_code=500, detail="Link page not found.")
    return HTMLResponse(content=link_html.read_text(encoding="utf-8"))


# ── Link Session Endpoints ────────────────────────────────────────────────────


@router.post("/link/sessions")
async def create_link_session(
    request: Request,
    site: Optional[str] = None,
    user: User = Depends(get_current_user_or_api_key),
    db: Session = Depends(get_db),
):
    """Create a new link session for the hosted Link page.

    Returns a link_token that can be used with /link?token=xxx.
    Also generates an ephemeral encryption keypair for the session.
    """
    if site:
        ensure_site_allowed_for_request(request, site)

    effective_scopes = constrain_requested_scopes(request, None)
    payload = _create_ephemeral_link_session(
        site=site,
        user_id=user.id,
        db=db,
        scopes=effective_scopes,
    )

    logger.info(
        "Link session created",
        extra={"extra_data": {"link_token": payload["link_token"]}},
    )
    return payload


@router.post("/link/sessions/public")
async def create_public_link_session(request: Request):
    """Create a temporary anonymous link session for hosted modal discovery flows."""
    _enforce_public_link_session_policy(request)
    payload = _create_ephemeral_link_session(site=None, user_id=None, db=None, scopes=None)

    logger.info(
        "Public link session created",
        extra={"extra_data": {"link_token": payload["link_token"]}},
    )
    return payload


@router.post("/link/bootstrap", response_model=HostedLinkBootstrapResponse)
async def create_link_bootstrap(
    body: HostedLinkBootstrapRequest,
    request: Request,
    user: User = Depends(get_current_user_or_api_key),
):
    """Create a signed one-time hosted-link bootstrap token for production clients."""
    if body.site:
        ensure_site_allowed_for_request(request, body.site)

    effective_scopes = constrain_requested_scopes(request, body.scopes)
    launch_id = str(uuid.uuid4())
    expires_in = settings.link_launch_token_expire_seconds

    launch_token = create_link_launch_token(
        launch_id=launch_id,
        user_id=user.id,
        site=body.site,
        allowed_origin=body.allowed_origin,
        scopes=effective_scopes,
        expires_seconds=expires_in,
    )
    session_store.store_link_launch_bootstrap(launch_id, expires_in)

    auth_context = get_auth_context(request)
    logger.info(
        "Hosted link bootstrap created",
        extra={
            "extra_data": {
                "launch_id": launch_id,
                "user_id": user.id,
                "auth_method": auth_context.auth_method if auth_context else "unknown",
                "site": body.site,
            }
        },
    )

    return HostedLinkBootstrapResponse(
        launch_token=launch_token,
        expires_in=expires_in,
        site=body.site,
        allowed_origin=body.allowed_origin,
        scopes=effective_scopes,
    )


@router.post("/link/sessions/bootstrap")
async def exchange_link_bootstrap(
    body: HostedLinkBootstrapExchangeRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """Redeem a signed one-time hosted-link bootstrap token into a live link session."""
    try:
        payload = decode_link_launch_token(body.launch_token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=410, detail="Link bootstrap token has expired.")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=400, detail="Invalid link bootstrap token.")

    allowed_origin = payload.get("allowed_origin")
    request_origin = _extract_request_origin(request)
    if allowed_origin and request_origin != allowed_origin:
        raise HTTPException(
            status_code=403,
            detail="This origin is not allowed to redeem the hosted-link bootstrap token.",
        )

    launch_id = payload.get("jti")
    if not launch_id or not session_store.consume_link_launch_bootstrap(launch_id):
        raise HTTPException(
            status_code=410,
            detail="Link bootstrap token has expired or has already been used.",
        )

    user_id_raw = payload.get("sub")
    try:
        user_id = int(user_id_raw) if user_id_raw is not None else None
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid link bootstrap token subject.")

    site = payload.get("site")
    scopes = payload.get("scopes")
    session_payload = _create_ephemeral_link_session(site=site, user_id=user_id, db=db, scopes=scopes)

    logger.info(
        "Hosted link bootstrap redeemed",
        extra={"extra_data": {"launch_id": launch_id, "site": site, "user_id": user_id}},
    )
    return session_payload


@router.get("/link/sessions/{link_token}/status")
async def get_link_session_status(link_token: str):
    """Get the current status of a link session."""
    session = _get_link_session(link_token)
    if not session:
        raise HTTPException(status_code=404, detail="Link session not found.")
    result = {
        "link_token": link_token,
        "status": session["status"],
        "site": session.get("site"),
        "events": [e["event"] for e in session["events"]],
    }
    if session["status"] == "completed" and session.get("public_token"):
        result["public_token"] = session["public_token"]
    return result


@router.post("/link/sessions/{link_token}/event")
async def post_link_session_event(
    link_token: str,
    request: Request,
    user: User = Depends(get_current_user_or_api_key),
):
    """Record an event for a link session (called by the link page)."""
    session = _get_link_session(link_token)
    if not session:
        raise HTTPException(status_code=404, detail="Link session not found.")
    if session.get("user_id") != user.id:
        raise HTTPException(status_code=403, detail="Not authorized for this session.")
    if session["status"] == "expired":
        raise HTTPException(status_code=410, detail="Link session has expired.")

    body = await request.json()
    event_name = body.get("event", "UNKNOWN")
    event_data = {
        "event": event_name,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "data": {k: v for k, v in body.items() if k != "event"},
    }

    public_token_value = None
    # Append event to session store
    session_store.append_link_session_event(link_token, event_data)

    # Build updates based on event type
    updates = {}
    if event_name == "INSTITUTION_SELECTED":
        updates["status"] = "awaiting_credentials"
        updates["site"] = body.get("site", session.get("site"))
    elif event_name == "CREDENTIALS_SUBMITTED":
        updates["status"] = "connecting"
    elif event_name == "MFA_REQUIRED":
        updates["status"] = "mfa_required"
    elif event_name == "MFA_SUBMITTED":
        updates["status"] = "verifying_mfa"
    elif event_name == "CONNECTED":
        updates["status"] = "completed"
        updates["access_token"] = body.get("access_token")
        # Generate a one-time public_token for the 3-token exchange flow
        access_token = body.get("access_token")
        user_id = session.get("user_id")
        if access_token and user_id:
            public_token_value = f"public-{uuid.uuid4()}"
            db = next(get_db())
            try:
                pt = PublicToken(
                    token=public_token_value,
                    link_token=link_token,
                    access_token=access_token,
                    user_id=user_id,
                    expires_at=datetime.now(timezone.utc) + timedelta(minutes=_PUBLIC_TOKEN_TTL_MINUTES),
                )
                db.add(pt)
                db.commit()
            finally:
                db.close()
            updates["public_token"] = public_token_value
    elif event_name == "ERROR":
        updates["status"] = "error"

    if updates:
        session_store.update_link_session(link_token, updates)

    if not session_store.publish_link_event(link_token, event_data):
        async with _sse_lock:
            for queue in _sse_subscribers.get(link_token, []):
                await queue.put(event_data)

    # Fire webhooks for terminal events
    webhook_event_map = {
        "CONNECTED": "LINK_COMPLETE",
        "ERROR": "LINK_ERROR",
        "MFA_REQUIRED": "MFA_REQUIRED",
    }
    if event_name in webhook_event_map:
        from src.routers.webhooks import fire_webhooks_for_session

        await fire_webhooks_for_session(link_token, webhook_event_map[event_name], event_data.get("data"))

    response = {"status": "ok"}
    if public_token_value:
        response["public_token"] = public_token_value
    return response


# ── SSE Event Stream ──────────────────────────────────────────────────────────


@router.get("/link/events/{link_token}")
async def link_event_stream(link_token: str):
    """SSE stream for real-time link session events.

    Agents can subscribe to this to get notified of each step in the Link flow.
    Events: INSTITUTION_SELECTED, CREDENTIALS_SUBMITTED, MFA_REQUIRED,
    MFA_SUBMITTED, CONNECTED, ERROR.
    """
    session = _get_link_session(link_token)
    if not session:
        raise HTTPException(status_code=404, detail="Link session not found.")

    redis_queue = await session_store.subscribe_link_events(link_token)
    use_local_queue = redis_queue is None
    queue: asyncio.Queue = redis_queue or asyncio.Queue()

    if use_local_queue:
        async with _sse_lock:
            _sse_subscribers.setdefault(link_token, []).append(queue)

    async def event_generator():
        try:
            # Send any existing events as replay
            for past_event in session["events"]:
                yield {
                    "event": past_event["event"],
                    "data": json.dumps(past_event),
                }

            # Stream new events
            while True:
                try:
                    event_data = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield {
                        "event": event_data["event"],
                        "data": json.dumps(event_data),
                    }
                    # Close stream when session completes
                    if event_data["event"] in ("CONNECTED", "ERROR"):
                        return
                except asyncio.TimeoutError:
                    # Send keep-alive ping
                    yield {"event": "ping", "data": ""}
                    # Check if session expired
                    current = _get_link_session(link_token)
                    if current is None or current.get("status") in ("completed", "error", "expired"):
                        return
        finally:
            if use_local_queue:
                async with _sse_lock:
                    subs = _sse_subscribers.get(link_token, [])
                    if queue in subs:
                        subs.remove(queue)
                    if not subs:
                        _sse_subscribers.pop(link_token, None)

    return EventSourceResponse(event_generator())
