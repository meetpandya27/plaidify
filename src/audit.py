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
    agent_id: Optional[str],
    resource: Optional[str],
    action: str,
    metadata_json: Optional[str],
    ip_address: Optional[str],
    timestamp: str,
    prev_hash: Optional[str],
) -> str:
    """Compute SHA-256 hash for an audit log entry."""
    payload = (
        f"{event_type}|{user_id}|{agent_id}|{resource}|{action}|{metadata_json}|{ip_address}|{timestamp}|{prev_hash}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def record_audit_event(
    db: Session,
    event_type: str,
    action: str,
    user_id: Optional[int] = None,
    agent_id: Optional[str] = None,
    resource: Optional[str] = None,
    metadata: Optional[dict] = None,
    ip_address: Optional[str] = None,
) -> AuditLog:
    """Record a tamper-evident audit log entry.

    Args:
        db: Database session.
        event_type: Category of event (auth, data_access, token, key_rotation, consent, agent, webhook).
        action: Specific action performed.
        user_id: ID of the user who performed the action.
        agent_id: ID of the agent that performed the action (if applicable).
        resource: The resource affected (e.g., link_token, access_token).
        metadata: Optional dict of additional context.
        ip_address: Client IP address.

    Returns:
        The created AuditLog entry.
    """
    # Get the hash of the most recent entry for chain continuity.
    # Use SELECT ... FOR UPDATE to prevent concurrent inserts from reading
    # the same prev_hash (PostgreSQL). SQLite serializes writes automatically.
    last_query = db.query(AuditLog).order_by(AuditLog.id.desc())
    try:
        last_entry = last_query.with_for_update().first()
    except Exception:
        # SQLite doesn't support FOR UPDATE — fall back to plain query
        last_entry = last_query.first()
    prev_hash = last_entry.entry_hash if last_entry else None

    ts = datetime.now(timezone.utc)
    # Use tz-naive ISO string for hashing (SQLite strips timezone info)
    ts_str = ts.replace(tzinfo=None).isoformat()
    metadata_json = json.dumps(metadata, default=str) if metadata else None

    entry_hash = _compute_hash(
        event_type, user_id, agent_id, resource, action, metadata_json, ip_address, ts_str, prev_hash
    )

    entry = AuditLog(
        event_type=event_type,
        user_id=user_id,
        agent_id=agent_id,
        resource=resource,
        action=action,
        metadata_json=metadata_json,
        ip_address=ip_address,
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
            errors.append(
                {
                    "id": entry.id,
                    "error": "prev_hash mismatch",
                    "expected": prev_hash,
                    "actual": entry.prev_hash,
                }
            )

        # Recompute and verify entry_hash
        expected_hash = _compute_hash(
            entry.event_type,
            entry.user_id,
            entry.agent_id,
            entry.resource,
            entry.action,
            entry.metadata_json,
            entry.ip_address,
            entry.timestamp.isoformat(),
            entry.prev_hash,
        )
        if entry.entry_hash != expected_hash:
            errors.append(
                {
                    "id": entry.id,
                    "error": "entry_hash mismatch",
                    "expected": expected_hash,
                    "actual": entry.entry_hash,
                }
            )

        prev_hash = entry.entry_hash

    return {
        "valid": len(errors) == 0,
        "total": len(entries),
        "errors": errors,
    }
