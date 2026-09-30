"""Plaidify FastAPI application bootstrap.

This module owns the live HTTP application wiring:
- app lifecycle and startup validation
- middleware and exception handlers
- metrics exposure
- router registration

Route implementations live in ``src.routers``.
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import socket
import time
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from typing import Callable, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from limits import parse as parse_rate_limit
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import or_, update
from sqlalchemy.exc import IntegrityError
from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src import session_store
from src.access_jobs import shutdown_access_jobs
from src.audit import prune_audit_logs
from src.config import get_settings
from src.core.browser_pool import shutdown_browser_pool
from src.crypto import _get_redis
from src.database import (
    KeyRotationIncomplete,
    LoginThrottle,
    MaintenanceLease,
    PasswordResetToken,
    PendingRegistration,
    RefreshToken,
    SessionLocal,
    get_current_key_version,
    get_db,
    init_db,
    purge_expired_job_results,
    re_encrypt_tokens,
    utcnow,
)
from src.dependencies import limiter
from src.exceptions import PlaidifyError
from src.logging_config import get_logger, setup_logging
from src.mailer import mail_configured
from src.models import MAX_PASSWORD_BYTES
from src.oauth_providers import missing_oauth_configuration
from src.routers import (
    access_jobs,
    admin,
    agents,
    api_keys,
    audit,
    auth,
    connection,
    consent,
    link_sessions,
    links,
    refresh,
    registry,
    system,
    webhooks,
)
from src.tracing import init_tracing

settings = get_settings()
logger = get_logger("api")
FRONTEND_NEXT_DIST = Path(__file__).resolve().parent.parent / "frontend-next" / "dist"

_TOKEN_CLEANUP_INTERVAL = 3600
_AUDIT_CLEANUP_INTERVAL = 86400
_KEY_REENCRYPT_INTERVAL = 3600
MAX_REQUEST_BODY_SIZE = 1 * 1024 * 1024
_BODY_TOO_LARGE = "Request body too large. Maximum size is 1MB."
_MIN_SECRET_LENGTH = 32
_LOGIN_THROTTLE_RETENTION = timedelta(days=1)


def _initialize_sentry() -> None:
    """Initialize Sentry error reporting when configured."""
    if not settings.sentry_dsn:
        return

    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration

        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            environment=settings.env,
            release=f"plaidify@{settings.app_version}",
            traces_sample_rate=0.02 if settings.env == "production" else 1.0,
            integrations=[FastApiIntegration(), SqlalchemyIntegration()],
            send_default_pii=False,
        )
        logger.info("Sentry error tracking initialized")
    except ImportError:
        logger.warning("sentry-sdk not installed — error tracking disabled")


def _validate_runtime_configuration() -> None:
    """Fail fast on unsafe or incomplete configuration (and production-only prerequisites)."""
    if len(settings.jwt_secret_key or "") < _MIN_SECRET_LENGTH:
        raise RuntimeError(
            f"JWT_SECRET_KEY must be at least {_MIN_SECRET_LENGTH} characters. Generate one with: openssl rand -hex 32"
        )
    if settings.link_launch_secret is not None:
        if len(settings.link_launch_secret) < _MIN_SECRET_LENGTH:
            raise RuntimeError(f"LINK_LAUNCH_SECRET must be at least {_MIN_SECRET_LENGTH} characters.")
        if secrets.compare_digest(settings.link_launch_secret.encode(), settings.jwt_secret_key.encode()):
            raise RuntimeError("LINK_LAUNCH_SECRET must differ from JWT_SECRET_KEY.")
    missing_oauth = missing_oauth_configuration(settings)
    if missing_oauth:
        raise RuntimeError(
            "OAUTH_ENABLED is set but " + ", ".join(missing_oauth) + " is missing: provider tokens could not be "
            "tied to Plaidify's own OAuth app. Set them, or remove the provider from OAUTH_ALLOWED_PROVIDERS."
        )

    if settings.env != "production":
        return

    if settings.debug:
        raise RuntimeError("DEBUG must be false in production.")

    if settings.registration_enabled and settings.registration_email_verification and not mail_configured():
        raise RuntimeError(
            "REGISTRATION_ENABLED is set with email-verified sign-up (REGISTRATION_EMAIL_VERIFICATION, on by default "
            "in production) but SMTP_HOST/SMTP_FROM are not set: no verification email could be sent, so no sign-up "
            "could finish. Set SMTP_HOST and SMTP_FROM, or set REGISTRATION_EMAIL_VERIFICATION=false to create "
            "accounts at once (registration then reveals whether a username or email is taken), or leave "
            "REGISTRATION_ENABLED unset."
        )

    if not settings.redis_url:
        raise RuntimeError("REDIS_URL is required in production for shared state and rate limiting.")

    redis_client = _get_redis()
    if redis_client is None:
        raise RuntimeError("Redis is required in production and must be reachable.")

    redis_client.ping()

    if settings.registration_enabled:
        logger.warning(
            "Public self-registration is ENABLED in production. Set REGISTRATION_ENABLED=false "
            "and provision accounts via BOOTSTRAP_USER_* to reduce abuse surface."
        )
    if not mail_configured():
        logger.warning("SMTP_HOST/SMTP_FROM are not set: password-reset emails are disabled.")
    if not settings.health_check_token:
        logger.warning("HEALTH_CHECK_TOKEN is not set: GET /health/detailed is disabled in production.")


def _bootstrap_user() -> None:
    """Create the configured bootstrap administrator if it doesn't exist yet.

    Lets operators provision the first account in production without enabling
    open registration or manipulating the database directly. It only ever
    creates that account: an existing account with the same username and
    email that is already an administrator is taken to be it (a no-op); any
    other account holding the username or the email is a conflict — someone
    may have registered it first — and is never promoted. A conflict stops
    startup in production and is logged as an error elsewhere. Remove the
    BOOTSTRAP_USER_* settings once the account exists.
    """
    username = settings.bootstrap_user_username
    email = settings.bootstrap_user_email
    password = settings.bootstrap_user_password
    if not (username and email and password):
        return

    from src.database import User, create_user_dek
    from src.dependencies import get_password_hash

    def refuse(message: str) -> None:
        logger.error(message)
        if settings.env == "production":
            raise RuntimeError(message)

    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        refuse(
            f"BOOTSTRAP_USER_PASSWORD is longer than {MAX_PASSWORD_BYTES} bytes; the bootstrap user was not created."
        )
        return

    db_gen = get_db()
    db = next(db_gen)
    try:
        by_name = db.query(User).filter(User.username == username).first()
        by_email = db.query(User).filter(User.email == email).first()
        if by_name is None and by_email is None:
            db.add(
                User(
                    username=username,
                    email=email,
                    hashed_password=get_password_hash(password),
                    encrypted_dek=create_user_dek(),
                    is_admin=True,
                )
            )
            try:
                db.commit()
            except IntegrityError:
                # Another worker created it at the same moment; the next start sees it.
                db.rollback()
                logger.info("Bootstrap user was created concurrently by another process")
                return
            logger.info("Bootstrap admin user created", extra={"extra_data": {"username": username}})
            return
        if by_name is not None and by_name is by_email and by_name.is_admin:
            logger.info("Bootstrap user already present; skipping creation")
            return
        refuse(
            "BOOTSTRAP_USER_USERNAME / BOOTSTRAP_USER_EMAIL match an existing account that is not the "
            "bootstrap administrator (it may have been registered by someone else). It was NOT promoted. "
            "Choose another username and email, or resolve the account and remove BOOTSTRAP_USER_*."
        )
    finally:
        db_gen.close()


# ── Periodic maintenance ──────────────────────────────────────────────────────
#
# Every API process runs the loops, but a job runs in whichever process first
# takes its lease (a row in maintenance_leases, valid for most of one
# interval), so across all workers and replicas each job runs about once per
# interval — never concurrently with itself.

_LEASE_HOLDER = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def _claim_maintenance_lease(name: str, ttl_seconds: float) -> bool:
    """Take the lease ``name`` for ``ttl_seconds`` unless another process holds a live one."""
    now = utcnow()
    expires_at = now + timedelta(seconds=ttl_seconds)
    with SessionLocal() as db:
        taken = db.execute(
            update(MaintenanceLease)
            .where(MaintenanceLease.name == name, MaintenanceLease.expires_at <= now)
            .values(holder=_LEASE_HOLDER, expires_at=expires_at)
            .execution_options(synchronize_session=False)
        ).rowcount
        if taken:
            db.commit()
            return True
        db.add(MaintenanceLease(name=name, holder=_LEASE_HOLDER, expires_at=expires_at))
        try:
            db.commit()
            return True
        except IntegrityError:
            db.rollback()
            return False


def _purge_expired_auth_rows() -> None:
    """Delete expired refresh and password-reset tokens, expired sign-ups and stale sign-in throttles.

    Revoked refresh tokens stay until they expire: presenting a rotated token
    again is how token theft is detected (and the whole family revoked).
    """
    now = utcnow()
    with SessionLocal() as db:
        refresh_tokens = db.query(RefreshToken).filter(RefreshToken.expires_at < now).delete(synchronize_session=False)
        reset_tokens = (
            db.query(PasswordResetToken).filter(PasswordResetToken.expires_at < now).delete(synchronize_session=False)
        )
        sign_ups = (
            db.query(PendingRegistration).filter(PendingRegistration.expires_at < now).delete(synchronize_session=False)
        )
        throttles = (
            db.query(LoginThrottle)
            .filter(
                LoginThrottle.updated_at < now - _LOGIN_THROTTLE_RETENTION,
                or_(LoginThrottle.locked_until.is_(None), LoginThrottle.locked_until < now),
            )
            .delete(synchronize_session=False)
        )
        db.commit()
    if refresh_tokens or reset_tokens or sign_ups or throttles:
        logger.info(
            "Cleaned up expired auth rows",
            extra={
                "extra_data": {
                    "refresh_tokens": refresh_tokens,
                    "password_reset_tokens": reset_tokens,
                    "pending_registrations": sign_ups,
                    "login_throttles": throttles,
                }
            },
        )


def _apply_retention() -> None:
    """Prune the audit log (keeping its hash chain verifiable) and erase old access-job results."""
    with SessionLocal() as db:
        prune_audit_logs(db)
    with SessionLocal() as db:
        purge_expired_job_results(db)


def _reencrypt_step() -> None:
    """One step of the master-key rotation sweep."""
    with SessionLocal() as db:
        try:
            count = re_encrypt_tokens(db, batch_size=100)
        except KeyRotationIncomplete as exc:
            logger.error(
                "Key rotation pass left rows no configured key can decrypt; keep ENCRYPTION_KEY_PREVIOUS set",
                extra={"extra_data": {"key_version": exc.key_version, "failed": len(exc.failures)}},
            )
            return
    if count:
        logger.info(
            "Re-encrypted tokens to current key version",
            extra={"extra_data": {"count": count, "key_version": get_current_key_version()}},
        )


async def _maintenance_loop(name: str, interval: float, job: Callable[[], None]) -> None:
    """Every ``interval`` seconds, run ``job`` (off the event loop) if this process takes its lease."""
    while True:
        try:
            await asyncio.sleep(interval)
            if await asyncio.to_thread(_claim_maintenance_lease, name, interval * 0.9):
                await asyncio.to_thread(job)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"Maintenance job {name} failed: {exc}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown lifecycle."""
    setup_logging(level=settings.log_level, log_format=settings.log_format)
    logger.info(
        "Starting Plaidify",
        extra={
            "extra_data": {
                "version": settings.app_version,
                "environment": settings.env,
                "debug": settings.debug,
            }
        },
    )

    _validate_runtime_configuration()
    _initialize_sentry()
    init_db()
    _bootstrap_user()

    logger.info("Browser engine ready (Playwright, lazy-start)")

    maintenance_tasks = [
        asyncio.create_task(_maintenance_loop("auth_cleanup", _TOKEN_CLEANUP_INTERVAL, _purge_expired_auth_rows)),
        asyncio.create_task(_maintenance_loop("retention", _AUDIT_CLEANUP_INTERVAL, _apply_retention)),
        asyncio.create_task(_maintenance_loop("key_reencrypt", _KEY_REENCRYPT_INTERVAL, _reencrypt_step)),
    ]

    yield

    for task in maintenance_tasks:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    logger.info("Shutting down in-process access jobs...")
    try:
        await shutdown_access_jobs(timeout=10)
    except Exception as exc:
        logger.error(f"Error while shutting down access jobs: {exc}")

    logger.info("Shutting down browser pool...")
    shutdown_timeout = 30
    try:
        await asyncio.wait_for(shutdown_browser_pool(), timeout=shutdown_timeout)
        logger.info("Browser pool shut down cleanly")
    except asyncio.TimeoutError:
        logger.warning(f"Browser pool shutdown timed out after {shutdown_timeout}s, forcing...")
    except Exception as exc:
        logger.error(f"Error during browser pool shutdown: {exc}")

    try:
        redis_client = _get_redis()
        if redis_client is not None:
            # Close off the event loop with a timeout so a hung socket can't
            # stall shutdown indefinitely.
            await asyncio.wait_for(asyncio.to_thread(redis_client.close), timeout=5)
            logger.info("Redis connection closed")
    except asyncio.TimeoutError:
        logger.warning("Redis connection close timed out; continuing shutdown")
    except Exception:
        pass

    logger.info("Shutting down Plaidify")


