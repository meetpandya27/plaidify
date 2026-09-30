"""
Tamper-evident audit logging with a keyed hash chain.

Every entry stores ``entry_hash = HMAC-SHA256(key, entry fields ‖ prev_hash)``
plus the previous entry's hash, so the entries form a chain that cannot be
edited, reordered or extended without the key. The key never touches the
database: it is ``AUDIT_HMAC_KEY`` or, when that is unset, a key derived from
``ENCRYPTION_KEY`` with HKDF.

- Appends are serialized — ``pg_advisory_xact_lock`` on PostgreSQL; on SQLite
  the first write of the append (to the chain-head row) takes the database
  write lock — so concurrent requests never fork the chain.
- The chain-head row records the newest entry under its own MAC, so deleting
  the newest entries is detected, not only edits and gaps in the middle.
- Retention pruning deletes a prefix of the chain and appends a signed
  checkpoint naming where the retained chain starts; verification starts there.
- When the signing key changes (``AUDIT_HMAC_KEY`` rotated, or ``ENCRYPTION_KEY``
  rotated while the key is derived from it) the chain is sealed: verified under
  the old key, then attested by a checkpoint signed with the new one, so it
  stays verifiable once the old key is gone.
- Verification streams the table in batches; the endpoint is admin-only.
"""

import hashlib
import hmac
import json
from datetime import datetime, timedelta
from typing import Any, Optional

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import delete, func, select, text, true, update
from sqlalchemy.orm import Session

from src import database
from src.database import AuditChainHead, AuditLog, as_utc, utcnow
from src.logging_config import get_logger

logger = get_logger(__name__)

# key_id of entries hashed with plain SHA-256 before the chain was keyed.
LEGACY_KEY_ID = "sha256"
# Checkpoints are ordinary chain entries with this event type and action.
CHECKPOINT_EVENT = "audit"
CHECKPOINT_ACTION = "checkpoint"

_HEAD_ID = 1
_ADVISORY_LOCK_KEY = 7163902517031925761  # arbitrary; any 64-bit value unique to this chain
_KEY_INFO = b"plaidify/audit-chain/hmac-sha256/v1"
_KEY_ID_INFO = b"plaidify/audit-chain/key-id/v1"
_ENTRY_FORMAT = "plaidify-audit-v2"
_HEAD_FORMAT = "plaidify-audit-head-v1"
_production_key_warning_logged = False


class AuditChainInvalid(RuntimeError):
    """The chain failed verification, so it was not sealed."""

    def __init__(self, report: dict) -> None:
        self.report = report
        first = report["errors"][0] if report["errors"] else None
        super().__init__(f"audit chain failed verification ({report['error_count']} error(s)); first: {first}")


# ── Keys ──────────────────────────────────────────────────────────────────────


def _derive_key(material: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_KEY_INFO).derive(material)


def _key_id(key: bytes) -> str:
    """Public identifier of a signing key (reveals nothing about the key)."""
    return hmac.new(key, _KEY_ID_INFO, hashlib.sha256).hexdigest()[:16]


def _signing_key() -> tuple[str, bytes]:
    """(key_id, key) that signs new entries: AUDIT_HMAC_KEY, else derived from ENCRYPTION_KEY."""
    global _production_key_warning_logged
    settings = database.settings
    if settings.audit_hmac_key:
        key = _derive_key(settings.audit_hmac_key.encode("utf-8"))
    else:
        if settings.env == "production" and not _production_key_warning_logged:
            _production_key_warning_logged = True
            logger.warning(
                "AUDIT_HMAC_KEY is not set; the audit chain is signed with a key derived from ENCRYPTION_KEY. "
                "Set AUDIT_HMAC_KEY so the audit key is managed separately from the data key."
            )
        key = _derive_key(database._get_encryption_key())
    return _key_id(key), key


def _verification_keys() -> dict[str, bytes]:
    """Every key an entry may have been signed with, by key_id."""
    settings = database.settings
    materials = [database._get_encryption_key()]
    previous = database._get_previous_encryption_key()
    if previous is not None:
        materials.append(previous)
    for secret in (settings.audit_hmac_key, settings.audit_hmac_key_previous):
        if secret:
            materials.append(secret.encode("utf-8"))
    keys: dict[str, bytes] = {}
    for material in materials:
        key = _derive_key(material)
        keys.setdefault(_key_id(key), key)
    return keys


# ── Hashing ───────────────────────────────────────────────────────────────────


