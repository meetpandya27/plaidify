"""
Authentication endpoints: register (and email verification), login, OAuth2, profile, token refresh.
"""

import functools
import hashlib
import hmac
import math
import secrets
from datetime import datetime, timedelta
from typing import Optional

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.audit import record_audit_event
from src.auth_utils import end_user_sessions, issue_token_pair
from src.config import get_settings
from src.database import (
    LoginThrottle,
    PasswordResetToken,
    PendingRegistration,
    RefreshToken,
    SessionLocal,
    User,
    create_user_dek,
    delete_user_data,
    ensure_user_dek,
    get_db,
    hash_refresh_token,
    utcnow,
)
from src.dependencies import (
    get_current_user,
    get_password_hash,
    limiter,
    verify_password,
)
from src.logging_config import get_logger
from src.mailer import (
    mail_configured,
    send_password_reset_email,
    send_sign_up_address_in_use,
    send_sign_up_username_taken,
    send_sign_up_verification,
)
from src.models import (
    DeleteAccountRequest,
    ForgotPasswordRequest,
    OAuth2LoginRequest,
    RefreshTokenRequest,
    RegistrationPendingResponse,
    ResetPasswordRequest,
    TokenResponse,
    UserProfileResponse,
    UserRegisterRequest,
    VerifyEmailRequest,
)
from src.oauth_providers import OAuthVerificationError, verify_oauth_token
from src.routers.links import unschedule_refresh_jobs

settings = get_settings()
logger = get_logger("api.auth")

router = APIRouter(prefix="/auth", tags=["auth"])

_RESET_TOKEN_TTL = timedelta(hours=1)
_FORGOT_PASSWORD_REPLY = {"message": "If an account with that email exists, a reset link has been sent."}
_SIGN_UP_TOKEN_TTL = timedelta(hours=24)
# With email verification on, every sign-up gets this reply, whatever is taken.
_SIGN_UP_PENDING_REPLY = {
    "status": "verification_sent",
    "detail": "If the address can be used, we sent it a link to finish signing up.",
}

# ── Sign-in throttling ────────────────────────────────────────────────────────
#
# Failed password sign-ins are counted per (username, client address) and per
# username, in the ``login_throttles`` table. One client that keeps failing
# is locked out on its own after a few tries; the whole username only after
# many failures from several addresses, so a single attacker cannot lock the
# real owner out from elsewhere. A lock that has run out starts the count
# afresh. Rows are keyed by HMACs and exist for unknown usernames too, so a
# lockout says nothing about whether an account exists.

_LOGIN_PAIR_MAX_FAILURES = 5
_LOGIN_ACCOUNT_MAX_FAILURES = 20
_LOGIN_FAILURE_WINDOW = timedelta(minutes=15)
_LOGIN_LOCK_DURATION = timedelta(minutes=15)
_LOGIN_THROTTLE_KEY_LABEL = b"plaidify/login-throttle/v1"


@functools.lru_cache(maxsize=1)
def _login_throttle_key() -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_LOGIN_THROTTLE_KEY_LABEL).derive(
        settings.jwt_secret_key.encode("utf-8")
    )


def _throttle_digest(*parts: str) -> str:
    return hmac.new(_login_throttle_key(), "\x00".join(parts).encode("utf-8"), hashlib.sha256).hexdigest()


def _login_throttle_keys(username: str, client_address: str) -> tuple[str, str]:
    """(account key, pair key); the account key is also every row's ``subject``."""
    return _throttle_digest("account", username), _throttle_digest("pair", username, client_address)


def _login_locked_for(db: Session, keys: list[str], now: datetime) -> Optional[int]:
    """Seconds until the longest active lock among ``keys`` ends, or None when none is active."""
    locks = [
        until
        for (until,) in db.query(LoginThrottle.locked_until).filter(
            LoginThrottle.key.in_(keys), LoginThrottle.locked_until > now
        )
    ]
    if not locks:
        return None
    return max(1, math.ceil((max(locks) - now).total_seconds()))


