"""Administrator-only endpoints (RBAC). Requires an admin user (is_admin)."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from src.audit import record_audit_event
from src.database import User, get_db
from src.dependencies import get_admin_user
from src.logging_config import get_logger

logger = get_logger("api.admin")

router = APIRouter(prefix="/admin", tags=["admin"])


def _serialize_user(u: User) -> dict:
    return {
        "id": u.id,
        "username": u.username,
        "email": u.email,
        "is_active": bool(u.is_active),
        "is_admin": bool(u.is_admin),
        "created_at": u.created_at.isoformat() if u.created_at else None,
    }


@router.get("/users")
async def list_users(
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """List all users (admin only)."""
    users = db.query(User).order_by(User.id).all()
    return {"users": [_serialize_user(u) for u in users], "count": len(users)}


@router.post("/users/{user_id}/promote")
async def promote_user(
    user_id: int,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Grant administrator rights to a user (admin only)."""
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")
    target.is_admin = True
    db.commit()
    record_audit_event(db, "admin", "promote_user", user_id=admin.id, metadata={"target_user_id": user_id})
    return {"status": "promoted", "user_id": user_id}


@router.post("/users/{user_id}/set-active")
async def set_user_active(
    user_id: int,
    active: bool,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Activate or deactivate a user account (admin only)."""
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")
    if target.id == admin.id and not active:
        raise HTTPException(status_code=400, detail="Admins cannot deactivate their own account.")
    target.is_active = active
    db.commit()
    record_audit_event(
        db,
        "admin",
        "set_user_active",
        user_id=admin.id,
        metadata={"target_user_id": user_id, "active": active},
    )
    return {"status": "updated", "user_id": user_id, "is_active": active}
