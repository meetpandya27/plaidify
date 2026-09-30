"""
Hosted link page, link session management, and SSE event streaming.
"""

import asyncio
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional
from urllib.parse import urlsplit

import jwt
from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session
from sse_starlette.sse import EventSourceResponse

from src import session_store
from src.access_jobs import cancel_access_job, serialize_access_job_runtime
from src.auth_utils import create_link_launch_token, decode_link_launch_token
from src.config import get_settings
from src.crypto import generate_keypair, token_fingerprint
from src.database import AccessJob, Link, PublicToken, SessionLocal, User, get_db
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
FRONTEND_NEXT_DIST = Path(__file__).resolve().parents[2] / "frontend-next" / "dist"

router = APIRouter(tags=["link_sessions"])

# TTL for link sessions (10 minutes)
_LINK_SESSION_TTL = session_store.LINK_SESSION_TTL

# Duration for which a public_token is valid (10 minutes).
_PUBLIC_TOKEN_TTL_MINUTES = 10

# Events after which a session's event stream ends.
_STREAM_CLOSING_EVENTS = frozenset({"CONNECTED", "ERROR", "EXIT"})
# How long one event-stream read waits before checking the client and keep-alive.
_STREAM_POLL_SECONDS = 1.0

_TERMINAL = session_store.TERMINAL_STATUSES


def _extract_request_origin(request: Request) -> Optional[str]:
    """Return the caller origin from Origin or Referer headers."""
    origin = request.headers.get("origin")
    if origin:
        return origin.rstrip("/")

    referer = request.headers.get("referer")
    if not referer:
        return None

    try:
        parsed = urlsplit(referer)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
    except Exception:
        return None

    return None


def _configured_public_link_allowed_origins() -> set[str]:
    return {origin.strip().rstrip("/") for origin in settings.public_link_allowed_origins.split(",") if origin.strip()}


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


def _is_loopback_host(host: str) -> bool:
    return host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".localhost")


def validate_embedding_origin(value: str) -> str:
    """An origin allowed to embed a hosted-link session: ``https://host[:port]``.

    ``http://`` is accepted only for localhost, and only outside production.
    """
    raw = (value or "").strip().rstrip("/")
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        raise ValueError("allowed_origins entries must be origins like https://app.example.com.") from None
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    if (
        not host
        or scheme not in {"http", "https"}
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
        or parts.username is not None
        or parts.password is not None
    ):
        raise ValueError("allowed_origins entries must be origins like https://app.example.com.")
    if scheme == "http" and (settings.env == "production" or not _is_loopback_host(host)):
        raise ValueError("allowed_origins must use https (http://localhost is allowed outside production).")
    netloc = f"[{host}]" if ":" in host else host
    return f"{scheme}://{netloc}{f':{port}' if port else ''}"


class LinkSessionCreateRequest(BaseModel):
    """Optional body for POST /link/sessions."""

    site: Optional[str] = Field(default=None, min_length=1, max_length=64)
    allowed_origins: Optional[list[str]] = Field(
        default=None,
        max_length=20,
        description="Origins that may embed the hosted page (frame-ancestors and postMessage targets).",
    )

    @field_validator("allowed_origins")
    @classmethod
    def _validate_origins(cls, value: Optional[list[str]]) -> Optional[list[str]]:
        if value is None:
            return None
        origins: list[str] = []
        for entry in value:
            origin = validate_embedding_origin(entry)
            if origin not in origins:
                origins.append(origin)
        return origins


def _allowed_origins_of(session: Dict[str, Any]) -> list[str]:
    """Every origin that may embed a session (its primary origin first), deduplicated."""
    origins: list[str] = []
    for candidate in [session.get("allowed_origin"), *(session.get("allowed_origins") or [])]:
        normalized = _normalize_origin(candidate) if isinstance(candidate, str) else None
        if normalized and normalized not in origins:
            origins.append(normalized)
    return origins


