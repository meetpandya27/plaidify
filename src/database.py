"""
Database models, session management, and encryption utilities.

Uses SQLAlchemy for ORM and AES-256-GCM for symmetric credential encryption.
All configuration is loaded from the Settings object — no hardcoded secrets.
"""

import os
import base64

from sqlalchemy import create_engine, Column, String, Text, Integer, Boolean, ForeignKey, DateTime
from sqlalchemy.orm import DeclarativeBase, sessionmaker, Session
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.fernet import Fernet
from datetime import datetime, timezone
from typing import Generator

from src.config import get_settings
from src.logging_config import get_logger

logger = get_logger(__name__)
settings = get_settings()

# ── Encryption (AES-256-GCM) ─────────────────────────────────────────────────

_GCM_NONCE_BYTES = 12  # 96-bit nonce, NIST recommended for GCM


def _get_encryption_key() -> bytes:
    """Decode the base64-encoded 256-bit encryption key."""
    raw = settings.encryption_key
    key_bytes = base64.urlsafe_b64decode(
        raw.encode("ascii") if isinstance(raw, str) else raw
    )
    if len(key_bytes) not in (32, 44):
        # 32 bytes = raw AES-256 key; 44 bytes = Fernet key (we extract first 16 + last 16)
        raise ValueError(
            f"ENCRYPTION_KEY must decode to 32 bytes (AES-256). Got {len(key_bytes)} bytes."
        )
    if len(key_bytes) == 44:
        # Legacy Fernet key: 16-byte signing key + 16-byte encryption key  (AES-128)
        # In legacy-compat mode we'll use Fernet directly for decrypt; for new
        # encryptions we still need 32 bytes, so double the encryption half.
        # NOTE: Operators should rotate to a proper 256-bit key.
        logger.warning(
            "Detected legacy Fernet-format ENCRYPTION_KEY. "
            "Generate a new 256-bit key: python -c \"import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())\""
        )
        key_bytes = key_bytes[16:32] * 2  # 16 → 32 bytes (temporary compat)
    return key_bytes


def _get_aesgcm() -> AESGCM:
    return AESGCM(_get_encryption_key())


def encrypt_credential(plaintext: str) -> str:
    """Encrypt a plaintext string using AES-256-GCM.

    Output format: base64url( nonce‖ciphertext‖tag )
    - 12-byte random nonce ensures uniqueness.
    - GCM provides both confidentiality and authenticity.
    """
    nonce = os.urandom(_GCM_NONCE_BYTES)
    ct = _get_aesgcm().encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.urlsafe_b64encode(nonce + ct).decode("ascii")


def decrypt_credential(ciphertext: str) -> str:
    """Decrypt an AES-256-GCM encrypted string.

    Falls back to legacy Fernet decryption for data encrypted before the
    AES-256-GCM migration.
    """
    raw = base64.urlsafe_b64decode(ciphertext)
    if len(raw) > _GCM_NONCE_BYTES:
        try:
            nonce = raw[:_GCM_NONCE_BYTES]
            ct = raw[_GCM_NONCE_BYTES:]
            return _get_aesgcm().decrypt(nonce, ct, None).decode("utf-8")
        except Exception:
            pass  # Fall through to Fernet legacy path

    # Legacy Fernet fallback — handles data encrypted before the migration.
    try:
        key = settings.encryption_key
        f = Fernet(key.encode("ascii") if isinstance(key, str) else key)
        return f.decrypt(ciphertext.encode()).decode()
    except Exception as exc:
        raise ValueError("Failed to decrypt credential (tried AES-256-GCM and legacy Fernet)") from exc


# Keep old names as aliases for backward compatibility
encrypt_password = encrypt_credential
decrypt_password = decrypt_credential


# ── Envelope Encryption (per-user DEK) ────────────────────────────────────────

def generate_dek() -> bytes:
    """Generate a random 256-bit Data Encryption Key."""
    return os.urandom(32)


def wrap_dek(dek: bytes) -> str:
    """Encrypt (wrap) a DEK with the master key using AES-256-GCM.

    Returns a base64url-encoded string: nonce‖ciphertext‖tag.
    """
    nonce = os.urandom(_GCM_NONCE_BYTES)
    ct = _get_aesgcm().encrypt(nonce, dek, None)
    return base64.urlsafe_b64encode(nonce + ct).decode("ascii")


