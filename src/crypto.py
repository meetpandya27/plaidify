"""
Ephemeral RSA keypair management for client-side credential encryption.

Each link session gets a unique RSA-2048 keypair. The public key is sent to
the client, which encrypts credentials before transmission. The server holds
the private key — either in Redis (multi-worker) or in-memory (single-worker dev).
"""

import threading
import time
from typing import Optional, Tuple

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from src.logging_config import get_logger

logger = get_logger(__name__)

# TTL for ephemeral keys (seconds). Keys older than this are purged.
_KEY_TTL_SECONDS = 600  # 10 minutes

# In-memory fallback store: link_token → (private_key, created_at)
_key_store: dict[str, Tuple[rsa.RSAPrivateKey, float]] = {}
_lock = threading.Lock()

# Redis client (initialized lazily)
_redis_client = None
_redis_available = None  # None = not checked yet, True/False after first check
_REDIS_KEY_PREFIX = "plaidify:ephemeral_key:"


def _get_redis():
    """Get or create a Redis client. Returns None if Redis is unavailable.

    Re-checks connectivity via ping() so that a transient outage doesn't
    permanently disable Redis-backed key storage.
    """
    global _redis_client, _redis_available

    from src.config import get_settings

    settings = get_settings()
    if not settings.redis_url:
        if settings.env == "production":
            raise RuntimeError("REDIS_URL is required in production for ephemeral key storage.")
        return None

    # If we have a cached client, verify it's still alive
    if _redis_client is not None:
        try:
            _redis_client.ping()
            return _redis_client
        except Exception as exc:
            if settings.env == "production":
                raise RuntimeError("Redis connection lost for ephemeral key storage.") from exc

            logger.warning("Redis connection lost for ephemeral keys, reconnecting...")
            _redis_client = None

    try:
        import redis as redis_lib

        _redis_client = redis_lib.Redis.from_url(
            settings.redis_url,
            decode_responses=False,
            socket_connect_timeout=2,
        )
        _redis_client.ping()
        _redis_available = True
        logger.info("Redis connected for ephemeral key storage")
        return _redis_client
    except Exception as e:
        if settings.env == "production":
            raise RuntimeError("Redis is unavailable for ephemeral key storage in production.") from e

        logger.warning(f"Redis unavailable, falling back to in-memory key store: {e}")
        _redis_client = None
        return None


def _serialize_private_key(private_key: rsa.RSAPrivateKey) -> bytes:
    """Serialize private key to PEM bytes."""
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _deserialize_private_key(pem_bytes: bytes) -> rsa.RSAPrivateKey:
    """Deserialize private key from PEM bytes."""
    return serialization.load_pem_private_key(pem_bytes, password=None)


def generate_keypair(link_token: str) -> str:
    """Generate an RSA-2048 ephemeral keypair for a link session.

    Returns the PEM-encoded public key (PKCS#1 SubjectPublicKeyInfo / SPKI)
    as a string for the client.

    The private key is stored in Redis (if available) or in-memory.
    """
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("ascii")
    )

    r = _get_redis()
    if r is not None:
        try:
            r.setex(
                f"{_REDIS_KEY_PREFIX}{link_token}",
                _KEY_TTL_SECONDS,
                _serialize_private_key(private_key),
            )
            logger.debug("Ephemeral keypair stored in Redis", extra={"extra_data": {"link_token": link_token}})
            return public_pem
        except Exception as e:
            logger.warning(f"Redis write failed, falling back to in-memory: {e}")

    # In-memory fallback
    with _lock:
        _key_store[link_token] = (private_key, time.monotonic())

    logger.debug("Ephemeral keypair generated (in-memory)", extra={"extra_data": {"link_token": link_token}})
    return public_pem


def _get_private_key(link_token: str) -> Optional[rsa.RSAPrivateKey]:
    """Retrieve the private key for a link session from Redis or in-memory."""
    r = _get_redis()
    if r is not None:
        try:
            pem_bytes = r.get(f"{_REDIS_KEY_PREFIX}{link_token}")
            if pem_bytes:
                return _deserialize_private_key(pem_bytes)
        except Exception as e:
            logger.warning(f"Redis read failed: {e}")

    # In-memory fallback
    with _lock:
        entry = _key_store.get(link_token)
    if entry is None:
        return None
    return entry[0]


def get_public_key(link_token: str) -> Optional[str]:
    """Retrieve the PEM public key for a link session, if it exists."""
    private_key = _get_private_key(link_token)
    if private_key is None:
        return None
    return (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("ascii")
    )


def decrypt_with_session_key(link_token: str, ciphertext: bytes) -> str:
    """Decrypt ciphertext using the ephemeral private key for this session.

    Uses RSA-OAEP with SHA-256, matching the WebCrypto / SDK encryption.

    Args:
        link_token: The session identifier whose key to use.
        ciphertext: Raw ciphertext bytes (base64-decoded by caller).

    Returns:
        Decrypted plaintext string.

    Raises:
        ValueError: If the link_token has no associated key or decryption fails.
    """
    private_key = _get_private_key(link_token)
    if private_key is None:
        raise ValueError("No ephemeral key found for link_token (expired or invalid)")

    try:
        plaintext = private_key.decrypt(
            ciphertext,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )
        return plaintext.decode("utf-8")
    except Exception as exc:
        raise ValueError("Failed to decrypt credentials with ephemeral key") from exc


def destroy_session_key(link_token: str) -> None:
    """Remove the ephemeral key for a session (called after use)."""
    r = _get_redis()
    if r is not None:
        try:
            r.delete(f"{_REDIS_KEY_PREFIX}{link_token}")
        except Exception:
            pass

    with _lock:
        _key_store.pop(link_token, None)
    logger.debug("Ephemeral key destroyed", extra={"extra_data": {"link_token": link_token}})


def cleanup_expired_keys() -> int:
    """Remove all in-memory keys older than TTL. Returns the number of keys purged.

    Redis keys auto-expire via TTL, so this only cleans the in-memory fallback.
    """
    cutoff = time.monotonic() - _KEY_TTL_SECONDS
    purged = 0
    with _lock:
        expired = [k for k, (_, ts) in _key_store.items() if ts < cutoff]
        for k in expired:
            del _key_store[k]
            purged += 1
    if purged:
        logger.info(f"Purged {purged} expired ephemeral key(s)")
    return purged


def _clear_all_keys() -> None:
    """Clear all keys. For testing only."""
    global _redis_client, _redis_available
    with _lock:
        _key_store.clear()
    # Also clear Redis keys if available
    r = _get_redis()
    if r is not None:
        try:
            for key in r.scan_iter(f"{_REDIS_KEY_PREFIX}*"):
                r.delete(key)
        except Exception:
            pass
    _redis_client = None
    _redis_available = None
