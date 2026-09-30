"""JWT and token utilities shared across auth-related routers.

Two kinds of signed JWT exist and neither may stand in for the other:

- access tokens: ``typ=access``, ``aud=plaidify:api``, signed with
  JWT_SECRET_KEY; they carry the user's ``token_version`` as ``tv``, so bumping
  the version ends every access token issued before;
- hosted-link launch tokens: ``typ=plaidify_link_launch``,
  ``aud=plaidify:link-launch``, signed with LINK_LAUNCH_SECRET, or, when that is
  unset, a key derived from JWT_SECRET_KEY under a fixed HKDF label. Launch
  tokens are handed to browsers and phones; a leaked one opens one hosted-link
  session and nothing else.
"""

import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import update
from sqlalchemy.orm import Session

from src.config import get_settings
from src.database import RefreshToken, User, hash_refresh_token

settings = get_settings()
ACCESS_TOKEN_TYPE = "access"
ACCESS_TOKEN_AUDIENCE = "plaidify:api"
LINK_LAUNCH_TOKEN_TYPE = "plaidify_link_launch"
LINK_LAUNCH_AUDIENCE = "plaidify:link-launch"
_LINK_LAUNCH_ALGORITHM = "HS256"
_LINK_LAUNCH_KEY_LABEL = b"plaidify/hosted-link-launch-token/v1"


def link_launch_signing_key() -> bytes:
    """The HMAC key for hosted-link launch tokens; never JWT_SECRET_KEY itself."""
    if settings.link_launch_secret:
        return settings.link_launch_secret.encode("utf-8")
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_LINK_LAUNCH_KEY_LABEL).derive(
        settings.jwt_secret_key.encode("utf-8")
    )


def create_access_token(user_id: int, *, token_version: int = 0, expires_delta: int | None = None) -> str:
    """Create a signed JWT access token for ``user_id`` at ``token_version``."""
    now = datetime.now(timezone.utc)
    expire = now + timedelta(minutes=expires_delta or settings.jwt_access_token_expire_minutes)
    payload = {
        "sub": str(user_id),
        "tv": int(token_version or 0),
        "typ": ACCESS_TOKEN_TYPE,
        "aud": ACCESS_TOKEN_AUDIENCE,
        "iat": now,
        "exp": expire,
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict:
    """Validate an access token's signature, expiry, audience and type; return its claims.

    Raises:
        jwt.ExpiredSignatureError: the token has expired.
        jwt.InvalidTokenError: anything else (bad signature, wrong audience or
            type — a launch token, say — or missing claims).
    """
    payload = jwt.decode(
        token,
        settings.jwt_secret_key,
        algorithms=[settings.jwt_algorithm],
        audience=ACCESS_TOKEN_AUDIENCE,
        options={"require": ["exp", "sub", "aud"]},
    )
    if payload.get("typ") != ACCESS_TOKEN_TYPE or not isinstance(payload.get("tv"), int):
        raise jwt.InvalidTokenError("Not an access token.")
    return payload


def create_refresh_token(user_id: int, db: Session, *, commit: bool = True) -> str:
    """Create a cryptographically random refresh token; only its SHA-256 hash is stored.

    With ``commit=False`` the row is only added to the session, so the caller
    can make it part of a larger transaction (token rotation does).
    """
    token = secrets.token_urlsafe(48)
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_refresh_token_expire_minutes)
    db.add(RefreshToken(token_hash=hash_refresh_token(token), user_id=user_id, expires_at=expires_at))
    if commit:
        db.commit()
    return token


def issue_token_pair(user: User, db: Session, *, commit: bool = True) -> dict:
    """Issue an access + refresh token pair for a user (at the user's current token version)."""
    access_token = create_access_token(user.id, token_version=user.token_version or 0)
    refresh_token = create_refresh_token(user.id, db, commit=commit)
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
    }


def end_user_sessions(db: Session, user_id: int) -> int:
    """End every session of a user: revoke the refresh tokens and bump the token version.

    Access tokens issued before stop working at once (their ``tv`` no longer
    matches). Does not commit; returns the number of refresh tokens revoked.
    """
    revoked = db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked == False)  # noqa: E712
        .values(revoked=True)
        .execution_options(synchronize_session=False)
    ).rowcount
    db.execute(
        update(User)
        .where(User.id == user_id)
        .values(token_version=User.token_version + 1)
        .execution_options(synchronize_session=False)
    )
    return revoked


def create_link_launch_token(
    *,
    launch_id: str,
    user_id: int,
    site: Optional[str] = None,
    allowed_origin: Optional[str] = None,
    allowed_origins: Optional[list[str]] = None,
    scopes: Optional[list[str]] = None,
    expires_seconds: Optional[int] = None,
) -> str:
    """Create a signed one-time bootstrap token for hosted link launch."""
    now = datetime.now(timezone.utc)
    expire = now + timedelta(seconds=expires_seconds or settings.link_launch_token_expire_seconds)
    payload = {
        "sub": str(user_id),
        "typ": LINK_LAUNCH_TOKEN_TYPE,
        "aud": LINK_LAUNCH_AUDIENCE,
        "jti": launch_id,
        "iat": now,
        "exp": expire,
    }
    if site:
        payload["site"] = site
    if allowed_origin:
        payload["allowed_origin"] = allowed_origin.rstrip("/")
    if allowed_origins:
        payload["allowed_origins"] = [entry.rstrip("/") for entry in allowed_origins]
    if scopes is not None:
        payload["scopes"] = scopes

    return jwt.encode(payload, link_launch_signing_key(), algorithm=_LINK_LAUNCH_ALGORITHM)


def decode_link_launch_token(token: str) -> dict:
    """Decode and validate a hosted link launch token (its own key and audience)."""
    payload = jwt.decode(
        token,
        link_launch_signing_key(),
        algorithms=[_LINK_LAUNCH_ALGORITHM],
        audience=LINK_LAUNCH_AUDIENCE,
        options={"require": ["exp", "jti", "aud"]},
    )
    if payload.get("typ") != LINK_LAUNCH_TOKEN_TYPE:
        raise jwt.InvalidTokenError("Invalid link launch token type.")
    return payload
