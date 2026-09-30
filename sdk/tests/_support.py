"""Shared helpers for the SDK tests."""

import base64

import httpx
import respx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

# One throwaway RSA key stands in for the server's /encryption/session key.
_SESSION_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
SESSION_PUBLIC_PEM = (
    _SESSION_KEY.public_key()
    .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    .decode("ascii")
)


def mock_encryption_session(base: str, link_token: str = "enc-lt"):
    """Mock POST /encryption/session (inside an active respx mock)."""
    return respx.post(f"{base}/encryption/session").mock(
        return_value=httpx.Response(200, json={"link_token": link_token, "public_key": SESSION_PUBLIC_PEM})
    )


def decrypt_credential(ciphertext_b64: str) -> str:
    """What the server's session key would recover from an encrypted field."""
    return _SESSION_KEY.decrypt(
        base64.b64decode(ciphertext_b64),
        padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    ).decode("utf-8")
