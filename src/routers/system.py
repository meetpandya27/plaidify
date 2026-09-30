"""
System endpoints: root, health, status, blueprint discovery, blueprint generation.
"""

import asyncio
import secrets
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from src.config import get_settings
from src.core.browser_pool import get_browser_pool
from src.database import User, get_db
from src.dependencies import get_admin_user, get_current_user_or_api_key
from src.error_taxonomy import serialize_taxonomy
from src.logging_config import get_logger
from src.organization_catalog import (
    get_organization_by_id,
    get_organization_summary,
    refresh_organization_catalog,
    search_organizations,
)

settings = get_settings()
logger = get_logger("api.system")

# Per-dependency timeout for /health/detailed probes so a single stuck backend
# (hung Playwright launch, unreachable Redis socket, slow KMS) can't block the
# health endpoint and cascade into load-balancer timeouts.
_HEALTH_CHECK_TIMEOUT = 5.0

router = APIRouter(tags=["system"])


@router.get("/")
async def root():
    """Root endpoint with welcome message."""
    return {
        "message": f"Welcome to {settings.app_name}!",
        "version": settings.app_version,
        "docs": "/docs",
    }


@router.get("/health")
async def health(db: Session = Depends(get_db)):
    """
    Public health check endpoint for load balancers and uptime monitors.

    Returns a simple status without exposing internal details.
    """
    try:
        from sqlalchemy import text

        db.execute(text("SELECT 1"))
        return {"status": "healthy"}
    except Exception:
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy"},
        )


def _browser_pool_state() -> str:
    """The browser pool's state, without starting it (a probe must never launch Chromium)."""
    from src.core import browser_pool

    pool = browser_pool._pool
    if pool is None or not getattr(pool, "_running", False):
        return "not_started"
    return "ok" if pool.is_healthy else "disconnected"


