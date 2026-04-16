"""
Webhook system endpoints: register, list, delete, test, deliveries.
Also provides the fire_webhooks_for_session helper used by link_sessions.
"""

import asyncio
import hashlib
import hmac
import json as json_mod
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from src.database import User, Webhook, get_db
from src.dependencies import get_current_user
from src.logging_config import get_logger
from src import session_store

logger = get_logger("api.webhooks")

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/register")
async def register_webhook(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Register a webhook URL for a link_token.

    The webhook will be called when events occur on the link session.
    Payload includes an HMAC-SHA256 signature for verification.
    """
    body = await request.json()
    link_token = body.get("link_token")
    url = body.get("url")
    webhook_secret = body.get("secret")

    if not link_token or not url or not webhook_secret:
        raise HTTPException(
            status_code=422,
            detail="link_token, url, and secret are required.",
        )

    # Validate URL format
    if not url.startswith("https://") and not url.startswith(
        "http://localhost"
    ):
        raise HTTPException(
            status_code=422,
            detail="Webhook URL must use HTTPS (http://localhost allowed for development).",
        )

    from src.routers.link_sessions import _get_link_session

    session = _get_link_session(link_token)
    if not session:
        raise HTTPException(
            status_code=404, detail="Link session not found."
        )

    webhook_id = str(uuid.uuid4())
    db_webhook = Webhook(
        id=webhook_id,
        link_token=link_token,
        url=url,
        secret=webhook_secret,
        user_id=user.id,
    )
    db.add(db_webhook)
    db.commit()

    logger.info(
        "Webhook registered",
        extra={
            "extra_data": {"webhook_id": webhook_id, "link_token": link_token}
        },
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
                "created_at": (
                    wh.created_at.isoformat() if wh.created_at else None
                ),
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
    """Delete a registered webhook."""
    wh = (
        db.query(Webhook).filter_by(id=webhook_id, user_id=user.id).first()
    )
    if not wh:
        raise HTTPException(status_code=404, detail="Webhook not found.")
    db.delete(wh)
    db.commit()
    session_store.delete_webhook_deliveries(webhook_id)
    return {"status": "deleted"}


@router.post("/test")
async def test_webhook(
    request: Request,
    db: Session = Depends(get_db),
):
    """Send a test event to a registered webhook URL."""
    body = await request.json()
    webhook_id = body.get("webhook_id")

    if not webhook_id:
        raise HTTPException(status_code=404, detail="Webhook not found.")
    wh = db.query(Webhook).filter_by(id=webhook_id).first()
    if not wh:
        raise HTTPException(status_code=404, detail="Webhook not found.")

    test_payload = {
        "event": "TEST",
        "link_token": wh.link_token,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "data": {"message": "This is a test webhook event."},
    }

    success = await _deliver_webhook(
        wh.id, wh.url, wh.secret, test_payload
    )
    return {"status": "delivered" if success else "failed"}


@router.get("/{webhook_id}/deliveries")
async def get_webhook_deliveries(
    webhook_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get delivery history for a webhook."""
    wh = (
        db.query(Webhook).filter_by(id=webhook_id, user_id=user.id).first()
    )
    if not wh:
        raise HTTPException(status_code=404, detail="Webhook not found.")
    deliveries = session_store.get_webhook_deliveries(webhook_id)
    return {
        "webhook_id": webhook_id,
        "url": wh.url,
        "deliveries": deliveries[-50:],  # Last 50 deliveries
        "total": len(deliveries),
    }


# ── Webhook Delivery Helpers ─────────────────────────────────────────────────


async def _deliver_webhook(
    webhook_id: str,
    url: str,
    secret: str,
    payload: Dict[str, Any],
    retries: int = 3,
) -> bool:
    """Deliver a webhook payload with HMAC signature and retry logic."""
    payload_bytes = json_mod.dumps(payload, separators=(",", ":")).encode(
        "utf-8"
    )
    signature = hmac.new(
        secret.encode("utf-8"),
        payload_bytes,
        hashlib.sha256,
    ).hexdigest()

    headers = {
        "Content-Type": "application/json",
        "X-Plaidify-Signature": f"sha256={signature}",
        "X-Plaidify-Event": payload.get("event", "UNKNOWN"),
        "User-Agent": "Plaidify-Webhook/1.0",
    }

    deliveries = []
    for attempt in range(retries):
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    url, content=payload_bytes, headers=headers
                )
                delivery = {
                    "attempt": attempt + 1,
                    "status_code": resp.status_code,
                    "timestamp": time.time(),
                    "success": resp.is_success,
                }
                deliveries.append(delivery)
                session_store.add_webhook_delivery(webhook_id, delivery)
                if resp.is_success:
                    return True
        except Exception as e:
            delivery = {
                "attempt": attempt + 1,
                "error": str(e),
                "timestamp": time.time(),
                "success": False,
            }
            deliveries.append(delivery)
            session_store.add_webhook_delivery(webhook_id, delivery)

        if attempt < retries - 1:
            await asyncio.sleep(2**attempt)  # Exponential backoff: 1s, 2s

    return False


async def fire_webhooks_for_session(
    link_token: str, event: str, data: Optional[Dict] = None
):
    """Fire all registered webhooks for a link session event."""
    from src.routers.link_sessions import _get_link_session

    payload = {
        "event": event,
        "link_token": link_token,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "data": data or {},
    }

    # Security: never include raw access_token in webhook payloads.
    # Use the public_token (which is single-use and time-limited) instead.
    session = _get_link_session(link_token)
    if session and event == "LINK_COMPLETE" and session.get("public_token"):
        payload["public_token"] = session["public_token"]

    # Query webhooks from DB using a fresh session
    db = next(get_db())
    try:
        webhooks = db.query(Webhook).filter_by(link_token=link_token).all()
        for wh in webhooks:
            asyncio.create_task(
                _deliver_webhook(wh.id, wh.url, wh.secret, payload)
            )
    finally:
        db.close()
