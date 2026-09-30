"""Access job status endpoints.

API-key and agent callers see only jobs for the sites they may use, and only
the result fields their scopes allow. This router also contributes the web
process's part of the application lifespan (``src.background_services``).
"""

import asyncio
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from src.access_jobs import cancel_access_job, load_job_owner, serialize_access_job_runtime
from src.background_services import web_lifespan
from src.database import AccessJob, User, get_db
from src.dependencies import AuthContext, get_auth_context, get_current_user_or_api_key

router = APIRouter(prefix="/access_jobs", tags=["access_jobs"], lifespan=web_lifespan)


def _optional_user(request: Request, db: Session) -> Optional[User]:
    """Return the authenticated user if credentials are present, otherwise None."""
    if not request.headers.get("authorization") and not request.headers.get("x-api-key"):
        return None
    return get_current_user_or_api_key(request, db)


def _normalize_field(name: str) -> str:
    value = str(name).strip()
    return value.split(":", 1)[1].strip() if ":" in value else value


def _site_allowed(auth: Optional[AuthContext], site: str) -> bool:
    return auth is None or auth.allowed_sites is None or site.strip().lower() in auth.allowed_sites


def _restrict_to_scopes(payload: Dict[str, Any], auth: Optional[AuthContext]) -> Dict[str, Any]:
    """Remove every result field (and field name) outside the caller's allowed scopes."""
    if auth is None or auth.allowed_scopes is None:
        return payload
    allowed = auth.allowed_scopes

    result = payload.get("result")
    if isinstance(result, dict):
        result = dict(result)
        data = result.get("data")
        if isinstance(data, dict):
            result["data"] = {key: value for key, value in data.items() if _normalize_field(key) in allowed}
        result.pop("metadata", None)  # may name or describe fields outside the scopes
        result["scopes_applied"] = sorted(allowed)
        payload["result"] = result

    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        metadata = dict(metadata)
        for key in ("result_fields", "extract_fields", "sensitive_fields"):
            if isinstance(metadata.get(key), list):
                metadata[key] = [name for name in metadata[key] if _normalize_field(name) in allowed]
        if "result_field_count" in metadata and isinstance(metadata.get("result_fields"), list):
            metadata["result_field_count"] = len(metadata["result_fields"])
        payload["metadata"] = metadata
    return payload


@router.get("")
async def list_access_jobs(
    request: Request,
    limit: int = 20,
    site: Optional[str] = None,
    status: Optional[str] = None,
    job_type: Optional[str] = None,
    user: User = Depends(get_current_user_or_api_key),
    db: Session = Depends(get_db),
):
    """List access jobs for the authenticated user (only the sites an API key or agent may use)."""
    auth = get_auth_context(request)
    page_size = max(1, min(limit, 100))
    query = db.query(AccessJob).filter(AccessJob.user_id == user.id)

    if auth is not None and auth.allowed_sites is not None:
        if not auth.allowed_sites:
            return {"jobs": [], "count": 0}
        query = query.filter(func.lower(AccessJob.site).in_(sorted(auth.allowed_sites)))
    if site:
        query = query.filter(AccessJob.site == site)
    if status:
        query = query.filter(AccessJob.status == status)
    if job_type:
        query = query.filter(AccessJob.job_type == job_type)

    jobs = query.order_by(AccessJob.created_at.desc()).limit(page_size).all()
    owner = await asyncio.to_thread(load_job_owner, user.id)
    job_payloads = [_restrict_to_scopes(await serialize_access_job_runtime(job, owner=owner), auth) for job in jobs]
    return {
        "jobs": job_payloads,
        "count": len(jobs),
    }


def _find_visible_job(request: Request, db: Session, job_id: str) -> AccessJob:
    """The job, if this caller may see it (404 otherwise, so ids are not probed)."""
    # Authenticate first: an API-key login commits the session, which would
    # expire a job row loaded before it.
    user = _optional_user(request, db)
    job = db.query(AccessJob).filter(AccessJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Access job not found.")

    if job.user_id is not None:
        if user is None:
            raise HTTPException(status_code=401, detail="Authentication required for this access job.")
        if user.id != job.user_id:
            raise HTTPException(status_code=404, detail="Access job not found.")
    if not _site_allowed(get_auth_context(request), job.site):
        raise HTTPException(status_code=404, detail="Access job not found.")
    return job


@router.get("/{job_id}")
async def get_access_job(
    job_id: str,
    request: Request,
    session_id: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Get the status of a specific access job.

    User-owned jobs require authentication (and, for an API key or agent,
    access to the job's site); their result is filtered to the caller's scopes.
    Anonymous jobs are addressable by job_id as a capability token and never
    carry a stored result.
    """
    job = _find_visible_job(request, db, job_id)
    owner = await asyncio.to_thread(load_job_owner, job.user_id) if job.user_id is not None else None
    payload = await serialize_access_job_runtime(job, owner=owner)
    return _restrict_to_scopes(payload, get_auth_context(request))


@router.post("/{job_id}/cancel")
async def cancel_job(
    job_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """Cancel a pending or running job: its browser session is closed and its lock freed at once."""
    job = _find_visible_job(request, db, job_id)
    cancelled = await cancel_access_job(job.id, reason="The access job was cancelled by the caller.")
    db.expire_all()
    job = db.query(AccessJob).filter(AccessJob.id == job_id).first()
    return {"job_id": job_id, "cancelled": cancelled, "status": job.status if job else "cancelled"}
