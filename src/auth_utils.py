"""JWT and token utilities shared across auth-related routers."""

import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from sqlalchemy.orm import Session

from src.config import get_settings
from src.database import RefreshToken

settings = get_settings()
LINK_LAUNCH_TOKEN_TYPE = "plaidify_link_launch"


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


def create_link_launch_token(
    *,
    launch_id: str,
    user_id: int,
    site: Optional[str] = None,
    allowed_origin: Optional[str] = None,
    scopes: Optional[list[str]] = None,
    expires_seconds: Optional[int] = None,
) -> str:
    """Create a signed one-time bootstrap token for hosted link launch."""
    now = datetime.now(timezone.utc)
    expire = now + timedelta(seconds=expires_seconds or settings.link_launch_token_expire_seconds)
    payload = {
        "sub": str(user_id),
        "typ": LINK_LAUNCH_TOKEN_TYPE,
        "jti": launch_id,
        "iat": now,
        "exp": expire,
    }
    if site:
        payload["site"] = site
    if allowed_origin:
        payload["allowed_origin"] = allowed_origin.rstrip("/")
    if scopes is not None:
        payload["scopes"] = scopes

    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_link_launch_token(token: str) -> dict:
    """Decode and validate a hosted link launch token."""
    payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    if payload.get("typ") != LINK_LAUNCH_TOKEN_TYPE:
        raise jwt.InvalidTokenError("Invalid link launch token type.")
    return payload
