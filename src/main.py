"""
Plaidify API Server — FastAPI application entry point.

Provides endpoints for:
- Site connections (connect, create_link, submit_credentials, fetch_data)
- User authentication (register, login, OAuth2, profile)
- Link and token management (CRUD)
- System health checks
"""

import uuid
import asyncio
import hashlib
import hmac
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional

import httpx
import jwt
from fastapi import FastAPI, HTTPException, Depends, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles
from passlib.context import CryptContext
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from sqlalchemy.orm import Session
from sse_starlette.sse import EventSourceResponse

from src.config import get_settings
from src.database import (
    init_db,
    get_db,
    User,
    Link,
    AccessToken,
    RefreshToken,
    Webhook,
    PublicToken,
    ConsentRequest,
    ConsentGrant,
    BlueprintRecord,
    encrypt_credential,
    decrypt_credential,
    create_user_dek,
    encrypt_credential_for_user,
    decrypt_credential_for_user,
    ensure_user_dek,
    get_current_key_version,
)
from src.exceptions import PlaidifyError, InvalidTokenError, UserNotFoundError, MFARequiredError
from src.logging_config import setup_logging, get_logger
from src.models import (
    ConnectRequest,
    ConnectResponse,
    UserRegisterRequest,
    TokenResponse,
    RefreshTokenRequest,
    OAuth2LoginRequest,
    UserProfileResponse,
)
from src.core.engine import connect_to_site, submit_mfa_code
from src.core.browser_pool import get_browser_pool, shutdown_browser_pool
from src.core.mfa_manager import get_mfa_manager
from src.crypto import (
    generate_keypair,
    get_public_key,
    decrypt_with_session_key,
    destroy_session_key,
    cleanup_expired_keys,
)

# ── Configuration ─────────────────────────────────────────────────────────────

settings = get_settings()
logger = get_logger("api")

# ── Rate Limiter ─────────────────────────────────────────────────────────────────

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[settings.rate_limit_default] if settings.rate_limit_enabled else [],
    enabled=settings.rate_limit_enabled,
)

# ── Application Lifecycle ─────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown lifecycle."""
    setup_logging(level=settings.log_level, log_format=settings.log_format)
    logger.info(
        "Starting Plaidify",
        extra={"extra_data": {"version": settings.app_version, "debug": settings.debug}},
    )
    init_db()

    # Pre-warm browser pool (lazy — actual start happens on first connection)
    logger.info("Browser engine ready (Playwright, lazy-start)")

    yield

    # Shutdown browser pool
    logger.info("Shutting down browser pool...")
    await shutdown_browser_pool()
    logger.info("Shutting down Plaidify")


# ── App Factory ───────────────────────────────────────────────────────────────

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="Open-source API for authenticated web data — for developers and AI agents.",
    lifespan=lifespan,
)

# Rate limiter state + error handler
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ── CORS ──────────────────────────────────────────────────────────────────────

_cors_origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]

if settings.env == "production" and "*" in _cors_origins:
    logger.critical(
        "CORS wildcard (*) is not allowed in production. "
        "Set CORS_ORIGINS to specific origins (e.g. 'https://app.example.com')."
    )
    import sys
    sys.exit(1)

if "*" in _cors_origins:
    logger.warning(
        "CORS wildcard (*) is enabled. This is acceptable in development "
        "but must be restricted before production deployment."
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Security Headers Middleware ───────────────────────────────────────────────

@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    """Add security headers to every response."""
    response = await call_next(request)

    # Prevent MIME-type sniffing
    response.headers["X-Content-Type-Options"] = "nosniff"
    # Prevent clickjacking (allow framing from same origin for Link widget)
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    # Enable XSS filter in older browsers
    response.headers["X-XSS-Protection"] = "1; mode=block"
    # Restrict referrer information
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    # Restrict permissions (camera, microphone, etc.)
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"

    # HSTS — only in production or when explicitly enabled
    if settings.enforce_https or settings.env == "production":
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )

    return response


# ── HTTPS Redirect Middleware ─────────────────────────────────────────────────

if settings.enforce_https or settings.env == "production":
    from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware
    # Note: In most deployments, TLS termination happens at the reverse proxy
    # (nginx, Cloudflare, ALB). This middleware is a safety net for direct access.
    # It checks the X-Forwarded-Proto header, so it works behind a proxy.
    logger.info("HTTPS enforcement enabled")


# Static files (frontend UI)
try:
    app.mount("/ui", StaticFiles(directory="frontend", html=True), name="frontend")
except Exception:
    logger.warning("Frontend directory not found, /ui will not be served")

# ── Auth Utilities ────────────────────────────────────────────────────────────

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token")


def create_access_token(data: dict, expires_delta: Optional[int] = None) -> str:
    """Create a signed JWT access token."""
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=expires_delta or settings.jwt_access_token_expire_minutes
    )
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def create_refresh_token(user_id: int, db: Session) -> str:
    """Create a cryptographically random refresh token and store it in the database."""
    token = secrets.token_urlsafe(48)
    expires_at = datetime.now(timezone.utc) + timedelta(
        minutes=settings.jwt_refresh_token_expire_minutes
    )
    db_token = RefreshToken(token=token, user_id=user_id, expires_at=expires_at)
    db.add(db_token)
    db.commit()
    return token


def _issue_token_pair(user_id: int, db: Session) -> dict:
    """Issue an access + refresh token pair for a user."""
    access_token = create_access_token({"sub": str(user_id)})
    refresh_token = create_refresh_token(user_id, db)
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
    }


