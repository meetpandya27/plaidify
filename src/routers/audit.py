"""
Audit log endpoints: query and verify.
"""

import json
from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from src.audit import verify_audit_chain
from src.database import AuditLog, User, get_db
from src.dependencies import get_current_user

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("/logs")
async def get_audit_logs(
    event_type: Optional[str] = None,
    user_id: Optional[int] = None,
    limit: int = 100,
    offset: int = 0,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Query audit logs. Only the user's own logs are returned (admin sees all in future)."""
    query = db.query(AuditLog)

    # Non-admin users can only see their own events
    if user_id and user_id == user.id:
        query = query.filter(AuditLog.user_id == user_id)
    else:
        query = query.filter(AuditLog.user_id == user.id)

    if event_type:
        query = query.filter(AuditLog.event_type == event_type)

    total = query.count()
    entries = (
        query.order_by(AuditLog.id.desc())
        .offset(offset)
        .limit(min(limit, 500))
        .all()
    )

    return {
        "total": total,
        "offset": offset,
        "limit": min(limit, 500),
        "entries": [
            {
                "id": e.id,
                "event_type": e.event_type,
                "user_id": e.user_id,
                "resource": e.resource,
                "action": e.action,
                "agent_id": e.agent_id,
                "metadata": (
                    json.loads(e.metadata_json) if e.metadata_json else None
                ),
                "ip_address": e.ip_address,
                "timestamp": e.timestamp.isoformat(),
                "entry_hash": e.entry_hash,
            }
            for e in entries
        ],
    }


@router.get("/verify")
async def verify_audit_logs(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Verify the integrity of the audit log hash chain."""
    result = verify_audit_chain(db)
    return result
