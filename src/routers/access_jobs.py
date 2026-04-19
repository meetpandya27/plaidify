"""Access job status endpoints."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from src.access_jobs import serialize_access_job_runtime
from src.database import AccessJob, User, get_db
from src.dependencies import get_current_user_or_api_key

router = APIRouter(prefix="/access_jobs", tags=["access_jobs"])


def _optional_user(request: Request, db: Session) -> Optional[User]:
    """Return the authenticated user if credentials are present, otherwise None."""
    if not request.headers.get("authorization") and not request.headers.get("x-api-key"):
        return None
    return get_current_user_or_api_key(request, db)


@router.get("")
async def list_access_jobs(
    limit: int = 20,
    site: Optional[str] = None,
    status: Optional[str] = None,
    job_type: Optional[str] = None,
    user: User = Depends(get_current_user_or_api_key),
    db: Session = Depends(get_db),
):
    """List access jobs for the authenticated user."""
    page_size = max(1, min(limit, 100))
    query = db.query(AccessJob).filter(AccessJob.user_id == user.id)

    if site:
        query = query.filter(AccessJob.site == site)
    if status:
        query = query.filter(AccessJob.status == status)
    if job_type:
        query = query.filter(AccessJob.job_type == job_type)

    jobs = query.order_by(AccessJob.created_at.desc()).limit(page_size).all()
    job_payloads = [await serialize_access_job_runtime(job) for job in jobs]
    return {
        "jobs": job_payloads,
        "count": len(jobs),
    }


@router.get("/{job_id}")
async def get_access_job(
    job_id: str,
    request: Request,
    session_id: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Get the status of a specific access job.

    User-owned jobs require authentication.
    Anonymous jobs are addressable by job_id as a capability token.
    """
    job = db.query(AccessJob).filter(AccessJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Access job not found.")

    if job.user_id is not None:
        user = _optional_user(request, db)
        if user is None:
            raise HTTPException(status_code=401, detail="Authentication required for this access job.")
        if user.id != job.user_id:
            raise HTTPException(status_code=404, detail="Access job not found.")

    return await serialize_access_job_runtime(job)