def get_password_hash(password: str) -> str:
    """Hash a password using bcrypt."""
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plain password against a bcrypt hash."""
    return pwd_context.verify(plain_password, hashed_password)


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


# ── Global Exception Handler ─────────────────────────────────────────────────


@app.exception_handler(PlaidifyError)
async def plaidify_error_handler(request: Request, exc: PlaidifyError) -> JSONResponse:
    """Catch all PlaidifyError subclasses and return structured JSON."""
    logger.error(
        exc.message,
        extra={"extra_data": {"status_code": exc.status_code, "path": request.url.path}},
    )
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.message},
    )


# ── System Endpoints ──────────────────────────────────────────────────────────


@app.get("/")
async def root():
    """Root endpoint with welcome message."""
    return {
        "message": f"Welcome to {settings.app_name}!",
        "version": settings.app_version,
        "docs": "/docs",
    }


@app.get("/health")
async def health(db: Session = Depends(get_db)):
    """
    Health check endpoint.

    Returns system status, version, and database connectivity.
    """
    db_healthy = True
    try:
        db.execute("SELECT 1" if hasattr(db, "execute") else None)  # type: ignore
    except Exception:
        db_healthy = False

    # Simple DB health check via query
    try:
        from sqlalchemy import text
        db.execute(text("SELECT 1"))
        db_healthy = True
    except Exception:
        db_healthy = False

    overall = "healthy" if db_healthy else "degraded"
    status_code = 200 if db_healthy else 503

    return JSONResponse(
        status_code=status_code,
        content={
            "status": overall,
            "version": settings.app_version,
            "checks": {
                "database": "ok" if db_healthy else "error",
            },
        },
    )


@app.get("/status")
async def app_status():
    """Simple status check."""
    return {"status": "API is running", "version": settings.app_version}


# ── Blueprint Discovery ──────────────────────────────────────────────────────


@app.get("/blueprints")
async def list_blueprints():
    """
    List all available blueprints.

    Returns the name and basic info for each blueprint in the connectors directory.
    """
    from pathlib import Path
    from src.core.blueprint import load_blueprint

    connectors_path = Path(settings.connectors_dir).resolve()
    blueprints = []

    if connectors_path.is_dir():
        for f in sorted(connectors_path.glob("*.json")):
            try:
                bp = load_blueprint(f)
                blueprints.append({
                    "site": f.stem,
                    "name": bp.name,
                    "domain": bp.domain,
                    "tags": bp.tags,
                    "has_mfa": bp.mfa is not None,
                    "schema_version": bp.schema_version,
                })
            except Exception as e:
                logger.warning(f"Failed to load blueprint {f.name}: {e}")

    return {"blueprints": blueprints, "count": len(blueprints)}


@app.get("/blueprints/{site}")
async def get_blueprint_info(site: str):
    """
    Get detailed info about a specific blueprint.

    Does NOT include auth steps or selectors (security).
    """
    from pathlib import Path
    from src.core.blueprint import load_blueprint

    blueprint_path = Path(settings.connectors_dir).resolve() / f"{site}.json"
    if not blueprint_path.exists():
        raise HTTPException(status_code=404, detail=f"Blueprint not found: {site}")

    bp = load_blueprint(blueprint_path)
    return {
        "name": bp.name,
        "domain": bp.domain,
        "tags": bp.tags,
        "has_mfa": bp.mfa is not None,
        "extract_fields": list(bp.extract.keys()),
        "schema_version": bp.schema_version,
        "rate_limit": bp.rate_limit.model_dump() if bp.rate_limit else None,
    }


# ── Blueprint Registry ─────────────────────────────────────────────────────────

_VALID_QUALITY_TIERS = {"community", "tested", "certified"}


@app.post("/registry/publish")
async def registry_publish(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Publish a blueprint to the registry.

    The full blueprint JSON is validated, and metadata is extracted and stored.
    If a blueprint with the same site name exists and belongs to the same user,
    it is updated (version bump).
    """
    import json as json_mod
    from src.core.blueprint import load_blueprint_from_dict

    body = await request.json()
    blueprint_json = body.get("blueprint")
    description = body.get("description", "")

    if not blueprint_json:
        raise HTTPException(status_code=422, detail="'blueprint' field is required (the full blueprint JSON object).")

    if isinstance(blueprint_json, str):
        try:
            blueprint_json = json_mod.loads(blueprint_json)
        except json_mod.JSONDecodeError:
            raise HTTPException(status_code=422, detail="'blueprint' is not valid JSON.")

    # Validate the blueprint by parsing it
    try:
        bp = load_blueprint_from_dict(blueprint_json)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Invalid blueprint: {e}")

    site = blueprint_json.get("domain", "").replace(".", "_").replace(" ", "_").lower()
    if not site:
        site = bp.name.lower().replace(" ", "_")

    # Check for existing blueprint
    existing = db.query(BlueprintRecord).filter_by(site=site).first()
    if existing:
        if existing.published_by != user.id:
            raise HTTPException(status_code=403, detail="A blueprint for this site already exists and belongs to another user.")
        # Update existing
        existing.name = bp.name
        existing.domain = bp.domain
        existing.description = description or existing.description
        existing.schema_version = bp.schema_version
        existing.tags = json_mod.dumps(bp.tags) if bp.tags else "[]"
        existing.has_mfa = bp.mfa is not None
        existing.blueprint_json = json_mod.dumps(blueprint_json)
        existing.extract_fields = json_mod.dumps(list(bp.extract.keys()))
        existing.updated_at = datetime.now(timezone.utc)
        # Bump version
        parts = existing.version.split(".")
        parts[-1] = str(int(parts[-1]) + 1)
        existing.version = ".".join(parts)
        db.commit()
        logger.info("Blueprint updated in registry", extra={"extra_data": {"site": site, "version": existing.version}})
        return {"status": "updated", "site": site, "version": existing.version}

    # Create new
    record = BlueprintRecord(
        name=bp.name,
        site=site,
        domain=bp.domain,
        description=description,
        author=user.username or user.email,
        version="1.0.0",
        schema_version=bp.schema_version,
        tags=json_mod.dumps(bp.tags) if bp.tags else "[]",
        has_mfa=bp.mfa is not None,
        quality_tier="community",
        blueprint_json=json_mod.dumps(blueprint_json),
        extract_fields=json_mod.dumps(list(bp.extract.keys())),
        published_by=user.id,
    )
    db.add(record)
    db.commit()
    logger.info("Blueprint published to registry", extra={"extra_data": {"site": site}})
    return {"status": "published", "site": site, "version": "1.0.0", "quality_tier": "community"}


