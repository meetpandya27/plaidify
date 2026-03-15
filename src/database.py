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
