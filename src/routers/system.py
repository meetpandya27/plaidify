"""
System endpoints: root, health, status, blueprint discovery.
"""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from src.config import get_settings
from src.core.browser_pool import get_browser_pool
from src.database import get_db
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
    Health check endpoint.

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
        pool = get_browser_pool()
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


@router.get("/blueprints/{site}")
async def get_blueprint_info(site: str):
    """
    Get detailed info about a specific blueprint.

    Does NOT include auth steps or selectors (security).
    """
    from pathlib import Path

    from src.core.blueprint import load_blueprint

    blueprint_path = Path(settings.connectors_dir).resolve() / f"{site}.json"
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
