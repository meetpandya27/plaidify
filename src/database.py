"""
Database models, session management, and encryption utilities.

Uses SQLAlchemy for ORM and AES-256-GCM for symmetric credential encryption.
All configuration is loaded from the Settings object — no hardcoded secrets.

Timestamps are stored as ``TIMESTAMP WITH TIME ZONE`` and always read back as
aware UTC datetimes (``UTCDateTime``); compare them with ``utcnow()``.
"""

import base64
import hashlib
import json
import os
import threading
import time as _time
from collections.abc import Generator, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    false,
    func,
    or_,
    select,
    update,
)
from sqlalchemy import event as _sa_event
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import DeclarativeBase, Session, relationship, sessionmaker
from sqlalchemy.types import TypeDecorator

from src.config import get_settings
from src.crypto import token_fingerprint
from src.logging_config import get_logger

logger = get_logger(__name__)
settings = get_settings()

# ── Time ──────────────────────────────────────────────────────────────────────


def utcnow() -> datetime:
    """The current time as an aware UTC datetime — what every timestamp column holds."""
    return datetime.now(timezone.utc)


def as_utc(value: Optional[datetime]) -> Optional[datetime]:
    """Normalise a datetime to aware UTC. A naive value is taken to be UTC already."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class UTCDateTime(TypeDecorator):
    """``DateTime(timezone=True)`` that always binds and returns aware UTC values.

    PostgreSQL stores ``TIMESTAMP WITH TIME ZONE``, so an instant never depends
    on the server's ``TimeZone``. SQLite has no zoned type: the value is
    written as UTC wall-clock time and read back as UTC. Either way callers get
    aware UTC datetimes and can compare them with ``utcnow()`` directly.
    """

    impl = DateTime
    cache_ok = True

    def __init__(self) -> None:
        super().__init__(timezone=True)

    def process_bind_param(self, value, dialect):
        return as_utc(value)

    def process_result_value(self, value, dialect):
        return as_utc(value)


# ── Encryption (AES-256-GCM) ─────────────────────────────────────────────────

_GCM_NONCE_BYTES = 12  # 96-bit nonce, NIST recommended for GCM


class CredentialDecryptionError(ValueError):
    """Stored ciphertext could not be decrypted with any configured key.

    A hard failure: callers must not fall back to using the stored value (the
    ciphertext is not the secret), e.g. as a webhook signing key.
    """


def _decode_master_key(raw: str, name: str) -> bytes:
    key_bytes = base64.urlsafe_b64decode(raw.encode("ascii") if isinstance(raw, str) else raw)
    if len(key_bytes) not in (32, 44):
        # 32 bytes = raw AES-256 key; 44 bytes = Fernet key (we extract first 16 + last 16)
        raise ValueError(f"{name} must decode to 32 bytes (AES-256). Got {len(key_bytes)} bytes.")
    if len(key_bytes) == 44:
        # Legacy Fernet key: 16-byte signing key + 16-byte encryption key (AES-128).
        # Reject: the old doubling hack only provided 128-bit effective strength.
        # Operators must rotate to a proper 256-bit key.
        raise ValueError(
            f"Detected legacy Fernet-format {name} (44 bytes). "
            "This key format is no longer supported — it only provides 128-bit "
            "effective strength. Generate a new 256-bit key: "
            'python -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())" '
            "and re-encrypt existing data with the key rotation procedure."
        )
    return key_bytes


def _get_encryption_key() -> bytes:
    """Decode the base64-encoded 256-bit encryption key."""
    return _decode_master_key(settings.encryption_key, "ENCRYPTION_KEY")


def _get_previous_encryption_key() -> Optional[bytes]:
    """Decode ENCRYPTION_KEY_PREVIOUS (set only while a rotation is in progress)."""
    previous = settings.encryption_key_previous
    return _decode_master_key(previous, "ENCRYPTION_KEY_PREVIOUS") if previous else None


def _master_keys() -> Iterator[bytes]:
    """The current master key, then the previous one (decoded only if reached)."""
    yield _get_encryption_key()
    previous = _get_previous_encryption_key()
    if previous is not None:
        yield previous


def _get_aesgcm() -> AESGCM:
    return AESGCM(_get_encryption_key())


def _gcm_encrypt(key: bytes, plaintext: bytes, aad: Optional[bytes] = None) -> str:
    """AES-256-GCM encrypt; returns base64url( nonce‖ciphertext‖tag )."""
    nonce = os.urandom(_GCM_NONCE_BYTES)
    return base64.urlsafe_b64encode(nonce + AESGCM(key).encrypt(nonce, plaintext, aad)).decode("ascii")


def _gcm_decrypt(key: bytes, token: str, aad: Optional[bytes] = None) -> bytes:
    """Inverse of ``_gcm_encrypt``; raises if the key (or AAD) does not match."""
    raw = base64.urlsafe_b64decode(token)
    if len(raw) <= _GCM_NONCE_BYTES:
        raise ValueError("ciphertext is too short")
    return AESGCM(key).decrypt(raw[:_GCM_NONCE_BYTES], raw[_GCM_NONCE_BYTES:], aad)


def encrypt_credential(plaintext: str) -> str:
    """Encrypt a plaintext string using AES-256-GCM.

    Output format: base64url( nonce‖ciphertext‖tag )
    - 12-byte random nonce ensures uniqueness.
    - GCM provides both confidentiality and authenticity.
    """
    return _gcm_encrypt(_get_encryption_key(), plaintext.encode("utf-8"))


def decrypt_credential(ciphertext: str) -> str:
    """Decrypt a value encrypted under the master key.

    Tries ENCRYPTION_KEY, then ENCRYPTION_KEY_PREVIOUS — values written before
    a rotation stay readable until the rotation sweep re-encrypts them — then
    the legacy Fernet format (data encrypted before the AES-256-GCM migration).

    Raises:
        CredentialDecryptionError: no configured key decrypts the value.
    """
    for key in _master_keys():
        try:
            return _gcm_decrypt(key, ciphertext).decode("utf-8")
        except Exception:
            continue

    for raw_key in (settings.encryption_key, settings.encryption_key_previous):
        if not raw_key:
            continue
        try:
            return Fernet(raw_key.encode("ascii")).decrypt(ciphertext.encode("ascii")).decode("utf-8")
        except Exception:
            continue

    raise CredentialDecryptionError(
        "Failed to decrypt credential: it is not encrypted under ENCRYPTION_KEY or ENCRYPTION_KEY_PREVIOUS "
        "(tried AES-256-GCM and legacy Fernet)"
    )


# Keep old names as aliases for backward compatibility
encrypt_password = encrypt_credential
decrypt_password = decrypt_credential


# ── Envelope Encryption (per-user DEK) ────────────────────────────────────────


def generate_dek() -> bytes:
    """Generate a random 256-bit Data Encryption Key."""
    return os.urandom(32)


def wrap_dek(dek: bytes) -> str:
    """Encrypt (wrap) a DEK with the configured KMS provider.

    Routes through ``src.kms.get_kms_provider().wrap_key_sync()`` so a
    single configuration switch (``KMS_PROVIDER``) flips between local
    AES-256-GCM, AWS KMS, Azure Key Vault, or HashiCorp Vault Transit
    without touching call sites.

    Returns an opaque base64url-encoded string for the local provider
    (nonce‖ciphertext‖tag) or whatever opaque payload the configured
    KMS provider produces.
    """
    from src.kms import get_kms_provider

    return get_kms_provider().wrap_key_sync(dek)


def unwrap_dek(wrapped_dek: str) -> bytes:
    """Decrypt (unwrap) a DEK using the configured KMS provider.

    For the local provider, falls back to ``ENCRYPTION_KEY_PREVIOUS``
    when the current master key fails (zero-downtime rotation). External
    providers (AWS / Azure / Vault) handle versioning natively and do
    not consult the env-var fallback; their errors propagate unchanged.

    Raises:
        CredentialDecryptionError: (local provider) neither master key unwraps it.
    """
    from src.kms import LocalKMSProvider, get_kms_provider

    provider = get_kms_provider()
    try:
        return provider.unwrap_key_sync(wrapped_dek)
    except Exception as exc:
        if not isinstance(provider, LocalKMSProvider):
            raise
        previous = _get_previous_encryption_key()
        if previous is not None:
            try:
                return _gcm_decrypt(previous, wrapped_dek)
            except Exception:
                pass
        raise CredentialDecryptionError(
            "Data encryption key could not be unwrapped with ENCRYPTION_KEY or ENCRYPTION_KEY_PREVIOUS"
        ) from exc


def create_user_dek() -> str:
    """Generate a new DEK and return it wrapped (ready to store in DB)."""
    dek = generate_dek()
    return wrap_dek(dek)


def encrypt_with_dek(dek: bytes, plaintext: str) -> str:
    """Encrypt a string under an unwrapped DEK (the per-user credential format)."""
    return _gcm_encrypt(dek, plaintext.encode("utf-8"))


def decrypt_with_dek(dek: bytes, ciphertext: str) -> str:
    """Inverse of ``encrypt_with_dek``; raises if ``dek`` did not encrypt it."""
    return _gcm_decrypt(dek, ciphertext).decode("utf-8")


def encrypt_credential_for_user(user: "User", plaintext: str) -> str:
    """Encrypt a credential using the user's per-user DEK.

    Falls back to the global master key if the user has no DEK yet
    (migration path for existing users).
    """
    if user.encrypted_dek:
        return encrypt_with_dek(unwrap_dek(user.encrypted_dek), plaintext)
    # Fallback: encrypt with master key (legacy path)
    return encrypt_credential(plaintext)


def decrypt_credential_for_user(user: "User", ciphertext: str) -> str:
    """Decrypt a credential using the user's per-user DEK.

    Falls back to master-key decryption (current, then previous key) for data
    encrypted before envelope encryption was enabled (migration compatibility).

    Raises:
        CredentialDecryptionError: neither the DEK nor any master key decrypts it.
    """
    if user.encrypted_dek:
        dek = unwrap_dek(user.encrypted_dek)
        try:
            return decrypt_with_dek(dek, ciphertext)
        except Exception:
            pass  # Fall through to legacy master-key decryption
    # Fallback: decrypt with master key (legacy data)
    return decrypt_credential(ciphertext)


def _claim_user_dek(db: Session, user: "User") -> None:
    """Give ``user`` a DEK unless another transaction already did. Flushes, does not commit.

    A conditional UPDATE (``WHERE encrypted_dek IS NULL``): of two concurrent
    callers exactly one DEK wins and both end up using it, so nothing is ever
    encrypted under a DEK that is then overwritten.
    """
    db.execute(
        update(User)
        .where(User.id == user.id, User.encrypted_dek.is_(None))
        .values(encrypted_dek=create_user_dek(), dek_key_version=get_current_key_version())
        .execution_options(synchronize_session=False)
    )
    db.refresh(user, attribute_names=["encrypted_dek", "dek_key_version"])


def ensure_user_dek(user: "User", db: "Session") -> None:
    """Ensure a user has a DEK. Creates one if missing (lazy migration)."""
    if user.encrypted_dek:
        return
    _claim_user_dek(db, user)
    db.commit()
    logger.info("Generated DEK for user", extra={"extra_data": {"user_id": user.id}})


# ── Webhook secrets ───────────────────────────────────────────────────────────


def encrypt_webhook_secret(user: "User", secret: str) -> str:
    """Encrypt a webhook signing secret for storage, under the owner's DEK."""
    return encrypt_credential_for_user(user, secret)


