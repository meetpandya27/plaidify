"""
Blueprint registry endpoints: publish, search, download, delete.
"""

import json as json_mod

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.database import BlueprintRecord, User, get_db, utcnow
from src.dependencies import get_current_user
from src.logging_config import get_logger
from src.models import RegistryPublishRequest

logger = get_logger("api.registry")

_VALID_QUALITY_TIERS = {"community", "tested", "certified"}

router = APIRouter(prefix="/registry", tags=["registry"])


def _may_manage(record: BlueprintRecord, user: User) -> bool:
    """A claimed site is changed or removed only by its publisher or an administrator."""
    return record.published_by == user.id or bool(getattr(user, "is_admin", False))


def _next_version(version: str) -> str:
    parts = (version or "1.0.0").split(".")
    try:
        parts[-1] = str(int(parts[-1]) + 1)
    except ValueError:
        parts.append("1")
    return ".".join(parts)


@router.post("/publish")
async def registry_publish(
    body: RegistryPublishRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Publish a blueprint to the registry.

    The full blueprint JSON is validated, and metadata is extracted and stored.
    The site key (from the blueprint's domain) is claimed by its first
    publisher: only that user, or an administrator, can publish updates for it
    (each update bumps the version).
    """
    from src.core.blueprint import load_blueprint_from_dict

    blueprint_json = body.blueprint
    if isinstance(blueprint_json, str):
        try:
            blueprint_json = json_mod.loads(blueprint_json)
        except json_mod.JSONDecodeError:
            raise HTTPException(status_code=422, detail="'blueprint' is not valid JSON.")
    if not isinstance(blueprint_json, dict) or not blueprint_json:
        raise HTTPException(status_code=422, detail="'blueprint' must be a JSON object.")

    # Validate the blueprint by parsing it
    try:
        bp = load_blueprint_from_dict(blueprint_json)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Invalid blueprint: {e}")

    domain = blueprint_json.get("domain") or ""
    site = str(domain).replace(".", "_").replace(" ", "_").lower() if isinstance(domain, str) else ""
    if not site:
        site = bp.name.lower().replace(" ", "_")
    description = body.description or ""

    # Check for existing blueprint
    existing = db.query(BlueprintRecord).filter_by(site=site).with_for_update().first()
    if existing:
        if not _may_manage(existing, user):
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
        existing.updated_at = utcnow()
        existing.version = _next_version(existing.version)
        db.commit()
        logger.info(
            "Blueprint updated in registry",
            extra={"extra_data": {"site": site, "version": existing.version, "user_id": user.id}},
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
    try:
        db.commit()
    except IntegrityError:
        # Someone claimed the same site between our check and the insert.
        db.rollback()
        raise HTTPException(status_code=409, detail="A blueprint for this site was just published; try again.")
    logger.info("Blueprint published to registry", extra={"extra_data": {"site": site}})
    return {
        "status": "published",
        "site": site,
        "version": "1.0.0",
        "quality_tier": "community",
    }


@router.get("/search")
async def registry_search(
    q: str | None = Query(default=None, max_length=200),
    tag: str | None = Query(default=None, max_length=100),
    tier: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    """Search the blueprint registry by name, domain, tag, or quality tier (most downloaded first)."""
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

    results = query.order_by(BlueprintRecord.downloads.desc(), BlueprintRecord.id).offset(offset).limit(limit).all()
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

    Increments the download counter (one atomic UPDATE, so concurrent
    downloads are all counted).
    """
    counted = db.execute(
        update(BlueprintRecord)
        .where(BlueprintRecord.site == site_name)
        .values(downloads=func.coalesce(BlueprintRecord.downloads, 0) + 1)
        .execution_options(synchronize_session=False)
    ).rowcount
    db.commit()
    record = db.query(BlueprintRecord).filter_by(site=site_name).first() if counted else None
    if not record:
        raise HTTPException(
            status_code=404,
            detail=f"Blueprint '{site_name}' not found in registry.",
        )

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
    """Remove a blueprint from the registry (its publisher or an administrator)."""
    record = db.query(BlueprintRecord).filter_by(site=site_name).first()
    if not record:
        raise HTTPException(
            status_code=404,
            detail=f"Blueprint '{site_name}' not found in registry.",
        )
    if not _may_manage(record, user):
        raise HTTPException(status_code=403, detail="Only the blueprint owner can delete it.")
    db.delete(record)
    db.commit()
    return {"status": "deleted", "site": site_name}
