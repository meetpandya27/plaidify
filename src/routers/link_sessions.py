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
from src.access_jobs import serialize_access_job_runtime
from src.auth_utils import create_link_launch_token, decode_link_launch_token
from src.config import get_settings
from src.crypto import generate_keypair
from src.database import AccessJob, Link, PublicToken, User, get_db
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


def _normalize_origin(origin: Optional[str]) -> Optional[str]:
    if origin is None:
        return None
    normalized = origin.strip().rstrip("/")
    return normalized or None


def _create_ephemeral_link_session(
    *,
    site: Optional[str],
    user_id: Optional[int],
    db: Optional[Session],
    scopes: Optional[list[str]],
    allowed_origin: Optional[str],
    allowed_origins: Optional[list[str]] = None,
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

    normalized_primary = _normalize_origin(allowed_origin)
    normalized_list: list[str] = []
    seen: set[str] = set()
    if normalized_primary:
        normalized_list.append(normalized_primary)
        seen.add(normalized_primary)
    for entry in allowed_origins or []:
        normalized_entry = _normalize_origin(entry)
        if normalized_entry and normalized_entry not in seen:
            seen.add(normalized_entry)
            normalized_list.append(normalized_entry)

    session_store.create_link_session(
        link_token,
        {
            "status": "awaiting_institution",
            "allowed_origin": normalized_primary,
            "allowed_origins": normalized_list,
            "current_job_id": None,
            "error_message": None,
            "site": site,
            "user_id": user_id,
            "events": [],
            "access_token": None,
            "message": None,
            "metadata": None,
            "mfa_type": None,
            "public_token": None,
            "result": None,
            "session_id": None,
        },
    )

    return {
        "link_token": link_token,
        "link_url": f"/link?token={link_token}",
        "public_key": public_key_pem,
        "expires_in": _LINK_SESSION_TTL,
        "scopes": scopes,
    }


# Keys that must never appear in hosted-link event payloads delivered to
# browser or mobile webview clients. Hosted Link's completion contract is
# public_token + metadata only; durable credentials (access_token, raw
# extracted data, passwords) stay server-side and are exchanged by the
# developer's backend via authenticated APIs.
_HOSTED_EVENT_FORBIDDEN_KEYS = frozenset(
    {
        "access_token",
        "accessToken",
        "password",
        "password_encrypted",
        "username_encrypted",
        "private_key",
        "secret",
        "result",
        "data",
    }
)


def _sanitize_hosted_event_data(data: Any) -> Any:
    """Recursively strip forbidden keys from hosted-link event payloads.

    Defense-in-depth so a future caller cannot accidentally leak
    access_token or extracted result data to browser/webview clients.
    """
    if isinstance(data, dict):
        return {
            key: _sanitize_hosted_event_data(value)
            for key, value in data.items()
            if key not in _HOSTED_EVENT_FORBIDDEN_KEYS
        }
    if isinstance(data, list):
        return [_sanitize_hosted_event_data(item) for item in data]
    return data


def _build_link_session_event(event_name: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "event": event_name,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "data": _sanitize_hosted_event_data(data or {}),
    }


def _ensure_public_token(db: Session, *, link_token: str, access_token: str, user_id: int) -> str:
    existing = (
        db.query(PublicToken)
        .filter_by(link_token=link_token, access_token=access_token, user_id=user_id)
        .order_by(PublicToken.created_at.desc())
        .first()
    )
    if existing:
        return existing.token

    public_token_value = f"public-{uuid.uuid4()}"
    db.add(
        PublicToken(
            token=public_token_value,
            link_token=link_token,
            access_token=access_token,
            user_id=user_id,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=_PUBLIC_TOKEN_TTL_MINUTES),
        )
    )
    db.commit()
    return public_token_value


async def _publish_link_session_event(link_token: str, event_data: Dict[str, Any]) -> None:
    if not session_store.publish_link_event(link_token, event_data):
        async with _sse_lock:
            for queue in _sse_subscribers.get(link_token, []):
                await queue.put(event_data)


