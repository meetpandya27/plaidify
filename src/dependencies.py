"""
Shared FastAPI dependencies used across routers.

Provides authentication, credential resolution, rate limiting,
and password hashing utilities.
"""

import base64
import hashlib
from datetime import datetime, timezone
from typing import Optional

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from passlib.context import CryptContext
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy.orm import Session

from src.config import get_settings
from src.crypto import decrypt_with_session_key, destroy_session_key
from src.database import ApiKey, User, get_db
from src.exceptions import InvalidTokenError
from src.logging_config import get_logger
from src.models import ConnectRequest

settings = get_settings()
logger = get_logger("dependencies")

# ── Rate Limiter ──────────────────────────────────────────────────────────────

_limiter_storage = None
if settings.redis_url and settings.rate_limit_enabled:
    try:
        from limits.storage import RedisStorage

        _limiter_storage = RedisStorage(settings.redis_url)
        logger.info("Rate limiter using Redis storage")
    except Exception:
        logger.warning("Failed to connect to Redis for rate limiter, using in-memory")

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[settings.rate_limit_default] if settings.rate_limit_enabled else [],
    enabled=settings.rate_limit_enabled,
    storage_uri=settings.redis_url if _limiter_storage else "memory://",
)

# ── Password Hashing ─────────────────────────────────────────────────────────

pwd_context = CryptContext(
    schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=12, bcrypt__ident="2b"
)
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token")


def get_password_hash(password: str) -> str:
    """Hash a password using bcrypt."""
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plain password against a bcrypt hash."""
    return pwd_context.verify(plain_password, hashed_password)


# ── User Dependencies ─────────────────────────────────────────────────────────


def get_current_user(
    token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)
) -> User:
    """FastAPI dependency: extract and validate the current user from a JWT."""
    try:
        payload = jwt.decode(
            token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm]
        )
        user_id = payload.get("sub")
        if user_id is None:
            raise InvalidTokenError()
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired.",
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials.",
        )

    user = db.query(User).filter(User.id == int(user_id)).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found.",
        )
    return user


def get_current_user_or_api_key(
    request: Request, db: Session = Depends(get_db)
) -> User:
    """FastAPI dependency: authenticate via JWT Bearer token OR X-API-Key header."""
    # Check for API key first
    api_key = request.headers.get("x-api-key")
    if api_key:
        key_hash = hashlib.sha256(api_key.encode()).hexdigest()
        db_key = db.query(ApiKey).filter_by(key_hash=key_hash, is_active=True).first()
        if not db_key:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key."
            )
        if db_key.expires_at:
            exp = (
                db_key.expires_at.replace(tzinfo=timezone.utc)
                if db_key.expires_at.tzinfo is None
                else db_key.expires_at
            )
            if datetime.now(timezone.utc) > exp:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="API key has expired.",
                )
        # Update last used
        db_key.last_used_at = datetime.now(timezone.utc)
        db.commit()
        user = db.query(User).filter(User.id == db_key.user_id).first()
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="API key owner not found.",
            )
        return user

    # Fall back to JWT
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication.",
        )
    token = auth_header[7:]
    return get_current_user(token=token, db=db)


# ── Credential Resolution ────────────────────────────────────────────────────


def resolve_credentials(body: ConnectRequest) -> tuple[str, str]:
    """Extract plaintext credentials from a ConnectRequest.

    Supports both plaintext and client-side encrypted credentials.
    If encrypted fields are present, they are decrypted using the ephemeral
    session key associated with the link_token.

    Returns:
        (username, password) as plaintext strings.

    Raises:
        HTTPException: If credentials are missing or decryption fails.
    """
    if body.encrypted_username and body.encrypted_password and body.link_token:
        try:
            enc_user = base64.b64decode(body.encrypted_username)
            enc_pass = base64.b64decode(body.encrypted_password)
            username = decrypt_with_session_key(body.link_token, enc_user)
            password = decrypt_with_session_key(body.link_token, enc_pass)
            # Destroy the key after single use
            destroy_session_key(body.link_token)
            return username, password
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception:
            raise HTTPException(
                status_code=400, detail="Failed to decrypt credentials."
            )

    if body.username and body.password:
        return body.username, body.password

    raise HTTPException(
        status_code=422,
        detail="Provide either (username + password) or (encrypted_username + encrypted_password + link_token).",
    )