def _docs_urls(env: str, docs_enabled: bool) -> dict[str, Optional[str]]:
    """Where /docs, /redoc and /openapi.json are served: nowhere in production unless DOCS_ENABLED.

    The schema maps every endpoint and parameter; public, it is free reconnaissance.
    """
    served = env != "production" or docs_enabled
    return {
        "docs_url": "/docs" if served else None,
        "redoc_url": "/redoc" if served else None,
        "openapi_url": "/openapi.json" if served else None,
    }


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="Open-source API for authenticated web data — for developers and AI agents.",
    lifespan=lifespan,
    **_docs_urls(settings.env, settings.docs_enabled),
)

# Instrument now, while the app is being imported: the tracing middleware
# must be in place before the first (lifespan) message builds the stack.
init_tracing(app, settings)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


def _require_metrics_token(request: Request) -> None:
    """With METRICS_TOKEN set, /metrics answers only 'Authorization: Bearer <METRICS_TOKEN>'."""
    if not settings.metrics_token:
        return
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(
        token.strip().encode("utf-8"), settings.metrics_token.encode("utf-8")
    ):
        raise HTTPException(
            status_code=401,
            detail="A valid metrics token is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )


try:
    from prometheus_fastapi_instrumentator import Instrumentator

    Instrumentator().instrument(app).expose(
        app,
        endpoint="/metrics",
        include_in_schema=False,
        dependencies=[Depends(_require_metrics_token)],
    )
    # Importing the metrics module registers the custom collectors (browser
    # pool gauge, extraction + MFA counters) so they appear at /metrics. The
    # engine and browser pool record into them via src.metrics recorders.
    import src.metrics  # noqa: F401

    logger.info("Prometheus metrics enabled at /metrics")
except ImportError:
    logger.warning("prometheus-fastapi-instrumentator not installed, /metrics endpoint disabled")


_cors_origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]