@app.get("/registry/search")
async def registry_search(
    q: Optional[str] = None,
    tag: Optional[str] = None,
    tier: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Search the blueprint registry by name, domain, tag, or quality tier."""
    import json as json_mod

    query = db.query(BlueprintRecord)

    if q:
        search_term = f"%{q}%"
        query = query.filter(
            (BlueprintRecord.name.ilike(search_term))
            | (BlueprintRecord.domain.ilike(search_term))
            | (BlueprintRecord.site.ilike(search_term))
            | (BlueprintRecord.description.ilike(search_term))
        )
    if tag:
        query = query.filter(BlueprintRecord.tags.ilike(f'%"{tag}"%'))
    if tier:
        if tier not in _VALID_QUALITY_TIERS:
            raise HTTPException(status_code=422, detail=f"Invalid tier. Must be one of: {', '.join(_VALID_QUALITY_TIERS)}")
        query = query.filter_by(quality_tier=tier)

    results = query.order_by(BlueprintRecord.downloads.desc()).all()
    return {
        "results": [
            {
                "site": r.site,
                "name": r.name,
                "domain": r.domain,
                "description": r.description,
                "author": r.author,
                "version": r.version,
                "schema_version": r.schema_version,
                "tags": json_mod.loads(r.tags) if r.tags else [],
                "has_mfa": r.has_mfa,
                "quality_tier": r.quality_tier,
                "extract_fields": json_mod.loads(r.extract_fields) if r.extract_fields else [],
                "downloads": r.downloads,
            }
            for r in results
        ],
        "count": len(results),
    }


@app.get("/registry/{site_name}")
async def registry_get(
    site_name: str,
    db: Session = Depends(get_db),
):
    """Download a blueprint from the registry.

    Increments the download counter.
    """
    import json as json_mod

    record = db.query(BlueprintRecord).filter_by(site=site_name).first()
    if not record:
        raise HTTPException(status_code=404, detail=f"Blueprint '{site_name}' not found in registry.")

    record.downloads = (record.downloads or 0) + 1
    db.commit()

    return {
        "site": record.site,
        "name": record.name,
        "domain": record.domain,
        "description": record.description,
        "author": record.author,
        "version": record.version,
        "schema_version": record.schema_version,
        "tags": json_mod.loads(record.tags) if record.tags else [],
        "has_mfa": record.has_mfa,
        "quality_tier": record.quality_tier,
        "extract_fields": json_mod.loads(record.extract_fields) if record.extract_fields else [],
        "downloads": record.downloads,
        "blueprint": json_mod.loads(record.blueprint_json),
    }


@app.delete("/registry/{site_name}")
async def registry_delete(
    site_name: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Remove a blueprint from the registry (owner only)."""
    record = db.query(BlueprintRecord).filter_by(site=site_name).first()
    if not record:
        raise HTTPException(status_code=404, detail=f"Blueprint '{site_name}' not found in registry.")
    if record.published_by != user.id:
        raise HTTPException(status_code=403, detail="Only the blueprint owner can delete it.")
    db.delete(record)
    db.commit()
    return {"status": "deleted", "site": site_name}


# ── Connection Endpoints ──────────────────────────────────────────────────────


def _resolve_credentials(body: ConnectRequest) -> tuple[str, str]:
    """Extract plaintext credentials from a ConnectRequest.

    Supports both plaintext and client-side encrypted credentials.
    If encrypted fields are present, they are decrypted using the ephemeral
    session key associated with the link_token.

    Returns:
        (username, password) as plaintext strings.

    Raises:
        HTTPException: If credentials are missing or decryption fails.
    """
    import base64

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


@app.get("/encryption/public_key/{link_token}")
async def get_encryption_key(link_token: str):
    """Get the ephemeral public key for a link session.

    Clients use this for the one-shot /connect flow: create a temporary
    link_token just to get the public key, then encrypt credentials before
    calling /connect.
    """
    pub_key = get_public_key(link_token)
    if not pub_key:
        raise HTTPException(status_code=404, detail="No encryption key found for this link token.")
    return {"link_token": link_token, "public_key": pub_key}


@app.post("/encryption/session")
async def create_encryption_session():
    """Create a temporary encryption session for one-shot /connect usage.

    Returns a link_token and public key without requiring authentication.
    The link_token is only used for credential encryption — not stored in DB.
    """
    link_token = str(uuid.uuid4())
    public_key_pem = generate_keypair(link_token)
    return {"link_token": link_token, "public_key": public_key_pem}


@app.post("/connect", response_model=ConnectResponse)
@limiter.limit(settings.rate_limit_connect)
async def connect(request: Request, body: ConnectRequest):
    """
    Connect to a site and extract data in a single step.

    This is the simplest integration path — send credentials, get data back.
    Credentials can be sent encrypted (recommended) or plaintext.
    If MFA is required, returns status='mfa_required' with a session_id.
    The client then calls POST /mfa/submit with the code.
    """
    username, password = _resolve_credentials(body)
    try:
        response_data = await connect_to_site(
            site=body.site,
            username=username,
            password=password,
            extract_fields=body.extract_fields,
        )
        return response_data
    except MFARequiredError as e:
        return ConnectResponse(
            status="mfa_required",
            mfa_type=e.mfa_type,
            session_id=e.session_id,
            metadata={"message": e.message},
        )


@app.post("/disconnect")
async def disconnect():
    """Disconnect / end a session."""
    return {"status": "disconnected"}


# ── MFA Endpoints ─────────────────────────────────────────────────────────────


@app.post("/mfa/submit")
async def mfa_submit(session_id: str, code: str):
    """
    Submit an MFA code for a pending session.

    After a connection returns status 'mfa_required', the client retrieves
    the code from the user and submits it here.
    """
    result = await submit_mfa_code(session_id, code)
    return result


@app.get("/mfa/status/{session_id}")
async def mfa_status(session_id: str):
    """
    Check the status of an MFA session.

    Returns session metadata (type, question text, etc.) or 404 if expired.
    """
    mfa_manager = get_mfa_manager()
    session = await mfa_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="MFA session not found or expired.")
    return {
        "session_id": session.session_id,
        "site": session.site,
        "mfa_type": session.mfa_type,
        "metadata": session.metadata,
    }