def _count_login_failure(db: Session, *, subject: str, key: str, limit: int, now: datetime) -> None:
    row = db.query(LoginThrottle).filter(LoginThrottle.key == key).with_for_update().one_or_none()
    if row is None:
        row = LoginThrottle(key=key, subject=subject, failures=0, window_started_at=now)
        db.add(row)
    elif (row.locked_until is not None and row.locked_until <= now) or (
        row.window_started_at <= now - _LOGIN_FAILURE_WINDOW
    ):
        # The lock ran out, or the earlier failures are old: count afresh.
        row.failures, row.window_started_at, row.locked_until = 0, now, None
    row.failures += 1
    row.updated_at = now
    if row.failures >= limit and row.locked_until is None:
        row.locked_until = now + _LOGIN_LOCK_DURATION


def _record_login_failure(db: Session, account_key: str, pair_key: str) -> None:
    for attempt in (1, 2):
        now = utcnow()
        try:
            _count_login_failure(db, subject=account_key, key=pair_key, limit=_LOGIN_PAIR_MAX_FAILURES, now=now)
            _count_login_failure(db, subject=account_key, key=account_key, limit=_LOGIN_ACCOUNT_MAX_FAILURES, now=now)
            db.commit()
            return
        except IntegrityError:
            # Another request inserted the same row first; count on top of it.
            db.rollback()
            if attempt == 2:
                raise


def _clear_login_failures(db: Session, *keys: str, subject: Optional[str] = None) -> None:
    query = db.query(LoginThrottle)
    query = query.filter(LoginThrottle.subject == subject) if subject else query.filter(LoginThrottle.key.in_(keys))
    query.delete(synchronize_session=False)


def _client_ip(request: Request) -> Optional[str]:
    return request.client.host if request.client else None


def _holders_of(db: Session, username: str, email: str) -> list[Optional[str]]:
    """The addresses of the accounts holding ``username`` or ``email``; empty when both are free."""
    return [row.email for row in db.query(User.email).filter((User.username == username) | (User.email == email))]


def _finish_registration(db: Session, user: User, request: Request) -> dict:
    """Log and audit a newly created account, and sign it in."""
    db.refresh(user)
    logger.info("User registered", extra={"extra_data": {"user_id": user.id}})
    record_audit_event(
        db,
        "auth",
        "register",
        user_id=user.id,
        metadata={"username": user.username},
        ip_address=_client_ip(request),
    )
    return issue_token_pair(user, db)


def _store_pending_registration(db: Session, username: str, email: str, hashed_password: str, raw_token: str) -> None:
    for attempt in (1, 2):
        now = utcnow()
        # One live sign-up per address: this one replaces an earlier one, and so its token.
        db.query(PendingRegistration).filter(PendingRegistration.email == email).delete(synchronize_session=False)
        db.add(
            PendingRegistration(
                username=username,
                email=email,
                hashed_password=hashed_password,
                token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
                created_at=now,
                expires_at=now + _SIGN_UP_TOKEN_TTL,
            )
        )
        try:
            db.commit()
            return
        except IntegrityError:
            # A sign-up for the same address was stored at the same moment; replace it as well.
            db.rollback()
            if attempt == 2:
                raise