if settings.env == "production" and "*" in _cors_origins:
    logger.critical(
        "CORS wildcard (*) is not allowed in production. "
        "Set CORS_ORIGINS to specific origins (e.g. 'https://app.example.com')."
    )
    raise RuntimeError("Refusing to start with wildcard CORS in production.")

if "*" in _cors_origins:
    logger.warning(
        "CORS wildcard (*) is enabled. This is acceptable in development "
        "but must be restricted before production deployment."
    )


class RequestBodyLimitMiddleware:
    """Refuse request bodies over ``max_bytes``: declared (Content-Length) or streamed (chunked).

    A chunked upload has no Content-Length, so the bytes are counted as the
    application reads them; going over raises a 413 from the read itself.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        for name, value in scope.get("headers") or []:
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    await JSONResponse(status_code=400, content={"detail": "Invalid Content-Length header."})(
                        scope, receive, send
                    )
                    return
                if declared > self.max_bytes:
                    await JSONResponse(status_code=413, content={"detail": _BODY_TOO_LARGE})(scope, receive, send)
                    return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise HTTPException(status_code=413, detail=_BODY_TOO_LARGE)
            return message

        await self.app(scope, limited_receive, send)


app.add_middleware(RequestBodyLimitMiddleware, max_bytes=MAX_REQUEST_BODY_SIZE)


# Probes, metrics scrapes, the hosted page and its own status polling and
# event stream, and its static assets never count against the default limit.
_DEFAULT_LIMIT_EXEMPT = re.compile(
    r"^/(?:health(?:/detailed)?|metrics|link|ui-next/.*|link/events/[^/]+|link/sessions/[^/]+/status)/?$"
)


@lru_cache(maxsize=8)
def _parsed_default_limit(value: str):
    return parse_rate_limit(value)


class DefaultRateLimitMiddleware:
    """Apply RATE_LIMIT_DEFAULT to every other request, per client address and path.

    slowapi's own middleware can't find handlers inside FastAPI's nested
    routers, so it could neither apply the default nor honour exemptions.
    Routes with their own, tighter limit (auth, connect, MFA, encryption)
    keep it on top of this. An unreachable limiter backend fails open, like
    the route limits.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not limiter.enabled or _DEFAULT_LIMIT_EXEMPT.match(path):
            await self.app(scope, receive, send)
            return

        item = _parsed_default_limit(settings.rate_limit_default)
        client = (scope.get("client") or ("unknown", 0))[0]
        try:
            allowed = await asyncio.to_thread(limiter.limiter.hit, item, "default", client, path)
            reset_at = (
                None
                if allowed
                else (
                    await asyncio.to_thread(limiter.limiter.get_window_stats, item, "default", client, path)
                ).reset_time
            )
        except Exception as exc:
            logger.warning(f"Default rate limit check failed: {exc}")
            allowed, reset_at = True, None

        if allowed:
            await self.app(scope, receive, send)
            return
        retry_after = max(1, int(reset_at - time.time())) if reset_at else 60
        response = JSONResponse(
            status_code=429,
            content={"detail": f"Rate limit exceeded: {settings.rate_limit_default}."},
            headers={"Retry-After": str(retry_after)},
        )
        await response(scope, receive, send)