def decrypt_webhook_secret(db: Session, webhook: "Webhook") -> str:
    """Decrypt a stored webhook signing secret.

    Reads the owner's DEK format first, then the master-key format (current,
    then previous key) that older rows use.

    Raises:
        CredentialDecryptionError: the secret cannot be decrypted. The stored
            value must never be used as the signing key in its place.
    """
    owner = db.get(User, webhook.user_id)
    if owner is None:
        return decrypt_credential(webhook.secret)
    return decrypt_credential_for_user(owner, webhook.secret)


# ── Encrypted JSON documents (per-user envelope) ──────────────────────────────

# Marks a stored value as an encrypted document (vs legacy plaintext JSON).
ENCRYPTED_JSON_PREFIX = "enc:v1:"


def _json_aad(context: str) -> bytes:
    return b"plaidify:json:v1|" + context.encode("utf-8")


def is_encrypted_json(stored: Optional[str]) -> bool:
    """True if ``stored`` was produced by ``encrypt_json_for_user``."""
    return bool(stored) and stored.startswith(ENCRYPTED_JSON_PREFIX)


def encrypt_json_for_user(db: Session, user: "User", document: Any, *, context: str) -> str:
    """Encrypt a JSON-serialisable document under ``user``'s DEK.

    Same envelope as credentials (AES-256-GCM under the per-user DEK, which the
    KMS provider wraps), so the document follows the user's key through master
    key rotation and KMS migration, and is unreadable from a database dump.

    Args:
        db: Session holding ``user``. A user without a DEK gets one here,
            flushed into the caller's transaction (not committed).
        user: Owner of the document.
        document: Anything ``json.dumps`` accepts (``default=str``).
        context: Where the value is stored, e.g. ``"access_job:<id>:result"``.
            Bound as associated data, so a ciphertext copied to another row or
            field does not decrypt there.

    Returns:
        ``"enc:v1:" + base64url(nonce‖ciphertext‖tag)``, safe for a Text column.
    """
    if not context:
        raise ValueError("context is required")
    if not user.encrypted_dek:
        _claim_user_dek(db, user)
    payload = json.dumps(document, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return ENCRYPTED_JSON_PREFIX + _gcm_encrypt(unwrap_dek(user.encrypted_dek), payload, _json_aad(context))


def decrypt_json_for_user(user: "User", stored: Optional[str], *, context: str) -> Any:
    """Decrypt a document stored by ``encrypt_json_for_user``.

    ``None`` stays ``None``. A value without the ``enc:v1:`` prefix is a legacy
    plaintext row written before encryption and is parsed as JSON.

    Raises:
        CredentialDecryptionError: the user's DEK (with this ``context``) does
            not decrypt the value.
    """
    if stored is None:
        return None
    if not is_encrypted_json(stored):
        return json.loads(stored)
    if not user.encrypted_dek:
        raise CredentialDecryptionError("Encrypted document found but the user has no data encryption key")
    dek = unwrap_dek(user.encrypted_dek)
    try:
        payload = _gcm_decrypt(dek, stored[len(ENCRYPTED_JSON_PREFIX) :], _json_aad(context))
    except Exception as exc:
        raise CredentialDecryptionError("Encrypted document does not decrypt with this user's key and context") from exc
    return json.loads(payload)


def purge_expired_job_results(
    db: Session,
    *,
    retention_days: Optional[int] = None,
    now: Optional[datetime] = None,
) -> int:
    """Erase stored access-job results older than ``RESULT_RETENTION_DAYS``.

    Only ``result_json`` is cleared; the job row (status, timings, metadata)
    stays. Age counts from ``completed_at``, or ``created_at`` for a job that
    never completed. Commits and returns the number of results erased.
    """
    days = settings.result_retention_days if retention_days is None else retention_days
    cutoff = (now or utcnow()) - timedelta(days=days)
    finished_at = func.coalesce(AccessJob.completed_at, AccessJob.created_at)
    result = db.execute(
        update(AccessJob)
        .where(AccessJob.result_json.isnot(None), finished_at < cutoff)
        .values(result_json=None)
        .execution_options(synchronize_session=False)
    )
    db.commit()
    if result.rowcount:
        logger.info(
            "Erased expired access job results",
            extra={"extra_data": {"count": result.rowcount, "retention_days": days}},
        )
    return result.rowcount


# ── Master-key rotation ───────────────────────────────────────────────────────
#
# Rotating ENCRYPTION_KEY touches three kinds of rows:
#   users.encrypted_dek           per-user DEKs wrapped by the master key (local KMS provider)
#   access_tokens.*_encrypted     credentials; legacy rows are under the master key itself
#   webhooks.secret               webhook signing secrets under the master key
# While a rotation is in progress ENCRYPTION_KEY is the new key and
# ENCRYPTION_KEY_PREVIOUS the old one; every read falls back to the old key.
# The sweep below brings each stale row under the new key and stamps it with
# ENCRYPTION_KEY_VERSION. When a pass ends with nothing left to do, the old key
# is no longer needed. SECURITY.md → "Key Rotation Procedure".


def get_current_key_version() -> int:
    """Return the current encryption key version from settings."""
    return settings.encryption_key_version


def _uses_local_kms() -> bool:
    from src.kms import LocalKMSProvider, get_kms_provider

    return isinstance(get_kms_provider(), LocalKMSProvider)


class KeyRotationIncomplete(RuntimeError):
    """A rotation pass ended with rows that no configured key could decrypt.

    Everything else was committed. Keep ENCRYPTION_KEY_PREVIOUS set until the
    listed rows are fixed or deleted; the next pass retries them.
    """

    def __init__(self, failures: list[str], key_version: int) -> None:
        self.failures = list(failures)
        self.key_version = key_version
        shown = ", ".join(self.failures[:10])
        if len(self.failures) > 10:
            shown += f", ... (+{len(self.failures) - 10} more)"
        super().__init__(
            f"{len(self.failures)} row(s) could not be brought to key_version={key_version} "
            f"with ENCRYPTION_KEY or ENCRYPTION_KEY_PREVIOUS: {shown}. "
            "Keep ENCRYPTION_KEY_PREVIOUS set until they are resolved."
        )


@dataclass
class KeyRotationProgress:
    """Outcome of one ``rotate_encryption_keys`` call."""

    key_version: int
    rotated: int = 0  # rows brought to key_version in this call
    pass_complete: bool = False  # this call reached the end of the stale rows
    pass_rotated: int = 0  # rows rotated over the whole pass (set when complete)
    pass_failures: list[str] = field(default_factory=list)  # rows that failed during the pass


class _RotationPass:
    """Position of the sweep in its current pass over the stale rows.

    Kept per process. Rows that fail are recorded and passed over, so they
    cannot starve the rows behind them; they are retried in the next pass.
    """

    def __init__(self, key_version: int) -> None:
        self.key_version = key_version
        self.step = 0
        self.cursor: Any = None
        self.rotated = 0
        self.failures: list[str] = []


_rotation_lock = threading.Lock()
_rotation_pass: Optional[_RotationPass] = None


def reset_key_rotation_state() -> None:
    """Forget where the sweep is (a new pass starts on the next call)."""
    global _rotation_pass
    with _rotation_lock:
        _rotation_pass = None


def _rewrap_under_current_key(wrapped: str) -> Optional[str]:
    """Return ``wrapped`` re-wrapped under ENCRYPTION_KEY, or None if it already is."""
    try:
        _gcm_decrypt(_get_encryption_key(), wrapped)
        return None
    except Exception:
        pass
    previous = _get_previous_encryption_key()
    if previous is None:
        raise CredentialDecryptionError("DEK is not wrapped by ENCRYPTION_KEY and ENCRYPTION_KEY_PREVIOUS is unset")
    return wrap_dek(_gcm_decrypt(previous, wrapped))


def _rotate_user_deks(db: Session, rp: _RotationPass, limit: int) -> tuple[int, bool]:
    if not _uses_local_kms():
        return 0, True  # an external KMS versions its own wrapping key
    version = rp.key_version
    rotated = 0
    while True:
        query = (
            select(User.id, User.encrypted_dek)
            .where(
                User.encrypted_dek.isnot(None),
                or_(User.dek_key_version.is_(None), User.dek_key_version < version),
            )
            .order_by(User.id)
            .limit(limit)
        )
        if rp.cursor is not None:
            query = query.where(User.id > rp.cursor)
        rows = db.execute(query).all()
        if not rows:
            return rotated, True
        for user_id, wrapped in rows:
            rp.cursor = user_id
            try:
                rewrapped = _rewrap_under_current_key(wrapped)
            except Exception:
                rp.failures.append(f"users:{user_id}")
                continue
            values: dict[str, Any] = {"dek_key_version": version}
            if rewrapped is not None:
                values["encrypted_dek"] = rewrapped
            result = db.execute(
                update(User)
                .where(User.id == user_id, User.encrypted_dek == wrapped)
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            rotated += result.rowcount
            if rotated >= limit:
                break
        db.commit()
        if rotated >= limit:
            return rotated, False


def _owner(db: Session, cache: dict, user_id: int) -> "User":
    if user_id not in cache:
        cache[user_id] = db.get(User, user_id)
    user = cache[user_id]
    if user is None:
        raise LookupError(f"owner {user_id} not found")
    return user


def _reencrypt_under_dek(db: Session, user: "User", ciphertexts: dict[str, str]) -> dict[str, str]:
    """Move master-key ciphertexts under the user's DEK; returns only the fields that changed."""
    dek = unwrap_dek(user.encrypted_dek) if user.encrypted_dek else None
    changed: dict[str, str] = {}
    for column, ciphertext in ciphertexts.items():
        if dek is not None:
            try:
                decrypt_with_dek(dek, ciphertext)
                continue  # already under the DEK, which the DEK step keeps current
            except Exception:
                pass
        plaintext = decrypt_credential(ciphertext)
        if dek is None:
            _claim_user_dek(db, user)
            dek = unwrap_dek(user.encrypted_dek)
        changed[column] = encrypt_with_dek(dek, plaintext)
    return changed


def _rotate_access_tokens(db: Session, rp: _RotationPass, limit: int) -> tuple[int, bool]:
    version = rp.key_version
    rotated = 0
    owners: dict[int, Optional[User]] = {}
    while True:
        query = (
            select(
                AccessToken.token, AccessToken.user_id, AccessToken.username_encrypted, AccessToken.password_encrypted
            )
            .where(AccessToken.key_version < version)
            .order_by(AccessToken.token)
            .limit(limit)
        )
        if rp.cursor is not None:
            query = query.where(AccessToken.token > rp.cursor)
        rows = db.execute(query).all()
        if not rows:
            return rotated, True
        for token, user_id, username_ct, password_ct in rows:
            rp.cursor = token
            try:
                values: dict[str, Any] = _reencrypt_under_dek(
                    db,
                    _owner(db, owners, user_id),
                    {"username_encrypted": username_ct, "password_encrypted": password_ct},
                )
            except Exception:
                rp.failures.append(f"access_tokens:{token_fingerprint(token)}")
                continue
            values["key_version"] = version
            result = db.execute(
                update(AccessToken)
                .where(
                    AccessToken.token == token,
                    AccessToken.username_encrypted == username_ct,
                    AccessToken.password_encrypted == password_ct,
                )
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            rotated += result.rowcount
            if rotated >= limit:
                break
        db.commit()
        if rotated >= limit:
            return rotated, False


def _reencrypt_webhook_secret(user: Optional["User"], stored: str) -> Optional[str]:
    """Return the secret re-encrypted under the current master key, or None if no change is needed.

    Secrets under the owner's DEK follow the DEK and need nothing. Master-key
    secrets stay on the master key so every reader of this format keeps
    working; ``scripts/migrate_to_kms.py`` moves them under the DEK.
    """
    if user is not None and user.encrypted_dek:
        try:
            decrypt_with_dek(unwrap_dek(user.encrypted_dek), stored)
            return None
        except Exception:
            pass
    try:
        _gcm_decrypt(_get_encryption_key(), stored)
        return None
    except Exception:
        pass
    return encrypt_credential(decrypt_credential(stored))


def _rotate_webhook_secrets(db: Session, rp: _RotationPass, limit: int) -> tuple[int, bool]:
    version = rp.key_version
    rotated = 0
    owners: dict[int, Optional[User]] = {}
    while True:
        query = (
            select(Webhook.id, Webhook.user_id, Webhook.secret)
            .where(Webhook.key_version < version)
            .order_by(Webhook.id)
            .limit(limit)
        )
        if rp.cursor is not None:
            query = query.where(Webhook.id > rp.cursor)
        rows = db.execute(query).all()
        if not rows:
            return rotated, True
        for webhook_id, user_id, secret in rows:
            rp.cursor = webhook_id
            try:
                if user_id not in owners:
                    owners[user_id] = db.get(User, user_id)
                reencrypted = _reencrypt_webhook_secret(owners[user_id], secret)
            except Exception:
                rp.failures.append(f"webhooks:{webhook_id}")
                continue
            values: dict[str, Any] = {"key_version": version}
            if reencrypted is not None:
                values["secret"] = reencrypted
            result = db.execute(
                update(Webhook)
                .where(Webhook.id == webhook_id, Webhook.secret == secret)
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            rotated += result.rowcount
            if rotated >= limit:
                break
        db.commit()
        if rotated >= limit:
            return rotated, False


_ROTATION_STEPS = (_rotate_user_deks, _rotate_access_tokens, _rotate_webhook_secrets)


def rotate_encryption_keys(db: Session, batch_size: int = 100) -> KeyRotationProgress:
    """Bring up to ``batch_size`` stale rows under the current master key.

    Works through re-wrapping user DEKs, then access-token credentials (legacy
    master-key rows move under the owner's DEK), then webhook secrets. Each row
    is written with a conditional UPDATE, so a row changed concurrently is left
    for the next pass instead of being overwritten. Rows that cannot be
    decrypted are recorded and skipped; the position is remembered between
    calls, so a pass always reaches the end. Safe to run from several workers.
    """
    global _rotation_pass
    batch_size = max(1, batch_size)
    with _rotation_lock:
        version = get_current_key_version()
        if _rotation_pass is None or _rotation_pass.key_version != version:
            _rotation_pass = _RotationPass(version)
        rp = _rotation_pass
        progress = KeyRotationProgress(key_version=version)
        while rp.step < len(_ROTATION_STEPS):
            budget = batch_size - progress.rotated
            if budget <= 0:
                return progress
            rotated, exhausted = _ROTATION_STEPS[rp.step](db, rp, budget)
            progress.rotated += rotated
            rp.rotated += rotated
            if not exhausted:
                return progress
            rp.step += 1
            rp.cursor = None
        progress.pass_complete = True
        progress.pass_rotated = rp.rotated
        progress.pass_failures = list(rp.failures)
        _rotation_pass = None

    _finish_rotation_pass(db, progress)
    return progress


def _finish_rotation_pass(db: Session, progress: KeyRotationProgress) -> None:
    from src.audit import AuditChainInvalid, seal_audit_chain_if_needed

    try:
        if seal_audit_chain_if_needed(db) is not None:
            logger.info("Audit chain sealed under the current signing key")
    except AuditChainInvalid as exc:
        # Do not attest a chain that fails verification; keep the old key around.
        progress.pass_failures.append("audit_logs:chain failed verification")
        logger.error(f"Audit chain not sealed: {exc}")

    if progress.pass_failures:
        logger.error(
            "Key rotation pass finished with rows that could not be re-encrypted",
            extra={
                "extra_data": {
                    "key_version": progress.key_version,
                    "rotated": progress.pass_rotated,
                    "failed": len(progress.pass_failures),
                    "rows": progress.pass_failures[:20],
                }
            },
        )
    elif progress.pass_rotated:
        logger.info(
            f"Key rotation pass complete: {progress.pass_rotated} row(s) now on key_version={progress.key_version}",
            extra={"extra_data": {"key_version": progress.key_version, "rotated": progress.pass_rotated}},
        )


def re_encrypt_tokens(db: "Session", batch_size: int = 100) -> int:
    """Run one step of the key-rotation sweep (``rotate_encryption_keys``).

    Kept under its original name: ``plaidify rotate-key --re-encrypt`` calls
    it in a loop until it returns fewer than ``batch_size``, and the app's
    background job calls it hourly.

    Returns:
        Rows brought to the current key version in this call (DEKs re-wrapped,
        credentials and webhook secrets re-encrypted). Fewer than
        ``batch_size`` means the pass is over.

    Raises:
        KeyRotationIncomplete: at the end of a pass that left rows no
            configured key could decrypt. Everything else is committed.
    """
    progress = rotate_encryption_keys(db, batch_size=batch_size)
    if progress.pass_complete and progress.pass_failures:
        raise KeyRotationIncomplete(progress.pass_failures, progress.key_version)
    return progress.rotated


def key_rotation_status(db: Session) -> dict:
    """What still depends on ENCRYPTION_KEY_PREVIOUS; ``complete`` means it can be removed.

    In-flight access-job payloads in Redis are not counted: they are encrypted
    under the master key and expire after ACCESS_JOB_PAYLOAD_TTL.
    """
    from src.audit import audit_seal_needed

    version = get_current_key_version()

    def count(model, *criteria) -> int:
        return db.execute(select(func.count()).select_from(model).where(*criteria)).scalar_one()

    stale_deks = 0
    if _uses_local_kms():
        stale_deks = count(
            User,
            User.encrypted_dek.isnot(None),
            or_(User.dek_key_version.is_(None), User.dek_key_version < version),
        )
    status = {
        "key_version": version,
        "stale_user_deks": stale_deks,
        "stale_access_tokens": count(AccessToken, AccessToken.key_version < version),
        "stale_webhooks": count(Webhook, Webhook.key_version < version),
        "audit_seal_pending": audit_seal_needed(db),
    }
    status["complete"] = not (
        status["stale_user_deks"]
        or status["stale_access_tokens"]
        or status["stale_webhooks"]
        or status["audit_seal_pending"]
    )
    return status


def rotate_master_key(old_key: str, new_key: str, db: "Session", batch_size: int = 500) -> int:
    """Re-wrap all user DEKs with a new master key.

    This does NOT re-encrypt any data — it only re-wraps the DEK envelopes
    (local KMS provider; an external KMS versions its own key and nothing is
    done). Idempotent: DEKs already under ``new_key`` are left alone, so an
    interrupted run can be repeated. Commits every ``batch_size`` users.

    Args:
        old_key: Current ENCRYPTION_KEY (base64url-encoded).
        new_key: New ENCRYPTION_KEY (base64url-encoded).
        db: Database session.

    Returns:
        Number of DEKs re-wrapped.

    Raises:
        KeyRotationIncomplete: some DEK is under neither key (the others are
            committed).
    """
    if not _uses_local_kms():
        logger.info("DEKs are wrapped by an external KMS provider; nothing to re-wrap")
        return 0

    old_key_bytes = _decode_master_key(old_key, "old key")
    new_key_bytes = _decode_master_key(new_key, "new key")
    # Stamp the version only when new_key is the configured current key.
    stamp = get_current_key_version() if new_key_bytes == _get_encryption_key() else None

    count = 0
    failures: list[str] = []
    last_id = 0
    while True:
        rows = db.execute(
            select(User.id, User.encrypted_dek)
            .where(User.encrypted_dek.isnot(None), User.id > last_id)
            .order_by(User.id)
            .limit(batch_size)
        ).all()
        if not rows:
            break
        for user_id, wrapped in rows:
            last_id = user_id
            values: dict[str, Any] = {}
            try:
                values["encrypted_dek"] = _gcm_encrypt(new_key_bytes, _gcm_decrypt(old_key_bytes, wrapped))
            except Exception:
                try:
                    _gcm_decrypt(new_key_bytes, wrapped)  # already rotated by an earlier run
                except Exception:
                    failures.append(f"users:{user_id}")
                    continue
            if stamp is not None:
                values["dek_key_version"] = stamp
            if not values:
                continue
            result = db.execute(
                update(User)
                .where(User.id == user_id, User.encrypted_dek == wrapped)
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            if "encrypted_dek" in values:
                count += result.rowcount
        db.commit()

    logger.info(f"Master key rotation complete: {count} DEK(s) re-wrapped")
    if failures:
        raise KeyRotationIncomplete(failures, stamp if stamp is not None else get_current_key_version())
    return count


# ── SQLAlchemy Setup ──────────────────────────────────────────────────────────

_engine_kwargs: dict = {
    "echo": False,
    "pool_pre_ping": True,
}

# SQLite doesn't support connection pooling options
if not settings.database_url.startswith("sqlite"):
    _engine_kwargs.update(
        {
            "pool_size": settings.db_pool_size,
            "max_overflow": settings.db_max_overflow,
            "pool_recycle": settings.db_pool_recycle,
        }
    )

engine = create_engine(settings.database_url, **_engine_kwargs)

# ── Slow Query Logging ────────────────────────────────────────────────────────

_SLOW_QUERY_THRESHOLD = 1.0  # seconds


@_sa_event.listens_for(engine, "before_cursor_execute")
def _before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
    conn.info.setdefault("query_start_time", []).append(_time.monotonic())


@_sa_event.listens_for(engine, "after_cursor_execute")
def _after_cursor_execute(conn, cursor, statement, parameters, context, executemany):
    total = _time.monotonic() - conn.info["query_start_time"].pop(-1)
    if total >= _SLOW_QUERY_THRESHOLD:
        logger.warning(
            "Slow query detected",
            extra={
                "extra_data": {
                    "duration_seconds": round(total, 3),
                    "statement": statement[:200],
                }
            },
        )


# ── DB Pool Metrics (Prometheus) ──────────────────────────────────────────────

try:
    from prometheus_client import Gauge as _Gauge

    _db_pool_size = _Gauge("plaidify_db_pool_size", "Database connection pool size")
    _db_pool_checked_in = _Gauge("plaidify_db_pool_checked_in", "Database connections available in pool")
    _db_pool_checked_out = _Gauge("plaidify_db_pool_checked_out", "Database connections currently in use")
    _db_pool_overflow = _Gauge("plaidify_db_pool_overflow", "Database connection pool overflow count")

    @_sa_event.listens_for(engine, "checkout")
    def _on_checkout(dbapi_conn, connection_record, connection_proxy):
        pool = engine.pool
        _db_pool_size.set(pool.size())
        _db_pool_checked_out.set(pool.checkedout())
        _db_pool_checked_in.set(pool.checkedin())
        _db_pool_overflow.set(pool.overflow())

    @_sa_event.listens_for(engine, "checkin")
    def _on_checkin(dbapi_conn, connection_record):
        pool = engine.pool
        _db_pool_checked_out.set(pool.checkedout())
        _db_pool_checked_in.set(pool.checkedin())
        _db_pool_overflow.set(pool.overflow())

    logger.info("Database pool metrics enabled")
except ImportError:
    pass  # prometheus_client not installed

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    """Declarative base class for all ORM models."""

    pass


def init_db() -> None:
    """Initialise the database.

    In **production** the schema is managed exclusively by Alembic migrations,
    so ``create_all`` is skipped.  In development / testing it is still called
    for convenience.
    """
    if settings.env == "production":
        logger.info("Production mode — skipping create_all (use Alembic migrations)")
        return
    logger.info("Initializing database tables (dev/test mode)")
    # Several gunicorn workers start at once. create_all checks for each table
    # and then creates it, so on a fresh database a worker can lose the race
    # ("table users already exists"). The next pass sees the tables and skips them.
    attempts = 5
    for attempt in range(1, attempts + 1):
        try:
            Base.metadata.create_all(bind=engine)
            return
        except DBAPIError as exc:
            message = str(exc.orig if exc.orig is not None else exc).lower()
            if attempt == attempts or not ("already exists" in message or "duplicate key" in message):
                raise
            logger.info("Another process created the schema concurrently; checking again")
            _time.sleep(0.05 * attempt)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency that provides a database session per request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ── ORM Models ────────────────────────────────────────────────────────────────
#
# Every foreign key has a many-to-one relationship() beside it. They are what
# tells the unit of work to INSERT a parent before a child added in the same
# flush (a Link before its AccessToken, an ApiKey before its Agent); without
# them the order is arbitrary and PostgreSQL rejects the child (SQLite does
# not enforce foreign keys, which hid this). There are no back-references.


def _dek_key_version_default(context) -> Optional[int]:
    """Stamp a newly inserted user's DEK with the key version that wrapped it."""
    return get_current_key_version() if context.get_current_parameters().get("encrypted_dek") else None


class User(Base):
    """A registered Plaidify user."""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True, nullable=True)
    email = Column(String, unique=True, index=True, nullable=True)
    hashed_password = Column(Text, nullable=True)
    oauth_provider = Column(String, nullable=True)  # e.g., 'google', 'github'
    oauth_sub = Column(String, nullable=True)  # Provider's user ID
    is_active = Column(Boolean, default=True)
    is_admin = Column(Boolean, default=False, nullable=False)
    created_at = Column(UTCDateTime(), default=utcnow)
    # Envelope encryption: per-user DEK wrapped by master key (base64url)
    encrypted_dek = Column(Text, nullable=True)
    # ENCRYPTION_KEY_VERSION whose key wraps encrypted_dek (local KMS); NULL = not yet checked
    dek_key_version = Column(Integer, nullable=True, default=_dek_key_version_default, index=True)
    # Account lockout
    failed_login_count = Column(Integer, default=0, nullable=False)
    locked_until = Column(UTCDateTime(), nullable=True)
    updated_at = Column(UTCDateTime(), default=utcnow, onupdate=utcnow)
    # API: carried in every access token ("tv"); bumping it ends every session at once
    # (password reset, "sign out everywhere", deactivation).
    token_version = Column(Integer, nullable=False, default=0, server_default="0")
    # API: the address is proven to belong to the account holder (a verified provider
    # email, or a completed emailed password reset). OAuth only links into such accounts.
    email_verified = Column(Boolean, nullable=False, default=False, server_default=false())


class PasswordResetToken(Base):
    """A one-time token for password reset."""

    __tablename__ = "password_reset_tokens"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash = Column(String, unique=True, nullable=False)
    expires_at = Column(UTCDateTime(), nullable=False)
    used = Column(Boolean, default=False, nullable=False)
    created_at = Column(UTCDateTime(), default=utcnow)

    user = relationship("User")


class PendingRegistration(Base):
    """A sign-up waiting for its email address to be proven (POST /auth/verify-email).

    Holds the username, the address and the password hash from registration.
    The account is created only when that password is presented again with the
    mailed token; of the token, only its SHA-256 is stored. One row per
    address: a new sign-up for it replaces the row, and so the earlier token.
    The username is not reserved: whichever sign-up is verified first gets it.
    """

    __tablename__ = "pending_registrations"

    id = Column(Integer, primary_key=True)
    username = Column(String, nullable=False)
    # As the users table stores it: the address as validated (domain lowercased).
    email = Column(String, unique=True, nullable=False)
    hashed_password = Column(Text, nullable=False)
    token_hash = Column(String(64), unique=True, nullable=False)
    created_at = Column(UTCDateTime(), nullable=False, default=utcnow)
    expires_at = Column(UTCDateTime(), nullable=False)


class Link(Base):
    """A link token representing a user's intent to connect a site."""

    __tablename__ = "links"

    link_token = Column(String, primary_key=True, index=True)
    site = Column(String, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at = Column(UTCDateTime(), default=utcnow)

    user = relationship("User")


class AccessToken(Base):
    """An access token storing encrypted credentials for a linked site."""

    __tablename__ = "access_tokens"

    token = Column(String, primary_key=True, index=True)
    link_token = Column(String, ForeignKey("links.link_token", ondelete="CASCADE"), nullable=False)
    username_encrypted = Column(Text, nullable=False)
    password_encrypted = Column(Text, nullable=False)
    instructions = Column(Text, nullable=True)
    scopes = Column(Text, nullable=True)  # JSON list of allowed field/scope strings; NULL = all
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    key_version = Column(Integer, default=1, nullable=False, index=True)
    created_at = Column(UTCDateTime(), default=utcnow)
    updated_at = Column(UTCDateTime(), default=utcnow, onupdate=utcnow)

    link = relationship("Link")
    user = relationship("User")


def hash_refresh_token(token: str) -> str:
    """SHA-256 hex digest under which a refresh token is stored (the token itself never is)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class RefreshToken(Base):
    """A refresh token for JWT token rotation.

    Only the SHA-256 hash of the token is stored. ``RefreshToken(token=raw)``
    hashes the raw value, so the bearer value cannot be stored by mistake.
    """

    __tablename__ = "refresh_tokens"

    id = Column(Integer, primary_key=True, index=True)
    token_hash = Column(String(64), unique=True, index=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    expires_at = Column(UTCDateTime(), nullable=False)
    revoked = Column(Boolean, default=False, nullable=False, server_default=false())
    created_at = Column(UTCDateTime(), default=utcnow)

    user = relationship("User")

    def __init__(self, *, token: Optional[str] = None, **kwargs: Any) -> None:
        if token is not None:
            kwargs["token_hash"] = hash_refresh_token(token)
        super().__init__(**kwargs)


class Webhook(Base):
    """A registered webhook endpoint for link session events."""

    __tablename__ = "webhooks"

    id = Column(String, primary_key=True, index=True)
    link_token = Column(String, nullable=False, index=True)
    url = Column(Text, nullable=False)
    secret = Column(Text, nullable=False)  # encrypt_webhook_secret(); never plaintext
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    # ENCRYPTION_KEY_VERSION current when the secret was last (re-)encrypted
    key_version = Column(Integer, nullable=False, default=get_current_key_version, server_default="1", index=True)
    created_at = Column(UTCDateTime(), default=utcnow)

    user = relationship("User")


class PublicToken(Base):
    """A one-time-use public token exchangeable for an access token.

    Implements the 3-token exchange flow: link_token → public_token → access_token.
    The public_token is short-lived and can only be exchanged once.
    """

    __tablename__ = "public_tokens"

    token = Column(String, primary_key=True, index=True)
    link_token = Column(String, nullable=False)
    access_token = Column(String, ForeignKey("access_tokens.token", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    exchanged = Column(Boolean, default=False)
    expires_at = Column(UTCDateTime(), nullable=False)
    created_at = Column(UTCDateTime(), default=utcnow)

    access_token_record = relationship("AccessToken")
    user = relationship("User")


class ConsentRequest(Base):
    """An agent's request for user consent to access specific data fields."""

    __tablename__ = "consent_requests"

    id = Column(String, primary_key=True, index=True)
    agent_name = Column(String, nullable=False)
    agent_description = Column(Text, nullable=True)
    scopes = Column(Text, nullable=False)  # JSON array of scope strings e.g. ["read:current_bill"]
    duration_seconds = Column(Integer, nullable=False, default=3600)
    access_token = Column(String, ForeignKey("access_tokens.token", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    status = Column(String, nullable=False, default="pending")  # pending, approved, denied, expired
    created_at = Column(UTCDateTime(), default=utcnow)
    updated_at = Column(UTCDateTime(), default=utcnow, onupdate=utcnow)
    # API: the agent that asked (its API key made the request); NULL = asked by the owner.
    agent_id = Column(String, ForeignKey("agents.id", ondelete="CASCADE"), nullable=True, index=True)

    access_token_record = relationship("AccessToken")
    user = relationship("User")
    agent = relationship("Agent")


class ConsentGrant(Base):
    """An approved consent grant — a time-limited, scoped token for data access."""

    __tablename__ = "consent_grants"

    token = Column(String, primary_key=True, index=True)
    consent_request_id = Column(
        String, ForeignKey("consent_requests.id", ondelete="CASCADE"), nullable=False, index=True
    )
    scopes = Column(Text, nullable=False)  # JSON array — copied from request on approval
    access_token = Column(String, ForeignKey("access_tokens.token", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    expires_at = Column(UTCDateTime(), nullable=False)
    revoked = Column(Boolean, default=False)
    created_at = Column(UTCDateTime(), default=utcnow)
    # API: only this agent may read data under the grant; NULL = usable by the owner only.
    agent_id = Column(String, ForeignKey("agents.id", ondelete="CASCADE"), nullable=True, index=True)

    consent_request = relationship("ConsentRequest")
    access_token_record = relationship("AccessToken")
    user = relationship("User")
    agent = relationship("Agent")


class BlueprintRecord(Base):
    """A published blueprint in the registry.

    Quality tiers:
    - community: user-submitted, unverified
    - tested: passes automated CI validation
    - certified: manually reviewed and approved
    """

    __tablename__ = "blueprint_registry"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    site = Column(String, unique=True, nullable=False, index=True)
    domain = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    author = Column(String, nullable=True)
    version = Column(String, nullable=False, default="1.0.0")
    schema_version = Column(String, nullable=False, default="2")
    tags = Column(Text, nullable=True)  # JSON array stored as text
    has_mfa = Column(Boolean, default=False)
    quality_tier = Column(String, nullable=False, default="community")
    blueprint_json = Column(Text, nullable=False)  # Full blueprint JSON
    extract_fields = Column(Text, nullable=True)  # JSON array of field names
    downloads = Column(Integer, default=0)
    published_by = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    created_at = Column(UTCDateTime(), default=utcnow)
    updated_at = Column(UTCDateTime(), default=utcnow, onupdate=utcnow)

    publisher = relationship("User")


class AuditLog(Base):
    """Tamper-evident audit log entry in a keyed hash chain (see src/audit.py).

    ``entry_hash`` is an HMAC-SHA256 over the entry's content and the previous
    entry's hash, under the key named by ``key_id``; entries from before the
    chain was keyed use plain SHA-256 (``key_id = "sha256"``).
    """

    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    event_type = Column(String, nullable=False, index=True)
    user_id = Column(Integer, nullable=True, index=True)
    agent_id = Column(String, nullable=True, index=True)  # Agent identity if action was by an agent
    resource = Column(String, nullable=True)
    action = Column(String, nullable=False)
    metadata_json = Column(Text, nullable=True)  # JSON-encoded metadata
    ip_address = Column(String(45), nullable=True)  # IPv4 or IPv6
    timestamp = Column(UTCDateTime(), nullable=False, default=utcnow, index=True)
    prev_hash = Column(String(64), nullable=True)  # entry_hash of the previous entry
    entry_hash = Column(String(64), nullable=False)  # HMAC-SHA256 hex of this entry (legacy: SHA-256)
    key_id = Column(String(16), nullable=False, server_default="sha256")  # signing key id


class AuditChainHead(Base):
    """The newest audit entry, under its own MAC (a single row, id = 1).

    Rewritten by every append while the chain lock is held, so verification
    can tell when the newest entries were deleted; on SQLite, writing it is
    the chain lock.
    """

    __tablename__ = "audit_chain_head"

    id = Column(Integer, primary_key=True, autoincrement=False)
    last_entry_id = Column(Integer, nullable=True)
    last_entry_hash = Column(String(64), nullable=True)
    key_id = Column(String(16), nullable=True)
    seal_pending = Column(Boolean, nullable=False, default=False, server_default=false())
    head_mac = Column(String(64), nullable=True)
    updated_at = Column(UTCDateTime(), nullable=True)


class ApiKey(Base):
    """An API key for programmatic access (alternative to JWT).

    The raw key is shown once on creation. Only the SHA-256 hash is stored.
    Keys can be scoped, expired, and revoked.
    """

    __tablename__ = "api_keys"

    id = Column(String, primary_key=True, index=True)
    name = Column(String, nullable=False)
    key_hash = Column(String(64), unique=True, nullable=False, index=True)  # SHA-256 hex
    key_prefix = Column(String(32), nullable=False)  # Leading chars of the key (pk_… / pk_agent_…) for identification
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    scopes = Column(Text, nullable=True)  # JSON array of scope strings; NULL = all
    is_active = Column(Boolean, default=True, nullable=False)
    expires_at = Column(UTCDateTime(), nullable=True)  # NULL = never expires
    last_used_at = Column(UTCDateTime(), nullable=True)
    created_at = Column(UTCDateTime(), default=utcnow)

    user = relationship("User")


class Agent(Base):
    """A registered AI agent with its own identity and permissions.

    Agents are created by users and receive their own API key for
    authenticated access. Each agent has:
    - A unique agent_id (prefixed with 'agent-')
    - Allowed scopes defining what data it can request
    - Allowed sites restricting which blueprints it can connect to
    - Rate limits independent of the owning user
    """

    __tablename__ = "agents"

    id = Column(String, primary_key=True, index=True)  # agent-uuid
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    owner_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    api_key_id = Column(String, ForeignKey("api_keys.id", ondelete="SET NULL"), nullable=True)  # Linked API key
    allowed_scopes = Column(Text, nullable=True)  # JSON array of scope strings; NULL = all
    allowed_sites = Column(Text, nullable=True)  # JSON array of site identifiers; NULL = all
    rate_limit = Column(String, nullable=True)  # e.g. "30/minute"
    is_active = Column(Boolean, default=True, nullable=False)
    last_active_at = Column(UTCDateTime(), nullable=True)
    created_at = Column(UTCDateTime(), default=utcnow)
    updated_at = Column(UTCDateTime(), default=utcnow, onupdate=utcnow)

    owner = relationship("User")
    api_key = relationship("ApiKey")


class AccessJob(Base):
    """A tracked site-access execution with per-scope concurrency control."""

    __tablename__ = "access_jobs"

    id = Column(String, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    site = Column(String, nullable=False, index=True)
    job_type = Column(String, nullable=False, index=True)
    status = Column(String, nullable=False, default="pending", index=True)
    lock_scope = Column(String, nullable=False, index=True)
    session_id = Column(String, nullable=True, index=True)
    metadata_json = Column(Text, nullable=True)
    # encrypt_json_for_user() output ("enc:v1:..."); legacy rows hold plaintext JSON.
    # Erased by purge_expired_job_results() after RESULT_RETENTION_DAYS.
    result_json = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(UTCDateTime(), default=utcnow, nullable=False)
    started_at = Column(UTCDateTime(), nullable=True)
    completed_at = Column(UTCDateTime(), nullable=True)
    # How a job failed, so a job run by another process fails the same way as
    # one run in-process: the LinkErrorCode value, the exception class name and
    # its HTTP status.
    error_code = Column(String(64), nullable=True)
    error_type = Column(String(64), nullable=True)
    error_status = Column(Integer, nullable=True)
    # Liveness. The process running the job refreshes heartbeat_at; the reaper
    # fails a job whose heartbeat went stale (its process died) or that is past
    # deadline_at.
    worker_id = Column(String(128), nullable=True)
    heartbeat_at = Column(UTCDateTime(), nullable=True)
    deadline_at = Column(UTCDateTime(), nullable=True)

    user = relationship("User")


class ScheduledRefreshJob(Base):
    """Persisted refresh job — survives server restarts.

    The RefreshScheduler loads these on startup and saves state after each run.
    """

    __tablename__ = "scheduled_refresh_jobs"

    access_token = Column(String, ForeignKey("access_tokens.token", ondelete="CASCADE"), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    interval_seconds = Column(Integer, nullable=False, default=3600)
    schedule_format = Column(String, nullable=False, default="interval")
    enabled = Column(Boolean, default=True, nullable=False)
    last_refreshed = Column(UTCDateTime(), nullable=True)
    last_error = Column(Text, nullable=True)
    consecutive_failures = Column(Integer, default=0, nullable=False)
    created_at = Column(UTCDateTime(), default=utcnow)
    # When the refresh is next due. The scheduler claims a due row by moving
    # this forward, so one refresh runs once however many processes look.
    next_run_at = Column(UTCDateTime(), nullable=True, index=True)
    # Why the schedule was disabled (e.g. "needs_reauth"); NULL while enabled.
    disabled_reason = Column(String(64), nullable=True)

    access_token_record = relationship("AccessToken")
    user = relationship("User")


# ── API: sign-in throttling and maintenance leases ────────────────────────────


class LoginThrottle(Base):
    """Recent failed password sign-ins for one (username, client address) pair or one username.

    Keyed by an HMAC of the submitted username (and address), never the
    values themselves, and kept for unknown usernames too, so a lockout says
    nothing about whether an account exists. ``subject`` (the username's
    HMAC) lets a password reset clear every row of that username.
    """

    __tablename__ = "login_throttles"

    key = Column(String(64), primary_key=True)
    subject = Column(String(64), nullable=False, index=True)
    failures = Column(Integer, nullable=False, default=0)
    window_started_at = Column(UTCDateTime(), nullable=False)
    locked_until = Column(UTCDateTime(), nullable=True)
    updated_at = Column(UTCDateTime(), nullable=False, default=utcnow, index=True)


class MaintenanceLease(Base):
    """Which process runs a periodic maintenance task; one holder until ``expires_at``."""

    __tablename__ = "maintenance_leases"

    name = Column(String(64), primary_key=True)
    holder = Column(String(128), nullable=False)
    expires_at = Column(UTCDateTime(), nullable=False)


# ── Jobs: durable webhook delivery ────────────────────────────────────────────


class WebhookDelivery(Base):
    """One webhook event for one endpoint: the durable delivery outbox.

    Written in the transaction that decides the event, sent by the outbox with
    retries and exponential backoff. ``id`` is the delivery id receivers use to
    drop duplicates (``X-Plaidify-Delivery``); it stays the same across retries.
    """

    __tablename__ = "webhook_deliveries"

    id = Column(String(64), primary_key=True)
    webhook_id = Column(String, ForeignKey("webhooks.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    event = Column(String(64), nullable=False)
    # encrypt_json_for_user() output (context "webhook_delivery:<id>:payload").
    payload_json = Column(Text, nullable=False)
    status = Column(String(16), nullable=False, default="pending", index=True)  # pending | delivered | failed
    attempts = Column(Integer, nullable=False, default=0)
    next_attempt_at = Column(UTCDateTime(), nullable=True, index=True)
    # Set while an attempt is in flight so no other process sends it too.
    locked_until = Column(UTCDateTime(), nullable=True)
    last_status_code = Column(Integer, nullable=True)
    last_error = Column(String(64), nullable=True)  # a category, never a raw error message
    created_at = Column(UTCDateTime(), default=utcnow, nullable=False)
    delivered_at = Column(UTCDateTime(), nullable=True)

    webhook = relationship("Webhook")
    user = relationship("User")


def delete_user_data(db: "Session", user_id: int) -> dict:
    """Erase all data owned by a user (GDPR right-to-erasure).

    Rows are deleted child-first so foreign-key constraints are satisfied on
    databases that enforce them (PostgreSQL); SQLite does not require ordering
    but follows the same path. Audit-log entries are intentionally NOT removed:
    they carry no direct credential PII and are retained for compliance
    (``AuditLog.user_id`` has no foreign key, so the user row can be deleted
    without orphaning the immutable hash chain).

    This function does not commit — the caller owns the transaction so the
    final user-row deletion is atomic with the cascade.

    Returns:
        Mapping of table name to the number of rows deleted (omits zero counts).
    """
    targets = [
        (ConsentGrant, ConsentGrant.user_id == user_id),
        (ConsentRequest, ConsentRequest.user_id == user_id),
        (PublicToken, PublicToken.user_id == user_id),
        (ScheduledRefreshJob, ScheduledRefreshJob.user_id == user_id),
        (AccessToken, AccessToken.user_id == user_id),
        (Link, Link.user_id == user_id),
        (WebhookDelivery, WebhookDelivery.user_id == user_id),
        (Webhook, Webhook.user_id == user_id),
        (RefreshToken, RefreshToken.user_id == user_id),
        (PasswordResetToken, PasswordResetToken.user_id == user_id),
        (Agent, Agent.owner_id == user_id),
        (ApiKey, ApiKey.user_id == user_id),
        (BlueprintRecord, BlueprintRecord.published_by == user_id),
        (AccessJob, AccessJob.user_id == user_id),
    ]
    summary: dict[str, int] = {}
    for model, condition in targets:
        count = db.query(model).filter(condition).delete(synchronize_session=False)
        if count:
            summary[model.__tablename__] = count
    return summary
