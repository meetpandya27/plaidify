"""
Webhook system endpoints: register, list, delete, test, deliveries.

Delivery is durable: every event is written to the ``webhook_deliveries``
outbox first, then sent — at once, and again with exponential backoff until
the receiver answers 2xx or ``WEBHOOK_MAX_ATTEMPTS`` is reached. The outbox
service (one process at a time, see ``src.background_services``) sends what
is due, so a receiver that is down for a while, or a redeploy, loses nothing.

Every request is signed. Headers:

* ``X-Plaidify-Delivery`` — the delivery id, the same on every retry of one
  event, so receivers can drop duplicates (it is also ``delivery_id`` in the body);
* ``X-Plaidify-Timestamp`` — Unix seconds when this attempt was signed;
* ``X-Plaidify-Signature`` — ``sha256=`` + hex HMAC-SHA256, keyed with the
  webhook's secret, of ``"{timestamp}." + raw body``. Receivers recompute it
  and reject timestamps older than a few minutes (see
  :func:`verify_webhook_signature`).

Destinations: https only in production (``http://localhost`` is allowed
elsewhere), no credentials in the URL, and every attempt resolves the host
again and connects only to a public address it checked, never following
redirects — so a webhook cannot be pointed at internal services or cloud
metadata, even through DNS rebinding.
"""

import asyncio
import hashlib
import hmac
import ipaddress
import json as json_mod
import random
import socket
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

import httpcore
import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from src import session_store
from src.config import get_settings
from src.core.network_policy import address_block_reason, normalize_host
from src.crypto import token_fingerprint
from src.database import (
    CredentialDecryptionError,
    Link,
    SessionLocal,
    User,
    Webhook,
    WebhookDelivery,
    decrypt_json_for_user,
    decrypt_webhook_secret,
    encrypt_json_for_user,
    encrypt_webhook_secret,
    get_db,
    utcnow,
)
from src.dependencies import get_current_user
from src.logging_config import get_logger

logger = get_logger("api.webhooks")
settings = get_settings()

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

SIGNATURE_TOLERANCE_SECONDS = 300
_DELIVERY_BATCH = 50
_DELIVERY_CONCURRENCY = 10
# Deliveries sent right after their event was stored, tracked so shutdown can wait for them.
_IMMEDIATE_DELIVERIES: set = set()


# ── Request models ────────────────────────────────────────────────────────────


class WebhookRegisterRequest(BaseModel):
    link_token: str = Field(..., min_length=1, max_length=256)
    url: str = Field(..., min_length=1, max_length=2048)
    secret: str = Field(..., min_length=1, max_length=512)


class WebhookTestRequest(BaseModel):
    webhook_id: str = Field(..., min_length=1, max_length=64)


# ── Destination checks ────────────────────────────────────────────────────────


class WebhookURLError(ValueError):
    """A webhook URL that may not be registered or called. The message is safe to show."""


class WebhookDestinationBlocked(Exception):
    """The webhook host resolved to an address webhooks may not reach."""


def _is_loopback_host(host: str) -> bool:
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _blocked_reason(ip: Any) -> Optional[str]:
    """Why a webhook may not connect to ``ip`` (None for a public address)."""
    production = settings.env == "production"
    reason = address_block_reason(ip, allow_loopback=not production)
    if (
        reason in ("private address", "shared (CGNAT) address")
        and settings.webhook_allow_private_targets
        and not production
    ):
        return None
    return reason


def validate_webhook_url(url: str) -> str:
    """Check a webhook URL and return it normalized (no fragment).

    Raises:
        WebhookURLError: not https (http only for localhost outside production),
            credentials in it, no host, or a literal non-public address.
    """
    try:
        parts = urlsplit((url or "").strip())
        port = parts.port
    except ValueError:
        raise WebhookURLError("Webhook URL is not a valid URL.") from None
    scheme = (parts.scheme or "").lower()
    host = normalize_host(parts.hostname or "")
    if scheme not in ("http", "https") or not host:
        raise WebhookURLError("Webhook URL must be an absolute https:// URL.")
    if parts.username is not None or parts.password is not None:
        raise WebhookURLError("Webhook URL must not contain credentials.")
    production = settings.env == "production"
    loopback = _is_loopback_host(host)
    if scheme == "http" and (production or not loopback):
        raise WebhookURLError("Webhook URL must use HTTPS (http://localhost is allowed outside production).")
    if loopback and production:
        raise WebhookURLError("Webhook URL must point to a public host.")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and _blocked_reason(literal):
        raise WebhookURLError("Webhook URL must point to a public host.")
    if port == 0:
        raise WebhookURLError("Webhook URL has an invalid port.")
    return urlunsplit((scheme, parts.netloc, parts.path or "/", parts.query, ""))