async def _push_link_session_event(
    link_token: str,
    event_name: str,
    *,
    data: Optional[Dict[str, Any]] = None,
    updates: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    event_data = _build_link_session_event(event_name, data=data)
    session_store.append_link_session_event(link_token, event_data)
    if updates:
        session_store.update_link_session(link_token, updates)

    await _publish_link_session_event(link_token, event_data)

    webhook_event_map = {
        "CONNECTED": "LINK_COMPLETE",
        "ERROR": "LINK_ERROR",
        "MFA_REQUIRED": "MFA_REQUIRED",
    }
    if event_name in webhook_event_map:
        from src.routers.webhooks import fire_webhooks_for_session

        await fire_webhooks_for_session(link_token, webhook_event_map[event_name], data)

    return event_data


async def reconcile_link_session(
    db: Session,
    *,
    link_token: str,
    job_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    session = _get_link_session(link_token)
    if not session or session.get("status") == "expired":
        return session

    current_job_id = job_id or session.get("current_job_id")
    if not current_job_id:
        return session

    job = db.query(AccessJob).filter(AccessJob.id == current_job_id).first()
    if not job:
        return session

    payload = await serialize_access_job_runtime(job)
    previous_status = session.get("status")
    updates: Dict[str, Any] = {
        "current_job_id": current_job_id,
        "session_id": payload.get("session_id"),
        "site": payload.get("site") or session.get("site"),
    }

    metadata = payload.get("metadata")
    if metadata is not None:
        updates["metadata"] = metadata

    if payload.get("mfa_type"):
        updates["mfa_type"] = payload["mfa_type"]

    runtime_status = payload.get("status")
    if runtime_status in {"pending", "running"}:
        updates["status"] = "connecting"
        session_store.update_link_session(link_token, updates)
        return _get_link_session(link_token)

    if runtime_status == "mfa_required":
        message = metadata.get("message") if isinstance(metadata, dict) else None
        updates.update(
            {
                "message": message,
                "status": "mfa_required",
            }
        )
        if previous_status != "mfa_required":
            await _push_link_session_event(
                link_token,
                "MFA_REQUIRED",
                data={
                    "mfa_type": payload.get("mfa_type"),
                    "session_id": payload.get("session_id"),
                    "site": updates.get("site"),
                },
                updates=updates,
            )
        else:
            session_store.update_link_session(link_token, updates)
        return _get_link_session(link_token)

    if runtime_status == "completed":
        updates.update(
            {
                "error_message": None,
                "message": None,
                "result": payload.get("result"),
                "status": "completed",
            }
        )

        access_token = session.get("access_token")
        user_id = session.get("user_id")
        public_token = session.get("public_token")
        if not public_token and access_token and user_id is not None:
            public_token = _ensure_public_token(
                db,
                link_token=link_token,
                access_token=access_token,
                user_id=user_id,
            )
        if public_token:
            updates["public_token"] = public_token

        if previous_status != "completed":
            await _push_link_session_event(
                link_token,
                "CONNECTED",
                data={
                    "job_id": current_job_id,
                    "public_token": public_token,
                    "site": updates.get("site"),
                },
                updates=updates,
            )
        else:
            session_store.update_link_session(link_token, updates)
        return _get_link_session(link_token)

    if runtime_status in {"blocked", "cancelled", "failed"}:
        updates.update(
            {
                "error_message": payload.get("error_message") or "The connection could not be completed.",
                "status": "error",
            }
        )
        if previous_status != "error":
            await _push_link_session_event(
                link_token,
                "ERROR",
                data={
                    "error": updates["error_message"],
                    "job_id": current_job_id,
                    "site": updates.get("site"),
                },
                updates=updates,
            )
        else:
            session_store.update_link_session(link_token, updates)
        return _get_link_session(link_token)

    session_store.update_link_session(link_token, updates)
    return _get_link_session(link_token)


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
        allowed_origin=_extract_request_origin(request),
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
    payload = _create_ephemeral_link_session(
        site=None,
        user_id=None,
        db=None,
        scopes=None,
        allowed_origin=_extract_request_origin(request),
    )

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
        allowed_origins=body.allowed_origins,
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
        allowed_origins=body.allowed_origins,
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
    allowed_origins_payload = payload.get("allowed_origins") or []
    request_origin = _extract_request_origin(request)
    allowed_set = {entry.rstrip("/") for entry in allowed_origins_payload if entry}
    if allowed_origin:
        allowed_set.add(allowed_origin.rstrip("/"))
    if allowed_set and (request_origin is None or request_origin.rstrip("/") not in allowed_set):
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
    session_payload = _create_ephemeral_link_session(
        site=site,
        user_id=user_id,
        db=db,
        scopes=scopes,
        allowed_origin=allowed_origin,
        allowed_origins=allowed_origins_payload,
    )

    logger.info(
        "Hosted link bootstrap redeemed",
        extra={"extra_data": {"launch_id": launch_id, "site": site, "user_id": user_id}},
    )
    return session_payload


@router.get("/link/sessions/{link_token}/status")
async def get_link_session_status(link_token: str):
    """Get the current status of a link session."""
    db = next(get_db())
    try:
        session = await reconcile_link_session(db, link_token=link_token)
    finally:
        db.close()

    if not session:
        raise HTTPException(status_code=404, detail="Link session not found.")

    result = {
        "link_token": link_token,
        "job_id": session.get("current_job_id"),
        "metadata": session.get("metadata"),
        "mfa_type": session.get("mfa_type"),
        "session_id": session.get("session_id"),
        "status": session["status"],
        "site": session.get("site"),
        "events": [e["event"] for e in session["events"]],
    }
    if session.get("error_message"):
        result["error_message"] = session["error_message"]
    if session.get("message"):
        result["message"] = session["message"]
    if session["status"] == "completed" and session.get("public_token"):
        result["public_token"] = session["public_token"]
    return result


@router.post("/link/sessions/{link_token}/event")
async def post_link_session_event(
    link_token: str,
    request: Request,
):
    """Record an event for a link session (called by the link page)."""
    session = _get_link_session(link_token)
    if not session:
        raise HTTPException(status_code=404, detail="Link session not found.")
    if session["status"] == "expired":
        raise HTTPException(status_code=410, detail="Link session has expired.")

    body = await request.json()
    event_name = body.get("event", "UNKNOWN")
    data = _sanitize_hosted_event_data(
        {k: v for k, v in body.items() if k != "event"}
    )
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
    elif event_name == "ERROR":
        updates["status"] = "error"
        updates["error_message"] = body.get("error")

    if event_name == "CONNECTED":
        return {"status": "ignored"}

    await _push_link_session_event(
        link_token,
        event_name,
        data=data,
        updates=updates or None,
    )
    return {"status": "ok"}


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