def unwrap_dek(wrapped_dek: str) -> bytes:
    """Decrypt (unwrap) a DEK using the master key.

    Tries the current master key first. If that fails and a previous key
    is configured (ENCRYPTION_KEY_PREVIOUS), falls back to it.

    Returns the raw 32-byte DEK.
    """
    raw = base64.urlsafe_b64decode(wrapped_dek)
    nonce = raw[:_GCM_NONCE_BYTES]
    ct = raw[_GCM_NONCE_BYTES:]
    try:
        return _get_aesgcm().decrypt(nonce, ct, None)
    except Exception:
        # Try previous master key if configured
        prev = settings.encryption_key_previous
        if prev:
            prev_bytes = base64.urlsafe_b64decode(prev.encode("ascii"))
            return AESGCM(prev_bytes).decrypt(nonce, ct, None)
        raise


def create_user_dek() -> str:
    """Generate a new DEK and return it wrapped (ready to store in DB)."""
    dek = generate_dek()
    return wrap_dek(dek)


def encrypt_credential_for_user(user: 'User', plaintext: str) -> str:
    """Encrypt a credential using the user's per-user DEK.

    Falls back to the global master key if the user has no DEK yet
    (migration path for existing users).
    """
    if user.encrypted_dek:
        dek = unwrap_dek(user.encrypted_dek)
        aesgcm = AESGCM(dek)
        nonce = os.urandom(_GCM_NONCE_BYTES)
        ct = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
        return base64.urlsafe_b64encode(nonce + ct).decode("ascii")
    # Fallback: encrypt with master key (legacy path)
    return encrypt_credential(plaintext)


def decrypt_credential_for_user(user: 'User', ciphertext: str) -> str:
    """Decrypt a credential using the user's per-user DEK.

    Falls back to master-key decryption for data encrypted before
    envelope encryption was enabled (migration compatibility).
    """
    if user.encrypted_dek:
        dek = unwrap_dek(user.encrypted_dek)
        aesgcm = AESGCM(dek)
        raw = base64.urlsafe_b64decode(ciphertext)
        try:
            nonce = raw[:_GCM_NONCE_BYTES]
            ct = raw[_GCM_NONCE_BYTES:]
            return aesgcm.decrypt(nonce, ct, None).decode("utf-8")
        except Exception:
            pass  # Fall through to legacy master-key decryption
    # Fallback: decrypt with master key (legacy data)
    return decrypt_credential(ciphertext)


def ensure_user_dek(user: 'User', db: 'Session') -> None:
    """Ensure a user has a DEK. Creates one if missing (lazy migration)."""
    if not user.encrypted_dek:
        user.encrypted_dek = create_user_dek()
        db.commit()
        logger.info("Generated DEK for user", extra={"extra_data": {"user_id": user.id}})


def rotate_master_key(old_key: str, new_key: str, db: 'Session') -> int:
    """Re-wrap all user DEKs with a new master key.

    This does NOT re-encrypt any data — it only re-wraps the DEK envelopes.
    After calling this, update ENCRYPTION_KEY to the new key value.

    Args:
        old_key: Current ENCRYPTION_KEY (base64url-encoded).
        new_key: New ENCRYPTION_KEY (base64url-encoded).
        db: Database session.

    Returns:
        Number of DEKs re-wrapped.
    """
    old_key_bytes = base64.urlsafe_b64decode(old_key.encode("ascii"))
    new_key_bytes = base64.urlsafe_b64decode(new_key.encode("ascii"))
    old_aesgcm = AESGCM(old_key_bytes)
    new_aesgcm = AESGCM(new_key_bytes)

    users = db.query(User).filter(User.encrypted_dek.isnot(None)).all()
    count = 0
    for user in users:
        # Unwrap with old key
        raw = base64.urlsafe_b64decode(user.encrypted_dek)
        nonce = raw[:_GCM_NONCE_BYTES]
        ct = raw[_GCM_NONCE_BYTES:]
        dek = old_aesgcm.decrypt(nonce, ct, None)

        # Re-wrap with new key
        new_nonce = os.urandom(_GCM_NONCE_BYTES)
        new_ct = new_aesgcm.encrypt(new_nonce, dek, None)
        user.encrypted_dek = base64.urlsafe_b64encode(new_nonce + new_ct).decode("ascii")
        count += 1

    db.commit()
    logger.info(f"Master key rotation complete: {count} DEK(s) re-wrapped")
    return count


