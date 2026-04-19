"""
System endpoints: root, health, status, blueprint discovery, blueprint generation.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from src.config import get_settings
from src.core.browser_pool import get_browser_pool
from src.database import User, get_db
from src.dependencies import get_current_user
from src.logging_config import get_logger

settings = get_settings()
logger = get_logger("api.system")

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


@router.get("/health/detailed")
async def health_detailed(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """
    Detailed health check (authenticated).

    Returns system status, version, database, browser pool, and Redis connectivity.
    """
    checks = {}

    # Database check
    try:
        from sqlalchemy import text

        db.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception:
        checks["database"] = "error"

    # Browser pool check
    try:
        get_browser_pool()
        checks["browser_pool"] = "ok"
    except Exception:
        checks["browser_pool"] = "unavailable"

    # Redis check
    try:
        from src.crypto import _get_redis

        r = _get_redis()
        if r is not None:
            r.ping()
            checks["redis"] = "ok"
        else:
            checks["redis"] = "not_configured"
    except Exception:
        checks["redis"] = "error"

    has_errors = any(v == "error" for v in checks.values())
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


# ── Blueprint Discovery ──────────────────────────────────────────────────────


@router.get("/blueprints")
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
                blueprints.append(
                    {
                        "site": f.stem,
                        "name": bp.name,
                        "domain": bp.domain,
                        "tags": bp.tags,
                        "has_mfa": bp.mfa is not None,
                        "schema_version": bp.schema_version,
                    }
                )
            except Exception as e:
                logger.warning(f"Failed to load blueprint {f.name}: {e}")

    return {"blueprints": blueprints, "count": len(blueprints)}


# ── Blueprint Auto-Generation ───────────────────────────────────────────────


@router.post("/blueprints/generate")
async def generate_blueprint(
    request: Request,
    user: User = Depends(get_current_user),
):
    """
    Auto-generate a V3 blueprint for an arbitrary website.

    Takes a login page URL and uses an LLM + headless browser to discover
    the login form, identify fields, and generate a draft blueprint.

    Body:
        url: str — Login page URL (required)
        site_type: str — Hint like "banking", "utility", "insurance" (optional)
        site_name: str — Human-readable name (optional)
        save: bool — If true, save to connectors/ directory (default: false)
        extra_fields: list — Additional extraction fields to include (optional)

    Returns:
        Generated blueprint JSON, confidence score, and any warnings.
    """
    from urllib.parse import urlparse

    from src.core.blueprint_generator import BlueprintGenerator
    from src.core.llm_provider import create_provider

    body = await request.json()
    url = body.get("url")
    site_type = body.get("site_type")
    site_name = body.get("site_name")
    save = body.get("save", False)
    extra_fields = body.get("extra_fields")

    if not url:
        raise HTTPException(status_code=422, detail="'url' field is required.")

    # Validate URL
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=422, detail="URL must use http or https scheme.")
    if not parsed.netloc:
        raise HTTPException(status_code=422, detail="Invalid URL: missing hostname.")

    # SSRF protection: reject private/loopback/link-local addresses
    import ipaddress
    import socket

    hostname = parsed.hostname
    if not hostname:
        raise HTTPException(status_code=422, detail="Invalid URL: missing hostname.")
    try:
        resolved_ips = socket.getaddrinfo(hostname, None)
        for _family, _type, _proto, _canonname, sockaddr in resolved_ips:
            ip = ipaddress.ip_address(sockaddr[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                raise HTTPException(
                    status_code=422,
                    detail="URL resolves to a private/internal address. Only public URLs are allowed.",
                )
    except socket.gaierror:
        raise HTTPException(status_code=422, detail=f"Cannot resolve hostname: {hostname}")

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
    )

    generator = BlueprintGenerator(provider)

    # Navigate browser to the URL and analyze
    pool = get_browser_pool()
    session_id = f"blueprint_gen_{uuid.uuid4().hex[:8]}"
    ctx = None
    try:
        ctx = await pool.acquire(session_id)
        page = await ctx.context.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        # Small wait for JS rendering
        await page.wait_for_timeout(2000)

        result = await generator.generate(
            url=url,
            page=page,
            site_type=site_type,
            site_name=site_name,
            extra_fields=extra_fields,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Blueprint generation failed: %s", e)
        raise HTTPException(
            status_code=500,
            detail=f"Blueprint generation failed: {str(e)}",
        )
    finally:
        if ctx:
            await pool.release(session_id)
        await provider.close()

    # Optionally save to connectors directory
    saved_path = None
    if save:
        from pathlib import Path

        connectors_path = Path(settings.connectors_dir).resolve()
        file_path = connectors_path / f"{result.site_key}.json"
        if file_path.exists():
            raise HTTPException(
                status_code=409,
                detail=f"Blueprint already exists for site key '{result.site_key}'. Use PUT to update.",
            )
        import json as json_mod

        with open(file_path, "w") as f:
            json_mod.dump(result.blueprint_json, f, indent=2)
        saved_path = str(file_path)

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

    from src.core.blueprint import load_blueprint

    # Validate site name to prevent path traversal
    if not _re.match(r"^[a-zA-Z0-9_-]+$", site):
        raise HTTPException(status_code=400, detail="Invalid site name.")

    blueprint_path = Path(settings.connectors_dir).resolve() / f"{site}.json"

    # Ensure resolved path stays within connectors directory
    if not str(blueprint_path.resolve()).startswith(str(Path(settings.connectors_dir).resolve())):
        raise HTTPException(status_code=400, detail="Invalid site name.")

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
