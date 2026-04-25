"""
Scheduled data refresh endpoints: schedule, unschedule, list jobs.
"""

from datetime import datetime, timezone
from typing import Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from src.access_jobs import run_access_job
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
from src.dependencies import get_current_user, limiter
from src.scheduled_refresh import (
    MIN_INTERVAL_SECONDS,
    RefreshScheduler,
    SCHEDULE_FORMATS,
    SCHEDULE_FORMAT_INTERVAL,
    resolve_schedule,
)

settings = get_settings()

router = APIRouter(prefix="/refresh", tags=["refresh"])

# Abuse controls: cap how many active schedules a single user may register.
MAX_SCHEDULES_PER_USER = 50

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
                token_record = db.query(AccessToken).filter_by(token=access_token, user_id=user_id).first()
                if not token_record:
                    raise ValueError("Access token not found")
                site = db.query(Link).filter_by(link_token=token_record.link_token, user_id=user_id).first()
                if not site:
                    raise ValueError("Link not found")
                user = db.query(User).filter_by(id=user_id).first()
                if not user:
                    raise ValueError("User not found")
                username = decrypt_credential_for_user(user, token_record.username_encrypted)
                password = decrypt_credential_for_user(user, token_record.password_encrypted)
                _job, result = await run_access_job(
                    db,
                    site=site.site,
                    job_type="scheduled_refresh",
                    executor=connect_to_site,
                    executor_kwargs={
                        "site": site.site,
                        "username": username,
                        "password": password,
                    },
                    user_id=user_id,
                    metadata={"access_token_prefix": access_token[:12]},
                )
                return result
            finally:
                db.close()

        async def _on_refresh_webhook(access_token: str, user_id: int, data: Dict) -> None:
            """Fire DATA_REFRESHED / REFRESH_FAILED webhooks after a refresh.

            Standardized payload contract (event_version=2):
              - event: "DATA_REFRESHED" | "REFRESH_FAILED"
              - event_version: 2
              - access_token_prefix: first 12 chars of token + "..."
              - timestamp: ISO 8601 UTC
              - success: bool
              - fields_updated: list[str]   (DATA_REFRESHED only)
              - error: str                  (REFRESH_FAILED only)
              - consecutive_failures: int   (REFRESH_FAILED only)
            """
            from src.routers.webhooks import _deliver_webhook

            db = next(get_db())
            try:
                token_record = db.query(AccessToken).filter_by(token=access_token, user_id=user_id).first()
                if not token_record:
                    return
                webhooks = db.query(Webhook).filter_by(link_token=token_record.link_token).all()
                base = {
                    "event_version": 2,
                    "access_token_prefix": access_token[:12] + "...",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                if isinstance(data, dict) and data.get("__refresh_failed__"):
                    payload = {
                        **base,
                        "event": "REFRESH_FAILED",
                        "success": False,
                        "error": str(data.get("error", "unknown")),
                        "consecutive_failures": int(data.get("consecutive_failures", 0)),
                    }
                else:
                    payload = {
                        **base,
                        "event": "DATA_REFRESHED",
                        "success": True,
                        "fields_updated": (list(data.keys()) if isinstance(data, dict) else []),
                    }
                for wh in webhooks:
                    asyncio.create_task(_deliver_webhook(wh.id, wh.url, wh.secret, payload))
            finally:
                db.close()

        _refresh_scheduler = RefreshScheduler(
            fetch_callback=_do_refresh,
            webhook_callback=_on_refresh_webhook,
        )
        _refresh_scheduler.load_from_db()
    return _refresh_scheduler


@router.post("/schedule")
@limiter.limit("30/minute")
async def schedule_refresh(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Schedule periodic data refresh for an access token.

    Body:
        access_token (str, required)
        schedule_format (str, optional): one of
            ``interval`` (default), ``hourly``, ``daily``, ``weekly``.
        interval_seconds (int, optional): required when format is ``interval``.
            Minimum 300 (5 minutes).
    """
    body = await request.json()
    access_token = body.get("access_token")
    interval = body.get("interval_seconds", 3600)
    schedule_format = body.get("schedule_format") or body.get("format")

    if not access_token:
        raise HTTPException(status_code=400, detail="access_token is required.")

    try:
        fmt, resolved_interval = resolve_schedule(
            schedule_format=schedule_format,
            interval_seconds=interval,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if resolved_interval < MIN_INTERVAL_SECONDS:
        raise HTTPException(
            status_code=400,
            detail=f"Minimum interval is {MIN_INTERVAL_SECONDS} seconds (5 minutes).",
        )

    # Verify the token belongs to this user
    token_record = db.query(AccessToken).filter_by(token=access_token, user_id=user.id).first()
    if not token_record:
        raise HTTPException(status_code=404, detail="Access token not found.")

    scheduler = _get_refresh_scheduler()
    # Abuse control: cap active schedules per user.
    if access_token not in scheduler.list_jobs():
        active = len(scheduler.jobs_for_user(user.id))
        if active >= MAX_SCHEDULES_PER_USER:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Refresh schedule quota exceeded "
                    f"(max {MAX_SCHEDULES_PER_USER} active schedules per user)."
                ),
            )
    if not scheduler.running:
        scheduler.start()
    scheduler.schedule(
        access_token,
        user.id,
        interval_seconds=resolved_interval,
        schedule_format=fmt,
    )

    record_audit_event(
        db,
        "refresh",
        "schedule",
        user_id=user.id,
        resource=access_token[:12],
        metadata={"interval_seconds": resolved_interval, "schedule_format": fmt},
    )
    return {
        "status": "scheduled",
        "access_token": access_token[:12] + "...",
        "interval_seconds": resolved_interval,
        "schedule_format": fmt,
    }


@router.patch("/schedule/{access_token}")
@limiter.limit("30/minute")
async def update_schedule(
    access_token: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update an existing refresh schedule.

    Body fields are all optional; any combination of ``interval_seconds``,
    ``schedule_format``, and ``enabled`` may be supplied.
    """
    body = await request.json()
    interval = body.get("interval_seconds")
    schedule_format = body.get("schedule_format") or body.get("format")
    enabled = body.get("enabled")

    token_record = db.query(AccessToken).filter_by(token=access_token, user_id=user.id).first()
    if not token_record:
        raise HTTPException(status_code=404, detail="Access token not found.")

    scheduler = _get_refresh_scheduler()
    if access_token not in scheduler.list_jobs():
        raise HTTPException(status_code=404, detail="No refresh schedule found for this token.")

    try:
        job = scheduler.update(
            access_token,
            interval_seconds=interval,
            schedule_format=schedule_format,
            enabled=enabled,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if job is not None and job.interval_seconds < MIN_INTERVAL_SECONDS:
        raise HTTPException(
            status_code=400,
            detail=f"Minimum interval is {MIN_INTERVAL_SECONDS} seconds (5 minutes).",
        )

    if job is None:
        raise HTTPException(status_code=404, detail="No refresh schedule found for this token.")

    record_audit_event(
        db,
        "refresh",
        "update",
        user_id=user.id,
        resource=access_token[:12],
        metadata={
            "interval_seconds": job.interval_seconds,
            "schedule_format": job.schedule_format,
            "enabled": job.enabled,
        },
    )
    return {
        "status": "updated",
        "access_token": access_token[:12] + "...",
        "interval_seconds": job.interval_seconds,
        "schedule_format": job.schedule_format,
        "enabled": job.enabled,
    }


@router.delete("/schedule/{access_token}")
async def unschedule_refresh(
    access_token: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Remove a scheduled refresh for an access token."""
    token_record = db.query(AccessToken).filter_by(token=access_token, user_id=user.id).first()
    if not token_record:
        raise HTTPException(status_code=404, detail="Access token not found.")

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
