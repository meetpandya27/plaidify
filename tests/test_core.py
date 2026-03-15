"""
Tests for encryption utilities and configuration validation.
"""

import os
import pytest
from src.database import encrypt_credential, decrypt_credential
from src.exceptions import (
    PlaidifyError,
    BlueprintNotFoundError,
    AuthenticationError,
    ConnectionFailedError,
    MFARequiredError,
    InvalidTokenError,
)


class TestEncryption:
    """Tests for AES-256-GCM credential encryption/decryption."""

    def test_encrypt_decrypt_roundtrip(self):
        plaintext = "my-secret-password-123!"
        encrypted = encrypt_credential(plaintext)
        assert encrypted != plaintext  # Should be different
        decrypted = decrypt_credential(encrypted)
        assert decrypted == plaintext

    def test_encrypt_produces_different_ciphertexts(self):
        """GCM uses a random nonce, so same plaintext → different ciphertext."""
        plaintext = "same-password"
        encrypted1 = encrypt_credential(plaintext)
        encrypted2 = encrypt_credential(plaintext)
        assert encrypted1 != encrypted2  # Different due to random nonce

    def test_encrypt_empty_string(self):
        encrypted = encrypt_credential("")
        decrypted = decrypt_credential(encrypted)
        assert decrypted == ""

    def test_encrypt_unicode(self):
        plaintext = "пароль-密码-パスワード"
        encrypted = encrypt_credential(plaintext)
        decrypted = decrypt_credential(encrypted)
        assert decrypted == plaintext

    def test_ciphertext_is_base64url(self):
        """Ciphertext should be valid base64url (nonce + ct + tag)."""
        import base64
        encrypted = encrypt_credential("test")
        raw = base64.urlsafe_b64decode(encrypted)
        # 12-byte nonce + at least 1 byte plaintext + 16-byte GCM tag
        assert len(raw) >= 12 + 1 + 16


class TestExceptions:
    """Tests for the custom exception hierarchy."""

    def test_base_exception(self):
        err = PlaidifyError("test error", status_code=400)
        assert str(err) == "test error"
        assert err.status_code == 400
        assert err.message == "test error"

    def test_blueprint_not_found(self):
        err = BlueprintNotFoundError(site="mybank")
        assert "mybank" in err.message
        assert err.status_code == 404
        assert err.site == "mybank"

    def test_authentication_error(self):
        err = AuthenticationError(site="example.com")
        assert "example.com" in err.message
        assert err.status_code == 401

    def test_connection_failed(self):
        err = ConnectionFailedError(site="example.com", detail="timeout")
        assert "timeout" in err.message
        assert err.status_code == 502

    def test_mfa_required(self):
        err = MFARequiredError(site="bank.com", mfa_type="otp", session_id="sess123")
        assert err.mfa_type == "otp"
        assert err.session_id == "sess123"
        assert err.status_code == 403

    def test_invalid_token(self):
        err = InvalidTokenError()
        assert err.status_code == 401

    def test_inheritance(self):
        """All custom exceptions should be catchable as PlaidifyError."""
        errors = [
            BlueprintNotFoundError(site="x"),
            AuthenticationError(site="x"),
            ConnectionFailedError(site="x"),
            MFARequiredError(site="x"),
            InvalidTokenError(),
        ]
        for err in errors:
            assert isinstance(err, PlaidifyError)
            assert isinstance(err, Exception)


class TestConfig:
    """Tests for configuration loading."""

    def test_settings_load(self):
        from src.config import get_settings
        s = get_settings()
        assert s.app_name == "Plaidify"
        assert s.encryption_key  # Should be non-empty
        assert s.jwt_secret_key  # Should be non-empty
        assert s.jwt_algorithm == "HS256"

    def test_settings_log_level(self):
        from src.config import get_settings
        s = get_settings()
        assert s.log_level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
