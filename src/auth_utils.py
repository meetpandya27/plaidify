"""
JWT and token utilities shared across auth-related routers.
"""

import secrets
from datetime import datetime, timedelta, timezone

import jwt
from sqlalchemy.orm import Session

from src.config import get_settings
from src.database import RefreshToken

settings = get_settings()


def create_access_token(data: dict, expires_delta: int | None = None) -> str:
    """Create a signed JWT access token."""
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=expires_delta or settings.jwt_access_token_expire_minutes)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def create_refresh_token(user_id: int, db: Session) -> str:
    """Create a cryptographically random refresh token and store it in the database."""
    token = secrets.token_urlsafe(48)
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_refresh_token_expire_minutes)
    db_token = RefreshToken(token=token, user_id=user_id, expires_at=expires_at)
    db.add(db_token)
    db.commit()
    return token


def issue_token_pair(user_id: int, db: Session) -> dict:
    """Issue an access + refresh token pair for a user."""
    access_token = create_access_token({"sub": str(user_id)})
    refresh_token = create_refresh_token(user_id, db)
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
    }
