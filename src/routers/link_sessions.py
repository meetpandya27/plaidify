"""
Hosted link page, link session management, and SSE event streaming.
"""

import asyncio
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sse_starlette.sse import EventSourceResponse
from sqlalchemy.orm import Session

from src.config import get_settings
from src.crypto import generate_keypair
from src.database import Link, PublicToken, User, Webhook, get_db
from src.dependencies import get_current_user, get_current_user_or_api_key
from src.logging_config import get_logger
from src import session_store

settings = get_settings()
logger = get_logger("api.link_sessions")

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


def _get_link_session(token: str) -> Optional[Dict[str, Any]]:
    """Return a link session if it exists and hasn't expired."""
    return session_store.get_link_session(token)


# ── Hosted Link Page ──────────────────────────────────────────────────────────


@router.get("/link", response_class=HTMLResponse)
async def hosted_link_page(token: Optional[str] = None):
    """Serve the hosted Link page.

    The page validates the token client-side via the /link/sessions API.
    """
    from pathlib import Path

    link_html = Path("frontend/link.html")
    if not link_html.exists():
        raise HTTPException(status_code=500, detail="Link page not found.")
    return HTMLResponse(content=link_html.read_text(encoding="utf-8"))


# ── Link Session Endpoints ────────────────────────────────────────────────────


@router.post("/link/sessions")
async def create_link_session(
    site: Optional[str] = None,
    user: User = Depends(get_current_user_or_api_key),
    db: Session = Depends(get_db),
):
    """Create a new link session for the hosted Link page.

    Returns a link_token that can be used with /link?token=xxx.
    Also generates an ephemeral encryption keypair for the session.
    """
    link_token = str(uuid.uuid4())

    # Store in DB if site is provided
    if site:
        new_link = Link(link_token=link_token, site=site, user_id=user.id)
        db.add(new_link)
        db.commit()

    # Generate ephemeral keypair
    public_key_pem = generate_keypair(link_token)

    # Create ephemeral session state
    session_store.create_link_session(link_token, {
        "status": "awaiting_institution",
        "site": site,
        "user_id": user.id,
        "events": [],
        "access_token": None,
    })

    logger.info(
        "Link session created",
        extra={"extra_data": {"link_token": link_token}},
    )
    return {
        "link_token": link_token,
        "link_url": f"/link?token={link_token}",
        "public_key": public_key_pem,
        "expires_in": _LINK_SESSION_TTL,
    }


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
async def post_link_session_event(link_token: str, request: Request):
    """Record an event for a link session (called by the link page)."""
    session = _get_link_session(link_token)
    if not session:
        raise HTTPException(status_code=404, detail="Link session not found.")
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
                    expires_at=datetime.now(timezone.utc)
                    + timedelta(minutes=_PUBLIC_TOKEN_TTL_MINUTES),
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

    # Notify local SSE subscribers
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

        await fire_webhooks_for_session(
            link_token, webhook_event_map[event_name], event_data.get("data")
        )

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

    queue: asyncio.Queue = asyncio.Queue()

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
                    event_data = await asyncio.wait_for(
                        queue.get(), timeout=15.0
                    )
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
                    if current is None or current.get("status") in (
                        "completed", "error", "expired"
                    ):
                        return
        finally:
            async with _sse_lock:
                subs = _sse_subscribers.get(link_token, [])
                if queue in subs:
                    subs.remove(queue)
                if not subs:
                    _sse_subscribers.pop(link_token, None)

    return EventSourceResponse(event_generator())