app.add_middleware(DefaultRateLimitMiddleware)

# Registered after (so outside) the body-size and default rate limits: their
# 413 and 429 answers carry CORS headers a browser client can read, and CORS
# preflights are answered here without counting against the default limit.
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    """Add X-Request-ID header for request tracing and correlation."""
    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


def _link_frame_ancestors(link_token: str) -> str:
    """frame-ancestors for the hosted page of one session: 'self' plus the origins it allows."""
    session = session_store.get_link_session(link_token)
    if not session:
        return "'self'"
    origins: list[str] = []
    for candidate in list(session.get("allowed_origins") or []) + [session.get("allowed_origin")]:
        normalized = (candidate or "").rstrip("/")
        if normalized and normalized not in origins:
            origins.append(normalized)
    return " ".join(["'self'", *origins])


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    """Add standard security headers to every response."""
    is_hosted_link_html = request.url.path == "/link"
    link_tokens = request.query_params.getlist("token") if is_hosted_link_html else []
    if len(link_tokens) > 1:
        # The page and this header must read the same session: with two
        # tokens they could disagree, and the frame allowlist of one session
        # would cover the page of another.
        response = JSONResponse(status_code=400, content={"detail": "The link URL must carry exactly one token."})
    else:
        response = await call_next(request)

    frame_ancestors = "'self'"
    if is_hosted_link_html and len(link_tokens) == 1 and link_tokens[0]:
        frame_ancestors = _link_frame_ancestors(link_tokens[0])

    response.headers["X-Content-Type-Options"] = "nosniff"
    if is_hosted_link_html and frame_ancestors != "'self'":
        if "X-Frame-Options" in response.headers:
            del response.headers["X-Frame-Options"]
    else:
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        f"frame-ancestors {frame_ancestors}"
    )

    if settings.enforce_https or settings.env == "production":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

    return response