def _start_sign_up(username: str, email: str, hashed_password: str) -> None:
    """Store a sign-up whose address is still to be proven and mail the address. Runs after the reply is sent.

    A new address asking for a free username gets a pending registration and
    its one-time token (a pending sign-up for another address holds no
    username). An address that already has an account, or a new one asking
    for a taken username, gets a note instead, and nothing is stored.
    """
    with SessionLocal() as db:
        holders = _holders_of(db, username, email)
        if email in holders:
            mail = functools.partial(send_sign_up_address_in_use, email)
        elif holders:
            mail = functools.partial(send_sign_up_username_taken, email)
        else:
            raw_token = secrets.token_urlsafe(32)
            _store_pending_registration(db, username, email, hashed_password, raw_token)
            mail = functools.partial(
                send_sign_up_verification,
                email,
                raw_token,
                username=username,
                expires_hours=int(_SIGN_UP_TOKEN_TTL.total_seconds() // 3600),
            )

    if not mail_configured():
        logger.warning(
            "Sign-up requested but SMTP is not configured (SMTP_HOST, SMTP_FROM): "
            "sign-up emails are disabled, so the sign-up cannot be completed"
        )
        return
    try:
        mail()
    except Exception as exc:
        logger.error("Sign-up email could not be sent", extra={"extra_data": {"error": type(exc).__name__}})


@router.post(
    "/register",
    response_model=TokenResponse,
    responses={
        202: {
            "model": RegistrationPendingResponse,
            "description": "Sign-ups prove their email address first: finish with POST /auth/verify-email {token, password}.",
        }
    },
)
@limiter.limit("3/minute")
def register_user(
    request: Request,
    body: UserRegisterRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Register a new user account.

    With REGISTRATION_EMAIL_VERIFICATION (on in production unless set) the
    reply is always 202 ``verification_sent``, in the same time, whether or
    not the username or address is taken: what is taken is looked up after the
    reply has been sent, and only the address is told. POST /auth/verify-email
    with the mailed token and this password creates the account. Otherwise the
    account is created at once and its tokens are returned.
    """
    if not settings.registration_enabled:
        raise HTTPException(
            status_code=403,
            detail="Self-registration is disabled. Contact an administrator for access.",
        )
    # Hash first: a taken username or email then costs as long as a new account.
    hashed_pw = get_password_hash(body.password)
    if settings.registration_email_verification:
        background_tasks.add_task(_start_sign_up, body.username, body.email, hashed_pw)
        return JSONResponse(status_code=202, content=dict(_SIGN_UP_PENDING_REPLY))

    already_registered = HTTPException(status_code=400, detail="Username or email already registered")
    if _holders_of(db, body.username, body.email):
        raise already_registered

    user = User(
        username=body.username,
        email=body.email,
        hashed_password=hashed_pw,
        encrypted_dek=create_user_dek(),
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise already_registered
    return _finish_registration(db, user, request)


@router.post("/verify-email", response_model=TokenResponse)
@limiter.limit("3/minute")
def verify_email(request: Request, body: VerifyEmailRequest, db: Session = Depends(get_db)):
    """Finish a sign-up: the mailed token together with the password chosen at registration.

    The link or the token alone does not create the account — someone who
    follows a sign-up they did not start does not have that password. The
    account is then created as an immediate registration creates it, with its
    address marked verified, and its tokens are returned. 400 for an unknown,
    used or expired token or a wrong password (the same answer, and the token
    is not spent); 409 when the username or the address was taken in the
    meantime (the sign-up is then void: register again). 404 while
    REGISTRATION_EMAIL_VERIFICATION is off.
    """
    if not settings.registration_enabled:
        raise HTTPException(
            status_code=403,
            detail="Self-registration is disabled. Contact an administrator for access.",
        )
    if not settings.registration_email_verification:
        raise HTTPException(status_code=404, detail="Email verification is not enabled.")

    invalid = HTTPException(status_code=400, detail="Invalid or expired verification token")
    unavailable = HTTPException(
        status_code=409, detail="That username or address is no longer available. Register again."
    )
    token_hash = hashlib.sha256(body.token.encode()).hexdigest()
    pending = db.execute(
        select(PendingRegistration.username, PendingRegistration.email, PendingRegistration.hashed_password).where(
            PendingRegistration.token_hash == token_hash, PendingRegistration.expires_at > utcnow()
        )
    ).first()
    # One bcrypt either way, and the same 400 for an unknown token and a wrong
    # password. The row stays until the password matches, so a link by itself
    # (or a guess at the password) cannot create the account or burn the token.
    password_ok = verify_password(body.password, pending.hashed_password if pending is not None else None)
    if pending is None or not password_ok:
        raise invalid
    # Claim the sign-up with one conditional DELETE: of two concurrent verifications with the token, one wins.
    claim = (
        delete(PendingRegistration)
        .where(PendingRegistration.token_hash == token_hash)
        .execution_options(synchronize_session=False)
    )
    if db.execute(claim).rowcount != 1:
        db.rollback()
        raise invalid

    if _holders_of(db, pending.username, pending.email):
        db.commit()  # the claimed sign-up stays deleted: it cannot finish
        raise unavailable
    user = User(
        username=pending.username,
        email=pending.email,
        hashed_password=pending.hashed_password,
        encrypted_dek=create_user_dek(),
        # The token reached the mailbox: the address is proven.
        email_verified=True,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        # Taken at the same moment. The rollback restored the claimed sign-up; delete it again.
        db.rollback()
        db.execute(claim)
        db.commit()
        raise unavailable
    return _finish_registration(db, user, request)


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

    ip_address = _client_ip(request)
    account_key, pair_key = _login_throttle_keys(form_data.username, ip_address or "unknown")
    locked_for = _login_locked_for(db, [pair_key, account_key], utcnow())
    if locked_for is not None:
        raise HTTPException(
            status_code=423,
            detail="Sign-in temporarily locked after too many failed attempts. Try again later.",
            headers={"Retry-After": str(locked_for)},
        )

    user = db.query(User).filter(User.username == form_data.username).first()
    # One bcrypt verification either way: an unknown username takes as long as a wrong password.
    password_ok = verify_password(form_data.password, user.hashed_password if user else None)
    if not user or not password_ok:
        _record_login_failure(db, account_key, pair_key)
        # The audit chain can't be edited later, so it never holds the typed
        # username: the keyed throttle subject still ties repeat attempts on
        # one username together without being reversible.
        record_audit_event(
            db,
            "auth",
            "login_failed",
            metadata={"account": account_key[:16], "known_account": user is not None},
            ip_address=ip_address,
        )
        raise HTTPException(status_code=400, detail="Incorrect username or password")

    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account is disabled.")

    _clear_login_failures(db, pair_key, account_key)
    db.commit()

    # Lazy migration: ensure existing users get a DEK
    ensure_user_dek(user, db)

    record_audit_event(
        db,
        "auth",
        "login",
        user_id=user.id,
        ip_address=ip_address,
    )
    return issue_token_pair(user, db)


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
    ip_address = _client_ip(request)

    # This process's scheduler would otherwise run (and re-save) the deleted jobs.
    unschedule_refresh_jobs(user_id=user_id)
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
    provider and checks it was issued to Plaidify's own OAuth app, then issues
    its own JWT pair. A verified provider email is required.

    An identity is linked into an existing account with the same email only
    when that account's email is itself verified; a password account whose
    address was never proven answers 409 (anyone could have registered it).
    First-time sign-ins create an account when both ``OAUTH_AUTO_REGISTER``
    and ``REGISTRATION_ENABLED`` allow it.
    """
    if not settings.oauth_enabled:
        raise HTTPException(status_code=403, detail="OAuth login is disabled.")

    provider = (body.provider or "").lower()
    allowed = [p.strip().lower() for p in settings.oauth_allowed_providers.split(",") if p.strip()]
    if provider not in allowed:
        raise HTTPException(status_code=400, detail=f"Unsupported OAuth provider: {body.provider!r}")

    ip_address = _client_ip(request)

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
    # 2) Otherwise an account with the same email: only one whose email is proven.
    if not user:
        user = db.query(User).filter(func.lower(User.email) == identity.email.lower()).first()
        if user is not None:
            if not user.email_verified:
                record_audit_event(
                    db,
                    "auth",
                    "oauth_link_refused",
                    user_id=user.id,
                    metadata={"provider": provider, "reason": "email_not_verified"},
                    ip_address=ip_address,
                )
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "An account with this email already exists, but its email address has not been "
                        "verified, so it cannot be linked automatically. Sign in with your password, or reset "
                        "the password by email, then sign in with this provider again."
                    ),
                )
            if not user.oauth_sub:
                user.oauth_provider = provider
                user.oauth_sub = identity.subject
                db.commit()
    # 3) Otherwise create an account, if sign-ups are open.
    if not user:
        if not settings.registration_enabled:
            raise HTTPException(
                status_code=403,
                detail="Self-registration is disabled. Contact an administrator for access.",
            )
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
            email_verified=True,
        )
        db.add(user)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(status_code=409, detail="An account with this email or username already exists.")
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
    return issue_token_pair(user, db)


