"""
Authentication endpoints: register, login, OAuth2, profile, token refresh.
"""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from src.audit import record_audit_event
from src.auth_utils import issue_token_pair
from src.config import get_settings
from src.database import PasswordResetToken, RefreshToken, User, create_user_dek, ensure_user_dek, get_db
from src.dependencies import (
    get_current_user,
    get_password_hash,
    limiter,
    verify_password,
)
from src.logging_config import get_logger
from src.models import (
    ForgotPasswordRequest,
    OAuth2LoginRequest,
    RefreshTokenRequest,
    ResetPasswordRequest,
    TokenResponse,
    UserProfileResponse,
    UserRegisterRequest,
)

settings = get_settings()
logger = get_logger("api.auth")

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=TokenResponse)
@limiter.limit("3/minute")
def register_user(request: Request, body: UserRegisterRequest, db: Session = Depends(get_db)):
    """Register a new user account."""
    existing = db.query(User).filter((User.username == body.username) | (User.email == body.email)).first()
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
    record_audit_event(
        db,
        "auth",
        "register",
        user_id=user.id,
        metadata={"username": body.username},
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
    # CSRF protection: reject cross-origin form submissions in production
    if settings.env == "production":
        origin = request.headers.get("origin")
        referer = request.headers.get("referer")
        allowed = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
        if origin and origin not in allowed:
            raise HTTPException(status_code=403, detail="Cross-origin request blocked")
        if not origin and referer:
            from urllib.parse import urlparse

            referer_origin = f"{urlparse(referer).scheme}://{urlparse(referer).netloc}"
            if referer_origin not in allowed:
                raise HTTPException(status_code=403, detail="Cross-origin request blocked")

    user = db.query(User).filter(User.username == form_data.username).first()

    # Account lockout check
    if user and user.locked_until:
        lock_time = user.locked_until
        if lock_time.tzinfo is None:
            lock_time = lock_time.replace(tzinfo=timezone.utc)
        if lock_time > datetime.now(timezone.utc):
            raise HTTPException(
                status_code=423,
                detail="Account temporarily locked due to too many failed attempts. Try again later.",
            )

    if not user or not verify_password(form_data.password, user.hashed_password):
        # Increment failed counter & lock after 5 consecutive failures
        if user:
            user.failed_login_count = (user.failed_login_count or 0) + 1
            if user.failed_login_count >= 5:
                user.locked_until = datetime.now(timezone.utc) + timedelta(minutes=15)
                logger.warning(
                    "Account locked", extra={"extra_data": {"user_id": user.id, "failures": user.failed_login_count}}
                )
            db.commit()
        record_audit_event(
            db,
            "auth",
            "login_failed",
            metadata={"username": form_data.username},
            ip_address=request.client.host if request.client else None,
        )
        raise HTTPException(status_code=400, detail="Incorrect username or password")

    # Successful login — reset lockout counters
    if user.failed_login_count:
        user.failed_login_count = 0
        user.locked_until = None
        db.commit()

    # Lazy migration: ensure existing users get a DEK
    ensure_user_dek(user, db)

    record_audit_event(
        db,
        "auth",
        "login",
        user_id=user.id,
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
def oauth2_login(request: Request, body: OAuth2LoginRequest, db: Session = Depends(get_db)):
    """
    OAuth2 login endpoint (Google, GitHub, etc.).

    Disabled until real provider token verification is implemented.
    To enable, implement provider-specific token validation (e.g. verify
    Google ID tokens via Google's tokeninfo endpoint, GitHub tokens via
    GitHub's /user API, etc.) and replace the guard below.
    """
    raise HTTPException(
        status_code=501,
        detail="OAuth2 login is not yet implemented. Configure provider-specific token verification before enabling.",
    )


@router.post("/forgot-password")
@limiter.limit("3/minute")
def forgot_password(request: Request, body: ForgotPasswordRequest, db: Session = Depends(get_db)):
    """
    Request a password reset link. Always returns 200 to prevent email enumeration.
    In production, send the token via email. For now, the token is logged.
    """
    user = db.query(User).filter(User.email == body.email).first()
    if user:
        # Invalidate any existing tokens for this user
        db.query(PasswordResetToken).filter(
            PasswordResetToken.user_id == user.id,
            PasswordResetToken.used == False,  # noqa: E712
        ).update({"used": True})

        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

        reset_token = PasswordResetToken(
            user_id=user.id,
            token_hash=token_hash,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        db.add(reset_token)
        db.commit()

        logger.info(
            "Password reset requested",
            extra={"extra_data": {"user_id": user.id}},
        )
        record_audit_event(
            db,
            "auth",
            "password_reset_requested",
            user_id=user.id,
            ip_address=request.client.host if request.client else None,
        )

    return {"message": "If an account with that email exists, a reset link has been sent."}


@router.post("/reset-password")
@limiter.limit("5/minute")
def reset_password(request: Request, body: ResetPasswordRequest, db: Session = Depends(get_db)):
    """Reset password using a valid reset token."""
    token_hash = hashlib.sha256(body.token.encode()).hexdigest()
    reset_record = (
        db.query(PasswordResetToken)
        .filter(
            PasswordResetToken.token_hash == token_hash,
            PasswordResetToken.used == False,  # noqa: E712
            PasswordResetToken.expires_at > datetime.now(timezone.utc),
        )
        .first()
    )
    if not reset_record:
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")

    user = db.query(User).filter(User.id == reset_record.user_id).first()
    if not user:
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")

    user.hashed_password = get_password_hash(body.new_password)
    user.failed_login_count = 0
    user.locked_until = None
    reset_record.used = True
    db.commit()

    record_audit_event(
        db,
        "auth",
        "password_reset",
        user_id=user.id,
        ip_address=request.client.host if request.client else None,
    )
    logger.info("Password reset completed", extra={"extra_data": {"user_id": user.id}})
    return {"message": "Password has been reset successfully."}


@router.post("/refresh", response_model=TokenResponse)
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

    return issue_token_pair(token_record.user_id, db)