def _canonical_ts(value: datetime) -> str:
    return as_utc(value).replace(tzinfo=None).isoformat(timespec="microseconds")


def compute_entry_hash(
    key: bytes,
    *,
    key_id: str,
    event_type: str,
    user_id: Optional[int],
    agent_id: Optional[str],
    resource: Optional[str],
    action: str,
    metadata_json: Optional[str],
    ip_address: Optional[str],
    timestamp: datetime,
    prev_hash: Optional[str],
) -> str:
    """HMAC-SHA256 of an entry. Fields are JSON-encoded, so no separator can be forged."""
    payload = json.dumps(
        [
            _ENTRY_FORMAT,
            key_id,
            event_type,
            user_id,
            agent_id,
            resource,
            action,
            metadata_json,
            ip_address,
            _canonical_ts(timestamp),
            prev_hash,
        ],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hmac.new(key, payload.encode("utf-8"), hashlib.sha256).hexdigest()


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
    """Legacy unkeyed SHA-256 entry hash — only for entries with key_id "sha256"."""
    payload = (
        f"{event_type}|{user_id}|{agent_id}|{resource}|{action}|{metadata_json}|{ip_address}|{timestamp}|{prev_hash}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _head_mac(key: bytes, entry_id: Optional[int], entry_hash: Optional[str]) -> str:
    payload = json.dumps([_HEAD_FORMAT, entry_id, entry_hash], separators=(",", ":"))
    return hmac.new(key, payload.encode("utf-8"), hashlib.sha256).hexdigest()


def _row_hash(key: bytes, row: Any) -> str:
    return compute_entry_hash(
        key,
        key_id=row.key_id,
        event_type=row.event_type,
        user_id=row.user_id,
        agent_id=row.agent_id,
        resource=row.resource,
        action=row.action,
        metadata_json=row.metadata_json,
        ip_address=row.ip_address,
        timestamp=row.timestamp,
        prev_hash=row.prev_hash,
    )


def _legacy_row_hash(row: Any) -> str:
    return _compute_hash(
        row.event_type,
        row.user_id,
        row.agent_id,
        row.resource,
        row.action,
        row.metadata_json,
        row.ip_address,
        as_utc(row.timestamp).replace(tzinfo=None).isoformat(),
        row.prev_hash,
    )


# ── Appending ─────────────────────────────────────────────────────────────────


def _lock_chain(db: Session) -> None:
    """Hold the chain lock until the current transaction ends."""
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _ADVISORY_LOCK_KEY})
    else:
        # SQLite: the first write of a transaction takes the database write
        # lock, even when it matches no row, and keeps it until commit.
        db.execute(
            update(AuditChainHead)
            .where(AuditChainHead.id == _HEAD_ID)
            .values(id=AuditChainHead.id)
            .execution_options(synchronize_session=False)
        )


def _read_head(db: Session) -> Optional[AuditChainHead]:
    return db.execute(
        select(AuditChainHead).where(AuditChainHead.id == _HEAD_ID).execution_options(populate_existing=True)
    ).scalar_one_or_none()


def _checkpoint_json(**fields: Any) -> str:
    return json.dumps(fields, sort_keys=True, default=str)


def _as_text(value: Any) -> Optional[str]:
    return None if value is None else str(value)


def _append_entry(
    db: Session,
    head: AuditChainHead,
    signing_id: str,
    signing_key: bytes,
    *,
    event_type: str,
    action: str,
    user_id: Optional[int] = None,
    agent_id: Optional[str] = None,
    resource: Optional[str] = None,
    metadata_json: Optional[str] = None,
    ip_address: Optional[str] = None,
) -> AuditLog:
    """Append one entry after ``head`` and advance the head. Chain lock must be held."""
    entry = AuditLog(
        event_type=event_type,
        user_id=user_id,
        agent_id=agent_id,
        resource=resource,
        action=action,
        metadata_json=metadata_json,
        ip_address=ip_address,
        timestamp=utcnow(),
        prev_hash=head.last_entry_hash,
        key_id=signing_id,
    )
    entry.entry_hash = compute_entry_hash(
        signing_key,
        key_id=signing_id,
        event_type=event_type,
        user_id=user_id,
        agent_id=agent_id,
        resource=resource,
        action=action,
        metadata_json=metadata_json,
        ip_address=ip_address,
        timestamp=entry.timestamp,
        prev_hash=entry.prev_hash,
    )
    db.add(entry)
    db.flush()

    if head.key_id not in (None, LEGACY_KEY_ID, signing_id):
        head.seal_pending = True  # older entries are under another key
    head.last_entry_id = entry.id
    head.last_entry_hash = entry.entry_hash
    head.key_id = signing_id
    head.head_mac = _head_mac(signing_key, entry.id, entry.entry_hash)
    head.updated_at = entry.timestamp
    db.flush()
    return entry


