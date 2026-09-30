"""
Shared FastAPI dependencies used across routers.

Provides authentication, credential resolution, rate limiting,
and password hashing utilities.
"""

import base64
import functools
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from typing import Optional

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from limits import parse as parse_rate_limit
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy.orm import Session

from src.auth_utils import decode_access_token
from src.config import get_settings
from src.crypto import _get_redis, decrypt_with_session_key, destroy_session_key
from src.database import Agent, ApiKey, User, as_utc, get_db, utcnow
from src.logging_config import get_logger
from src.models import MAX_PASSWORD_BYTES, ConnectRequest, scope_field

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
    # Fail open: if the rate-limit backend (Redis) is unreachable at request
    # time, allow the request rather than returning 500. Availability over a
    # transient throttling gap; the outage is still surfaced via /health.
    swallow_errors=True,
)

# ── Password Hashing ─────────────────────────────────────────────────────────

# bcrypt directly (passlib is unmaintained). Stored hashes are standard
# "$2b$" strings, so accounts hashed through passlib verify unchanged.
BCRYPT_ROUNDS = 12


def _bcrypt_hash(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode("ascii")


def _bcrypt_matches(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("ascii"))


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
    """Hash a password using bcrypt. Refuses passwords bcrypt would truncate."""
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise ValueError(f"Passwords longer than {MAX_PASSWORD_BYTES} bytes cannot be hashed with bcrypt.")
    return _bcrypt_hash(password)


@functools.lru_cache(maxsize=1)
def _dummy_password_hash() -> str:
    return _bcrypt_hash(secrets.token_urlsafe(24))


def verify_password(plain_password: str, hashed_password: Optional[str]) -> bool:
    """Verify a password against a bcrypt hash.

    Costs one bcrypt verification whether or not there is a hash to check, so
    an unknown user, an account without a password and a wrong password take
    the same time. A password longer than bcrypt's 72 bytes never matches
    (bcrypt would compare only its first 72 bytes).
    """
    if not hashed_password or len(plain_password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        try:
            _bcrypt_matches("timing-equalizer", _dummy_password_hash())
        except (ValueError, TypeError):
            pass
        return False
    try:
        return _bcrypt_matches(plain_password, hashed_password)
    except (ValueError, TypeError):
        return False


def hash_api_key(raw_key: str) -> str:
    """Digest under which an API key is stored and looked up.

    Plain SHA-256 is deliberate: keys are 256-bit random values
    (``secrets.token_urlsafe(32)``), so unlike a password there is no
    dictionary to try, and a slow or peppered hash would cost every request
    without making a stolen digest any easier to reverse. (Static analysers
    flag SHA-256 over a secret as a weak *password* hash; for a key with this
    much entropy it is the standard construction.)
    """
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def generate_api_key(prefix: str) -> tuple[str, str]:
    """A new raw API key (``<prefix><43 url-safe chars>``) and the digest to store."""
    raw_key = f"{prefix}{secrets.token_urlsafe(32)}"
    return raw_key, hash_api_key(raw_key)


# ── Scope and site restrictions ──────────────────────────────────────────────


def load_scope_set(scopes_json: Optional[str]) -> Optional[set[str]]:
    """The fields a stored scope list allows: ``None`` = unrestricted, empty = nothing.

    Only a NULL column means "every scope". Anything else that is not a JSON
    list of valid scopes (a bare string, garbage, a mistyped entry) allows
    nothing: a restriction that cannot be read must never read as none.
    """
    if scopes_json is None:
        return None
    try:
        values = json.loads(scopes_json)
        if not isinstance(values, list):
            raise ValueError("not a list")
        return {scope_field(value) for value in values}
    except (TypeError, ValueError):
        logger.warning("Unreadable stored scope list; allowing no scopes")
        return set()


def load_site_set(sites_json: Optional[str]) -> Optional[set[str]]:
    """The sites a stored site list allows: ``None`` = unrestricted, empty = nothing (fails closed)."""
    if sites_json is None:
        return None
    try:
        values = json.loads(sites_json)
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise ValueError("not a list of strings")
    except (TypeError, ValueError):
        logger.warning("Unreadable stored site list; allowing no sites")
        return set()
    return {value.strip().lower() for value in values if value.strip()}


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


def site_allowed_for_request(request: Request, site: Optional[str]) -> bool:
    """Whether the caller's API key or agent may reach ``site`` (always true for a login)."""
    auth_context = get_auth_context(request)
    if auth_context is None or auth_context.allowed_sites is None:
        return True
    return bool(site) and site.strip().lower() in auth_context.allowed_sites


def ensure_site_allowed_for_request(request: Request, site: str) -> Optional[AuthContext]:
    if not site_allowed_for_request(request, site):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This API key or agent is not allowed to access the requested site.",
        )
    return get_auth_context(request)


def constrain_requested_scopes(
    request: Request,
    requested_scopes: Optional[list[str]],
) -> Optional[list[str]]:
    auth_context = get_auth_context(request)
    if auth_context is None or auth_context.allowed_scopes is None:
        return requested_scopes

    if requested_scopes is None:
        return sorted(auth_context.allowed_scopes)

    try:
        requested_set = {scope_field(scope) for scope in requested_scopes}
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc))
    if requested_set - auth_context.allowed_scopes:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requested scopes exceed API key or agent permissions.",
        )

    return sorted(requested_set)


