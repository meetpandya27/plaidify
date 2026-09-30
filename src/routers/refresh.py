"""
Scheduled data refresh endpoints: schedule, update, unschedule, list jobs.

Schedules are rows in ``scheduled_refresh_jobs``; the scheduler itself runs
in one process (see ``src.background_services``). Refreshes run the engine
unattended (``interactive_mfa=False``).
"""

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.access_jobs import run_access_job
from src.audit import record_audit_event
from src.config import get_settings
from src.core.engine import connect_to_site
from src.crypto import token_fingerprint
from src.database import (
    AccessToken,
    Link,
    SessionLocal,
    User,
    decrypt_credential_for_user,
    get_db,
)
from src.dependencies import get_admin_user, get_current_user, limiter
from src.scheduled_refresh import (
    MIN_INTERVAL_SECONDS,
    RefreshOwnerInactive,
    RefreshScheduler,
    RefreshTargetMissing,
    mask_access_token,
    resolve_schedule,
)

settings = get_settings()

router = APIRouter(prefix="/refresh", tags=["refresh"])

# Abuse controls: cap how many active schedules a single user may register.
MAX_SCHEDULES_PER_USER = 50

_refresh_scheduler: Optional[RefreshScheduler] = None


class RefreshScheduleRequest(BaseModel):
    access_token: Optional[str] = Field(default=None, max_length=256)
    interval_seconds: Optional[int] = Field(default=None, ge=1, le=10 * 365 * 86400)
    schedule_format: Optional[str] = Field(default=None, max_length=16)
    format: Optional[str] = Field(default=None, max_length=16)


class RefreshScheduleUpdate(BaseModel):
    interval_seconds: Optional[int] = Field(default=None, ge=1, le=10 * 365 * 86400)
    schedule_format: Optional[str] = Field(default=None, max_length=16)
    format: Optional[str] = Field(default=None, max_length=16)
    enabled: Optional[bool] = None


def _load_refresh_credentials(access_token: str, user_id: int) -> Dict[str, str]:
    """Site and decrypted credentials of a scheduled token. Decryption may reach the key service."""
    with SessionLocal() as db:
        token_record = db.query(AccessToken).filter_by(token=access_token, user_id=user_id).first()
        if not token_record:
            raise RefreshTargetMissing("Access token not found")
        link = db.query(Link).filter_by(link_token=token_record.link_token, user_id=user_id).first()
        if not link:
            raise RefreshTargetMissing("Link not found")
        user = db.get(User, user_id)
        if not user:
            raise RefreshTargetMissing("User not found")
        if user.is_active is False:
            raise RefreshOwnerInactive("Owner is deactivated")
        return {
            "site": link.site,
            "username": decrypt_credential_for_user(user, token_record.username_encrypted),
            "password": decrypt_credential_for_user(user, token_record.password_encrypted),
        }


async def _do_refresh(access_token: str, user_id: int) -> Dict:
    """Fetch fresh data for a scheduled token, unattended: an MFA challenge ends the run at once."""
    credentials = await asyncio.to_thread(_load_refresh_credentials, access_token, user_id)
    _job, result = await run_access_job(
        None,
        site=credentials["site"],
        job_type="scheduled_refresh",
        executor=connect_to_site,
        executor_kwargs={
            "site": credentials["site"],
            "username": credentials["username"],
            "password": credentials["password"],
            "interactive_mfa": False,
        },
        user_id=user_id,
        metadata={"access_token_prefix": mask_access_token(access_token)},
    )
    return result


def _link_token_of(access_token: str, user_id: int) -> Optional[str]:
    with SessionLocal() as db:
        token_record = db.query(AccessToken).filter_by(token=access_token, user_id=user_id).first()
        return token_record.link_token if token_record else None


def _refreshed_fields(data: Any) -> list:
    if not isinstance(data, dict):
        return []
    extracted = data.get("data")
    if isinstance(extracted, dict):
        return sorted(extracted.keys())
    return sorted(key for key in data.keys() if not str(key).startswith("__"))