def get_current_key_version() -> int:
    """Return the current encryption key version from settings."""
    return settings.encryption_key_version


def re_encrypt_tokens(db: 'Session', batch_size: int = 100) -> int:
    """Re-encrypt AccessToken credentials that are on an older key_version.

    This is the background job that should be run after a master-key rotation.
    It decrypts each credential with the user's DEK (unwrapping with the
    current or previous master key) and re-encrypts it, then stamps the
    current key_version.

    Args:
        db: Database session.
        batch_size: Number of tokens to process per batch.

    Returns:
        Number of tokens re-encrypted.
    """
    current_version = get_current_key_version()
    tokens = (
        db.query(AccessToken)
        .filter(AccessToken.key_version < current_version)
        .limit(batch_size)
        .all()
    )
    count = 0
    for token in tokens:
        user = db.query(User).filter(User.id == token.user_id).first()
        if not user or not user.encrypted_dek:
            logger.warning(
                "Skipping token re-encryption: no user or DEK",
                extra={"extra_data": {"token": token.token}},
            )
            continue

        try:
            plain_user = decrypt_credential_for_user(user, token.username_encrypted)
            plain_pass = decrypt_credential_for_user(user, token.password_encrypted)
            token.username_encrypted = encrypt_credential_for_user(user, plain_user)
            token.password_encrypted = encrypt_credential_for_user(user, plain_pass)
            token.key_version = current_version
            count += 1
        except Exception as e:
            logger.error(
                f"Failed to re-encrypt token {token.token}: {e}",
                extra={"extra_data": {"token": token.token}},
            )

    db.commit()
    logger.info(f"Re-encrypted {count} access token(s) to key_version={current_version}")
    return count

# ── SQLAlchemy Setup ──────────────────────────────────────────────────────────

engine = create_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,  # Verify connections before use
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    """Declarative base class for all ORM models."""
    pass


def init_db() -> None:
    """Create all database tables. Use Alembic migrations in production."""
    logger.info("Initializing database tables")
    Base.metadata.create_all(bind=engine)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency that provides a database session per request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ── ORM Models ────────────────────────────────────────────────────────────────


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
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    # Envelope encryption: per-user DEK wrapped by master key (base64url)
    encrypted_dek = Column(Text, nullable=True)


class Link(Base):
    """A link token representing a user's intent to connect a site."""

    __tablename__ = "links"

    link_token = Column(String, primary_key=True, index=True)
    site = Column(String, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class AccessToken(Base):
    """An access token storing encrypted credentials for a linked site."""

    __tablename__ = "access_tokens"

    token = Column(String, primary_key=True, index=True)
    link_token = Column(String, ForeignKey("links.link_token"), nullable=False)
    username_encrypted = Column(Text, nullable=False)
    password_encrypted = Column(Text, nullable=False)
    instructions = Column(Text, nullable=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    key_version = Column(Integer, default=1, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class RefreshToken(Base):
    """A refresh token for JWT token rotation."""

    __tablename__ = "refresh_tokens"

    id = Column(Integer, primary_key=True, index=True)
    token = Column(String, unique=True, index=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    expires_at = Column(DateTime, nullable=False)
    revoked = Column(Boolean, default=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class Webhook(Base):
    """A registered webhook endpoint for link session events."""

    __tablename__ = "webhooks"

    id = Column(String, primary_key=True, index=True)
    link_token = Column(String, nullable=False, index=True)
    url = Column(Text, nullable=False)
    secret = Column(Text, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class PublicToken(Base):
    """A one-time-use public token exchangeable for an access token.

    Implements the 3-token exchange flow: link_token → public_token → access_token.
    The public_token is short-lived and can only be exchanged once.
    """

    __tablename__ = "public_tokens"

    token = Column(String, primary_key=True, index=True)
    link_token = Column(String, nullable=False)
    access_token = Column(String, ForeignKey("access_tokens.token"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    exchanged = Column(Boolean, default=False)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


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
    published_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))
