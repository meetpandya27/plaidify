"""
Scheduled data refresh endpoints: schedule, unschedule, list jobs.
"""

from datetime import datetime, timezone
from typing import Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from src.audit import record_audit_event
from src.config import get_settings
from src.core.engine import connect_to_site
from src.database import (
    AccessToken,
    Link,
    User,
    Webhook,
    decrypt_credential_for_user,
    get_db,
)
from src.dependencies import get_current_user
from src.scheduled_refresh import RefreshScheduler

settings = get_settings()

router = APIRouter(prefix="/refresh", tags=["refresh"])

_refresh_scheduler: Optional[RefreshScheduler] = None


def _get_refresh_scheduler() -> RefreshScheduler:
    """Get or create the global refresh scheduler."""
    global _refresh_scheduler
    if _refresh_scheduler is None:
        import asyncio

        async def _do_refresh(access_token: str, user_id: int) -> Dict:
            """Perform a data refresh using the same logic as GET /fetch_data."""
            db = next(get_db())
            try:
                token_record = (
                    db.query(AccessToken)
                    .filter_by(token=access_token, user_id=user_id)
                    .first()
                )
                if not token_record:
                    raise ValueError("Access token not found")
                site = (
                    db.query(Link)
                    .filter_by(
                        link_token=token_record.link_token, user_id=user_id
                    )
                    .first()
                )
                if not site:
                    raise ValueError("Link not found")
                user = db.query(User).filter_by(id=user_id).first()
                if not user:
                    raise ValueError("User not found")
                username = decrypt_credential_for_user(
                    user, token_record.username_encrypted
                )
                password = decrypt_credential_for_user(
                    user, token_record.password_encrypted
                )
                result = await connect_to_site(site.site, username, password)
                return result
            finally:
                db.close()

        async def _on_refresh_webhook(
            access_token: str, user_id: int, data: Dict
        ) -> None:
            """Fire DATA_REFRESHED webhooks after a successful refresh."""
            from src.routers.webhooks import _deliver_webhook

            db = next(get_db())
            try:
                token_record = (
                    db.query(AccessToken)
                    .filter_by(token=access_token, user_id=user_id)
                    .first()
                )
                if token_record:
                    webhooks = (
                        db.query(Webhook)
                        .filter_by(link_token=token_record.link_token)
                        .all()
                    )
                    payload = {
                        "event": "DATA_REFRESHED",
                        "access_token_prefix": access_token[:12] + "...",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "fields_updated": (
                            list(data.keys())
                            if isinstance(data, dict)
                            else []
                        ),
                    }
                    for wh in webhooks:
                        asyncio.create_task(
                            _deliver_webhook(
                                wh.id, wh.url, wh.secret, payload
                            )
                        )
            finally:
                db.close()

        _refresh_scheduler = RefreshScheduler(
            fetch_callback=_do_refresh,
            webhook_callback=_on_refresh_webhook,
        )
        _refresh_scheduler.load_from_db()
    return _refresh_scheduler


@router.post("/schedule")
async def schedule_refresh(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Schedule periodic data refresh for an access token.

    Body:
        access_token: The access token to refresh.
        interval_seconds: How often to refresh (minimum 300 = 5 minutes).
    """
    body = await request.json()
    access_token = body.get("access_token")
    interval = body.get("interval_seconds", 3600)

    if not access_token:
        raise HTTPException(
            status_code=400, detail="access_token is required."
        )
    if interval < 300:
        raise HTTPException(
            status_code=400,
            detail="Minimum interval is 300 seconds (5 minutes).",
        )

    # Verify the token belongs to this user
    token_record = (
        db.query(AccessToken)
        .filter_by(token=access_token, user_id=user.id)
        .first()
    )
    if not token_record:
        raise HTTPException(
            status_code=404, detail="Access token not found."
        )

    scheduler = _get_refresh_scheduler()
    if not scheduler.running:
        scheduler.start()
    job = scheduler.schedule(access_token, user.id, interval)

    record_audit_event(
        db,
        "refresh",
        "schedule",
        user_id=user.id,
        resource=access_token[:12],
        metadata={"interval_seconds": interval},
    )
    return {
        "status": "scheduled",
        "access_token": access_token[:12] + "...",
        "interval_seconds": interval,
    }


@router.delete("/schedule/{access_token}")
async def unschedule_refresh(
    access_token: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Remove a scheduled refresh for an access token."""
    token_record = (
        db.query(AccessToken)
        .filter_by(token=access_token, user_id=user.id)
        .first()
    )
    if not token_record:
        raise HTTPException(
            status_code=404, detail="Access token not found."
        )

    scheduler = _get_refresh_scheduler()
    removed = scheduler.unschedule(access_token)
    if not removed:
        raise HTTPException(
            status_code=404,
            detail="No refresh schedule found for this token.",
        )

    record_audit_event(
        db,
        "refresh",
        "unschedule",
        user_id=user.id,
        resource=access_token[:12],
    )
    return {
        "status": "unscheduled",
        "access_token": access_token[:12] + "...",
    }


@router.get("/jobs")
async def list_refresh_jobs(
    user: User = Depends(get_current_user),
):
    """List all active refresh jobs (admin view)."""
    scheduler = _get_refresh_scheduler()
    return {"jobs": scheduler.list_jobs()}
