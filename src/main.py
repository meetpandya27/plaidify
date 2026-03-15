"""
Plaidify API Server — FastAPI application entry point.

Provides endpoints for:
- Site connections (connect, create_link, submit_credentials, fetch_data)
- User authentication (register, login, OAuth2, profile)
- Link and token management (CRUD)
- System health checks
"""

import uuid
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from typing import Optional

import jwt
from fastapi import FastAPI, HTTPException, Depends, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles
from passlib.context import CryptContext
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from sqlalchemy.orm import Session

from src.config import get_settings
from src.database import (
    init_db,
    get_db,
    User,
    Link,
    AccessToken,
    RefreshToken,
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
    site: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Create a link token for a specific site.

    Step 1 of the Plaid-style multi-step flow.
    """
    link_token = str(uuid.uuid4())
    new_link = Link(link_token=link_token, site=site, user_id=user.id)
    db.add(new_link)
    db.commit()

    # Generate ephemeral RSA keypair for client-side encryption
    public_key_pem = generate_keypair(link_token)

    logger.info("Link created", extra={"extra_data": {"site": site, "user_id": user.id}})
    return {"link_token": link_token, "public_key": public_key_pem}


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

    new_token = AccessToken(
        token=access_token,
        link_token=link_token,
        username_encrypted=encrypted_username_stored,
        password_encrypted=encrypted_password_stored,
        user_id=user.id,
        key_version=get_current_key_version(),
    )
    db.add(new_token)
    db.commit()
    logger.info("Credentials submitted", extra={"extra_data": {"link_token": link_token, "user_id": user.id}})
    return {"access_token": access_token}


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
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Fetch data using a previously submitted access token.

    Step 3 of the multi-step flow. Decrypts credentials, connects to the site,
    and returns extracted data.
    """
    token_record = db.query(AccessToken).filter_by(token=access_token, user_id=user.id).first()
    if not token_record:
        raise HTTPException(status_code=401, detail="Invalid access token.")

    site = db.query(Link).filter_by(link_token=token_record.link_token, user_id=user.id).first()
    if not site:
        raise HTTPException(status_code=401, detail="Linked data not found.")

    username = decrypt_credential_for_user(user, token_record.username_encrypted)
    password = decrypt_credential_for_user(user, token_record.password_encrypted)
    user_instructions = token_record.instructions

    response_data = await connect_to_site(site.site, username, password)
    if user_instructions:
        response_data["instructions_applied"] = user_instructions
    return response_data


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
