"""
Database models, session management, and encryption utilities.

Uses SQLAlchemy for ORM and AES-256-GCM for symmetric credential encryption.
All configuration is loaded from the Settings object — no hardcoded secrets.
"""

import base64
import os
import time as _time
from collections.abc import Generator
from datetime import datetime, timezone

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text, create_engine
from sqlalchemy import event as _sa_event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from src.config import get_settings
from src.logging_config import get_logger

logger = get_logger(__name__)
settings = get_settings()

# ── Encryption (AES-256-GCM) ─────────────────────────────────────────────────

_GCM_NONCE_BYTES = 12  # 96-bit nonce, NIST recommended for GCM


def _get_encryption_key() -> bytes:
    """Decode the base64-encoded 256-bit encryption key."""
    raw = settings.encryption_key
    key_bytes = base64.urlsafe_b64decode(raw.encode("ascii") if isinstance(raw, str) else raw)
    if len(key_bytes) not in (32, 44):
        # 32 bytes = raw AES-256 key; 44 bytes = Fernet key (we extract first 16 + last 16)
        raise ValueError(f"ENCRYPTION_KEY must decode to 32 bytes (AES-256). Got {len(key_bytes)} bytes.")
    if len(key_bytes) == 44:
        # Legacy Fernet key: 16-byte signing key + 16-byte encryption key (AES-128).
        # Reject: the old doubling hack only provided 128-bit effective strength.
        # Operators must rotate to a proper 256-bit key.
        raise ValueError(
            "Detected legacy Fernet-format ENCRYPTION_KEY (44 bytes). "
            "This key format is no longer supported — it only provides 128-bit "
            "effective strength. Generate a new 256-bit key: "
            'python -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())" '
            "and re-encrypt existing data with the key rotation procedure."
        )
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


def encrypt_credential_for_user(user: "User", plaintext: str) -> str:
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


def decrypt_credential_for_user(user: "User", ciphertext: str) -> str:
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


def ensure_user_dek(user: "User", db: "Session") -> None:
    """Ensure a user has a DEK. Creates one if missing (lazy migration)."""
    if not user.encrypted_dek:
        user.encrypted_dek = create_user_dek()
        db.commit()
        logger.info("Generated DEK for user", extra={"extra_data": {"user_id": user.id}})


def rotate_master_key(old_key: str, new_key: str, db: "Session") -> int:
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


def re_encrypt_tokens(db: "Session", batch_size: int = 100) -> int:
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
    tokens = db.query(AccessToken).filter(AccessToken.key_version < current_version).limit(batch_size).all()
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
    # Account lockout
    failed_login_count = Column(Integer, default=0, nullable=False)
    locked_until = Column(DateTime, nullable=True)
    updated_at = Column(
        DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )


class PasswordResetToken(Base):
    """A one-time token for password reset."""

    __tablename__ = "password_reset_tokens"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash = Column(String, unique=True, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    used = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class Link(Base):
    """A link token representing a user's intent to connect a site."""

    __tablename__ = "links"

    link_token = Column(String, primary_key=True, index=True)
    site = Column(String, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


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
    key_version = Column(Integer, default=1, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )


class RefreshToken(Base):
    """A refresh token for JWT token rotation."""

    __tablename__ = "refresh_tokens"

    id = Column(Integer, primary_key=True, index=True)
    token = Column(String, unique=True, index=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
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
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


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
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


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
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )


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
    expires_at = Column(DateTime, nullable=False)
    revoked = Column(Boolean, default=False)
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
    published_by = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )


class AuditLog(Base):
    """Tamper-evident audit log entry with hash chain.

    Each entry stores a SHA-256 hash linking to the previous entry,
    forming an immutable chain that can be verified for integrity.
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
    timestamp = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc), index=True)
    prev_hash = Column(String(64), nullable=True)  # SHA-256 hex of previous entry
    entry_hash = Column(String(64), nullable=False)  # SHA-256 hex of this entry


class ApiKey(Base):
    """An API key for programmatic access (alternative to JWT).

    The raw key is shown once on creation. Only the SHA-256 hash is stored.
    Keys can be scoped, expired, and revoked.
    """

    __tablename__ = "api_keys"

    id = Column(String, primary_key=True, index=True)
    name = Column(String, nullable=False)
    key_hash = Column(String(64), unique=True, nullable=False, index=True)  # SHA-256 hex
    key_prefix = Column(String(12), nullable=False)  # First 8 chars of key for identification
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    scopes = Column(Text, nullable=True)  # JSON array of scope strings; NULL = all
    is_active = Column(Boolean, default=True, nullable=False)
    expires_at = Column(DateTime, nullable=True)  # NULL = never expires
    last_used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


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
    last_active_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )


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
    result_json = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)


class ScheduledRefreshJob(Base):
    """Persisted refresh job — survives server restarts.

    The RefreshScheduler loads these on startup and saves state after each run.
    """

    __tablename__ = "scheduled_refresh_jobs"

    access_token = Column(String, ForeignKey("access_tokens.token", ondelete="CASCADE"), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    interval_seconds = Column(Integer, nullable=False, default=3600)
    enabled = Column(Boolean, default=True, nullable=False)
    last_refreshed = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    consecutive_failures = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
