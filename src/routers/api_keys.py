"""
API key management endpoints: create, list, revoke.
"""

import json
import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from src.database import ApiKey, User, get_db, utcnow
from src.dependencies import generate_api_key, get_current_user
from src.logging_config import get_logger
from src.models import ApiKeyCreateRequest

logger = get_logger("api.api_keys")

router = APIRouter(prefix="/api-keys", tags=["api-keys"])


def _stored_scopes(scopes_json):
    """A key's scopes as a list for the API (the column holds JSON); unreadable → []."""
    if scopes_json is None:
        return None
    try:
        values = json.loads(scopes_json)
    except (TypeError, ValueError):
        return []
    return values if isinstance(values, list) else []


@router.post("")
async def create_api_key(
    body: ApiKeyCreateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Create a new API key. The raw key is returned ONCE — store it securely.

    ``scopes`` limits the data fields the key can read (omit for all, ``[]``
    for none); ``expires_days`` sets an expiry (omit for none).
    """
    raw_key, key_hash = generate_api_key("pk_")
    key_prefix = raw_key[:12]

    expires_at = utcnow() + timedelta(days=body.expires_days) if body.expires_days else None

    db_key = ApiKey(
        id=str(uuid.uuid4()),
        name=body.name,
        key_hash=key_hash,
        key_prefix=key_prefix,
        user_id=user.id,
        scopes=json.dumps(body.scopes) if body.scopes is not None else None,
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
        "scopes": body.scopes,
        "expires_at": expires_at.isoformat() if expires_at else None,
        "created_at": (db_key.created_at.isoformat() if db_key.created_at else None),
    }


@router.get("")
async def list_api_keys(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List the current user's active API keys (without the raw key), newest first."""
    keys = (
        db.query(ApiKey)
        .filter_by(user_id=user.id, is_active=True)
        .order_by(ApiKey.created_at.desc(), ApiKey.id)
        .offset(offset)
        .limit(limit)
        .all()
    )
    return [
        {
            "id": k.id,
            "name": k.name,
            "key_prefix": k.key_prefix,
            "scopes": _stored_scopes(k.scopes),
            "expires_at": k.expires_at.isoformat() if k.expires_at else None,
            "last_used_at": (k.last_used_at.isoformat() if k.last_used_at else None),
            "created_at": (k.created_at.isoformat() if k.created_at else None),
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
    db_key = db.query(ApiKey).filter_by(id=key_id, user_id=user.id).first()
    if not db_key:
        raise HTTPException(status_code=404, detail="API key not found.")
    db_key.is_active = False
    db.commit()
    logger.info(
        "API key revoked",
        extra={"extra_data": {"key_id": key_id, "user_id": user.id}},
    )
    return {"status": "revoked", "key_id": key_id}
