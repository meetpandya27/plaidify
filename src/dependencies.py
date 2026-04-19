"""
Shared FastAPI dependencies used across routers.

Provides authentication, credential resolution, rate limiting,
and password hashing utilities.
"""

import base64
import hashlib
import json
from dataclasses import dataclass
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
from src.crypto import _get_redis, decrypt_with_session_key, destroy_session_key
from src.database import Agent, ApiKey, User, get_db
from src.exceptions import InvalidTokenError
from src.logging_config import get_logger
from src.models import ConnectRequest

settings = get_settings()
logger = get_logger("dependencies")

# ── Rate Limiter ──────────────────────────────────────────────────────────────

_limiter_storage = None
if settings.rate_limit_enabled:
    if settings.redis_url:
        try:
            redis_client = _get_redis()
            if redis_client is None:
                raise RuntimeError("Redis client unavailable")

            from limits.storage import RedisStorage

            _limiter_storage = RedisStorage(settings.redis_url)
            logger.info("Rate limiter using Redis storage")
        except Exception as exc:
            if settings.env == "production":
                raise RuntimeError("Redis-backed rate limiting is required in production.") from exc

            logger.warning("Failed to connect to Redis for rate limiter, using in-memory")
    elif settings.env == "production":
        raise RuntimeError("REDIS_URL is required in production when rate limiting is enabled.")

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[settings.rate_limit_default] if settings.rate_limit_enabled else [],
    enabled=settings.rate_limit_enabled,
    storage_uri=settings.redis_url if _limiter_storage else "memory://",
)

# ── Password Hashing ─────────────────────────────────────────────────────────

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=12, bcrypt__ident="2b")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token")


@dataclass
class AuthContext:
    user: User
    auth_method: str
    api_key_id: Optional[str] = None
    agent_id: Optional[str] = None
    allowed_scopes: Optional[set[str]] = None
    allowed_sites: Optional[set[str]] = None


def get_password_hash(password: str) -> str:
    """Hash a password using bcrypt."""
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plain password against a bcrypt hash."""
    return pwd_context.verify(plain_password, hashed_password)


def _normalize_scope_name(scope: str) -> str:
    value = str(scope).strip()
    if ":" in value:
        value = value.split(":", 1)[1]
    return value.strip()


def _load_scope_set(scopes_json: Optional[str]) -> Optional[set[str]]:
    if not scopes_json:
        return None

    try:
        values = json.loads(scopes_json)
    except (TypeError, ValueError):
        return None

    if not isinstance(values, list):
        return None

    normalized = {_normalize_scope_name(value) for value in values if _normalize_scope_name(value)}
    return normalized or set()


def _load_site_set(sites_json: Optional[str]) -> Optional[set[str]]:
    if not sites_json:
        return None

    try:
        values = json.loads(sites_json)
    except (TypeError, ValueError):
        return None

    if not isinstance(values, list):
        return None

    normalized = {str(value).strip().lower() for value in values if str(value).strip()}
    return normalized or set()


def _combine_scope_sets(
    left: Optional[set[str]],
    right: Optional[set[str]],
) -> Optional[set[str]]:
    if left is None:
        return right
    if right is None:
        return left
    return left & right


def _store_auth_context(request: Request, auth_context: AuthContext) -> None:
    request.state.auth_context = auth_context


def get_auth_context(request: Request) -> Optional[AuthContext]:
    return getattr(request.state, "auth_context", None)


def get_principal_allowed_scopes(request: Request) -> Optional[set[str]]:
    auth_context = get_auth_context(request)
    return None if auth_context is None else auth_context.allowed_scopes


def ensure_site_allowed_for_request(request: Request, site: str) -> Optional[AuthContext]:
    auth_context = get_auth_context(request)
    if auth_context is None or auth_context.allowed_sites is None:
        return auth_context

    if site.strip().lower() not in auth_context.allowed_sites:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This API key or agent is not allowed to access the requested site.",
        )

    return auth_context


def constrain_requested_scopes(
    request: Request,
    requested_scopes: Optional[list[str]],
) -> Optional[list[str]]:
    auth_context = get_auth_context(request)
    if auth_context is None or auth_context.allowed_scopes is None:
        return requested_scopes

    if requested_scopes is None:
        return sorted(auth_context.allowed_scopes)

    requested_set = {
        normalized for normalized in (_normalize_scope_name(scope) for scope in requested_scopes) if normalized
    }
    if requested_set - auth_context.allowed_scopes:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requested scopes exceed API key or agent permissions.",
        )

    return sorted(requested_set)


# ── User Dependencies ─────────────────────────────────────────────────────────


def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
    """FastAPI dependency: extract and validate the current user from a JWT."""
    try:
        payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
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


def get_current_user_or_api_key(request: Request, db: Session = Depends(get_db)) -> User:
    """FastAPI dependency: authenticate via JWT Bearer token OR X-API-Key header."""
    # Check for API key first
    api_key = request.headers.get("x-api-key")
    if api_key:
        key_hash = hashlib.sha256(api_key.encode()).hexdigest()
        db_key = db.query(ApiKey).filter_by(key_hash=key_hash, is_active=True).first()
        if not db_key:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key.")
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
        agent = db.query(Agent).filter_by(api_key_id=db_key.id).first()
        if agent and not agent.is_active:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Agent is inactive.",
            )

        user = db.query(User).filter(User.id == db_key.user_id).first()
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="API key owner not found.",
            )

        if agent:
            agent.last_active_at = datetime.now(timezone.utc)

        db.commit()
        _store_auth_context(
            request,
            AuthContext(
                user=user,
                auth_method="api_key",
                api_key_id=db_key.id,
                agent_id=agent.id if agent else None,
                allowed_scopes=_combine_scope_sets(
                    _load_scope_set(db_key.scopes),
                    _load_scope_set(agent.allowed_scopes) if agent else None,
                ),
                allowed_sites=_load_site_set(agent.allowed_sites) if agent else None,
            ),
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
    user = get_current_user(token=token, db=db)
    _store_auth_context(request, AuthContext(user=user, auth_method="jwt"))
    return user


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
            raise HTTPException(status_code=400, detail="Failed to decrypt credentials.")

    if body.username and body.password:
        return body.username, body.password

    raise HTTPException(
        status_code=422,
        detail="Provide either (username + password) or (encrypted_username + encrypted_password + link_token).",
    )
