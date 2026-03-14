"""
Database models, session management, and encryption utilities.

Uses SQLAlchemy for ORM and Fernet for symmetric credential encryption.
All configuration is loaded from the Settings object — no hardcoded secrets.
"""

from sqlalchemy import create_engine, Column, String, Text, Integer, Boolean, ForeignKey, DateTime
from sqlalchemy.orm import DeclarativeBase, sessionmaker, Session
from cryptography.fernet import Fernet
from datetime import datetime, timezone
from typing import Generator

from src.config import get_settings
from src.logging_config import get_logger

logger = get_logger(__name__)
settings = get_settings()

# ── Encryption ────────────────────────────────────────────────────────────────

fernet = Fernet(settings.encryption_key.encode() if isinstance(settings.encryption_key, str) else settings.encryption_key)


def encrypt_credential(plaintext: str) -> str:
    """Encrypt a plaintext string using Fernet symmetric encryption."""
    return fernet.encrypt(plaintext.encode()).decode()


def decrypt_credential(ciphertext: str) -> str:
    """Decrypt a Fernet-encrypted string back to plaintext."""
    return fernet.decrypt(ciphertext.encode()).decode()


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
