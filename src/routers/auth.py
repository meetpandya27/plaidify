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
from src.database import (
    PasswordResetToken,
    RefreshToken,
    User,
    create_user_dek,
    delete_user_data,
    ensure_user_dek,
    get_db,
)
from src.dependencies import (
    get_current_user,
    get_password_hash,
    limiter,
    verify_password,
)
from src.logging_config import get_logger
from src.models import (
    DeleteAccountRequest,
    ForgotPasswordRequest,
    OAuth2LoginRequest,
    RefreshTokenRequest,
    ResetPasswordRequest,
    TokenResponse,
    UserProfileResponse,
    UserRegisterRequest,
)
from src.oauth_providers import OAuthVerificationError, verify_oauth_token

settings = get_settings()
logger = get_logger("api.auth")

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=TokenResponse)
@limiter.limit("3/minute")
def register_user(request: Request, body: UserRegisterRequest, db: Session = Depends(get_db)):
    """Register a new user account."""
    if not settings.registration_enabled:
        raise HTTPException(
            status_code=403,
            detail="Self-registration is disabled. Contact an administrator for access.",
        )
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


@router.delete("/me")
def delete_account(
    request: Request,
    body: DeleteAccountRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Permanently delete the authenticated user's account and all associated data.

    Implements the GDPR right to erasure. Password-based accounts must confirm
    intent by supplying their current password. All credentials, tokens, links,
    consents, API keys, agents, and refresh/scheduled jobs are erased;
    tamper-evident audit-log entries are retained (they hold no credential PII)
    so the immutable hash chain stays intact for compliance.
    """
    if user.hashed_password:
        if not body.password or not verify_password(body.password, user.hashed_password):
            raise HTTPException(
                status_code=403,
                detail="Password confirmation is required to delete your account.",
            )

    user_id = user.id
    ip_address = request.client.host if request.client else None

    removed = delete_user_data(db, user_id)
    db.delete(user)
    db.commit()

    # Recorded after the deletion commits so the trail reflects a completed
    # erasure. AuditLog has no FK to users, so referencing the old id is safe.
    record_audit_event(
        db,
        "auth",
        "account_deleted",
        user_id=user_id,
        metadata={"removed": removed},
        ip_address=ip_address,
    )
    logger.info(
        "Account deleted",
        extra={"extra_data": {"user_id": user_id, "removed": removed}},
    )
    return {"status": "deleted", "user_id": user_id, "removed": removed}


def _unique_username(db: Session, base: str | None) -> str:
    """Return a username derived from ``base`` that is unique in the users table."""
    base = (base or "user").strip() or "user"
    candidate = base
    while db.query(User).filter(User.username == candidate).first() is not None:
        candidate = f"{base}-{secrets.token_hex(3)}"
    return candidate


@router.post("/oauth2", response_model=TokenResponse)
@limiter.limit(settings.rate_limit_auth)
def oauth2_login(request: Request, body: OAuth2LoginRequest, db: Session = Depends(get_db)):
    """Authenticate with an external OAuth2 provider (Google, GitHub).

    The client completes the provider's own OAuth flow, then posts the resulting
    access/ID token here. Plaidify verifies the token server-side against the
    provider, then issues its own JWT pair. A verified provider email is
    required; first-time logins auto-provision an account when
    ``OAUTH_AUTO_REGISTER`` is enabled.
    """
    if not settings.oauth_enabled:
        raise HTTPException(status_code=403, detail="OAuth login is disabled.")

    provider = (body.provider or "").lower()
    allowed = [p.strip().lower() for p in settings.oauth_allowed_providers.split(",") if p.strip()]
    if provider not in allowed:
        raise HTTPException(status_code=400, detail=f"Unsupported OAuth provider: {body.provider!r}")

    ip_address = request.client.host if request.client else None

    try:
        identity = verify_oauth_token(provider, body.oauth_token, settings)
    except OAuthVerificationError as exc:
        record_audit_event(
            db,
            "auth",
            "oauth_login_failed",
            metadata={"provider": provider, "reason": str(exc)},
            ip_address=ip_address,
        )
        raise HTTPException(status_code=401, detail="OAuth token verification failed.") from exc

    if not identity.email or not identity.email_verified:
        raise HTTPException(
            status_code=403,
            detail="A verified email from the provider is required to sign in.",
        )

    # 1) Match by provider identity (stable subject).
    user = db.query(User).filter(User.oauth_provider == provider, User.oauth_sub == identity.subject).first()
    # 2) Otherwise link to an existing account with the same verified email.
    if not user:
        user = db.query(User).filter(User.email == identity.email).first()
        if user and not user.oauth_sub:
            user.oauth_provider = provider
            user.oauth_sub = identity.subject
            db.commit()
    # 3) Otherwise auto-provision a new account.
    if not user:
        if not settings.oauth_auto_register:
            raise HTTPException(status_code=403, detail="No account is linked to this identity.")
        user = User(
            username=_unique_username(db, identity.username or identity.email.split("@", 1)[0]),
            email=identity.email,
            hashed_password=None,
            oauth_provider=provider,
            oauth_sub=identity.subject,
            encrypted_dek=create_user_dek(),
            is_active=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        record_audit_event(
            db,
            "auth",
            "oauth_register",
            user_id=user.id,
            metadata={"provider": provider},
            ip_address=ip_address,
        )
        logger.info("OAuth user registered", extra={"extra_data": {"user_id": user.id, "provider": provider}})

    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account is disabled.")

    ensure_user_dek(user, db)
    record_audit_event(
        db,
        "auth",
        "oauth_login",
        user_id=user.id,
        metadata={"provider": provider},
        ip_address=ip_address,
    )
    logger.info("OAuth login", extra={"extra_data": {"user_id": user.id, "provider": provider}})
    return issue_token_pair(user.id, db)


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


@router.get("/sessions")
def list_sessions(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """List the current user's active (non-revoked, unexpired) refresh-token sessions."""
    now = datetime.now(timezone.utc)
    tokens = (
        db.query(RefreshToken)
        .filter(
            RefreshToken.user_id == user.id,
            RefreshToken.revoked == False,  # noqa: E712
            RefreshToken.expires_at > now,
        )
        .order_by(RefreshToken.created_at.desc())
        .all()
    )
    return {
        "sessions": [
            {
                "id": t.id,
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "expires_at": t.expires_at.isoformat() if t.expires_at else None,
            }
            for t in tokens
        ],
        "count": len(tokens),
    }


@router.post("/sessions/revoke-all")
def revoke_all_sessions(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Revoke all of the current user's refresh tokens (force logout everywhere)."""
    count = (
        db.query(RefreshToken)
        .filter(RefreshToken.user_id == user.id, RefreshToken.revoked == False)  # noqa: E712
        .update({RefreshToken.revoked: True}, synchronize_session=False)
    )
    db.commit()
    record_audit_event(
        db,
        "auth",
        "revoke_all_sessions",
        user_id=user.id,
        metadata={"revoked_count": count},
        ip_address=request.client.host if request.client else None,
    )
    logger.info("All sessions revoked", extra={"extra_data": {"user_id": user.id, "count": count}})
    return {"status": "revoked", "count": count}