def _allowed_sites_of(request: Request) -> Optional[list[str]]:
    """The site restriction of the calling API key or agent (None: unrestricted)."""
    auth_context = get_auth_context(request)
    if auth_context is None or auth_context.allowed_sites is None:
        return None
    return sorted(auth_context.allowed_sites)


async def _create_ephemeral_link_session(
    *,
    site: Optional[str],
    user_id: Optional[int],
    db: Optional[Session],
    scopes: Optional[list[str]],
    allowed_origin: Optional[str],
    allowed_origins: Optional[list[str]] = None,
    allowed_sites: Optional[list[str]] = None,
) -> Dict[str, Any]:
    """Create a hosted link session and its ephemeral encryption material.

    ``allowed_sites`` is the creating agent's site restriction; it stays on the
    session (key ``allowed_sites``, lower-case site ids) for the hosted page's
    /connect and institution choice to honour.
    """
    link_token = str(uuid.uuid4())

    if site and user_id is not None and db is not None:
        new_link = Link(link_token=link_token, site=site, user_id=user_id)
        db.add(new_link)
        db.commit()

    # RSA-2048 generation takes ~0.1 s of CPU: keep it off the event loop.
    public_key_pem = await asyncio.to_thread(generate_keypair, link_token)

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
            "error_code": None,
            "site": site,
            "user_id": user_id,
            "events": [],
            "access_token": None,
            "message": None,
            "metadata": None,
            "mfa_type": None,
            "public_token": None,
            "session_id": None,
            "allowed_sites": allowed_sites,
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
        "event_id": uuid.uuid4().hex,
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
    await session_store.apublish_link_event(link_token, event_data)


def _not_terminal(session: Dict[str, Any]) -> bool:
    return session.get("status") not in _TERMINAL


def _can_complete(session: Dict[str, Any]) -> bool:
    # A job that completes after a page-reported ERROR still completes the session.
    return session.get("status") not in {"completed", "exited", "expired"}


def _default_once_key(event_name: str, data: Optional[Dict[str, Any]]) -> Optional[str]:
    """Events sent at most once: CONNECTED and EXIT per session, ERROR per job, MFA_REQUIRED per challenge."""
    data = data or {}
    if event_name in ("CONNECTED", "EXIT"):
        return event_name
    if event_name == "ERROR" and data.get("job_id"):
        return f"ERROR:{data['job_id']}"
    if event_name == "MFA_REQUIRED":
        return f"MFA_REQUIRED:{data.get('session_id') or ''}:{int(data.get('challenge') or 0)}"
    return None


def _default_guard(event_name: str) -> Optional[Callable[[Dict[str, Any]], bool]]:
    """A finished session takes no further ERROR or MFA prompt, and a completed one no second CONNECTED."""
    if event_name == "CONNECTED":
        return _can_complete
    if event_name in ("ERROR", "MFA_REQUIRED"):
        return _not_terminal
    return None


_WEBHOOK_EVENT_MAP = {
    "OPEN": "LINK_OPEN",
    "CONNECTED": "LINK_COMPLETE",
    "ERROR": "LINK_ERROR",
    "EXIT": "LINK_EXIT",
    "MFA_REQUIRED": "MFA_REQUIRED",
}