class _PublicAddressBackend(httpcore.AsyncNetworkBackend):
    """Resolves the host itself and connects only to an address it checked (no DNS rebinding)."""

    def __init__(self) -> None:
        self._inner = httpcore.AnyIOBackend()

    async def _resolve(self, host: str, port: int) -> List[Any]:
        try:
            return [ipaddress.ip_address(host.strip("[]"))]
        except ValueError:
            pass
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        addresses: List[Any] = []
        for _family, _type, _proto, _canon, sockaddr in infos:
            try:
                address = ipaddress.ip_address(str(sockaddr[0]).split("%", 1)[0])
            except ValueError:
                continue
            if address not in addresses:
                addresses.append(address)
        return addresses

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        try:
            addresses = await self._resolve(host, port)
        except OSError as exc:
            raise httpcore.ConnectError(f"could not resolve {host}") from exc
        if not addresses:
            raise httpcore.ConnectError(f"could not resolve {host}")
        for address in addresses:
            reason = _blocked_reason(address)
            if reason:
                raise WebhookDestinationBlocked(reason)
        last_error: Optional[Exception] = None
        for address in addresses:
            try:
                return await self._inner.connect_tcp(
                    str(address),
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
        raise last_error or httpcore.ConnectError(f"could not connect to {host}")

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise WebhookDestinationBlocked("unix sockets are not webhook destinations")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def _webhook_transport() -> httpx.AsyncHTTPTransport:
    transport = httpx.AsyncHTTPTransport(trust_env=False, retries=0)
    # Same pool httpx builds, with the address-checking network backend.
    transport._pool = httpcore.AsyncConnectionPool(
        ssl_context=httpx.create_ssl_context(trust_env=False),
        max_connections=10,
        max_keepalive_connections=0,
        http1=True,
        http2=False,
        retries=0,
        network_backend=_PublicAddressBackend(),
    )
    return transport


# ── Signing ───────────────────────────────────────────────────────────────────


def sign_webhook_payload(secret: str, timestamp: str, body: bytes) -> str:
    """Hex HMAC-SHA256 of ``"{timestamp}." + body`` keyed with the webhook secret."""
    return hmac.new(secret.encode("utf-8"), timestamp.encode("ascii") + b"." + body, hashlib.sha256).hexdigest()


def verify_webhook_signature(
    secret: str,
    *,
    body: bytes,
    timestamp: str,
    signature: str,
    tolerance_seconds: int = SIGNATURE_TOLERANCE_SECONDS,
    now: Optional[float] = None,
) -> bool:
    """Receiver side: check ``X-Plaidify-Signature`` and that ``X-Plaidify-Timestamp`` is recent."""
    try:
        signed_at = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs((now if now is not None else time.time()) - signed_at) > tolerance_seconds:
        return False
    expected = "sha256=" + sign_webhook_payload(secret, timestamp, body)
    return hmac.compare_digest(expected, signature or "")


# ── Outbox ────────────────────────────────────────────────────────────────────


def _payload_context(delivery_id: str) -> str:
    return f"webhook_delivery:{delivery_id}:payload"


def _new_delivery_id() -> str:
    return f"whd_{uuid.uuid4().hex}"


def _backoff_seconds(attempts: int) -> float:
    delay = settings.webhook_retry_base_seconds * (2 ** max(attempts - 1, 0))
    return min(delay, settings.webhook_retry_max_seconds) * random.uniform(0.85, 1.15)


def _insert_deliveries(link_token: str, payload: Dict[str, Any], owner_id: Optional[int]) -> List[str]:
    """Store one delivery per webhook registered for ``link_token`` (by its owner)."""
    now = utcnow()
    created: List[str] = []
    with SessionLocal() as db:
        query = db.query(Webhook).filter(Webhook.link_token == link_token)
        if owner_id is not None:
            query = query.filter(Webhook.user_id == owner_id)
        owners: Dict[int, Optional[User]] = {}
        for webhook in query.all():
            owner = owners.get(webhook.user_id)
            if webhook.user_id not in owners:
                owner = owners[webhook.user_id] = db.get(User, webhook.user_id)
            if owner is None:
                continue
            delivery_id = _new_delivery_id()
            body = {**payload, "delivery_id": delivery_id, "webhook_id": webhook.id}
            db.add(
                WebhookDelivery(
                    id=delivery_id,
                    webhook_id=webhook.id,
                    user_id=webhook.user_id,
                    event=str(payload.get("event", "UNKNOWN"))[:64],
                    payload_json=encrypt_json_for_user(db, owner, body, context=_payload_context(delivery_id)),
                    status="pending",
                    attempts=0,
                    next_attempt_at=now,
                    created_at=now,
                )
            )
            created.append(delivery_id)
        if created:
            db.commit()
    return created


@dataclass
class _Attempt:
    delivery_id: str
    webhook_id: str
    url: str
    secret: str
    event: str
    payload: Dict[str, Any]
    attempts: int


def _finish_delivery(db: Session, delivery_id: str, *, status: str, error: Optional[str]) -> None:
    db.execute(
        update(WebhookDelivery)
        .where(WebhookDelivery.id == delivery_id)
        .values(status=status, last_error=error, locked_until=None, next_attempt_at=None)
        .execution_options(synchronize_session=False)
    )
    db.commit()


def _claim_delivery(delivery_id: str) -> Optional[_Attempt]:
    """Claim one due delivery for an attempt (so no other process sends it too) and load what it needs."""
    now = utcnow()
    lock_until = now + timedelta(seconds=settings.webhook_timeout_seconds * 3 + 30)
    with SessionLocal() as db:
        claimed = db.execute(
            update(WebhookDelivery)
            .where(
                WebhookDelivery.id == delivery_id,
                WebhookDelivery.status == "pending",
                WebhookDelivery.next_attempt_at <= now,
                or_(WebhookDelivery.locked_until.is_(None), WebhookDelivery.locked_until < now),
            )
            .values(locked_until=lock_until, attempts=WebhookDelivery.attempts + 1)
            .execution_options(synchronize_session=False)
        )
        db.commit()
        if claimed.rowcount != 1:
            return None
        delivery = db.get(WebhookDelivery, delivery_id)
        webhook = db.get(Webhook, delivery.webhook_id) if delivery is not None else None
        owner = db.get(User, delivery.user_id) if delivery is not None else None
        if delivery is None or webhook is None or owner is None:
            if delivery is not None:
                _finish_delivery(db, delivery_id, status="failed", error="webhook_deleted")
            return None
        try:
            secret = decrypt_webhook_secret(db, webhook)
        except CredentialDecryptionError:
            # Never sign with anything else: the receiver could not verify it.
            logger.error(
                "Webhook secret cannot be decrypted; delivery skipped",
                extra={"extra_data": {"webhook_id": webhook.id, "delivery_id": delivery_id}},
            )
            _finish_delivery(db, delivery_id, status="failed", error="secret_unavailable")
            return None
        try:
            payload = decrypt_json_for_user(owner, delivery.payload_json, context=_payload_context(delivery_id))
        except CredentialDecryptionError:
            logger.error(
                "Webhook payload cannot be decrypted; delivery skipped",
                extra={"extra_data": {"webhook_id": webhook.id, "delivery_id": delivery_id}},
            )
            _finish_delivery(db, delivery_id, status="failed", error="payload_unavailable")
            return None
        return _Attempt(
            delivery_id=delivery_id,
            webhook_id=webhook.id,
            url=webhook.url,
            secret=secret,
            event=delivery.event,
            payload=payload,
            attempts=delivery.attempts,
        )


def _record_attempt(
    delivery_id: str,
    *,
    attempts: int,
    status_code: Optional[int],
    error: Optional[str],
    retry: bool = True,
) -> str:
    """Store an attempt's outcome and schedule the next one. Returns the delivery's status."""
    now = utcnow()
    delivered = error is None and status_code is not None and 200 <= status_code < 300
    values: Dict[str, Any] = {"last_status_code": status_code, "last_error": error, "locked_until": None}
    if delivered:
        values.update(status="delivered", delivered_at=now, next_attempt_at=None)
    elif not retry or attempts >= settings.webhook_max_attempts:
        values.update(status="failed", next_attempt_at=None)
    else:
        values.update(status="pending", next_attempt_at=now + timedelta(seconds=_backoff_seconds(attempts)))
    with SessionLocal() as db:
        db.execute(
            update(WebhookDelivery)
            .where(WebhookDelivery.id == delivery_id)
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        db.commit()
    return values["status"]


async def _send_signed(attempt: _Attempt) -> Tuple[Optional[int], Optional[str]]:
    """POST one signed attempt. Returns (status code, error category); never a raw error message."""
    try:
        url = validate_webhook_url(attempt.url)
    except WebhookURLError:
        return None, "invalid_url"
    body = json_mod.dumps(attempt.payload, separators=(",", ":"), sort_keys=True, default=str).encode("utf-8")
    timestamp = str(int(time.time()))
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Plaidify-Webhook/2.0",
        "X-Plaidify-Event": attempt.event,
        "X-Plaidify-Delivery": attempt.delivery_id,
        "X-Plaidify-Timestamp": timestamp,
        "X-Plaidify-Signature": "sha256=" + sign_webhook_payload(attempt.secret, timestamp, body),
    }
    try:
        async with httpx.AsyncClient(
            transport=_webhook_transport(),
            timeout=settings.webhook_timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.post(url, content=body, headers=headers)
    except WebhookDestinationBlocked:
        logger.warning(
            "Webhook destination resolves to a non-public address; not sent",
            extra={"extra_data": {"webhook_id": attempt.webhook_id, "delivery_id": attempt.delivery_id}},
        )
        return None, "blocked_destination"
    except httpx.TimeoutException:
        return None, "timeout"
    except httpx.ConnectError:
        return None, "connection_error"
    except httpx.HTTPError:
        return None, "request_error"
    if response.is_success:
        return response.status_code, None
    return response.status_code, ("redirect_not_followed" if response.is_redirect else "http_error")


async def deliver_webhook(delivery_id: str, *, retry: bool = True) -> Optional[str]:
    """Make one attempt at a due delivery. Returns its status afterwards, or None if it was not due."""
    attempt = await asyncio.to_thread(_claim_delivery, delivery_id)
    if attempt is None:
        return None
    status_code, error = await _send_signed(attempt)
    return await asyncio.to_thread(
        _record_attempt,
        delivery_id,
        attempts=attempt.attempts,
        status_code=status_code,
        error=error,
        retry=retry,
    )


async def _deliver_webhook(delivery_id: str, *, retry: bool = True) -> bool:
    """One attempt at a due delivery; True when the receiver accepted it."""
    return await deliver_webhook(delivery_id, retry=retry) == "delivered"


def _due_delivery_ids(limit: int) -> List[str]:
    now = utcnow()
    with SessionLocal() as db:
        rows = db.execute(
            select(WebhookDelivery.id)
            .where(
                WebhookDelivery.status == "pending",
                WebhookDelivery.next_attempt_at <= now,
                or_(WebhookDelivery.locked_until.is_(None), WebhookDelivery.locked_until < now),
            )
            .order_by(WebhookDelivery.next_attempt_at)
            .limit(limit)
        ).all()
        return [row[0] for row in rows]


async def _deliver_many(delivery_ids: List[str]) -> None:
    semaphore = asyncio.Semaphore(_DELIVERY_CONCURRENCY)

    async def one(delivery_id: str) -> None:
        async with semaphore:
            try:
                await deliver_webhook(delivery_id)
            except Exception as exc:
                logger.warning(
                    "Webhook delivery attempt failed",
                    extra={"extra_data": {"delivery_id": delivery_id, "error": type(exc).__name__}},
                )

    await asyncio.gather(*(one(delivery_id) for delivery_id in delivery_ids))


async def deliver_due_webhooks(limit: int = _DELIVERY_BATCH) -> int:
    """The outbox tick: attempt every delivery that is due. Returns how many were attempted."""
    delivery_ids = await asyncio.to_thread(_due_delivery_ids, limit)
    if delivery_ids:
        await _deliver_many(delivery_ids)
    return len(delivery_ids)


def purge_webhook_deliveries(now: Optional[datetime] = None) -> int:
    """Remove delivered and failed deliveries older than WEBHOOK_DELIVERY_RETENTION_DAYS."""
    cutoff = (now or utcnow()) - timedelta(days=settings.webhook_delivery_retention_days)
    with SessionLocal() as db:
        deleted = (
            db.query(WebhookDelivery)
            .filter(WebhookDelivery.status.in_(("delivered", "failed")), WebhookDelivery.created_at < cutoff)
            .delete(synchronize_session=False)
        )
        db.commit()
    return deleted


def _deliver_soon(delivery_ids: List[str]) -> None:
    """Attempt freshly stored deliveries now; the outbox retries whatever does not go through."""
    try:
        task = asyncio.get_running_loop().create_task(_deliver_many(delivery_ids))
    except RuntimeError:
        return
    _IMMEDIATE_DELIVERIES.add(task)
    task.add_done_callback(_IMMEDIATE_DELIVERIES.discard)


async def shutdown_webhook_deliveries(timeout: float = 5.0) -> None:
    """Give immediate delivery attempts in flight a moment to finish (their rows stay pending otherwise)."""
    loop = asyncio.get_running_loop()
    tasks = [task for task in list(_IMMEDIATE_DELIVERIES) if not task.done() and task.get_loop() is loop]
    _IMMEDIATE_DELIVERIES.difference_update(task for task in list(_IMMEDIATE_DELIVERIES) if task.done())
    if not tasks:
        return
    _done, pending = await asyncio.wait(tasks, timeout=timeout)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)


async def enqueue_webhook_event(
    link_token: str,
    payload: Dict[str, Any],
    *,
    owner_id: Optional[int] = None,
) -> List[str]:
    """Store ``payload`` for every webhook of ``link_token`` (by ``owner_id``, when given) and send it.

    Returns the delivery ids. The payload must not carry secrets or extracted
    values: it is what receivers get.
    """
    delivery_ids = await asyncio.to_thread(_insert_deliveries, link_token, payload, owner_id)
    if delivery_ids:
        _deliver_soon(delivery_ids)
    return delivery_ids


async def fire_webhooks_for_session(link_token: str, event: str, data: Optional[Dict] = None) -> List[str]:
    """Fire all registered webhooks for a link session event."""
    payload: Dict[str, Any] = {
        "event": event,
        "link_token": link_token,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "data": data or {},
    }

    # Security: never include raw access_token in webhook payloads.
    # Use the public_token (which is single-use and time-limited) instead.
    session = await session_store.aget_link_session(link_token)
    if session and event == "LINK_COMPLETE" and session.get("public_token"):
        payload["public_token"] = session["public_token"]

    owner_id = session.get("user_id") if session else None
    return await enqueue_webhook_event(link_token, payload, owner_id=owner_id)


# ── Endpoints ─────────────────────────────────────────────────────────────────


def _owns_link_token(db: Session, user: User, link_token: str) -> bool:
    """Whether ``user`` owns the hosted session or /create_link token ``link_token``."""
    session = session_store.get_link_session(link_token)
    if session is not None and session.get("user_id") == user.id:
        return True
    return db.query(Link).filter_by(link_token=link_token, user_id=user.id).first() is not None


@router.post("/register")
async def register_webhook(
    body: WebhookRegisterRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Register a webhook URL for a link token you own (hosted session or /create_link token).

    Deliveries are signed; see this module's docstring for the headers.
    """
    try:
        url = validate_webhook_url(body.url)
    except WebhookURLError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    if not _owns_link_token(db, user, body.link_token):
        raise HTTPException(status_code=404, detail="Link session not found.")

    webhook_id = str(uuid.uuid4())
    db_webhook = Webhook(
        id=webhook_id,
        link_token=body.link_token,
        url=url,
        secret=encrypt_webhook_secret(user, body.secret),
        user_id=user.id,
    )
    db.add(db_webhook)
    db.commit()

    logger.info(
        "Webhook registered",
        extra={"extra_data": {"webhook_id": webhook_id, "link_token": token_fingerprint(body.link_token)}},
    )
    return {"webhook_id": webhook_id, "status": "registered"}


@router.get("")
async def list_webhooks(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List all webhooks registered by the current user."""
    webhooks = db.query(Webhook).filter_by(user_id=user.id).all()
    return {
        "webhooks": [
            {
                "webhook_id": wh.id,
                "link_token": wh.link_token,
                "url": wh.url,
                "created_at": (wh.created_at.isoformat() if wh.created_at else None),
            }
            for wh in webhooks
        ],
        "count": len(webhooks),
    }


@router.delete("/{webhook_id}")
async def delete_webhook(
    webhook_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete a registered webhook and its delivery history."""
    wh = db.query(Webhook).filter_by(id=webhook_id, user_id=user.id).first()
    if not wh:
        raise HTTPException(status_code=404, detail="Webhook not found.")
    db.query(WebhookDelivery).filter_by(webhook_id=webhook_id).delete(synchronize_session=False)
    db.delete(wh)
    db.commit()
    return {"status": "deleted"}


@router.post("/test")
async def test_webhook(
    body: WebhookTestRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Send a test event to a registered webhook URL (one attempt, no retries)."""
    wh = db.query(Webhook).filter_by(id=body.webhook_id, user_id=user.id).first()
    if not wh:
        raise HTTPException(status_code=404, detail="Webhook not found.")
    try:
        decrypt_webhook_secret(db, wh)
    except CredentialDecryptionError:
        logger.error("Webhook secret cannot be decrypted", extra={"extra_data": {"webhook_id": wh.id}})
        raise HTTPException(
            status_code=409,
            detail="This webhook's signing secret can no longer be read. Delete the webhook and register it again.",
        )

    delivery_id = _new_delivery_id()
    now = utcnow()
    payload = {
        "event": "TEST",
        "link_token": wh.link_token,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "data": {"message": "This is a test webhook event."},
        "delivery_id": delivery_id,
        "webhook_id": wh.id,
    }
    db.add(
        WebhookDelivery(
            id=delivery_id,
            webhook_id=wh.id,
            user_id=user.id,
            event="TEST",
            payload_json=encrypt_json_for_user(db, user, payload, context=_payload_context(delivery_id)),
            status="pending",
            attempts=0,
            next_attempt_at=now,
            created_at=now,
        )
    )
    db.commit()

    delivered = await _deliver_webhook(delivery_id, retry=False)
    record = await asyncio.to_thread(_delivery_view, delivery_id)
    response: Dict[str, Any] = {"status": "delivered" if delivered else "failed", "delivery_id": delivery_id}
    if record is not None:
        response["status_code"] = record["last_status_code"]
        if record["last_error"]:
            response["error"] = record["last_error"]
    return response


def _delivery_view(delivery_id: str) -> Optional[Dict[str, Any]]:
    with SessionLocal() as db:
        delivery = db.get(WebhookDelivery, delivery_id)
        return _serialize_delivery(delivery) if delivery is not None else None


def _serialize_delivery(delivery: WebhookDelivery) -> Dict[str, Any]:
    def iso(value):
        return value.isoformat() if value else None

    return {
        "delivery_id": delivery.id,
        "event": delivery.event,
        "status": delivery.status,
        "attempts": delivery.attempts,
        "last_status_code": delivery.last_status_code,
        "last_error": delivery.last_error,
        "success": delivery.status == "delivered",
        "created_at": iso(delivery.created_at),
        "delivered_at": iso(delivery.delivered_at),
        "next_attempt_at": iso(delivery.next_attempt_at),
    }


@router.get("/{webhook_id}/deliveries")
async def get_webhook_deliveries(
    webhook_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delivery history of a webhook: the latest 50 events, newest first."""
    wh = db.query(Webhook).filter_by(id=webhook_id, user_id=user.id).first()
    if not wh:
        raise HTTPException(status_code=404, detail="Webhook not found.")
    base = db.query(WebhookDelivery).filter(WebhookDelivery.webhook_id == webhook_id)
    total = base.count()
    deliveries = base.order_by(WebhookDelivery.created_at.desc()).limit(50).all()
    return {
        "webhook_id": webhook_id,
        "url": wh.url,
        "deliveries": [_serialize_delivery(delivery) for delivery in deliveries],
        "total": total,
    }
