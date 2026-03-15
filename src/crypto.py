"""
Ephemeral RSA keypair management for client-side credential encryption.

Each link session gets a unique RSA-2048 keypair. The public key is sent to
the client, which encrypts credentials before transmission. The server holds
the private key in memory only — it is never persisted to disk or database.
"""

import threading
import time
from typing import Optional, Tuple

from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.primitives import hashes, serialization

from src.logging_config import get_logger

logger = get_logger(__name__)

# TTL for ephemeral keys (seconds). Keys older than this are purged.
_KEY_TTL_SECONDS = 600  # 10 minutes

# Module-level store: link_token → (private_key, created_at)
_key_store: dict[str, Tuple[rsa.RSAPrivateKey, float]] = {}
_lock = threading.Lock()


def generate_keypair(link_token: str) -> str:
    """Generate an RSA-2048 ephemeral keypair for a link session.

    Returns the PEM-encoded public key (PKCS#1 SubjectPublicKeyInfo / SPKI)
    as a string for the client.

    The private key is held in memory and associated with the link_token.
    """
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")

    with _lock:
        _key_store[link_token] = (private_key, time.monotonic())

    logger.debug("Ephemeral keypair generated", extra={"extra_data": {"link_token": link_token}})
    return public_pem


def get_public_key(link_token: str) -> Optional[str]:
    """Retrieve the PEM public key for a link session, if it exists."""
    with _lock:
        entry = _key_store.get(link_token)
    if entry is None:
        return None
    private_key, _ = entry
    return private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")


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
    with _lock:
        entry = _key_store.get(link_token)

    if entry is None:
        raise ValueError(f"No ephemeral key found for link_token (expired or invalid)")

    private_key, _ = entry
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
    with _lock:
        _key_store.pop(link_token, None)
    logger.debug("Ephemeral key destroyed", extra={"extra_data": {"link_token": link_token}})


def cleanup_expired_keys() -> int:
    """Remove all keys older than TTL. Returns the number of keys purged."""
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
    with _lock:
        _key_store.clear()