async def _push_link_session_event(
    link_token: str,
    event_name: str,
    *,
    data: Optional[Dict[str, Any]] = None,
    updates: Optional[Dict[str, Any]] = None,
    once_key: Optional[str] = None,
    guard: Optional[Callable[[Dict[str, Any]], bool]] = None,
) -> Optional[Dict[str, Any]]:
    """Record an event with its state change, atomically, then publish it and fire webhooks.

    CONNECTED and EXIT go out at most once per session, a server ERROR once per
    job, MFA_REQUIRED once per challenge; a finished session takes no further
    ERROR or MFA prompt, so a page-reported ERROR never overrides a completed
    session. Returns the event, or None when it was refused (duplicate,
    finished session, or the session is gone).
    """
    public_data = dict(data or {})
    challenge = public_data.pop("challenge", None)
    event_data = _build_link_session_event(event_name, data=public_data)
    key = once_key if once_key is not None else _default_once_key(event_name, {**public_data, "challenge": challenge})
    applied, _session = await session_store.atransition_link_session(
        link_token,
        event=event_data,
        updates=updates,
        once_key=key,
        guard=guard if guard is not None else _default_guard(event_name),
    )
    if not applied:
        return None

    await _publish_link_session_event(link_token, event_data)

    if event_name in _WEBHOOK_EVENT_MAP:
        from src.routers.webhooks import fire_webhooks_for_session

        try:
            await fire_webhooks_for_session(link_token, _WEBHOOK_EVENT_MAP[event_name], event_data["data"])
        except Exception as exc:
            logger.error(
                "Could not queue link session webhooks",
                extra={"extra_data": {"event": event_name, "error": type(exc).__name__}},
            )

    return event_data


