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
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from src import session_store
from src.access_jobs import shutdown_access_jobs
from src.config import get_settings
from src.core.browser_pool import shutdown_browser_pool
from src.crypto import _get_redis
from src.database import (
    AuditLog,
    RefreshToken,
    get_current_key_version,
    get_db,
    init_db,
    re_encrypt_tokens,
)
from src.dependencies import limiter
from src.exceptions import PlaidifyError
from src.logging_config import get_logger, setup_logging
from src.routers import (
    access_jobs,
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

settings = get_settings()
logger = get_logger("api")
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

_TOKEN_CLEANUP_INTERVAL = 3600
_AUDIT_CLEANUP_INTERVAL = 86400
_KEY_REENCRYPT_INTERVAL = 3600
MAX_REQUEST_BODY_SIZE = 1 * 1024 * 1024


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
    """Fail fast on production-only prerequisites."""
    if settings.env != "production":
        return

    if settings.debug:
        raise RuntimeError("DEBUG must be false in production.")

    if not settings.redis_url:
        raise RuntimeError("REDIS_URL is required in production for shared state and rate limiting.")

    redis_client = _get_redis()
    if redis_client is None:
        raise RuntimeError("Redis is required in production and must be reachable.")

    redis_client.ping()


async def _cleanup_expired_tokens() -> None:
    """Periodically purge expired and revoked refresh tokens."""
    while True:
        try:
            await asyncio.sleep(_TOKEN_CLEANUP_INTERVAL)
            db = next(get_db())
            try:
                now = datetime.now(timezone.utc)
                deleted = (
                    db.query(RefreshToken)
                    .filter(
                        (RefreshToken.expires_at < now) | (RefreshToken.revoked == True)  # noqa: E712
                    )
                    .delete(synchronize_session=False)
                )
                db.commit()
                if deleted:
                    logger.info(
                        "Cleaned up expired refresh tokens",
                        extra={"extra_data": {"count": deleted}},
                    )
            finally:
                db.close()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"Refresh token cleanup failed: {exc}")


async def _cleanup_old_audit_logs() -> None:
    """Periodically delete audit log entries older than retention period."""
    while True:
        try:
            await asyncio.sleep(_AUDIT_CLEANUP_INTERVAL)
            db = next(get_db())
            try:
                cutoff = datetime.now(timezone.utc) - timedelta(days=settings.audit_retention_days)
                deleted = db.query(AuditLog).filter(AuditLog.timestamp < cutoff).delete(synchronize_session=False)
                db.commit()
                if deleted:
                    logger.info(
                        "Archived old audit log entries",
                        extra={
                            "extra_data": {
                                "count": deleted,
                                "retention_days": settings.audit_retention_days,
                            }
                        },
                    )
            finally:
                db.close()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"Audit log cleanup failed: {exc}")


async def _reencrypt_stale_tokens() -> None:
    """Background job to re-encrypt tokens with outdated key versions."""
    while True:
        try:
            await asyncio.sleep(_KEY_REENCRYPT_INTERVAL)
            db = next(get_db())
            try:
                count = re_encrypt_tokens(db, batch_size=100)
                if count:
                    logger.info(
                        "Re-encrypted tokens to current key version",
                        extra={
                            "extra_data": {
                                "count": count,
                                "key_version": get_current_key_version(),
                            }
                        },
                    )
            finally:
                db.close()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"Token re-encryption failed: {exc}")


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

    logger.info("Browser engine ready (Playwright, lazy-start)")

    cleanup_task = asyncio.create_task(_cleanup_expired_tokens())
    audit_cleanup_task = asyncio.create_task(_cleanup_old_audit_logs())
    reencrypt_task = asyncio.create_task(_reencrypt_stale_tokens())

    yield

    for task in (cleanup_task, audit_cleanup_task, reencrypt_task):
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
            redis_client.close()
            logger.info("Redis connection closed")
    except Exception:
        pass

    logger.info("Shutting down Plaidify")


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="Open-source API for authenticated web data — for developers and AI agents.",
    lifespan=lifespan,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


try:
    from prometheus_client import Counter, Gauge
    from prometheus_fastapi_instrumentator import Instrumentator

    Instrumentator().instrument(app).expose(app, endpoint="/metrics")
    browser_pool_active = Gauge(
        "plaidify_browser_pool_active_contexts",
        "Number of active browser contexts in the pool",
    )
    extraction_total = Counter(
        "plaidify_blueprint_extractions_total",
        "Total blueprint data extractions",
        ["site", "status"],
    )
    mfa_challenges_total = Counter(
        "plaidify_mfa_challenges_total",
        "Total MFA challenges encountered",
        ["mfa_type"],
    )
    logger.info("Prometheus metrics enabled at /metrics")
except ImportError:
    logger.warning("prometheus-fastapi-instrumentator not installed, /metrics endpoint disabled")
    browser_pool_active = None
    extraction_total = None
    mfa_challenges_total = None


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


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    """Add standard security headers to every response."""
    response = await call_next(request)

    frame_ancestors = "'self'"
    is_hosted_link_html = request.url.path in {"/link", "/ui/link.html"}
    if is_hosted_link_html:
        link_token = request.query_params.get("token")
        if link_token:
            session = session_store.get_link_session(link_token)
            if session:
                origins: list[str] = []
                seen: set[str] = set()
                for candidate in list(session.get("allowed_origins") or []) + [session.get("allowed_origin")]:
                    if not candidate:
                        continue
                    normalized = candidate.rstrip("/")
                    if normalized and normalized not in seen:
                        seen.add(normalized)
                        origins.append(normalized)
                if origins:
                    frame_ancestors = " ".join(["'self'", *origins])

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


@app.middleware("http")
async def limit_request_body_middleware(request: Request, call_next):
    """Reject requests with bodies larger than MAX_REQUEST_BODY_SIZE."""
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > MAX_REQUEST_BODY_SIZE:
        return JSONResponse(
            status_code=413,
            content={"error": "Request body too large. Maximum size is 1MB."},
        )
    return await call_next(request)


if settings.enforce_https or settings.env == "production":
    from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware

    app.add_middleware(HTTPSRedirectMiddleware)
    logger.info("HTTPS enforcement enabled")


try:
    app.mount("/ui", StaticFiles(directory=str(FRONTEND_DIR), html=False), name="frontend")
except Exception:
    logger.warning("Frontend directory not found, /ui will not be served")


@app.exception_handler(PlaidifyError)
async def plaidify_error_handler(request: Request, exc: PlaidifyError) -> JSONResponse:
    """Catch PlaidifyError subclasses and return structured JSON."""
    logger.error(
        exc.message,
        extra={"extra_data": {"status_code": exc.status_code, "path": request.url.path}},
    )
    return JSONResponse(status_code=exc.status_code, content={"error": exc.message})


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
):
    app.include_router(router)


__all__ = ["app", "settings"]