def _ensure_head(db: Session, signing_id: str, signing_key: bytes) -> AuditChainHead:
    """The chain head, created on first use. Chain lock must be held."""
    head = _read_head(db)
    if head is not None:
        return head

    last = db.execute(
        select(AuditLog.id, AuditLog.entry_hash, AuditLog.key_id).order_by(AuditLog.id.desc()).limit(1)
    ).first()
    head = AuditChainHead(
        id=_HEAD_ID,
        last_entry_id=last.id if last else None,
        last_entry_hash=last.entry_hash if last else None,
        key_id=last.key_id if last else None,
        seal_pending=False,
    )
    db.add(head)
    db.flush()
    if last is not None and last.key_id != LEGACY_KEY_ID:
        # A keyed chain always has a head, so it was deleted. Adopting the
        # newest surviving entry would hide a truncation; leave evidence.
        logger.warning("Audit chain head was missing; restored from the newest entry")
        _append_entry(
            db,
            head,
            signing_id,
            signing_key,
            event_type=CHECKPOINT_EVENT,
            action=CHECKPOINT_ACTION,
            metadata_json=_checkpoint_json(kind="head_restored", restored_from_entry_id=last.id),
        )
    return head


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

    Commits the session (as before), holding the chain lock only for the
    append itself.

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
    metadata_json = json.dumps(metadata, default=str) if metadata else None
    signing_id, signing_key = _signing_key()
    try:
        _lock_chain(db)
        head = _ensure_head(db, signing_id, signing_key)
        # Hash exactly what the columns will hand back to the verifier.
        entry = _append_entry(
            db,
            head,
            signing_id,
            signing_key,
            event_type=str(event_type),
            action=str(action),
            user_id=int(user_id) if user_id is not None else None,
            agent_id=_as_text(agent_id),
            resource=_as_text(resource),
            metadata_json=metadata_json,
            ip_address=_as_text(ip_address),
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(entry)
    return entry


# ── Retention ─────────────────────────────────────────────────────────────────


def prune_audit_logs(
    db: Session,
    *,
    cutoff: Optional[datetime] = None,
    retention_days: Optional[int] = None,
    now: Optional[datetime] = None,
) -> int:
    """Delete entries older than the retention period without breaking the chain.

    Deletes the prefix of the chain before the first entry at or after
    ``cutoff`` (default: now − AUDIT_RETENTION_DAYS), then appends a signed
    checkpoint recording where the retained chain starts and the hash it links
    to, so ``verify_audit_chain`` starts from there. Commits and returns the
    number of entries deleted.
    """
    if cutoff is None:
        days = database.settings.audit_retention_days if retention_days is None else retention_days
        cutoff = (now or utcnow()) - timedelta(days=days)
    signing_id, signing_key = _signing_key()
    try:
        _lock_chain(db)
        head = _ensure_head(db, signing_id, signing_key)
        first_kept = db.execute(select(func.min(AuditLog.id)).where(AuditLog.timestamp >= cutoff)).scalar()
        doomed = AuditLog.id < first_kept if first_kept is not None else true()
        count, last_pruned = db.execute(select(func.count(AuditLog.id), func.max(AuditLog.id)).where(doomed)).one()
        if not count:
            db.rollback()
            return 0
        if first_kept is not None:
            anchor = db.execute(select(AuditLog.prev_hash).where(AuditLog.id == first_kept)).scalar()
        else:
            anchor = head.last_entry_hash  # everything goes; the checkpoint itself links to it
        db.execute(delete(AuditLog).where(doomed).execution_options(synchronize_session=False))
        _append_entry(
            db,
            head,
            signing_id,
            signing_key,
            event_type=CHECKPOINT_EVENT,
            action=CHECKPOINT_ACTION,
            metadata_json=_checkpoint_json(
                kind="prune",
                first_entry_id=first_kept,
                anchor_hash=anchor,
                pruned_count=count,
                pruned_through_id=last_pruned,
                cutoff=_canonical_ts(cutoff),
            ),
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    logger.info(
        "Pruned audit log entries",
        extra={"extra_data": {"count": count, "pruned_through_id": last_pruned}},
    )
    return count


# ── Verification ──────────────────────────────────────────────────────────────

_ENTRY_COLUMNS = (
    AuditLog.id,
    AuditLog.event_type,
    AuditLog.user_id,
    AuditLog.agent_id,
    AuditLog.resource,
    AuditLog.action,
    AuditLog.metadata_json,
    AuditLog.ip_address,
    AuditLog.timestamp,
    AuditLog.prev_hash,
    AuditLog.entry_hash,
    AuditLog.key_id,
)


def _checkpoint_mac_ok(row: Any, keys: dict[str, bytes], sealed_through: int) -> bool:
    key = keys.get(row.key_id)
    if key is None:
        return row.id <= sealed_through
    return hmac.compare_digest(row.entry_hash, _row_hash(key, row))


def _trusted_checkpoints(db: Session, keys: dict[str, bytes]) -> tuple[Optional[tuple[int, dict]], int, list[int]]:
    """(latest prune checkpoint (id, metadata) or None, sealed-through id, head-restore ids).

    Only checkpoints whose MAC verifies — or that a later trusted seal covers —
    are trusted. Newest first, so a seal is known before the entries it covers.
    """
    prune: Optional[tuple[int, dict]] = None
    sealed_through = 0
    restored: list[int] = []
    rows = db.execute(
        select(*_ENTRY_COLUMNS)
        .where(AuditLog.event_type == CHECKPOINT_EVENT, AuditLog.action == CHECKPOINT_ACTION)
        .order_by(AuditLog.id.desc())
    ).all()
    for row in rows:
        if row.key_id == LEGACY_KEY_ID or not _checkpoint_mac_ok(row, keys, sealed_through):
            continue
        try:
            meta = json.loads(row.metadata_json or "{}")
        except ValueError:
            continue
        kind = meta.get("kind")
        if kind == "seal":
            sealed_through = max(sealed_through, int(meta.get("sealed_through_id") or 0))
        elif kind == "prune" and prune is None:
            prune = (row.id, meta)
        elif kind == "head_restored":
            restored.append(row.id)
    return prune, sealed_through, restored


def verify_audit_chain(db: Session, *, batch_size: int = 1000, max_errors: int = 100) -> dict:
    """Verify the chain from its latest trusted prune checkpoint (or the first entry) to the head.

    Streams entries in id order, ``batch_size`` at a time, checking each link
    (``prev_hash``) and each MAC with the key its ``key_id`` names, then checks
    the last entry against the chain head. Detects edited, reordered, inserted
    and deleted entries — including deleted newest entries.

    Returns:
        dict with ``valid``, ``total`` (entries verified), ``errors`` (the first
        ``max_errors``), ``error_count``, ``sealed`` (entries under a retired key
        that a seal checkpoint attests), ``keyed``, ``checkpoint_id``,
        ``start_id``, ``last_entry_id`` / ``last_entry_hash``.
    """
    keys = _verification_keys()
    errors: list[dict] = []
    error_count = 0

    def fail(entry_id: Optional[int], message: str, **details: Any) -> None:
        nonlocal error_count
        error_count += 1
        if len(errors) < max_errors:
            errors.append({"id": entry_id, "error": message, **details})

    prune, sealed_through, restored = _trusted_checkpoints(db, keys)
    if prune is not None:
        checkpoint_id, meta = prune
        start_id = meta.get("first_entry_id") or checkpoint_id
        expected_prev = meta.get("anchor_hash")
        cursor: Optional[int] = start_id - 1
    else:
        checkpoint_id = start_id = None
        expected_prev = None
        cursor = None

    for restored_id in restored:
        fail(restored_id, "chain head was deleted and restored; newest entries before this point may be missing")

    total = sealed = 0
    keyed = False
    first_id = last_id = None
    last_hash = None

    def verify_rows(rows) -> None:
        nonlocal expected_prev, keyed, total, sealed, first_id, last_id, last_hash
        for row in rows:
            total += 1
            if first_id is None:
                first_id = row.id
            if row.prev_hash != expected_prev:
                fail(row.id, "prev_hash mismatch", expected=expected_prev, actual=row.prev_hash)
            key_id = row.key_id or LEGACY_KEY_ID
            if key_id == LEGACY_KEY_ID:
                if keyed:
                    fail(row.id, "unkeyed entry after the chain became keyed")
                elif row.entry_hash != _legacy_row_hash(row):
                    fail(row.id, "entry_hash mismatch", key_id=key_id)
            else:
                keyed = True
                key = keys.get(key_id)
                if key is None:
                    if row.id <= sealed_through:
                        sealed += 1
                    else:
                        fail(row.id, "signed with a key that is not configured", key_id=key_id)
                elif not hmac.compare_digest(row.entry_hash, _row_hash(key, row)):
                    fail(row.id, "entry_hash mismatch", key_id=key_id)
            expected_prev = row.entry_hash
            last_id, last_hash = row.id, row.entry_hash

    head = None
    for _round in range(4):
        while True:
            query = select(*_ENTRY_COLUMNS).order_by(AuditLog.id).limit(batch_size)
            after = last_id if last_id is not None else cursor
            if after is not None:
                query = query.where(AuditLog.id > after)
            rows = db.execute(query).all()
            if not rows:
                break
            verify_rows(rows)
        # Each append commits its entry and the head together; if the head moved
        # past what was streamed, entries arrived meanwhile — pick them up.
        head = _read_head(db)
        if head is None or head.last_entry_id is None or last_id is None or head.last_entry_id <= last_id:
            break

    if head is None:
        if keyed:
            fail(None, "chain head is missing")
    else:
        head_key = keys.get(head.key_id or "")
        if head_key is None:
            fail(None, "chain head is signed with a key that is not configured", key_id=head.key_id)
        elif not hmac.compare_digest(
            head.head_mac or "", _head_mac(head_key, head.last_entry_id, head.last_entry_hash)
        ):
            fail(None, "chain head MAC mismatch")
        if (last_id, last_hash) != (head.last_entry_id, head.last_entry_hash):
            in_flight = (
                last_id is not None
                and head.last_entry_id is not None
                and head.last_entry_id > last_id
                and db.execute(select(AuditLog.entry_hash).where(AuditLog.id == head.last_entry_id)).scalar()
                == head.last_entry_hash
            )
            if not in_flight:
                fail(
                    None,
                    "newest entries do not match the chain head (entries deleted or appended outside the chain)",
                    expected_last_id=head.last_entry_id,
                    actual_last_id=last_id,
                )

    return {
        "valid": error_count == 0,
        "total": total,
        "errors": errors,
        "error_count": error_count,
        "sealed": sealed,
        "keyed": keyed,
        "checkpoint_id": checkpoint_id,
        "start_id": first_id if first_id is not None else start_id,
        "last_entry_id": last_id,
        "last_entry_hash": last_hash,
    }


# ── Sealing (signing-key changes) ─────────────────────────────────────────────


def audit_seal_needed(db: Session) -> bool:
    """True when entries under another signing key have not been sealed yet."""
    head = _read_head(db)
    if head is None or head.key_id in (None, LEGACY_KEY_ID):
        return False
    return bool(head.seal_pending) or head.key_id != _signing_key()[0]


def seal_audit_chain(db: Session) -> AuditLog:
    """Verify the chain and append a checkpoint, signed with the current key, attesting it.

    After a signing-key change this keeps entries signed with the old key
    verifiable once that key is removed: they count as ``sealed``. Verification
    runs without the chain lock (entries up to the head never change); the
    checkpoint is appended under it.

    Raises:
        AuditChainInvalid: the chain does not verify; nothing is sealed.
    """
    report = verify_audit_chain(db)
    if not report["valid"]:
        raise AuditChainInvalid(report)
    signing_id, signing_key = _signing_key()
    through = report["last_entry_id"] or 0
    try:
        _lock_chain(db)
        head = _ensure_head(db, signing_id, signing_key)
        entry = _append_entry(
            db,
            head,
            signing_id,
            signing_key,
            event_type=CHECKPOINT_EVENT,
            action=CHECKPOINT_ACTION,
            metadata_json=_checkpoint_json(
                kind="seal",
                sealed_through_id=report["last_entry_id"],
                sealed_through_hash=report["last_entry_hash"],
                verified_entries=report["total"],
            ),
        )
        # Entries appended after verification under yet another key still need a seal.
        unsealed = db.execute(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.id > through, AuditLog.id < entry.id, AuditLog.key_id != signing_id)
        ).scalar_one()
        head.seal_pending = bool(unsealed)
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(entry)
    return entry


def seal_audit_chain_if_needed(db: Session) -> Optional[AuditLog]:
    """Seal the chain if the signing key changed since the last seal; returns the seal or None."""
    if not audit_seal_needed(db):
        return None
    return seal_audit_chain(db)
