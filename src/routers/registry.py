"""
Blueprint registry endpoints: publish, search, download, delete.
"""

import json as json_mod
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from src.database import BlueprintRecord, User, get_db
from src.dependencies import get_current_user
from src.logging_config import get_logger

logger = get_logger("api.registry")

_VALID_QUALITY_TIERS = {"community", "tested", "certified"}

router = APIRouter(prefix="/registry", tags=["registry"])


@router.post("/publish")
async def registry_publish(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Publish a blueprint to the registry.

    The full blueprint JSON is validated, and metadata is extracted and stored.
    If a blueprint with the same site name exists and belongs to the same user,
    it is updated (version bump).
    """
    from src.core.blueprint import load_blueprint_from_dict

    body = await request.json()
    blueprint_json = body.get("blueprint")
    description = body.get("description", "")

    if not blueprint_json:
        raise HTTPException(
            status_code=422,
            detail="'blueprint' field is required (the full blueprint JSON object).",
        )

    if isinstance(blueprint_json, str):
        try:
            blueprint_json = json_mod.loads(blueprint_json)
        except json_mod.JSONDecodeError:
            raise HTTPException(status_code=422, detail="'blueprint' is not valid JSON.")

    # Validate the blueprint by parsing it
    try:
        bp = load_blueprint_from_dict(blueprint_json)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Invalid blueprint: {e}")

    site = blueprint_json.get("domain", "").replace(".", "_").replace(" ", "_").lower()
    if not site:
        site = bp.name.lower().replace(" ", "_")

    # Check for existing blueprint
    existing = db.query(BlueprintRecord).filter_by(site=site).first()
    if existing:
        if existing.published_by != user.id:
            raise HTTPException(
                status_code=403,
                detail="A blueprint for this site already exists and belongs to another user.",
            )
        # Update existing
        existing.name = bp.name
        existing.domain = bp.domain
        existing.description = description or existing.description
        existing.schema_version = bp.schema_version
        existing.tags = json_mod.dumps(bp.tags) if bp.tags else "[]"
        existing.has_mfa = bp.mfa is not None
        existing.blueprint_json = json_mod.dumps(blueprint_json)
        existing.extract_fields = json_mod.dumps(list(bp.extract.keys()))
        existing.updated_at = datetime.now(timezone.utc)
        # Bump version
        parts = existing.version.split(".")
        parts[-1] = str(int(parts[-1]) + 1)
        existing.version = ".".join(parts)
        db.commit()
        logger.info(
            "Blueprint updated in registry",
            extra={"extra_data": {"site": site, "version": existing.version}},
        )
        return {"status": "updated", "site": site, "version": existing.version}

    # Create new
    record = BlueprintRecord(
        name=bp.name,
        site=site,
        domain=bp.domain,
        description=description,
        author=user.username or user.email,
        version="1.0.0",
        schema_version=bp.schema_version,
        tags=json_mod.dumps(bp.tags) if bp.tags else "[]",
        has_mfa=bp.mfa is not None,
        quality_tier="community",
        blueprint_json=json_mod.dumps(blueprint_json),
        extract_fields=json_mod.dumps(list(bp.extract.keys())),
        published_by=user.id,
    )
    db.add(record)
    db.commit()
    logger.info("Blueprint published to registry", extra={"extra_data": {"site": site}})
    return {
        "status": "published",
        "site": site,
        "version": "1.0.0",
        "quality_tier": "community",
    }


@router.get("/search")
async def registry_search(
    q: str | None = None,
    tag: str | None = None,
    tier: str | None = None,
    db: Session = Depends(get_db),
):
    """Search the blueprint registry by name, domain, tag, or quality tier."""
    query = db.query(BlueprintRecord)

    if q:
        search_term = f"%{q}%"
        query = query.filter(
            (BlueprintRecord.name.ilike(search_term))
            | (BlueprintRecord.domain.ilike(search_term))
            | (BlueprintRecord.site.ilike(search_term))
            | (BlueprintRecord.description.ilike(search_term))
        )
    if tag:
        query = query.filter(BlueprintRecord.tags.ilike(f'%"{tag}"%'))
    if tier:
        if tier not in _VALID_QUALITY_TIERS:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid tier. Must be one of: {', '.join(_VALID_QUALITY_TIERS)}",
            )
        query = query.filter_by(quality_tier=tier)

    results = query.order_by(BlueprintRecord.downloads.desc()).all()
    return {
        "results": [
            {
                "site": r.site,
                "name": r.name,
                "domain": r.domain,
                "description": r.description,
                "author": r.author,
                "version": r.version,
                "schema_version": r.schema_version,
                "tags": json_mod.loads(r.tags) if r.tags else [],
                "has_mfa": r.has_mfa,
                "quality_tier": r.quality_tier,
                "extract_fields": (json_mod.loads(r.extract_fields) if r.extract_fields else []),
                "downloads": r.downloads,
            }
            for r in results
        ],
        "count": len(results),
    }


@router.get("/{site_name}")
async def registry_get(
    site_name: str,
    db: Session = Depends(get_db),
):
    """Download a blueprint from the registry.

    Increments the download counter.
    """
    record = db.query(BlueprintRecord).filter_by(site=site_name).first()
    if not record:
        raise HTTPException(
            status_code=404,
            detail=f"Blueprint '{site_name}' not found in registry.",
        )

    record.downloads = (record.downloads or 0) + 1
    db.commit()

    return {
        "site": record.site,
        "name": record.name,
        "domain": record.domain,
        "description": record.description,
        "author": record.author,
        "version": record.version,
        "schema_version": record.schema_version,
        "tags": json_mod.loads(record.tags) if record.tags else [],
        "has_mfa": record.has_mfa,
        "quality_tier": record.quality_tier,
        "extract_fields": (json_mod.loads(record.extract_fields) if record.extract_fields else []),
        "downloads": record.downloads,
        "blueprint": json_mod.loads(record.blueprint_json),
    }


@router.delete("/{site_name}")
async def registry_delete(
    site_name: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Remove a blueprint from the registry (owner only)."""
    record = db.query(BlueprintRecord).filter_by(site=site_name).first()
    if not record:
        raise HTTPException(
            status_code=404,
            detail=f"Blueprint '{site_name}' not found in registry.",
        )
    if record.published_by != user.id:
        raise HTTPException(status_code=403, detail="Only the blueprint owner can delete it.")
    db.delete(record)
    db.commit()
    return {"status": "deleted", "site": site_name}
