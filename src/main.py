"""
Plaidify API Server — FastAPI application entry point.

Provides endpoints for:
- Site connections (connect, create_link, submit_credentials, fetch_data)
- User authentication (register, login, OAuth2, profile)
- Link and token management (CRUD)
- System health checks

All endpoint logic lives in src/routers/. This file handles:
- App creation, lifespan, middleware, exception handlers
- Router registration
- Static file mounts
"""

import asyncio
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from src.config import get_settings
from src.database import init_db
from src.dependencies import limiter
from src.exceptions import PlaidifyError
from src.logging_config import get_logger, setup_logging

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

    # Pre-warm browser pool (lazy — actual start happens on first connection)
    logger.info("Browser engine ready (Playwright, lazy-start)")

    yield

    # Graceful shutdown — drain in-flight browser sessions
    from src.core.browser_pool import shutdown_browser_pool

    logger.info("Shutting down browser pool...")
    shutdown_timeout = 30  # seconds
    try:
        await asyncio.wait_for(shutdown_browser_pool(), timeout=shutdown_timeout)
        logger.info("Browser pool shut down cleanly")
    except asyncio.TimeoutError:
        logger.warning(
            f"Browser pool shutdown timed out after {shutdown_timeout}s, forcing..."
        )
    except Exception as e:
        logger.error(f"Error during browser pool shutdown: {e}")

    # Close Redis connections if any
    try:
        from src.crypto import _get_redis

        r = _get_redis()
        if r is not None:
            r.close()
            logger.info("Redis connection closed")
    except Exception:
        pass

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


# ── Prometheus Metrics ────────────────────────────────────────────────────────

try:
    from prometheus_fastapi_instrumentator import Instrumentator

    Instrumentator().instrument(app).expose(app, endpoint="/metrics")
    logger.info("Prometheus metrics enabled at /metrics")
except ImportError:
    logger.warning(
        "prometheus-fastapi-instrumentator not installed, /metrics endpoint disabled"
    )


# ── CORS ──────────────────────────────────────────────────────────────────────

_cors_origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]

if settings.env == "production" and "*" in _cors_origins:
    logger.critical(
        "CORS wildcard (*) is not allowed in production. "
        "Set CORS_ORIGINS to specific origins (e.g. 'https://app.example.com')."
    )
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

    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "frame-ancestors 'self'"
    )

    if settings.enforce_https or settings.env == "production":
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )

    return response


# ── Request Body Size Limit ───────────────────────────────────────────────────

MAX_REQUEST_BODY_SIZE = 1 * 1024 * 1024  # 1 MB


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


# ── HTTPS Redirect Middleware ─────────────────────────────────────────────────

if settings.enforce_https or settings.env == "production":
    from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware

    app.add_middleware(HTTPSRedirectMiddleware)
    logger.info("HTTPS enforcement enabled")


# ── Static Files ──────────────────────────────────────────────────────────────

try:
    app.mount("/ui", StaticFiles(directory="frontend", html=True), name="frontend")
except Exception:
    logger.warning("Frontend directory not found, /ui will not be served")


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


# ── Register Routers ─────────────────────────────────────────────────────────

from src.routers.agents import router as agents_router
from src.routers.api_keys import router as api_keys_router
from src.routers.audit import router as audit_router
from src.routers.auth import router as auth_router
from src.routers.connection import router as connection_router
from src.routers.consent import router as consent_router
from src.routers.link_sessions import router as link_sessions_router
from src.routers.links import router as links_router
from src.routers.refresh import router as refresh_router
from src.routers.registry import router as registry_router
from src.routers.system import router as system_router
from src.routers.webhooks import router as webhooks_router

app.include_router(system_router)
app.include_router(auth_router)
app.include_router(registry_router)
app.include_router(connection_router)
app.include_router(links_router)
app.include_router(link_sessions_router)
app.include_router(consent_router)
app.include_router(webhooks_router)
app.include_router(audit_router)
app.include_router(api_keys_router)
app.include_router(agents_router)
app.include_router(refresh_router)
