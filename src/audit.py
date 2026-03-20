"""
Tamper-evident audit logging with SHA-256 hash chains.

Every audit entry hashes its own content together with the previous entry's hash,
forming a linked chain that can be verified end-to-end.
"""

import hashlib
import json
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from src.database import AuditLog
from src.logging_config import get_logger

logger = get_logger(__name__)


def _compute_hash(
    event_type: str,
    user_id: Optional[int],
    resource: Optional[str],
    action: str,
    metadata_json: Optional[str],
    timestamp: str,
    prev_hash: Optional[str],
) -> str:
    """Compute SHA-256 hash for an audit log entry."""
    payload = f"{event_type}|{user_id}|{resource}|{action}|{metadata_json}|{timestamp}|{prev_hash}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def record_audit_event(
    db: Session,
    event_type: str,
    action: str,
    user_id: Optional[int] = None,
    resource: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> AuditLog:
    """Record a tamper-evident audit log entry.

    Args:
        db: Database session.
        event_type: Category of event (auth, data_access, token, key_rotation, consent).
        action: Specific action performed.
        user_id: ID of the user who performed the action.
        resource: The resource affected (e.g., link_token, access_token).
        metadata: Optional dict of additional context.

    Returns:
        The created AuditLog entry.
    """
    # Get the hash of the most recent entry for chain continuity
    last_entry = db.query(AuditLog).order_by(AuditLog.id.desc()).first()
    prev_hash = last_entry.entry_hash if last_entry else None

    ts = datetime.now(timezone.utc)
    # Use tz-naive ISO string for hashing (SQLite strips timezone info)
    ts_str = ts.replace(tzinfo=None).isoformat()
    metadata_json = json.dumps(metadata, default=str) if metadata else None

    entry_hash = _compute_hash(
        event_type, user_id, resource, action, metadata_json, ts_str, prev_hash
    )

    entry = AuditLog(
        event_type=event_type,
        user_id=user_id,
        resource=resource,
        action=action,
        metadata_json=metadata_json,
        timestamp=ts,
        prev_hash=prev_hash,
        entry_hash=entry_hash,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def verify_audit_chain(db: Session) -> dict:
    """Verify the integrity of the entire audit log hash chain.

    Returns:
        dict with keys: valid (bool), total (int), errors (list of dicts).
    """
    entries = db.query(AuditLog).order_by(AuditLog.id.asc()).all()
    errors = []
    prev_hash = None

    for entry in entries:
        # Check prev_hash linkage
        if entry.prev_hash != prev_hash:
            errors.append({
                "id": entry.id,
                "error": "prev_hash mismatch",
                "expected": prev_hash,
                "actual": entry.prev_hash,
            })

        # Recompute and verify entry_hash
        expected_hash = _compute_hash(
            entry.event_type,
            entry.user_id,
            entry.resource,
            entry.action,
            entry.metadata_json,
            entry.timestamp.isoformat(),
            entry.prev_hash,
        )
        if entry.entry_hash != expected_hash:
            errors.append({
                "id": entry.id,
                "error": "entry_hash mismatch",
                "expected": expected_hash,
                "actual": entry.entry_hash,
            })

        prev_hash = entry.entry_hash

    return {
        "valid": len(errors) == 0,
        "total": len(entries),
        "errors": errors,
    }