def reject_agent_caller(request: Request, action: str) -> None:
    """Refuse an action only the account owner may take (approving consent, say) to an agent key."""
    auth_context = get_auth_context(request)
    if auth_context is not None and auth_context.agent_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"An agent's API key cannot {action}; the account owner must.",
        )


# ── User Dependencies ─────────────────────────────────────────────────────────


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
    """FastAPI dependency: extract and validate the current user from an access token.

    The token must be an access token (not, say, a hosted-link launch token),
    its owner must be active, and it must carry the owner's current token
    version: a password reset, "sign out everywhere" or deactivation ends it.
    """
    try:
        payload = decode_access_token(token)
        user_id = int(payload["sub"])
    except jwt.ExpiredSignatureError:
        raise _unauthorized("Token has expired.")
    except (jwt.InvalidTokenError, KeyError, TypeError, ValueError):
        raise _unauthorized("Invalid authentication credentials.")

    user = db.get(User, user_id)
    if not user:
        raise _unauthorized("User not found.")
    if not user.is_active:
        raise _unauthorized("Account is disabled.")
    if payload["tv"] != (user.token_version or 0):
        raise _unauthorized("This session has ended. Sign in again.")
    return user


def get_admin_user(user: User = Depends(get_current_user)) -> User:
    """FastAPI dependency: require the current user to be an administrator."""
    if not getattr(user, "is_admin", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator access required.",
        )
    return user


def has_credentials(request: Request) -> bool:
    """Whether the request carries an API key or an Authorization header at all."""
    return bool(request.headers.get("x-api-key") or request.headers.get("authorization"))


def _enforce_agent_rate_limit(agent: Agent) -> None:
    """Apply the agent's own ``rate_limit`` ("N/period"), counted per agent across all its requests."""
    if not agent.rate_limit or not limiter.enabled:
        return
    try:
        item = parse_rate_limit(agent.rate_limit)
    except ValueError:
        logger.warning(
            "Agent has an unreadable rate_limit; applying the default limit",
            extra={"extra_data": {"agent_id": agent.id}},
        )
        item = parse_rate_limit(settings.rate_limit_default)
    try:
        allowed = limiter.limiter.hit(item, "agent", agent.id)
        reset_at = limiter.limiter.get_window_stats(item, "agent", agent.id).reset_time if not allowed else None
    except Exception as exc:  # like the route limits: an unreachable backend fails open
        logger.warning(f"Agent rate limit check failed: {exc}")
        return
    if not allowed:
        retry_after = max(1, int(reset_at - time.time())) if reset_at else 60
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Agent rate limit exceeded ({agent.rate_limit}).",
            headers={"Retry-After": str(retry_after)},
        )


def authenticate_api_key(request: Request, db: Session, raw_key: str) -> User:
    """Resolve an ``X-API-Key`` to its owner and record the key's restrictions on the request."""
    db_key = db.query(ApiKey).filter_by(key_hash=hash_api_key(raw_key), is_active=True).first()
    if not db_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key.")
    now = utcnow()
    if db_key.expires_at and now > as_utc(db_key.expires_at):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="API key has expired.")
    agent = db.query(Agent).filter_by(api_key_id=db_key.id).first()
    if agent and not agent.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Agent is inactive.")

    user = db.get(User, db_key.user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="API key owner not found.")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Account is disabled.")
    if agent:
        _enforce_agent_rate_limit(agent)

    db_key.last_used_at = now
    if agent:
        agent.last_active_at = now
    db.commit()
    _store_auth_context(
        request,
        AuthContext(
            user=user,
            auth_method="api_key",
            api_key_id=db_key.id,
            agent_id=agent.id if agent else None,
            allowed_scopes=_combine_scope_sets(
                load_scope_set(db_key.scopes),
                load_scope_set(agent.allowed_scopes) if agent else None,
            ),
            allowed_sites=load_site_set(agent.allowed_sites) if agent else None,
        ),
    )
    return user


def get_current_user_or_api_key(request: Request, db: Session = Depends(get_db)) -> User:
    """FastAPI dependency: authenticate via an X-API-Key header OR a Bearer access token."""
    api_key = request.headers.get("x-api-key")
    if api_key:
        return authenticate_api_key(request, db, api_key)

    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise _unauthorized("Missing authentication.")
    user = get_current_user(token=token.strip(), db=db)
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