class HTTPSRedirectExceptProbesMiddleware:
    """Redirect plain-HTTP requests to HTTPS, except the probes at exactly /health and /metrics.

    Load-balancer health checks, container probes and the Prometheus scrape
    reach the API directly over plain HTTP and must get their 200/503, not a
    307. A request that arrived over HTTPS at a trusted proxy is not
    redirected: uvicorn rewrites the scheme from X-Forwarded-Proto for peers
    in FORWARDED_ALLOW_IPS (see gunicorn.conf.py).
    """

    _PROBE_PATHS = frozenset({"/health", "/metrics"})

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.redirect = HTTPSRedirectMiddleware(app)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("path") in self._PROBE_PATHS:
            await self.app(scope, receive, send)
        else:
            await self.redirect(scope, receive, send)


if settings.enforce_https or settings.env == "production":
    app.add_middleware(HTTPSRedirectExceptProbesMiddleware)
    logger.info("HTTPS enforcement enabled (except /health and /metrics)")


if FRONTEND_NEXT_DIST.exists():
    try:
        app.mount(
            "/ui-next",
            StaticFiles(directory=str(FRONTEND_NEXT_DIST), html=False),
            name="frontend-next",
        )
    except Exception:
        logger.warning("frontend-next/dist present but could not be mounted at /ui-next")
else:
    logger.info("frontend-next/dist not found; /ui-next will not be served (run 'npm run build' in frontend-next)")


@app.exception_handler(PlaidifyError)
async def plaidify_error_handler(request: Request, exc: PlaidifyError) -> JSONResponse:
    """Catch PlaidifyError subclasses and return structured JSON."""
    code = exc.error_code.value if hasattr(exc.error_code, "value") else str(exc.error_code)
    logger.error(
        exc.message,
        extra={
            "extra_data": {
                "status_code": exc.status_code,
                "path": request.url.path,
                "error_code": code,
            }
        },
    )
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.message, "error_code": code},
    )


for router in (
    system.router,
    connection.router,
    auth.router,
    access_jobs.router,
    links.router,
    link_sessions.router,
    consent.router,
    audit.router,
    api_keys.router,
    webhooks.router,
    registry.router,
    refresh.router,
    agents.router,
    admin.router,
):
    app.include_router(router)


__all__ = ["app", "settings"]