@router.get("/health/detailed")
async def health_detailed(
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Detailed health check with bearer-token gating.

    Returns system status, version, database, browser pool, and Redis connectivity.
    With HEALTH_CHECK_TOKEN set, callers need that token (or a login / API
    key); in production it is required, and without it the endpoint is off.
    The browser pool is reported as it is: the probe never starts it.
    """
    if settings.env == "production" and not settings.health_check_token:
        raise HTTPException(status_code=404, detail="Detailed health is disabled: HEALTH_CHECK_TOKEN is not set.")
    if settings.health_check_token:
        auth_header = request.headers.get("authorization", "")
        bearer_token = auth_header[7:] if auth_header.startswith("Bearer ") else None

        if not (bearer_token and secrets.compare_digest(bearer_token, settings.health_check_token)):
            try:
                get_current_user_or_api_key(request, db)
            except HTTPException as exc:
                if exc.status_code == 401:
                    raise HTTPException(
                        status_code=401,
                        detail="Invalid health check token or authentication.",
                    )
                raise

    checks = {}

    # Database check
    try:
        from sqlalchemy import text

        db.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception:
        checks["database"] = "error"

    # Browser pool check. The pool starts lazily on the first connection; a
    # probe must not launch Chromium, so an unstarted pool is reported as such.
    # A pool whose browser crashed says "disconnected" instead of "ok".
    try:
        checks["browser_pool"] = _browser_pool_state()
    except Exception:
        checks["browser_pool"] = "unavailable"

    # Redis check (ping off the event loop with a timeout so a hung socket
    # can't block the worker thread)
    try:
        from src.crypto import _get_redis

        r = _get_redis()
        if r is not None:
            await asyncio.wait_for(asyncio.to_thread(r.ping), timeout=_HEALTH_CHECK_TIMEOUT)
            checks["redis"] = "ok"
        else:
            checks["redis"] = "not_configured"
    except asyncio.TimeoutError:
        checks["redis"] = "timeout"
    except Exception:
        checks["redis"] = "error"

    # KMS check (verifies the configured provider can wrap/unwrap credentials)
    try:
        from src.kms import get_kms_provider

        kms_result = await asyncio.wait_for(get_kms_provider().health_check(), timeout=_HEALTH_CHECK_TIMEOUT)
        kms_status = kms_result.get("status", "unknown")
        checks["kms"] = "ok" if kms_status == "healthy" else kms_status
    except asyncio.TimeoutError:
        checks["kms"] = "timeout"
    except Exception:
        checks["kms"] = "error"

    # DB, Redis, and KMS are required to serve credential traffic; their failure
    # (or a probe timeout) marks the service degraded (503). Browser-pool
    # unavailability is non-fatal because the pool starts lazily on first use.
    error_states = {"error", "unhealthy", "degraded", "timeout"}
    critical_checks = ("database", "redis", "kms")
    has_errors = any(checks.get(name) in error_states for name in critical_checks)
    overall = "degraded" if has_errors else "healthy"
    status_code = 503 if has_errors else 200

    return JSONResponse(
        status_code=status_code,
        content={
            "status": overall,
            "version": settings.app_version,
            "checks": checks,
        },
    )


@router.get("/status")
async def app_status():
    """Simple status check."""
    return {"status": "API is running", "version": settings.app_version}


@router.get("/organizations/summary")
async def organization_summary():
    """Return high-level counts for the generated organization directory."""
    return get_organization_summary()


@router.get("/organizations/search")
async def organization_search(
    q: str | None = None,
    category: str | None = None,
    country: str | None = None,
    site: str | None = None,
    limit: int = 40,
    offset: int = 0,
    include_unsupported: bool = False,
):
    """Search the organization directory used by the hosted Link flow.

    Only organizations backed by a real connector are returned unless
    ``include_unsupported`` is set (demo mode adds unsupported sample entries).
    """
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 100.")
    if offset < 0:
        raise HTTPException(status_code=422, detail="offset must be greater than or equal to 0.")

    return search_organizations(
        q=q,
        category=category,
        country=country,
        site=site,
        limit=limit,
        offset=offset,
        include_unsupported=include_unsupported,
    )


@router.get("/organizations/{organization_id}")
async def organization_detail(organization_id: str):
    """Return a single organization directory entry."""
    entry = get_organization_by_id(organization_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Organization not found.")
    return entry


@router.get("/link/error-taxonomy")
async def link_error_taxonomy():
    """Return the shared hosted-link error taxonomy (issue #55).

    Shared between server, SDKs, and the hosted Link page so that error
    codes, remediation copy, and CTAs stay consistent across surfaces.
    """
    return serialize_taxonomy()


# ── Blueprint Discovery ──────────────────────────────────────────────────────


@router.get("/blueprints")
async def list_blueprints():
    """
    List all available blueprints.

    Returns the name and basic info for each blueprint in the connectors directory.
    """
    from pathlib import Path

    from src.core.blueprint import blueprint_is_discoverable, load_blueprint

    connectors_path = Path(settings.connectors_dir).resolve()
    blueprints = []

    if connectors_path.is_dir():
        for f in sorted(connectors_path.glob("*.json")):
            try:
                bp = load_blueprint(f)
                tags = bp.tags or []
                if not blueprint_is_discoverable(tags, demo_mode=settings.demo_mode):
                    continue
                blueprints.append(
                    {
                        "site": f.stem,
                        "name": bp.name,
                        "domain": bp.domain,
                        "tags": tags,
                        "has_mfa": bp.mfa is not None,
                        "schema_version": bp.schema_version,
                    }
                )
            except Exception as e:
                logger.warning(f"Failed to load blueprint {f.name}: {e}")

    return {"blueprints": blueprints, "count": len(blueprints)}


# ── Blueprint Auto-Generation ───────────────────────────────────────────────


async def _resolves(hostname: str) -> bool:
    """Whether ``hostname`` resolves at all (without blocking the event loop)."""
    try:
        await asyncio.get_running_loop().getaddrinfo(hostname, None)
        return True
    except (OSError, UnicodeError):
        return False


@router.post("/blueprints/generate")
async def generate_blueprint(
    request: Request,
    user: User = Depends(get_admin_user),
):
    """
    Auto-generate a V3 blueprint draft for an arbitrary website (administrators only).

    Takes a login page URL and uses an LLM + headless browser to discover
    the login form, identify fields, and generate a draft blueprint. The draft
    is returned for review; nothing is written unless ``save`` is true, and a
    saved blueprint is keyed by the URL's hostname and runs as untrusted (no
    JavaScript, no private networks).

    Body:
        url: str — Login page URL (required; http/https, no credentials in it)
        site_type: str — Hint like "banking", "utility", "insurance" (optional)
        site_name: str — Human-readable name (optional)
        save: bool — If true, save to the connectors directory (default: false)
        extra_fields: list — Additional extraction fields to include (optional)

    Returns:
        Generated blueprint JSON, confidence score, and any warnings.
    """
    import json as json_mod
    import re as _re
    from pathlib import Path
    from urllib.parse import urlsplit

    from src.core.blueprint_generator import BlueprintGenerator
    from src.core.llm_provider import create_provider
    from src.core.network_policy import AddressPolicy
    from src.core.read_only_policy import ExecutionPhase, ReadOnlyExecutionPolicy
    from src.core.step_executor import describe_browser_error

    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="Request body must be a JSON object.")
    url = body.get("url")
    site_type = body.get("site_type")
    site_name = body.get("site_name")
    save = body.get("save", False) is True
    extra_fields = body.get("extra_fields")

    if not url or not isinstance(url, str):
        raise HTTPException(status_code=422, detail="'url' field is required.")

    # Validate URL
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        parsed.port  # noqa: B018 - raises ValueError on a malformed port
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid URL.")
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=422, detail="URL must use http or https scheme.")
    if parsed.username is not None or parsed.password is not None:
        raise HTTPException(status_code=422, detail="URL must not contain credentials (user:password@host).")
    if not parsed.netloc or not hostname:
        raise HTTPException(status_code=422, detail="Invalid URL: missing hostname.")

    # SSRF protection. This first check gives a clear error; the browser session
    # below applies the same policy to every request the page makes, redirects included.
    address_policy = AddressPolicy(
        block_private=True,
        allow_loopback=settings.demo_mode or settings.engine_allow_internal_connectors,
    )
    if not await _resolves(hostname):
        raise HTTPException(status_code=422, detail=f"Cannot resolve hostname: {hostname}")
    reason = await address_policy.host_block_reason(hostname)
    if reason:
        raise HTTPException(
            status_code=422,
            detail="URL resolves to a private/internal address. Only public URLs are allowed.",
        )

    # Ensure LLM is configured
    if not settings.llm_api_key:
        raise HTTPException(
            status_code=503,
            detail="LLM provider not configured. Set LLM_API_KEY to enable blueprint generation.",
        )

    # Create LLM provider
    provider = create_provider(
        settings.llm_provider,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        base_url=settings.llm_base_url,
        timeout=settings.llm_timeout,
    )

    generator = BlueprintGenerator(provider)

    # Navigate browser to the URL and analyze. The page is only read: no form
    # submissions, and every request goes through the address policy.
    session_id = f"blueprint_gen_{uuid.uuid4().hex[:8]}"
    read_policy = ReadOnlyExecutionPolicy(enabled=True, phase=ExecutionPhase.READ)
    pool = None
    ctx = None
    try:
        pool = await get_browser_pool()
        ctx = await pool.acquire(session_id, read_only_policy=read_policy, address_policy=address_policy)
        page = await ctx.context.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        # Small wait for JS rendering
        await page.wait_for_timeout(2000)
        if ctx.network_violation:
            raise HTTPException(status_code=422, detail="The page redirected to an address that is not allowed.")

        result = await generator.generate(
            url=url,
            page=page,
            site_type=site_type,
            site_name=site_name,
            extra_fields=extra_fields if isinstance(extra_fields, list) else None,
        )
    except HTTPException:
        raise
    except Exception as e:
        reason = describe_browser_error(e)
        logger.error("Blueprint generation failed: %s", reason)
        if ctx is not None and ctx.network_violation:
            raise HTTPException(status_code=422, detail="The page redirected to an address that is not allowed.")
        raise HTTPException(
            status_code=500,
            detail="Blueprint generation failed.",
        )
    finally:
        if ctx is not None and pool is not None:
            await pool.release(session_id)
        await provider.close()

    # Optionally save to connectors directory (the draft is untrusted either way)
    saved_path = None
    if save:
        site_key = result.site_key
        if not _re.match(r"^[a-zA-Z0-9_-]+$", site_key):
            raise HTTPException(status_code=422, detail="Could not derive a valid site key from the URL.")

        connectors_path = Path(settings.connectors_dir).resolve()
        file_path = (connectors_path / f"{site_key}.json").resolve()
        if file_path.parent != connectors_path:
            raise HTTPException(status_code=422, detail="Could not derive a valid site key from the URL.")
        try:
            with open(file_path, "x") as f:
                json_mod.dump(result.blueprint_json, f, indent=2)
        except FileExistsError:
            raise HTTPException(
                status_code=409,
                detail=f"Blueprint already exists for site key '{site_key}'.",
            )
        saved_path = str(file_path)
        logger.info(
            "Generated blueprint saved",
            extra={"extra_data": {"site": site_key, "admin_user_id": user.id}},
        )

        refresh_organization_catalog()

    return {
        "blueprint": result.blueprint_json,
        "site_key": result.site_key,
        "domain": result.domain,
        "confidence": result.confidence,
        "warnings": result.warnings,
        "saved": saved_path,
    }


@router.get("/blueprints/{site}")
async def get_blueprint_info(site: str):
    """
    Get detailed info about a specific blueprint.

    Does NOT include auth steps or selectors (security).
    """
    import re as _re
    from pathlib import Path

    from src.core.blueprint import blueprint_is_discoverable, load_blueprint

    # Validate site name to prevent path traversal
    if not _re.match(r"^[a-zA-Z0-9_-]+$", site):
        raise HTTPException(status_code=400, detail="Invalid site name.")

    connectors_dir = Path(settings.connectors_dir).resolve()
    blueprint_path = connectors_dir / f"{site}.json"

    # Ensure the resolved path stays within the connectors directory (a
    # string-prefix check would also accept a sibling like "connectors-evil").
    if not blueprint_path.resolve().is_relative_to(connectors_dir):
        raise HTTPException(status_code=400, detail="Invalid site name.")

    if not blueprint_path.exists():
        raise HTTPException(status_code=404, detail=f"Blueprint not found: {site}")

    bp = load_blueprint(blueprint_path)
    if not blueprint_is_discoverable(bp.tags, demo_mode=settings.demo_mode):
        raise HTTPException(status_code=404, detail=f"Blueprint not found: {site}")
    return {
        "name": bp.name,
        "domain": bp.domain,
        "tags": bp.tags,
        "has_mfa": bp.mfa is not None,
        "extract_fields": list(bp.extract.keys()),
        "schema_version": bp.schema_version,
        "rate_limit": bp.rate_limit.model_dump() if bp.rate_limit else None,
    }
