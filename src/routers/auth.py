"""
Authentication endpoints: register, login, OAuth2, profile, token refresh.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from src.audit import record_audit_event
from src.auth_utils import issue_token_pair
from src.config import get_settings
from src.database import RefreshToken, User, create_user_dek, ensure_user_dek, get_db
from src.dependencies import (
    get_current_user,
    get_password_hash,
    limiter,
    verify_password,
)
from src.logging_config import get_logger
from src.models import (
    OAuth2LoginRequest,
    RefreshTokenRequest,
    TokenResponse,
    UserProfileResponse,
    UserRegisterRequest,
)

settings = get_settings()
logger = get_logger("api.auth")

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=TokenResponse)
@limiter.limit("3/minute")
def register_user(
    request: Request, body: UserRegisterRequest, db: Session = Depends(get_db)
):
    """Register a new user account."""
    existing = (
        db.query(User)
        .filter((User.username == body.username) | (User.email == body.email))
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=400, detail="Username or email already registered"
        )

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
    record_audit_event(
        db, "auth", "register", user_id=user.id, metadata={"username": body.username},
        ip_address=request.client.host if request.client else None,
    )
    return issue_token_pair(user.id, db)


@router.post("/token", response_model=TokenResponse)
@limiter.limit(settings.rate_limit_auth)
def login_user(
    request: Request,
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
):
    """Log in and receive a JWT access token."""
    user = db.query(User).filter(User.username == form_data.username).first()
    if not user or not verify_password(form_data.password, user.hashed_password):
        record_audit_event(
            db, "auth", "login_failed", metadata={"username": form_data.username},
            ip_address=request.client.host if request.client else None,
        )
        raise HTTPException(
            status_code=400, detail="Incorrect username or password"
        )

    # Lazy migration: ensure existing users get a DEK
    ensure_user_dek(user, db)

    record_audit_event(
        db, "auth", "login", user_id=user.id,
        ip_address=request.client.host if request.client else None,
    )
    return issue_token_pair(user.id, db)


@router.get("/me", response_model=UserProfileResponse)
def get_profile(user: User = Depends(get_current_user)):
    """Get the current user's profile."""
    return UserProfileResponse(
        id=user.id,
        username=user.username,
        email=user.email,
        is_active=user.is_active,
    )


@router.post("/oauth2", response_model=TokenResponse)
@limiter.limit(settings.rate_limit_auth)
def oauth2_login(
    request: Request, body: OAuth2LoginRequest, db: Session = Depends(get_db)
):
    """
    OAuth2 login endpoint (Google, GitHub, etc.).

    NOTE: This is a placeholder. In production, verify the token with the
    provider's API before creating/finding the user.
    """
    provider = body.provider.lower()
    # In production: validate token with provider and extract real sub
    oauth_sub = f"{provider}|{body.oauth_token[:8]}"

    user = (
        db.query(User)
        .filter_by(oauth_provider=provider, oauth_sub=oauth_sub)
        .first()
    )
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
        logger.info(
            "OAuth2 user created",
            extra={"extra_data": {"provider": provider, "user_id": user.id}},
        )

    return issue_token_pair(user.id, db)


@router.post("/refresh", response_model=TokenResponse)
@limiter.limit(settings.rate_limit_auth)
def refresh_tokens(
    request: Request, body: RefreshTokenRequest, db: Session = Depends(get_db)
):
    """
    Exchange a valid refresh token for a new access + refresh token pair.

    Implements token rotation: the old refresh token is revoked on use.
    """
    token_record = (
        db.query(RefreshToken)
        .filter_by(token=body.refresh_token, revoked=False)
        .first()
    )
    if not token_record:
        raise HTTPException(
            status_code=401, detail="Invalid or revoked refresh token."
        )

    if token_record.expires_at.replace(tzinfo=timezone.utc) < datetime.now(
        timezone.utc
    ):
        token_record.revoked = True
        db.commit()
        raise HTTPException(status_code=401, detail="Refresh token has expired.")

    # Revoke the old refresh token (rotation)
    token_record.revoked = True
    db.commit()

    return issue_token_pair(token_record.user_id, db)
