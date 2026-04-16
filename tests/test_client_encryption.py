"""
Tests for client-side credential encryption (Issue #12).

Covers:
- Ephemeral keypair generation and retrieval
- RSA-OAEP encrypt/decrypt round-trip
- /connect with encrypted credentials
- /submit_credentials with encrypted credentials
- /encryption/session endpoint
- Key expiry and destruction after use
"""

import base64
import pytest
from unittest.mock import patch, AsyncMock
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives import hashes, serialization


# Mock connect_to_site to avoid Playwright browser launch
_mock_connect_response = {
    "status": "connected",
    "data": {"account": "12345"},
}


def _encrypt_with_pem(pem_public_key: str, plaintext: str) -> str:
    """Helper: encrypt plaintext with RSA-OAEP using a PEM public key."""
    public_key = serialization.load_pem_public_key(pem_public_key.encode("ascii"))
    ciphertext = public_key.encrypt(
        plaintext.encode("utf-8"),
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return base64.b64encode(ciphertext).decode("ascii")


class TestCryptoModule:
    """Unit tests for src/crypto.py."""

    def test_generate_and_retrieve_keypair(self):
        from src.crypto import generate_keypair, get_public_key, _clear_all_keys

        _clear_all_keys()
        pub_key = generate_keypair("test-link-1")
        assert "BEGIN PUBLIC KEY" in pub_key
        assert "END PUBLIC KEY" in pub_key

        retrieved = get_public_key("test-link-1")
        assert retrieved == pub_key
        _clear_all_keys()

    def test_encrypt_decrypt_roundtrip(self):
        from src.crypto import generate_keypair, decrypt_with_session_key, _clear_all_keys

        _clear_all_keys()
        pub_pem = generate_keypair("test-roundtrip")
        ciphertext = _encrypt_with_pem(pub_pem, "hello-world")
        raw_ct = base64.b64decode(ciphertext)
        result = decrypt_with_session_key("test-roundtrip", raw_ct)
        assert result == "hello-world"
        _clear_all_keys()

    def test_decrypt_invalid_link_token(self):
        from src.crypto import decrypt_with_session_key, _clear_all_keys

        _clear_all_keys()
        with pytest.raises(ValueError, match="No ephemeral key"):
            decrypt_with_session_key("nonexistent", b"garbage")

    def test_destroy_session_key(self):
        from src.crypto import generate_keypair, destroy_session_key, get_public_key, _clear_all_keys

        _clear_all_keys()
        generate_keypair("destroy-test")
        assert get_public_key("destroy-test") is not None
        destroy_session_key("destroy-test")
        assert get_public_key("destroy-test") is None
        _clear_all_keys()

    def test_cleanup_expired_keys(self):
        import time as _time
        from src.crypto import _key_store, _lock, cleanup_expired_keys, _clear_all_keys
        from cryptography.hazmat.primitives.asymmetric import rsa

        _clear_all_keys()
        # Insert a key with an old timestamp
        pk = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        with _lock:
            _key_store["old-key"] = (pk, _time.monotonic() - 99999)

        purged = cleanup_expired_keys()
        assert purged == 1
        assert "old-key" not in _key_store
        _clear_all_keys()


class TestEncryptionEndpoints:
    """Tests for /encryption/session and /encryption/public_key."""

    def test_create_encryption_session(self, client):
        response = client.post("/encryption/session")
        assert response.status_code == 200
        data = response.json()
        assert "link_token" in data
        assert "public_key" in data
        assert "BEGIN PUBLIC KEY" in data["public_key"]

    def test_get_public_key_for_session(self, client):
        # Create session first
        session = client.post("/encryption/session").json()
        link_token = session["link_token"]

        response = client.get(f"/encryption/public_key/{link_token}")
        assert response.status_code == 200
        data = response.json()
        assert data["public_key"] == session["public_key"]

    def test_get_public_key_invalid_token(self, client):
        response = client.get("/encryption/public_key/nonexistent-token")
        assert response.status_code == 404


class TestConnectWithEncryption:
    """Tests for POST /connect with encrypted credentials."""

    @patch("src.routers.connection.connect_to_site", new_callable=AsyncMock, return_value=_mock_connect_response)
    def test_connect_with_encrypted_credentials(self, mock_connect, client):
        from src.crypto import _clear_all_keys
        _clear_all_keys()

        # Get encryption session
        session = client.post("/encryption/session").json()
        pub_key = session["public_key"]
        link_token = session["link_token"]

        # Encrypt credentials
        enc_user = _encrypt_with_pem(pub_key, "demo_user")
        enc_pass = _encrypt_with_pem(pub_key, "demo_pass123")

        # Connect with encrypted credentials
        response = client.post("/connect", json={
            "site": "test_bank",
            "encrypted_username": enc_user,
            "encrypted_password": enc_pass,
            "link_token": link_token,
        })
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "connected"
        # Verify the mock was called with decrypted credentials
        mock_connect.assert_called_once()
        call_kwargs = mock_connect.call_args
        assert call_kwargs.kwargs["username"] == "demo_user"
        assert call_kwargs.kwargs["password"] == "demo_pass123"

    @patch("src.routers.connection.connect_to_site", new_callable=AsyncMock, return_value=_mock_connect_response)
    def test_connect_encrypted_then_key_destroyed(self, mock_connect, client):
        """After use, the ephemeral key should be destroyed."""
        from src.crypto import get_public_key, _clear_all_keys
        _clear_all_keys()

        session = client.post("/encryption/session").json()
        link_token = session["link_token"]
        pub_key = session["public_key"]

        enc_user = _encrypt_with_pem(pub_key, "demo_user")
        enc_pass = _encrypt_with_pem(pub_key, "demo_pass123")

        # Use the key
        client.post("/connect", json={
            "site": "test_bank",
            "encrypted_username": enc_user,
            "encrypted_password": enc_pass,
            "link_token": link_token,
        })

        # Key should be destroyed after use
        assert get_public_key(link_token) is None

    @patch("src.routers.connection.connect_to_site", new_callable=AsyncMock, return_value=_mock_connect_response)
    def test_connect_plaintext_still_works(self, mock_connect, client):
        """Plaintext credentials should still work for backward compatibility."""
        response = client.post("/connect", json={
            "site": "test_bank",
            "username": "demo_user",
            "password": "demo_pass123",
        })
        assert response.status_code == 200
        mock_connect.assert_called_once()

    def test_connect_missing_credentials(self, client):
        """Should return 422 if neither plaintext nor encrypted creds provided."""
        response = client.post("/connect", json={
            "site": "test_bank",
        })
        assert response.status_code == 422


class TestCreateLinkReturnsPublicKey:
    """Tests for POST /create_link returning a public key."""

    def test_create_link_has_public_key(self, client, auth_headers):
        response = client.post("/create_link?site=test_bank", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert "link_token" in data
        assert "public_key" in data
        assert "BEGIN PUBLIC KEY" in data["public_key"]

    def test_submit_credentials_encrypted(self, client, auth_headers):
        """Submit encrypted credentials via the multi-step flow."""
        from src.crypto import _clear_all_keys
        _clear_all_keys()

        # Step 1: create link (get public key)
        link_resp = client.post("/create_link?site=test_bank", headers=auth_headers)
        link_data = link_resp.json()
        link_token = link_data["link_token"]
        pub_key = link_data["public_key"]

        # Step 2: encrypt and submit credentials
        enc_user = _encrypt_with_pem(pub_key, "myuser")
        enc_pass = _encrypt_with_pem(pub_key, "mypass123")

        response = client.post(
            "/submit_credentials",
            params={
                "link_token": link_token,
                "encrypted_username": enc_user,
                "encrypted_password": enc_pass,
            },
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data