# ── Link Token Flow ───────────────────────────────────────────────────────────


@app.post("/create_link")
async def create_link(
    request: Request,
    site: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Create a link token for a specific site.

    Step 1 of the Plaid-style multi-step flow.
    Optionally accepts a JSON body with ``scopes`` — a list of field names
    or scope strings (e.g. ``["balance", "transactions"]``) that will be
    enforced on data retrieval. If omitted, all fields are allowed.
    """
    import json as json_mod

    # Parse optional scopes from JSON body
    scopes = None
    try:
        body = await request.json()
        scopes = body.get("scopes") if body else None
    except Exception:
        pass  # No body or non-JSON body is fine

    link_token = str(uuid.uuid4())
    new_link = Link(link_token=link_token, site=site, user_id=user.id)
    db.add(new_link)
    db.commit()

    # Generate ephemeral RSA keypair for client-side encryption
    public_key_pem = generate_keypair(link_token)

    logger.info("Link created", extra={"extra_data": {"site": site, "user_id": user.id}})
    result = {"link_token": link_token, "public_key": public_key_pem}
    if scopes is not None:
        # Store scopes on the link for propagation to access tokens
        # We stash them in a lightweight in-memory map keyed by link_token
        _link_scopes[link_token] = json_mod.dumps(scopes)
        result["scopes"] = scopes
    return result


@app.post("/submit_credentials")
async def submit_credentials(
    link_token: str,
    username: Optional[str] = None,
    password: Optional[str] = None,
    encrypted_username: Optional[str] = None,
    encrypted_password: Optional[str] = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Submit credentials for a link token.

    Step 2 of the multi-step flow. Credentials are encrypted at rest.
    Accepts plaintext or RSA-OAEP encrypted credentials.
    """
    existing_link = db.query(Link).filter_by(link_token=link_token, user_id=user.id).first()
    if not existing_link:
        raise HTTPException(status_code=404, detail="Invalid link token.")

    # Resolve credentials — encrypted takes precedence
    import base64
    if encrypted_username and encrypted_password:
        try:
            plain_user = decrypt_with_session_key(link_token, base64.b64decode(encrypted_username))
            plain_pass = decrypt_with_session_key(link_token, base64.b64decode(encrypted_password))
            destroy_session_key(link_token)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    elif username and password:
        plain_user, plain_pass = username, password
    else:
        raise HTTPException(
            status_code=422,
            detail="Provide either (username + password) or (encrypted_username + encrypted_password).",
        )

    encrypted_username_stored = encrypt_credential_for_user(user, plain_user)
    encrypted_password_stored = encrypt_credential_for_user(user, plain_pass)
    access_token = str(uuid.uuid4())

    # Inherit scopes from the link creation step (if any)
    token_scopes = _link_scopes.pop(link_token, None)

    new_token = AccessToken(
        token=access_token,
        link_token=link_token,
        username_encrypted=encrypted_username_stored,
        password_encrypted=encrypted_password_stored,
        scopes=token_scopes,
        user_id=user.id,
        key_version=get_current_key_version(),
    )
    db.add(new_token)
    db.commit()
    logger.info("Credentials submitted", extra={"extra_data": {"link_token": link_token, "user_id": user.id}})
    result = {"access_token": access_token}
    if token_scopes:
        import json as json_mod
        result["scopes"] = json_mod.loads(token_scopes)
    return result


@app.post("/submit_instructions")
async def submit_instructions(
    access_token: str,
    instructions: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Store processing instructions for an access token."""
    token_record = db.query(AccessToken).filter_by(token=access_token, user_id=user.id).first()
    if not token_record:
        raise HTTPException(status_code=401, detail="Invalid access token.")
    token_record.instructions = instructions
    db.commit()
    return {"status": "Instructions stored successfully"}


@app.get("/fetch_data")
async def fetch_data(
    access_token: str,
    consent_token: Optional[str] = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Fetch data using a previously submitted access token.

    Step 3 of the multi-step flow. Decrypts credentials, connects to the site,
    and returns extracted data.

    If a consent_token is provided, the returned data is filtered to only the
    scopes granted by that consent.
    """
    token_record = db.query(AccessToken).filter_by(token=access_token, user_id=user.id).first()
    if not token_record:
        raise HTTPException(status_code=401, detail="Invalid access token.")

    site = db.query(Link).filter_by(link_token=token_record.link_token, user_id=user.id).first()
    if not site:
        raise HTTPException(status_code=401, detail="Linked data not found.")

    # Validate consent token if provided
    allowed_fields = None
    if consent_token:
        import json as json_mod
        grant = db.query(ConsentGrant).filter_by(token=consent_token, user_id=user.id).first()
        if not grant:
            raise HTTPException(status_code=401, detail="Invalid consent token.")
        if grant.revoked:
            raise HTTPException(status_code=403, detail="Consent has been revoked.")
        grant_expires = grant.expires_at
        if grant_expires.tzinfo is None:
            grant_expires = grant_expires.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > grant_expires:
            raise HTTPException(status_code=403, detail="Consent token has expired.")
        if grant.access_token != access_token:
            raise HTTPException(status_code=403, detail="Consent token does not match the access token.")
        scopes = json_mod.loads(grant.scopes)
        # Extract field names from scopes like "read:current_bill" -> "current_bill"
        allowed_fields = set()
        for scope in scopes:
            if ":" in scope:
                allowed_fields.add(scope.split(":", 1)[1])
            else:
                allowed_fields.add(scope)

    # Also check access token scopes
    token_allowed = None
    if token_record.scopes:
        import json as json_mod_scopes
        token_scopes_list = json_mod_scopes.loads(token_record.scopes)
        token_allowed = set()
        for scope in token_scopes_list:
            if ":" in scope:
                token_allowed.add(scope.split(":", 1)[1])
            else:
                token_allowed.add(scope)

    # Merge: use the most restrictive set of allowed fields
    if allowed_fields is not None and token_allowed is not None:
        allowed_fields = allowed_fields & token_allowed
    elif token_allowed is not None:
        allowed_fields = token_allowed

    username = decrypt_credential_for_user(user, token_record.username_encrypted)
    password = decrypt_credential_for_user(user, token_record.password_encrypted)
    user_instructions = token_record.instructions

    response_data = await connect_to_site(site.site, username, password)
    if user_instructions:
        response_data["instructions_applied"] = user_instructions

    # Filter data by scopes if applicable (consent + access token)
    if allowed_fields is not None and "data" in response_data:
        response_data["data"] = {
            k: v for k, v in response_data["data"].items() if k in allowed_fields
        }
        response_data["scopes_applied"] = sorted(allowed_fields)

    return response_data


# ── Consent Engine ────────────────────────────────────────────────────────────

_MAX_CONSENT_DURATION = 30 * 24 * 3600  # 30 days in seconds


@app.post("/consent/request")
async def consent_request(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Request user consent for scoped, time-limited data access.

    An AI agent calls this endpoint to ask for permission to read specific fields.
    The returned request_id is used by the user to approve or deny.
    """
    import json as json_mod

    body = await request.json()
    agent_name = body.get("agent_name")
    scopes = body.get("scopes")
    access_token_str = body.get("access_token")
    duration = body.get("duration_seconds", 3600)

    if not agent_name:
        raise HTTPException(status_code=422, detail="'agent_name' is required.")
    if not scopes or not isinstance(scopes, list):
        raise HTTPException(status_code=422, detail="'scopes' must be a non-empty list of scope strings.")
    if not access_token_str:
        raise HTTPException(status_code=422, detail="'access_token' is required.")
    if duration > _MAX_CONSENT_DURATION:
        raise HTTPException(status_code=422, detail=f"Duration cannot exceed {_MAX_CONSENT_DURATION} seconds (30 days).")
    if duration < 60:
        raise HTTPException(status_code=422, detail="Duration must be at least 60 seconds.")

    # Verify the access token belongs to this user
    token_record = db.query(AccessToken).filter_by(token=access_token_str, user_id=user.id).first()
    if not token_record:
        raise HTTPException(status_code=401, detail="Invalid access token.")

    request_id = f"creq-{uuid.uuid4()}"
    cr = ConsentRequest(
        id=request_id,
        agent_name=agent_name,
        agent_description=body.get("agent_description", ""),
        scopes=json_mod.dumps(scopes),
        duration_seconds=duration,
        access_token=access_token_str,
        user_id=user.id,
    )
    db.add(cr)
    db.commit()

    logger.info("Consent requested", extra={"extra_data": {"request_id": request_id, "agent": agent_name}})
    return {
        "request_id": request_id,
        "agent_name": agent_name,
        "scopes": scopes,
        "duration_seconds": duration,
        "status": "pending",
    }


@app.post("/consent/{request_id}/approve")
async def consent_approve(
    request_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Approve a consent request. Creates a time-limited consent grant token."""
    import json as json_mod

    cr = db.query(ConsentRequest).filter_by(id=request_id, user_id=user.id).first()
    if not cr:
        raise HTTPException(status_code=404, detail="Consent request not found.")
    if cr.status != "pending":
        raise HTTPException(status_code=409, detail=f"Consent request is already '{cr.status}'.")

    cr.status = "approved"
    consent_token = f"consent-{uuid.uuid4()}"
    grant = ConsentGrant(
        token=consent_token,
        consent_request_id=request_id,
        scopes=cr.scopes,
        access_token=cr.access_token,
        user_id=user.id,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=cr.duration_seconds),
    )
    db.add(grant)
    db.commit()

    logger.info("Consent approved", extra={"extra_data": {"request_id": request_id, "consent_token": consent_token}})
    return {
        "consent_token": consent_token,
        "scopes": json_mod.loads(cr.scopes),
        "expires_at": grant.expires_at.isoformat(),
        "status": "approved",
    }


@app.post("/consent/{request_id}/deny")
async def consent_deny(
    request_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Deny a consent request."""
    cr = db.query(ConsentRequest).filter_by(id=request_id, user_id=user.id).first()
    if not cr:
        raise HTTPException(status_code=404, detail="Consent request not found.")
    if cr.status != "pending":
        raise HTTPException(status_code=409, detail=f"Consent request is already '{cr.status}'.")

    cr.status = "denied"
    db.commit()

    logger.info("Consent denied", extra={"extra_data": {"request_id": request_id}})
    return {"request_id": request_id, "status": "denied"}


@app.get("/consent")
async def list_consents(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List all active consent grants for the current user."""
    import json as json_mod

    grants = (
        db.query(ConsentGrant)
        .filter_by(user_id=user.id, revoked=False)
        .all()
    )
    now = datetime.now(timezone.utc)
    results = []
    for g in grants:
        expires = g.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if now > expires:
            continue  # Skip expired grants
        req = db.query(ConsentRequest).filter_by(id=g.consent_request_id).first()
        results.append({
            "consent_token": g.token,
            "agent_name": req.agent_name if req else "unknown",
            "scopes": json_mod.loads(g.scopes),
            "access_token": g.access_token,
            "expires_at": expires.isoformat(),
            "created_at": g.created_at.isoformat() if g.created_at else None,
        })
    return {"grants": results, "count": len(results)}


@app.delete("/consent/{consent_token}")
async def revoke_consent(
    consent_token: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Revoke a consent grant immediately."""
    grant = db.query(ConsentGrant).filter_by(token=consent_token, user_id=user.id).first()
    if not grant:
        raise HTTPException(status_code=404, detail="Consent grant not found.")
    if grant.revoked:
        raise HTTPException(status_code=409, detail="Consent already revoked.")

    grant.revoked = True
    db.commit()

    logger.info("Consent revoked", extra={"extra_data": {"consent_token": consent_token}})
    return {"status": "revoked", "consent_token": consent_token}


# ── Auth Endpoints ────────────────────────────────────────────────────────────


@app.post("/auth/register", response_model=TokenResponse)
@limiter.limit("3/minute")
def register_user(request: Request, body: UserRegisterRequest, db: Session = Depends(get_db)):
    """Register a new user account."""
    existing = db.query(User).filter(
        (User.username == body.username) | (User.email == body.email)
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="Username or email already registered")

    hashed_pw = get_password_hash(body.password)
    user = User(
        username=body.username,
        email=body.email,
        hashed_password=hashed_pw,
        encrypted_dek=create_user_dek(),
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    logger.info("User registered", extra={"extra_data": {"user_id": user.id}})
    return _issue_token_pair(user.id, db)


@app.post("/auth/token", response_model=TokenResponse)
@limiter.limit(settings.rate_limit_auth)
def login_user(request: Request, form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    """Log in and receive a JWT access token."""
    user = db.query(User).filter(User.username == form_data.username).first()
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(status_code=400, detail="Incorrect username or password")

    # Lazy migration: ensure existing users get a DEK
    ensure_user_dek(user, db)

    return _issue_token_pair(user.id, db)


@app.get("/auth/me", response_model=UserProfileResponse)
def get_profile(user: User = Depends(get_current_user)):
    """Get the current user's profile."""
    return UserProfileResponse(
        id=user.id,
        username=user.username,
        email=user.email,
        is_active=user.is_active,
    )


@app.post("/auth/oauth2", response_model=TokenResponse)
@limiter.limit(settings.rate_limit_auth)
def oauth2_login(request: Request, body: OAuth2LoginRequest, db: Session = Depends(get_db)):
    """
    OAuth2 login endpoint (Google, GitHub, etc.).

    NOTE: This is a placeholder. In production, verify the token with the
    provider's API before creating/finding the user.
    """
    provider = body.provider.lower()
    # In production: validate token with provider and extract real sub
    oauth_sub = f"{provider}|{body.oauth_token[:8]}"

    user = db.query(User).filter_by(oauth_provider=provider, oauth_sub=oauth_sub).first()
    if not user:
        user = User(
            username=None,
            email=None,
            hashed_password=None,
            oauth_provider=provider,
            oauth_sub=oauth_sub,
            encrypted_dek=create_user_dek(),
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        logger.info("OAuth2 user created", extra={"extra_data": {"provider": provider, "user_id": user.id}})

    return _issue_token_pair(user.id, db)


@app.post("/auth/refresh", response_model=TokenResponse)
@limiter.limit(settings.rate_limit_auth)
def refresh_tokens(request: Request, body: RefreshTokenRequest, db: Session = Depends(get_db)):
    """
    Exchange a valid refresh token for a new access + refresh token pair.

    Implements token rotation: the old refresh token is revoked on use.
    """
    token_record = db.query(RefreshToken).filter_by(token=body.refresh_token, revoked=False).first()
    if not token_record:
        raise HTTPException(status_code=401, detail="Invalid or revoked refresh token.")

    if token_record.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        token_record.revoked = True
        db.commit()
        raise HTTPException(status_code=401, detail="Refresh token has expired.")

    # Revoke the old refresh token (rotation)
    token_record.revoked = True
    db.commit()

    return _issue_token_pair(token_record.user_id, db)


# ── Link & Token Management ──────────────────────────────────────────────────


@app.get("/links")
async def list_links(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """List all links for the current user."""
    links = db.query(Link).filter_by(user_id=user.id).all()
    return [{"link_token": l.link_token, "site": l.site} for l in links]


@app.delete("/links/{link_token}")
async def delete_link(
    link_token: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete a link and all its associated access tokens."""
    link = db.query(Link).filter_by(link_token=link_token, user_id=user.id).first()
    if not link:
        raise HTTPException(status_code=404, detail="Link not found.")
    db.query(AccessToken).filter_by(link_token=link_token, user_id=user.id).delete()
    db.delete(link)
    db.commit()
    return {"status": "Link and associated tokens deleted."}


@app.get("/tokens")
async def list_tokens(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """List all access tokens for the current user."""
    tokens = db.query(AccessToken).filter_by(user_id=user.id).all()
    return [{"token": t.token, "link_token": t.link_token} for t in tokens]


@app.delete("/tokens/{token}")
async def delete_token(
    token: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete a specific access token."""
    token_obj = db.query(AccessToken).filter_by(token=token, user_id=user.id).first()
    if not token_obj:
        raise HTTPException(status_code=404, detail="Token not found.")
    db.delete(token_obj)
    db.commit()
    return {"status": "Token deleted."}


# ── Link Session Store (in-memory for link flow) ─────────────────────────────

# Stores ephemeral link sessions keyed by link_token.
# Each session tracks: status, site, events list, created_at, access_token, subscribers.
_link_sessions: Dict[str, Dict[str, Any]] = {}
_link_session_lock = asyncio.Lock()

# Temporary scope storage: maps link_token -> JSON scopes string.
# Populated in /create_link, consumed (and removed) in /submit_credentials.
_link_scopes: Dict[str, str] = {}

# TTL for link sessions (30 minutes)
_LINK_SESSION_TTL = 1800


def _get_link_session(token: str) -> Optional[Dict[str, Any]]:
    """Return a link session if it exists and hasn't expired."""
    session = _link_sessions.get(token)
    if not session:
        return None
    if time.time() - session["created_at"] > _LINK_SESSION_TTL:
        session["status"] = "expired"
    return session


# ── Hosted Link Page ──────────────────────────────────────────────────────────


@app.get("/link", response_class=HTMLResponse)
async def hosted_link_page(token: Optional[str] = None):
    """Serve the hosted Link page.

    The page validates the token client-side via the /link/sessions API.
    """
    from pathlib import Path

    link_html = Path("frontend/link.html")
    if not link_html.exists():
        raise HTTPException(status_code=500, detail="Link page not found.")
    return HTMLResponse(content=link_html.read_text(encoding="utf-8"))


# ── Link Session Endpoints ────────────────────────────────────────────────────


@app.post("/link/sessions")
async def create_link_session(
    site: Optional[str] = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a new link session for the hosted Link page.

    Returns a link_token that can be used with /link?token=xxx.
    Also generates an ephemeral encryption keypair for the session.
    """
    link_token = str(uuid.uuid4())

    # Store in DB if site is provided
    if site:
        new_link = Link(link_token=link_token, site=site, user_id=user.id)
        db.add(new_link)
        db.commit()

    # Generate ephemeral keypair
    public_key_pem = generate_keypair(link_token)

    # Create ephemeral session state
    async with _link_session_lock:
        _link_sessions[link_token] = {
            "status": "awaiting_institution",
            "site": site,
            "user_id": user.id,
            "events": [],
            "created_at": time.time(),
            "access_token": None,
            "subscribers": [],
        }

    logger.info("Link session created", extra={"extra_data": {"link_token": link_token}})
    return {
        "link_token": link_token,
        "link_url": f"/link?token={link_token}",
        "public_key": public_key_pem,
        "expires_in": _LINK_SESSION_TTL,
    }


@app.get("/link/sessions/{link_token}/status")
async def get_link_session_status(link_token: str):
    """Get the current status of a link session."""
    session = _get_link_session(link_token)
    if not session:
        raise HTTPException(status_code=404, detail="Link session not found.")
    result = {
        "link_token": link_token,
        "status": session["status"],
        "site": session.get("site"),
        "events": [e["event"] for e in session["events"]],
    }
    if session["status"] == "completed" and session.get("public_token"):
        result["public_token"] = session["public_token"]
    return result


@app.post("/link/sessions/{link_token}/event")
async def post_link_session_event(link_token: str, request: Request):
    """Record an event for a link session (called by the link page)."""
    session = _get_link_session(link_token)
    if not session:
        raise HTTPException(status_code=404, detail="Link session not found.")
    if session["status"] == "expired":
        raise HTTPException(status_code=410, detail="Link session has expired.")

    body = await request.json()
    event_name = body.get("event", "UNKNOWN")
    event_data = {
        "event": event_name,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "data": {k: v for k, v in body.items() if k != "event"},
    }

    public_token_value = None
    async with _link_session_lock:
        session["events"].append(event_data)
        if event_name == "INSTITUTION_SELECTED":
            session["status"] = "awaiting_credentials"
            session["site"] = body.get("site", session.get("site"))
        elif event_name == "CREDENTIALS_SUBMITTED":
            session["status"] = "connecting"
        elif event_name == "MFA_REQUIRED":
            session["status"] = "mfa_required"
        elif event_name == "MFA_SUBMITTED":
            session["status"] = "verifying_mfa"
        elif event_name == "CONNECTED":
            session["status"] = "completed"
            session["access_token"] = body.get("access_token")
            # Generate a one-time public_token for the 3-token exchange flow
            access_token = body.get("access_token")
            user_id = session.get("user_id")
            if access_token and user_id:
                public_token_value = f"public-{uuid.uuid4()}"
                db = next(get_db())
                try:
                    pt = PublicToken(
                        token=public_token_value,
                        link_token=link_token,
                        access_token=access_token,
                        user_id=user_id,
                        expires_at=datetime.now(timezone.utc) + timedelta(minutes=_PUBLIC_TOKEN_TTL_MINUTES),
                    )
                    db.add(pt)
                    db.commit()
                finally:
                    db.close()
                session["public_token"] = public_token_value
        elif event_name == "ERROR":
            session["status"] = "error"

        # Notify SSE subscribers
        for queue in session["subscribers"]:
            await queue.put(event_data)

    # Fire webhooks for terminal events
    webhook_event_map = {
        "CONNECTED": "LINK_COMPLETE",
        "ERROR": "LINK_ERROR",
        "MFA_REQUIRED": "MFA_REQUIRED",
    }
    if event_name in webhook_event_map:
        await fire_webhooks_for_session(
            link_token, webhook_event_map[event_name], event_data.get("data")
        )

    response = {"status": "ok"}
    if public_token_value:
        response["public_token"] = public_token_value
    return response


# ── SSE Event Stream ──────────────────────────────────────────────────────────


@app.get("/link/events/{link_token}")
async def link_event_stream(link_token: str):
    """SSE stream for real-time link session events.

    Agents can subscribe to this to get notified of each step in the Link flow.
    Events: INSTITUTION_SELECTED, CREDENTIALS_SUBMITTED, MFA_REQUIRED,
    MFA_SUBMITTED, CONNECTED, ERROR.
    """
    session = _get_link_session(link_token)
    if not session:
        raise HTTPException(status_code=404, detail="Link session not found.")

    queue: asyncio.Queue = asyncio.Queue()

    async with _link_session_lock:
        session["subscribers"].append(queue)

    async def event_generator():
        try:
            # Send any existing events as replay
            for past_event in session["events"]:
                yield {
                    "event": past_event["event"],
                    "data": __import__("json").dumps(past_event),
                }

            # Stream new events
            while True:
                try:
                    event_data = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield {
                        "event": event_data["event"],
                        "data": __import__("json").dumps(event_data),
                    }
                    # Close stream when session completes
                    if event_data["event"] in ("CONNECTED", "ERROR"):
                        return
                except asyncio.TimeoutError:
                    # Send keep-alive ping
                    yield {"event": "ping", "data": ""}
                    # Check if session expired
                    if _get_link_session(link_token) is None or session["status"] in ("completed", "error", "expired"):
                        return
        finally:
            async with _link_session_lock:
                if queue in session["subscribers"]:
                    session["subscribers"].remove(queue)

    return EventSourceResponse(event_generator())


# ── Webhook System ────────────────────────────────────────────────────────────

# In-memory delivery log (not critical — lost on restart is acceptable)
_webhook_delivery_log: Dict[str, list] = {}


@app.post("/webhooks/register")
async def register_webhook(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Register a webhook URL for a link_token.

    The webhook will be called when events occur on the link session.
    Payload includes an HMAC-SHA256 signature for verification.
    """
    body = await request.json()
    link_token = body.get("link_token")
    url = body.get("url")
    webhook_secret = body.get("secret")

    if not link_token or not url or not webhook_secret:
        raise HTTPException(status_code=422, detail="link_token, url, and secret are required.")

    # Validate URL format
    if not url.startswith("https://") and not url.startswith("http://localhost"):
        raise HTTPException(
            status_code=422,
            detail="Webhook URL must use HTTPS (http://localhost allowed for development).",
        )

    session = _get_link_session(link_token)
    if not session:
        raise HTTPException(status_code=404, detail="Link session not found.")

    webhook_id = str(uuid.uuid4())
    db_webhook = Webhook(
        id=webhook_id,
        link_token=link_token,
        url=url,
        secret=webhook_secret,
        user_id=user.id,
    )
    db.add(db_webhook)
    db.commit()

    logger.info("Webhook registered", extra={"extra_data": {"webhook_id": webhook_id, "link_token": link_token}})
    return {"webhook_id": webhook_id, "status": "registered"}


@app.get("/webhooks")
async def list_webhooks(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List all webhooks registered by the current user."""
    webhooks = db.query(Webhook).filter_by(user_id=user.id).all()
    return {
        "webhooks": [
            {
                "webhook_id": wh.id,
                "link_token": wh.link_token,
                "url": wh.url,
                "created_at": wh.created_at.isoformat() if wh.created_at else None,
            }
            for wh in webhooks
        ],
        "count": len(webhooks),
    }


@app.delete("/webhooks/{webhook_id}")
async def delete_webhook(
    webhook_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete a registered webhook."""
    wh = db.query(Webhook).filter_by(id=webhook_id, user_id=user.id).first()
    if not wh:
        raise HTTPException(status_code=404, detail="Webhook not found.")
    db.delete(wh)
    db.commit()
    _webhook_delivery_log.pop(webhook_id, None)
    return {"status": "deleted"}


@app.post("/webhooks/test")
async def test_webhook(
    request: Request,
    db: Session = Depends(get_db),
):
    """Send a test event to a registered webhook URL."""
    body = await request.json()
    webhook_id = body.get("webhook_id")

    if not webhook_id:
        raise HTTPException(status_code=404, detail="Webhook not found.")
    wh = db.query(Webhook).filter_by(id=webhook_id).first()
    if not wh:
        raise HTTPException(status_code=404, detail="Webhook not found.")

    test_payload = {
        "event": "TEST",
        "link_token": wh.link_token,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "data": {"message": "This is a test webhook event."},
    }

    success = await _deliver_webhook(wh.id, wh.url, wh.secret, test_payload)
    return {"status": "delivered" if success else "failed"}


async def _deliver_webhook(
    webhook_id: str,
    url: str,
    secret: str,
    payload: Dict[str, Any],
    retries: int = 3,
) -> bool:
    """Deliver a webhook payload with HMAC signature and retry logic."""
    import json as json_mod

    payload_bytes = json_mod.dumps(payload, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(
        secret.encode("utf-8"),
        payload_bytes,
        hashlib.sha256,
    ).hexdigest()

    headers = {
        "Content-Type": "application/json",
        "X-Plaidify-Signature": f"sha256={signature}",
        "X-Plaidify-Event": payload.get("event", "UNKNOWN"),
        "User-Agent": "Plaidify-Webhook/1.0",
    }

    deliveries = _webhook_delivery_log.setdefault(webhook_id, [])
    for attempt in range(retries):
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, content=payload_bytes, headers=headers)
                delivery = {
                    "attempt": attempt + 1,
                    "status_code": resp.status_code,
                    "timestamp": time.time(),
                    "success": resp.is_success,
                }
                deliveries.append(delivery)
                if resp.is_success:
                    return True
        except Exception as e:
            deliveries.append({
                "attempt": attempt + 1,
                "error": str(e),
                "timestamp": time.time(),
                "success": False,
            })

        if attempt < retries - 1:
            await asyncio.sleep(2 ** attempt)  # Exponential backoff: 1s, 2s

    return False


async def fire_webhooks_for_session(link_token: str, event: str, data: Optional[Dict] = None):
    """Fire all registered webhooks for a link session event."""
    payload = {
        "event": event,
        "link_token": link_token,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "data": data or {},
    }

    session = _get_link_session(link_token)
    if session and event == "LINK_COMPLETE":
        payload["access_token"] = session.get("access_token")

    # Query webhooks from DB using a fresh session
    db = next(get_db())
    try:
        webhooks = db.query(Webhook).filter_by(link_token=link_token).all()
        for wh in webhooks:
            asyncio.create_task(_deliver_webhook(wh.id, wh.url, wh.secret, payload))
    finally:
        db.close()


# ── Public Token Exchange (3-Token Flow) ──────────────────────────────────────

# Duration for which a public_token is valid (10 minutes).
_PUBLIC_TOKEN_TTL_MINUTES = 10


@app.post("/exchange/public_token")
async def exchange_public_token(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Exchange a one-time public_token for a permanent access_token.

    This implements the Plaid-style 3-token exchange flow:
      link_token → public_token (short-lived, client-safe) → access_token (permanent, server-only)

    The public_token can only be exchanged once and expires after 10 minutes.
    """
    body = await request.json()
    public_token = body.get("public_token")
    if not public_token:
        raise HTTPException(status_code=422, detail="public_token is required.")

    pt = db.query(PublicToken).filter_by(token=public_token).first()
    if not pt:
        raise HTTPException(status_code=404, detail="Invalid public_token.")
    if pt.user_id != user.id:
        raise HTTPException(status_code=403, detail="Not authorized to exchange this token.")
    if pt.exchanged:
        raise HTTPException(status_code=410, detail="public_token has already been exchanged.")
    expires_at = pt.expires_at.replace(tzinfo=timezone.utc) if pt.expires_at.tzinfo is None else pt.expires_at
    if datetime.now(timezone.utc) > expires_at:
        raise HTTPException(status_code=410, detail="public_token has expired.")

    # Mark as exchanged (single-use)
    pt.exchanged = True
    db.commit()

    logger.info("Public token exchanged", extra={
        "extra_data": {"link_token": pt.link_token, "user_id": user.id},
    })
    return {"access_token": pt.access_token}
