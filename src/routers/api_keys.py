"""
API key management endpoints: create, list, revoke.
"""

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from src.database import ApiKey, User, get_db
from src.dependencies import get_current_user
from src.logging_config import get_logger

logger = get_logger("api.api_keys")

router = APIRouter(prefix="/api-keys", tags=["api-keys"])


@router.post("")
async def create_api_key(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Create a new API key. The raw key is returned ONCE — store it securely."""
    body = await request.json()
    name = body.get("name", "default")
    expires_days = body.get("expires_days")

    raw_key = f"pk_{secrets.token_urlsafe(32)}"
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    key_prefix = raw_key[:12]

    expires_at = None
    if expires_days:
        expires_at = datetime.now(timezone.utc) + timedelta(
            days=int(expires_days)
        )

    db_key = ApiKey(
        id=str(uuid.uuid4()),
        name=name,
        key_hash=key_hash,
        key_prefix=key_prefix,
        user_id=user.id,
        scopes=body.get("scopes"),
        expires_at=expires_at,
    )
    db.add(db_key)
    db.commit()

    logger.info(
        "API key created",
        extra={"extra_data": {"key_id": db_key.id, "user_id": user.id}},
    )
    return {
        "id": db_key.id,
        "name": db_key.name,
        "key": raw_key,  # Only time the raw key is exposed
        "key_prefix": key_prefix,
        "expires_at": expires_at.isoformat() if expires_at else None,
        "created_at": (
            db_key.created_at.isoformat() if db_key.created_at else None
        ),
    }


@router.get("")
async def list_api_keys(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List all API keys for the current user (without the raw key)."""
    keys = db.query(ApiKey).filter_by(user_id=user.id, is_active=True).all()
    return [
        {
            "id": k.id,
            "name": k.name,
            "key_prefix": k.key_prefix,
            "scopes": k.scopes,
            "expires_at": k.expires_at.isoformat() if k.expires_at else None,
            "last_used_at": (
                k.last_used_at.isoformat() if k.last_used_at else None
            ),
            "created_at": (
                k.created_at.isoformat() if k.created_at else None
            ),
        }
        for k in keys
    ]


@router.delete("/{key_id}")
async def revoke_api_key(
    key_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Revoke an API key."""
    db_key = (
        db.query(ApiKey).filter_by(id=key_id, user_id=user.id).first()
    )
    if not db_key:
        raise HTTPException(status_code=404, detail="API key not found.")
    db_key.is_active = False
    db.commit()
    logger.info(
        "API key revoked",
        extra={"extra_data": {"key_id": key_id, "user_id": user.id}},
    )
    return {"status": "revoked", "key_id": key_id}