def _issue_password_reset(user_id: int, ip_address: Optional[str]) -> None:
    """Create a reset token for the user and email it. Runs after the response is sent."""
    raw_token = secrets.token_urlsafe(32)
    with SessionLocal() as db:
        user = db.get(User, user_id)
        if user is None or not user.email:
            return
        # Invalidate any existing tokens for this user
        db.query(PasswordResetToken).filter(
            PasswordResetToken.user_id == user.id,
            PasswordResetToken.used == False,  # noqa: E712
        ).update({"used": True})
        db.add(
            PasswordResetToken(
                user_id=user.id,
                token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
                expires_at=utcnow() + _RESET_TOKEN_TTL,
            )
        )
        db.commit()
        email = user.email
        logger.info("Password reset requested", extra={"extra_data": {"user_id": user_id}})
        record_audit_event(db, "auth", "password_reset_requested", user_id=user_id, ip_address=ip_address)

    if not mail_configured():
        logger.warning(
            "Password reset requested but SMTP is not configured (SMTP_HOST, SMTP_FROM): "
            "reset emails are disabled, so the reset cannot be completed",
            extra={"extra_data": {"user_id": user_id}},
        )
        return
    try:
        send_password_reset_email(email, raw_token, expires_minutes=int(_RESET_TOKEN_TTL.total_seconds() // 60))
    except Exception as exc:
        logger.error(
            "Password reset email could not be sent",
            extra={"extra_data": {"user_id": user_id, "error": type(exc).__name__}},
        )


@router.post("/forgot-password")
@limiter.limit("3/minute")
def forgot_password(
    request: Request,
    body: ForgotPasswordRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    Request a password reset email.

    The reply is the same whether or not an account uses the address, and it
    takes the same time: the token is created and mailed after the response
    has been sent.
    """
    user_id = db.query(User.id).filter(User.email == body.email, User.is_active == True).scalar()  # noqa: E712
    if user_id is not None:
        background_tasks.add_task(_issue_password_reset, user_id, _client_ip(request))
    return dict(_FORGOT_PASSWORD_REPLY)


@router.post("/reset-password")
@limiter.limit("5/minute")
def reset_password(request: Request, body: ResetPasswordRequest, db: Session = Depends(get_db)):
    """Reset password using a valid reset token.

    Ends every session of the account (refresh tokens revoked, access tokens
    invalidated through the token version) and clears sign-in lockouts.
    """
    invalid = HTTPException(status_code=400, detail="Invalid or expired reset token")
    token_hash = hashlib.sha256(body.token.encode()).hexdigest()
    # Claim the token with one conditional UPDATE: of two concurrent resets with it, one wins.
    claimed = db.execute(
        update(PasswordResetToken)
        .where(
            PasswordResetToken.token_hash == token_hash,
            PasswordResetToken.used == False,  # noqa: E712
            PasswordResetToken.expires_at > utcnow(),
        )
        .values(used=True)
        .execution_options(synchronize_session=False)
    ).rowcount
    if claimed != 1:
        db.rollback()
        raise invalid

    user_id = db.execute(select(PasswordResetToken.user_id).where(PasswordResetToken.token_hash == token_hash)).scalar()
    user = db.get(User, user_id) if user_id is not None else None
    if user is None or not user.is_active:
        db.rollback()
        raise invalid

    user.hashed_password = get_password_hash(body.new_password)
    # The token reached the account's mailbox: the address is proven.
    user.email_verified = True
    user.failed_login_count = 0
    user.locked_until = None
    # Every other outstanding reset token for the account dies with this one.
    db.query(PasswordResetToken).filter(
        PasswordResetToken.user_id == user.id,
        PasswordResetToken.used == False,  # noqa: E712
    ).update({"used": True}, synchronize_session=False)
    revoked = end_user_sessions(db, user.id)
    if user.username:
        _clear_login_failures(db, subject=_login_throttle_keys(user.username, "")[0])
    db.commit()

    record_audit_event(
        db,
        "auth",
        "password_reset",
        user_id=user.id,
        metadata={"sessions_revoked": revoked},
        ip_address=_client_ip(request),
    )
    logger.info("Password reset completed", extra={"extra_data": {"user_id": user.id}})
    return {"message": "Password has been reset successfully."}


@router.post("/refresh", response_model=TokenResponse)
@limiter.limit(settings.rate_limit_auth)
def refresh_tokens(request: Request, body: RefreshTokenRequest, db: Session = Depends(get_db)):
    """
    Exchange a valid refresh token for a new access + refresh token pair.

    Implements token rotation: the old refresh token is revoked on use. The
    revocation is one conditional UPDATE committed together with the new
    token, so of any number of concurrent requests presenting the same token
    exactly one succeeds. Presenting a token that was already rotated or
    revoked means two parties hold it (theft or replay): every refresh token
    of that user is revoked.
    """
    token_hash = hash_refresh_token(body.refresh_token)
    now = utcnow()

    claimed = db.execute(
        update(RefreshToken)
        .where(
            RefreshToken.token_hash == token_hash,
            RefreshToken.revoked == False,  # noqa: E712
            RefreshToken.expires_at > now,
        )
        .values(revoked=True)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount == 1:
        user_id = db.execute(select(RefreshToken.user_id).where(RefreshToken.token_hash == token_hash)).scalar_one()
        user = db.get(User, user_id)
        if user is None or not user.is_active:
            db.commit()  # the presented token stays spent
            raise HTTPException(status_code=401, detail="Account is disabled.")
        tokens = issue_token_pair(user, db, commit=False)
        db.commit()
        return tokens

    record = db.execute(
        select(RefreshToken.user_id, RefreshToken.revoked, RefreshToken.expires_at).where(
            RefreshToken.token_hash == token_hash
        )
    ).first()
    if record is None:
        db.rollback()
        raise HTTPException(status_code=401, detail="Invalid or revoked refresh token.")
    if record.expires_at <= now:
        db.rollback()
        raise HTTPException(status_code=401, detail="Refresh token has expired.")
    if not record.revoked:  # lost a race with a transaction that rolled back; not a reuse
        db.rollback()
        raise HTTPException(status_code=401, detail="Invalid or revoked refresh token.")

    # Reuse of a rotated (or revoked) token: revoke the whole family.
    revoked = db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == record.user_id, RefreshToken.revoked == False)  # noqa: E712
        .values(revoked=True)
        .execution_options(synchronize_session=False)
    ).rowcount
    db.commit()
    logger.warning(
        "Refresh token reuse detected; revoked all of the user's sessions",
        extra={"extra_data": {"user_id": record.user_id, "revoked_count": revoked}},
    )
    record_audit_event(
        db,
        "auth",
        "refresh_token_reuse",
        user_id=record.user_id,
        metadata={"revoked_count": revoked},
        ip_address=_client_ip(request),
    )
    raise HTTPException(status_code=401, detail="Invalid or revoked refresh token.")


@router.get("/sessions")
def list_sessions(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """List the current user's active (non-revoked, unexpired) refresh-token sessions."""
    now = utcnow()
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
    """Sign out everywhere: revoke every refresh token and end every access token (this one included)."""
    count = end_user_sessions(db, user.id)
    db.commit()
    record_audit_event(
        db,
        "auth",
        "revoke_all_sessions",
        user_id=user.id,
        metadata={"revoked_count": count},
        ip_address=_client_ip(request),
    )
    logger.info("All sessions revoked", extra={"extra_data": {"user_id": user.id, "count": count}})
    return {"status": "revoked", "count": count}