async def _on_refresh_webhook(access_token: str, user_id: int, data: Dict) -> None:
    """Queue DATA_REFRESHED / REFRESH_FAILED webhooks after a refresh.

    Standardized payload contract (event_version=2):
      - event: "DATA_REFRESHED" | "REFRESH_FAILED"
      - event_version: 2
      - access_token_prefix: first 12 chars of token + "..."
      - timestamp: ISO 8601 UTC
      - success: bool
      - fields_updated: list[str]   (DATA_REFRESHED only; names, never values)
      - error: str                  (REFRESH_FAILED only)
      - reason: str                 (REFRESH_FAILED only: "needs_reauth" when the
                                     user must link again, "max_failures" otherwise)
      - consecutive_failures: int   (REFRESH_FAILED only)
    Deliveries are signed with the webhook's (decrypted) secret by the outbox.
    """
    from src.routers.webhooks import enqueue_webhook_event

    link_token = await asyncio.to_thread(_link_token_of, access_token, user_id)
    if not link_token:
        return
    base = {
        "event_version": 2,
        "access_token_prefix": mask_access_token(access_token),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if isinstance(data, dict) and data.get("__refresh_failed__"):
        payload = {
            **base,
            "event": "REFRESH_FAILED",
            "success": False,
            "reason": str(data.get("reason") or "max_failures"),
            "error": str(data.get("error", "unknown")),
            "consecutive_failures": int(data.get("consecutive_failures", 0)),
        }
    else:
        payload = {
            **base,
            "event": "DATA_REFRESHED",
            "success": True,
            "fields_updated": _refreshed_fields(data),
        }
    await enqueue_webhook_event(link_token, payload, owner_id=user_id)


def get_refresh_scheduler() -> RefreshScheduler:
    """The process's refresh scheduler (schedules are rows; the loop is started at boot)."""
    global _refresh_scheduler
    if _refresh_scheduler is None:
        _refresh_scheduler = RefreshScheduler(
            fetch_callback=_do_refresh,
            webhook_callback=_on_refresh_webhook,
        )
    return _refresh_scheduler


# Older name, still used by other modules.
_get_refresh_scheduler = get_refresh_scheduler


def _resolve_or_400(schedule_format: Optional[str], interval_seconds: Optional[int]) -> tuple[str, int]:
    try:
        fmt, resolved_interval = resolve_schedule(schedule_format=schedule_format, interval_seconds=interval_seconds)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if resolved_interval < MIN_INTERVAL_SECONDS:
        raise HTTPException(
            status_code=400,
            detail=f"Minimum interval is {MIN_INTERVAL_SECONDS} seconds (5 minutes).",
        )
    return fmt, resolved_interval


@router.post("/schedule")
@limiter.limit("30/minute")
async def schedule_refresh(
    request: Request,
    body: RefreshScheduleRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Schedule periodic data refresh for an access token.

    Body:
        access_token (str, required)
        schedule_format (str, optional): one of
            ``interval`` (default), ``hourly``, ``daily``, ``weekly``.
        interval_seconds (int, optional): used when format is ``interval``
            (default 3600). Minimum 300 (5 minutes).
    """
    access_token = body.access_token
    if not access_token:
        raise HTTPException(status_code=400, detail="access_token is required.")

    fmt, resolved_interval = _resolve_or_400(
        body.schedule_format or body.format,
        body.interval_seconds if body.interval_seconds is not None else 3600,
    )

    # Verify the token belongs to this user
    token_record = db.query(AccessToken).filter_by(token=access_token, user_id=user.id).first()
    if not token_record:
        raise HTTPException(status_code=404, detail="Access token not found.")

    scheduler = get_refresh_scheduler()
    # Abuse control: cap active schedules per user.
    if scheduler.get(access_token) is None:
        active = sum(1 for job in scheduler.jobs_for_user(user.id) if job.enabled)
        if active >= MAX_SCHEDULES_PER_USER:
            raise HTTPException(
                status_code=429,
                detail=(f"Refresh schedule quota exceeded (max {MAX_SCHEDULES_PER_USER} active schedules per user)."),
            )
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
        resource=token_fingerprint(access_token),
        metadata={"interval_seconds": resolved_interval, "schedule_format": fmt},
    )
    return {
        "status": "scheduled",
        "access_token": mask_access_token(access_token),
        "interval_seconds": resolved_interval,
        "schedule_format": fmt,
    }


@router.patch("/schedule/{access_token}")
@limiter.limit("30/minute")
async def update_schedule(
    access_token: str,
    request: Request,
    body: RefreshScheduleUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update an existing refresh schedule.

    Body fields are all optional; any combination of ``interval_seconds``,
    ``schedule_format``, and ``enabled`` may be supplied. The new schedule is
    validated before anything is saved.
    """
    token_record = db.query(AccessToken).filter_by(token=access_token, user_id=user.id).first()
    if not token_record:
        raise HTTPException(status_code=404, detail="Access token not found.")

    scheduler = get_refresh_scheduler()
    existing = scheduler.get(access_token)
    if existing is None or existing.user_id != user.id:
        raise HTTPException(status_code=404, detail="No refresh schedule found for this token.")

    schedule_format = body.schedule_format or body.format
    fmt: Optional[str] = None
    interval: Optional[int] = None
    if schedule_format is not None or body.interval_seconds is not None:
        fmt, interval = _resolve_or_400(
            schedule_format or existing.schedule_format,
            body.interval_seconds if body.interval_seconds is not None else existing.interval_seconds,
        )

    job = scheduler.update(
        access_token,
        interval_seconds=interval,
        schedule_format=fmt,
        enabled=body.enabled,
    )
    if job is None:
        raise HTTPException(status_code=404, detail="No refresh schedule found for this token.")

    record_audit_event(
        db,
        "refresh",
        "update",
        user_id=user.id,
        resource=token_fingerprint(access_token),
        metadata={
            "interval_seconds": job.interval_seconds,
            "schedule_format": job.schedule_format,
            "enabled": job.enabled,
        },
    )
    return {
        "status": "updated",
        "access_token": mask_access_token(access_token),
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

    scheduler = get_refresh_scheduler()
    existing = scheduler.get(access_token)
    if existing is None or existing.user_id != user.id or not scheduler.unschedule(access_token):
        raise HTTPException(
            status_code=404,
            detail="No refresh schedule found for this token.",
        )

    record_audit_event(
        db,
        "refresh",
        "unschedule",
        user_id=user.id,
        resource=token_fingerprint(access_token),
    )
    return {
        "status": "unscheduled",
        "access_token": mask_access_token(access_token),
    }


@router.get("/jobs")
async def list_refresh_jobs(
    user: User = Depends(get_current_user),
):
    """List your refresh schedules (tokens masked)."""
    return {"jobs": get_refresh_scheduler().list_jobs(user_id=user.id)}


@router.get("/admin/jobs")
async def list_all_refresh_jobs(
    admin: User = Depends(get_admin_user),
):
    """Every tenant's refresh schedules, tokens masked (administrators only)."""
    return {"jobs": get_refresh_scheduler().list_jobs()}
