"""
Plaidify API Server — FastAPI application entry point.

Provides endpoints for:
- Site connections (connect, create_link, submit_credentials, fetch_data)
- User authentication (register, login, OAuth2, profile)
- Link and token management (CRUD)
- System health checks
"""

import uuid
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
from sqlalchemy.orm import Session

from src.config import get_settings
from src.database import (
    init_db,
    get_db,
    User,
    Link,
    AccessToken,
    encrypt_credential,
    decrypt_credential,
)
from src.exceptions import PlaidifyError, InvalidTokenError, UserNotFoundError
from src.logging_config import setup_logging, get_logger
from src.models import (
    ConnectRequest,
    ConnectResponse,
    UserRegisterRequest,
    TokenResponse,
    OAuth2LoginRequest,
    UserProfileResponse,
)
from src.core.engine import connect_to_site

# ── Configuration ─────────────────────────────────────────────────────────────

settings = get_settings()
logger = get_logger("api")

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
    yield
    logger.info("Shutting down Plaidify")


# ── App Factory ───────────────────────────────────────────────────────────────

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="Open-source API for authenticated web data — for developers and AI agents.",
    lifespan=lifespan,
)

# CORS
origins = [o.strip() for o in settings.cors_origins.split(",")]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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


# ── Connection Endpoints ──────────────────────────────────────────────────────


@app.post("/connect", response_model=ConnectResponse)
async def connect(request: ConnectRequest):
    """
    Connect to a site and extract data in a single step.

    This is the simplest integration path — send credentials, get data back.
    """
    response_data = await connect_to_site(request.site, request.username, request.password)
    return response_data


@app.post("/disconnect")
async def disconnect():
    """Disconnect / end a session."""
    return {"status": "disconnected"}


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
    logger.info("Link created", extra={"extra_data": {"site": site, "user_id": user.id}})
    return {"link_token": link_token}


@app.post("/submit_credentials")
async def submit_credentials(
    link_token: str,
    username: str,
    password: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Submit credentials for a link token.

    Step 2 of the multi-step flow. Credentials are encrypted at rest.
    """
    existing_link = db.query(Link).filter_by(link_token=link_token, user_id=user.id).first()
    if not existing_link:
        raise HTTPException(status_code=404, detail="Invalid link token.")

    encrypted_username = encrypt_credential(username)
    encrypted_password = encrypt_credential(password)
    access_token = str(uuid.uuid4())

    new_token = AccessToken(
        token=access_token,
        link_token=link_token,
        username_encrypted=encrypted_username,
        password_encrypted=encrypted_password,
        user_id=user.id,
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

    username = decrypt_credential(token_record.username_encrypted)
    password = decrypt_credential(token_record.password_encrypted)
    user_instructions = token_record.instructions

    response_data = await connect_to_site(site.site, username, password)
    if user_instructions:
        response_data["instructions_applied"] = user_instructions
    return response_data


# ── Auth Endpoints ────────────────────────────────────────────────────────────


@app.post("/auth/register", response_model=TokenResponse)
def register_user(request: UserRegisterRequest, db: Session = Depends(get_db)):
    """Register a new user account."""
    existing = db.query(User).filter(
        (User.username == request.username) | (User.email == request.email)
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="Username or email already registered")

    hashed_pw = get_password_hash(request.password)
    user = User(username=request.username, email=request.email, hashed_password=hashed_pw)
    db.add(user)
    db.commit()
    db.refresh(user)

    access_token = create_access_token({"sub": str(user.id)})
    logger.info("User registered", extra={"extra_data": {"user_id": user.id}})
    return {"access_token": access_token, "token_type": "bearer"}


@app.post("/auth/token", response_model=TokenResponse)
def login_user(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    """Log in and receive a JWT access token."""
    user = db.query(User).filter(User.username == form_data.username).first()
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(status_code=400, detail="Incorrect username or password")

    access_token = create_access_token({"sub": str(user.id)})
    return {"access_token": access_token, "token_type": "bearer"}


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
def oauth2_login(request: OAuth2LoginRequest, db: Session = Depends(get_db)):
    """
    OAuth2 login endpoint (Google, GitHub, etc.).

    NOTE: This is a placeholder. In production, verify the token with the
    provider's API before creating/finding the user.
    """
    provider = request.provider.lower()
    # In production: validate token with provider and extract real sub
    oauth_sub = f"{provider}|{request.oauth_token[:8]}"

    user = db.query(User).filter_by(oauth_provider=provider, oauth_sub=oauth_sub).first()
    if not user:
        user = User(
            username=None,
            email=None,
            hashed_password=None,
            oauth_provider=provider,
            oauth_sub=oauth_sub,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        logger.info("OAuth2 user created", extra={"extra_data": {"provider": provider, "user_id": user.id}})

    access_token = create_access_token({"sub": str(user.id)})
    return {"access_token": access_token, "token_type": "bearer"}


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