async def reconcile_link_session(
    db: Session,
    *,
    link_token: str,
    job_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Bring a hosted session up to date with its access job, emitting the events that implies."""
    session = await session_store.aget_link_session(link_token)
    if not session or session.get("status") == "expired":
        return session

    current_job_id = job_id or session.get("current_job_id")
    if not current_job_id:
        return session

    job = db.query(AccessJob).filter(AccessJob.id == current_job_id).first()
    if not job:
        return session

    payload = await serialize_access_job_runtime(job, include_result=False)
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
        updates["status"] = "verifying_mfa" if payload.get("mfa_state") == "verifying" else "connecting"
        await session_store.amutate_link_session(
            link_token, session_store.transition_mutator(updates=updates, guard=_not_terminal)
        )
        return await session_store.aget_link_session(link_token)

    if runtime_status == "mfa_required":
        message = metadata.get("message") if isinstance(metadata, dict) else None
        updates.update({"message": message, "status": "mfa_required"})
        event = {
            "mfa_type": payload.get("mfa_type"),
            "session_id": payload.get("session_id"),
            "site": updates.get("site"),
            "challenge": payload.get("mfa_attempts") or 0,
        }
        if isinstance(metadata, dict) and metadata.get("mfa_error"):
            # The site rejected the last code: ask again, saying why.
            event["mfa_error"] = metadata.get("mfa_error")
            event["attempts_remaining"] = metadata.get("attempts_remaining")
        pushed = await _push_link_session_event(link_token, "MFA_REQUIRED", data=event, updates=updates)
        if pushed is None:
            await session_store.amutate_link_session(
                link_token, session_store.transition_mutator(updates=updates, guard=_not_terminal)
            )
        return await session_store.aget_link_session(link_token)

    if runtime_status == "completed":
        updates.update(
            {
                "error_message": None,
                "error_code": None,
                "message": None,
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
        return await session_store.aget_link_session(link_token)

    # Every other status is a finished, unsuccessful job (failed, blocked,
    # cancelled, mfa_timeout).
    error_message = payload.get("error_message") or "The connection could not be completed."
    error_code = payload.get("error_code") or ("mfa_timeout" if runtime_status == "mfa_timeout" else None)
    updates.update({"error_message": error_message, "error_code": error_code, "status": "error"})
    await _push_link_session_event(
        link_token,
        "ERROR",
        data={
            "error": error_message,
            "error_code": error_code,
            "job_id": current_job_id,
            "site": updates.get("site"),
        },
        updates=updates,
    )
    return await session_store.aget_link_session(link_token)


async def end_link_sessions_for_jobs(jobs: Iterable[AccessJob]) -> None:
    """Report jobs that ended from outside (reaped) on their hosted sessions: ERROR + LINK_ERROR, once."""
    for job in jobs:
        link_token = await session_store.alink_token_for_job(job.id)
        if not link_token:
            continue
        session = await session_store.aget_link_session(link_token)
        if not session or session.get("current_job_id") != job.id:
            continue  # the session moved on to another attempt
        with SessionLocal() as db:
            await reconcile_link_session(db, link_token=link_token, job_id=job.id)


# ── Hosted Link Page ──────────────────────────────────────────────────────────


@router.get("/link", response_class=HTMLResponse)
async def hosted_link_page(token: Optional[str] = None):
    """Serve the hosted Link page.

    The page validates the token client-side via the /link/sessions API.
    The React bundle under frontend-next/dist/ is the only supported
    frontend; the legacy static page has been retired (#65).
    """
    react_index = FRONTEND_NEXT_DIST / "index.html"
    if react_index.exists():
        return HTMLResponse(content=react_index.read_text(encoding="utf-8"))
    logger.error(
        "frontend-next/dist/index.html is missing; the hosted Link bundle has "
        "not been built. Run `npm --prefix frontend-next run build`."
    )
    raise HTTPException(status_code=500, detail="Link page not found.")


# ── Link Session Endpoints ────────────────────────────────────────────────────


@router.post("/link/sessions")
async def create_link_session(
    request: Request,
    site: Optional[str] = None,
    body: Optional[LinkSessionCreateRequest] = None,
    user: User = Depends(get_current_user_or_api_key),
    db: Session = Depends(get_db),
):
    """Create a new link session for the hosted Link page.

    Returns a link_token that can be used with /link?token=xxx.
    Also generates an ephemeral encryption keypair for the session.

    A backend creating a session for a page on another origin passes
    ``allowed_origins`` in the JSON body: the origins that may embed the hosted
    page (https, or http://localhost outside production). A browser caller's
    own Origin is allowed as well.
    """
    effective_site = (body.site if body is not None and body.site else None) or site
    if effective_site:
        ensure_site_allowed_for_request(request, effective_site)

    effective_scopes = constrain_requested_scopes(request, None)
    payload = await _create_ephemeral_link_session(
        site=effective_site,
        user_id=user.id,
        db=db,
        scopes=effective_scopes,
        allowed_origin=_extract_request_origin(request),
        allowed_origins=body.allowed_origins if body is not None else None,
        allowed_sites=_allowed_sites_of(request),
    )

    logger.info(
        "Link session created",
        extra={"extra_data": {"link_token": token_fingerprint(payload["link_token"])}},
    )
    return payload


@router.post("/link/sessions/public")
async def create_public_link_session(request: Request):
    """Create a temporary anonymous link session for hosted modal discovery flows."""
    _enforce_public_link_session_policy(request)
    payload = await _create_ephemeral_link_session(
        site=None,
        user_id=None,
        db=None,
        scopes=None,
        allowed_origin=_extract_request_origin(request),
    )

    logger.info(
        "Public link session created",
        extra={"extra_data": {"link_token": token_fingerprint(payload["link_token"])}},
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
    session_store.store_link_launch_bootstrap(launch_id, expires_in, allowed_sites=_allowed_sites_of(request))

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
    session_payload = await _create_ephemeral_link_session(
        site=site,
        user_id=user_id,
        db=db,
        scopes=scopes,
        allowed_origin=allowed_origin,
        allowed_origins=allowed_origins_payload,
        allowed_sites=session_store.pop_link_launch_sites(launch_id),
    )

    logger.info(
        "Hosted link bootstrap redeemed",
        extra={"extra_data": {"launch_id": launch_id, "site": site, "user_id": user_id}},
    )
    return session_payload


@router.get("/link/sessions/{link_token}/status")
async def get_link_session_status(link_token: str, db: Session = Depends(get_db)):
    """Get the current status of a link session."""
    session = await reconcile_link_session(db, link_token=link_token)

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
        "events": [e["event"] for e in session.get("events", [])],
        "allowed_origins": _allowed_origins_of(session),
    }
    if session.get("error_message"):
        result["error_message"] = session["error_message"]
    if session.get("error_code"):
        result["error_code"] = session["error_code"]
    if session.get("message"):
        result["message"] = session["message"]
    if session["status"] == "completed" and session.get("public_token"):
        result["public_token"] = session["public_token"]
    return result


def _select_institution(requested_site: Any) -> Callable[[Dict[str, Any]], bool]:
    """INSTITUTION_SELECTED's transition. A session created for a site keeps it;
    one created without may take the picked site, if the creating agent may use it."""

    def mutate(session: Dict[str, Any]) -> bool:
        if session.get("status") in _TERMINAL:
            return False
        session["status"] = "awaiting_credentials"
        if not session.get("site") and isinstance(requested_site, str) and requested_site.strip():
            allowed = session.get("allowed_sites")
            if allowed is None or requested_site.strip().lower() in allowed:
                session["site"] = requested_site.strip()
        return True

    return mutate


def _mark_exited(session: Dict[str, Any]) -> bool:
    """EXIT's transition: a session still in progress becomes "exited"; a finished one keeps its status."""
    if session.get("status") not in _TERMINAL:
        session["status"] = "exited"
    return True


def _browser_mfa_prompt_is_known(session: Dict[str, Any], data: Dict[str, Any]) -> bool:
    """Whether the server already reported the MFA challenge the page is echoing."""
    return session.get("status") == "mfa_required" and (
        not data.get("session_id") or data.get("session_id") == session.get("session_id")
    )


@router.post("/link/sessions/{link_token}/event")
async def post_link_session_event(
    link_token: str,
    body: Dict[str, Any] = Body(...),
):
    """Record an event for a link session (called by the link page)."""
    session = await session_store.aget_link_session(link_token)
    if not session:
        raise HTTPException(status_code=404, detail="Link session not found.")
    if session["status"] == "expired":
        raise HTTPException(status_code=410, detail="Link session has expired.")

    event_name = body.get("event", "UNKNOWN")
    if not isinstance(event_name, str) or not event_name or len(event_name) > 64:
        raise HTTPException(status_code=422, detail="event must be a short string.")
    data = _sanitize_hosted_event_data({k: v for k, v in body.items() if k != "event"})

    if event_name == "CONNECTED":
        # CONNECTED is authoritative from the server-side job reconciler,
        # not from the browser, to prevent a client from lying about a
        # successful completion. We still 200 so the page's retry queue
        # treats the write as durable.
        return {"status": "ignored"}

    updates: Dict[str, Any] = {}
    guard: Optional[Callable[[Dict[str, Any]], bool]] = None
    once_key: Optional[str] = None
    if event_name == "OPEN":
        # Page loaded; no session-state transition required but event is
        # still recorded and fanned out to SSE/webhooks so developers can
        # observe when the user actually saw the Link modal.
        pass
    elif event_name == "INSTITUTION_SELECTED":
        guard = _select_institution(body.get("site"))
    elif event_name == "CREDENTIALS_SUBMITTED":
        updates["status"] = "connecting"
        guard = _not_terminal
    elif event_name == "MFA_REQUIRED":
        if _browser_mfa_prompt_is_known(session, data):
            return {"status": "ignored"}
        updates["status"] = "mfa_required"
        once_key = f"MFA_REQUIRED:{data.get('session_id') or ''}:browser"
    elif event_name == "MFA_SUBMITTED":
        updates["status"] = "verifying_mfa"
        guard = _not_terminal
    elif event_name == "EXIT":
        # Preserves completed/error states when the page unloads after
        # success/failure; otherwise the user left, so stop their connection.
        guard = _mark_exited
    elif event_name == "ERROR":
        # A page error never overrides a finished session (e.g. completed).
        updates["status"] = "error"
        updates["error_message"] = body.get("error")
        if isinstance(body.get("error_code"), str):
            updates["error_code"] = body["error_code"][:64]

    pushed = await _push_link_session_event(
        link_token,
        event_name,
        data=data,
        updates=updates or None,
        once_key=once_key,
        guard=guard,
    )
    if pushed is None:
        return {"status": "ignored"}

    if event_name == "EXIT":
        await _cancel_session_job(link_token, session)
    return {"status": "ok"}


async def _cancel_session_job(link_token: str, session: Dict[str, Any]) -> None:
    """The user closed Link mid-connection: cancel the job, freeing its lock and browser."""
    job_id = session.get("current_job_id")
    if not job_id or session.get("status") in _TERMINAL:
        return
    try:
        if await cancel_access_job(job_id, reason="The user closed Link before the connection finished."):
            logger.info(
                "Cancelled the access job of a closed link session",
                extra={"extra_data": {"job_id": job_id, "link_token": token_fingerprint(link_token)}},
            )
    except Exception as exc:
        logger.warning(
            "Could not cancel the access job of a closed link session",
            extra={"extra_data": {"job_id": job_id, "error": type(exc).__name__}},
        )


# ── SSE Event Stream ──────────────────────────────────────────────────────────


def _sse_message(event: Dict[str, Any]) -> Dict[str, Any]:
    message = {"event": event.get("event", "message"), "data": json.dumps(event)}
    if event.get("event_id"):
        message["id"] = event["event_id"]
    return message


@router.get("/link/events/{link_token}")
async def link_event_stream(link_token: str, request: Request):
    """SSE stream for real-time link session events.

    Agents can subscribe to this to get notified of each step in the Link flow.
    Events: INSTITUTION_SELECTED, CREDENTIALS_SUBMITTED, MFA_REQUIRED,
    MFA_SUBMITTED, CONNECTED, ERROR, EXIT. Past events are replayed first; the
    stream ends after CONNECTED, ERROR or EXIT, when the session ends or
    expires, or when the client goes away. It never blocks the server: Redis
    is read through asyncio with timeouts.
    """
    session = await session_store.aget_link_session(link_token)
    if not session:
        raise HTTPException(status_code=404, detail="Link session not found.")
    if session.get("status") == "expired":
        raise HTTPException(status_code=410, detail="Link session has expired.")

    keepalive = settings.link_event_keepalive_seconds

    async def event_generator():
        subscription = session_store.LinkEventSubscription(link_token)
        try:
            # Subscribe before reading the replay, so nothing falls in between.
            await subscription.open()
            snapshot = await session_store.aget_link_session(link_token) or session
            seen: set[str] = set()
            for past_event in snapshot.get("events", []):
                if past_event.get("event_id"):
                    seen.add(past_event["event_id"])
                yield _sse_message(past_event)
                if past_event.get("event") in _STREAM_CLOSING_EVENTS:
                    return
            if snapshot.get("status") in _TERMINAL:
                return

            # The session cannot outlive its TTL, and neither can the stream.
            stream_deadline = time.monotonic() + _LINK_SESSION_TTL + keepalive
            next_keepalive = time.monotonic() + keepalive
            while time.monotonic() < stream_deadline:
                if await request.is_disconnected():
                    return
                event_data = await subscription.get(timeout=_STREAM_POLL_SECONDS)
                if event_data is not None:
                    event_id = event_data.get("event_id")
                    if event_id and event_id in seen:
                        continue
                    if event_id:
                        seen.add(event_id)
                    yield _sse_message(event_data)
                    if event_data.get("event") in _STREAM_CLOSING_EVENTS:
                        return
                    continue
                if time.monotonic() >= next_keepalive:
                    next_keepalive = time.monotonic() + keepalive
                    yield {"event": "ping", "data": ""}
                    current = await session_store.aget_link_session(link_token)
                    if current is None or current.get("status") in _TERMINAL:
                        return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Link event stream ended with an error",
                extra={"extra_data": {"link_token": token_fingerprint(link_token), "error": type(exc).__name__}},
            )
        finally:
            await subscription.close()

    return EventSourceResponse(event_generator(), ping=keepalive)
