"""
Tests for Issue #14: Encryption key rotation with versioning.

Covers:
- key_version column stamped on new AccessTokens
- get_current_key_version reads from settings
- unwrap_dek fallback to previous master key
- re_encrypt_tokens background job
- rotate_master_key + re_encrypt_tokens full flow
- CLI rotate-key command
"""

import base64
import os
from unittest.mock import patch

import pytest

import src.database as _db_mod
from src.database import (
    AccessToken,
    Link,
    User,
    create_user_dek,
    decrypt_credential_for_user,
    encrypt_credential_for_user,
    generate_dek,
    get_current_key_version,
    re_encrypt_tokens,
    rotate_master_key,
    unwrap_dek,
    wrap_dek,
)
from tests.conftest import TestSessionLocal

# The settings instance used inside src.database (must patch this one, not a local copy)
db_settings = _db_mod.settings


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_user(db, username="keyrotuser") -> User:
    """Create a user with a DEK for testing."""
    from passlib.context import CryptContext

    pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
    user = User(
        username=username,
        email=f"{username}@example.com",
        hashed_password=pwd_context.hash("password123"),
        encrypted_dek=create_user_dek(),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _make_link(db, user, site="test_bank") -> Link:
    """Create a link for a user."""
    import uuid

    link = Link(link_token=str(uuid.uuid4()), site=site, user_id=user.id)
    db.add(link)
    db.commit()
    return link


def _make_access_token(db, user, link, plain_user="myuser", plain_pass="mypass", key_version=1) -> AccessToken:
    """Create an access token with encrypted credentials."""
    import uuid

    token = AccessToken(
        token=str(uuid.uuid4()),
        link_token=link.link_token,
        username_encrypted=encrypt_credential_for_user(user, plain_user),
        password_encrypted=encrypt_credential_for_user(user, plain_pass),
        user_id=user.id,
        key_version=key_version,
    )
    db.add(token)
    db.commit()
    db.refresh(token)
    return token


# ── Tests: get_current_key_version ────────────────────────────────────────────


class TestGetCurrentKeyVersion:
    """Test the get_current_key_version function."""

    def test_returns_settings_value(self):
        """get_current_key_version returns the configured version."""
        version = get_current_key_version()
        assert version == db_settings.encryption_key_version

    def test_returns_integer(self):
        """get_current_key_version returns an integer."""
        assert isinstance(get_current_key_version(), int)


# ── Tests: key_version stamped on AccessToken ─────────────────────────────────


class TestKeyVersionStamped:
    """Test that key_version is set when creating AccessTokens via the API."""

    def test_submit_credentials_stamps_key_version(self, client, auth_headers):
        """submit_credentials sets key_version on new AccessTokens."""
        # Create link
        resp = client.post("/create_link", params={"site": "test_bank"}, headers=auth_headers)
        assert resp.status_code == 200
        link_token = resp.json()["link_token"]

        # Submit credentials
        resp = client.post(
            "/submit_credentials",
            params={
                "link_token": link_token,
                "username": "siteuser",
                "password": "sitepass",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        access_token = resp.json()["access_token"]

        # Check key_version in DB
        db = TestSessionLocal()
        try:
            token = db.query(AccessToken).filter_by(token=access_token).first()
            assert token is not None
            assert token.key_version == get_current_key_version()
        finally:
            db.close()

    def test_key_version_defaults_to_1(self):
        """AccessToken.key_version defaults to 1."""
        db = TestSessionLocal()
        try:
            user = _make_user(db)
            link = _make_link(db, user)
            token = _make_access_token(db, user, link)
            assert token.key_version == 1
        finally:
            db.close()


# ── Tests: unwrap_dek with previous key fallback ─────────────────────────────


class TestUnwrapDekFallback:
    """Test unwrap_dek tries previous key when current key fails."""

    def test_unwrap_with_current_key(self):
        """unwrap_dek works with the current master key."""
        dek = generate_dek()
        wrapped = wrap_dek(dek)
        assert unwrap_dek(wrapped) == dek

    def test_unwrap_falls_back_to_previous_key(self):
        """unwrap_dek uses ENCRYPTION_KEY_PREVIOUS when current key fails."""
        # Generate a "previous" master key and wrap a DEK with it
        old_master = base64.urlsafe_b64encode(os.urandom(32)).decode()
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        old_bytes = base64.urlsafe_b64decode(old_master)
        dek = generate_dek()
        nonce = os.urandom(12)
        ct = AESGCM(old_bytes).encrypt(nonce, dek, None)
        wrapped_with_old = base64.urlsafe_b64encode(nonce + ct).decode()

        # Without previous key set, should raise
        with patch.object(db_settings, "encryption_key_previous", None), pytest.raises(Exception):
            unwrap_dek(wrapped_with_old)

        # With previous key set, should succeed
        with patch.object(db_settings, "encryption_key_previous", old_master):
            result = unwrap_dek(wrapped_with_old)
            assert result == dek

    def test_unwrap_raises_without_previous_key(self):
        """unwrap_dek raises an error for unknown keys with no fallback."""
        bogus_master = base64.urlsafe_b64encode(os.urandom(32)).decode()
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        bogus_bytes = base64.urlsafe_b64decode(bogus_master)
        dek = generate_dek()
        nonce = os.urandom(12)
        ct = AESGCM(bogus_bytes).encrypt(nonce, dek, None)
        wrapped = base64.urlsafe_b64encode(nonce + ct).decode()

        with patch.object(db_settings, "encryption_key_previous", None), pytest.raises(Exception):
            unwrap_dek(wrapped)


# ── Tests: re_encrypt_tokens ─────────────────────────────────────────────────


class TestReEncryptTokens:
    """Test the re_encrypt_tokens background job."""

    def test_re_encrypts_old_version_tokens(self):
        """Tokens with key_version < current are re-encrypted."""
        db = TestSessionLocal()
        try:
            user = _make_user(db)
            link = _make_link(db, user)
            token = _make_access_token(db, user, link, "alice", "secret", key_version=1)
            old_username_enc = token.username_encrypted
            old_password_enc = token.password_encrypted

            # Pretend we're on version 2
            with patch.object(db_settings, "encryption_key_version", 2):
                count = re_encrypt_tokens(db)
                assert count == 1

            db.refresh(token)
            assert token.key_version == 2
            # Ciphertext should have changed (new random nonce)
            assert token.username_encrypted != old_username_enc
            assert token.password_encrypted != old_password_enc
            # But plaintext should be the same
            assert decrypt_credential_for_user(user, token.username_encrypted) == "alice"
            assert decrypt_credential_for_user(user, token.password_encrypted) == "secret"
        finally:
            db.close()

    def test_skips_current_version_tokens(self):
        """Tokens already at current key_version are not re-encrypted."""
        db = TestSessionLocal()
        try:
            user = _make_user(db)
            link = _make_link(db, user)
            _make_access_token(db, user, link, "bob", "pass", key_version=1)

            # Current version is also 1 — nothing to do
            with patch.object(db_settings, "encryption_key_version", 1):
                count = re_encrypt_tokens(db)
                assert count == 0
        finally:
            db.close()

    def test_batch_size_limits_processing(self):
        """re_encrypt_tokens respects batch_size."""
        db = TestSessionLocal()
        try:
            user = _make_user(db)
            link = _make_link(db, user)
            for i in range(5):
                _make_access_token(db, user, link, f"user{i}", f"pass{i}", key_version=1)

            with patch.object(db_settings, "encryption_key_version", 2):
                count = re_encrypt_tokens(db, batch_size=2)
                assert count == 2

                # Run again — should get more
                count2 = re_encrypt_tokens(db, batch_size=2)
                assert count2 == 2

                # Run again — should get the last one
                count3 = re_encrypt_tokens(db, batch_size=2)
                assert count3 == 1
        finally:
            db.close()

    def test_skips_tokens_without_user_dek(self):
        """Tokens whose user has no DEK are skipped gracefully."""
        db = TestSessionLocal()
        try:
            from passlib.context import CryptContext

            pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
            # Create a user WITHOUT a DEK
            user = User(
                username="nodekuser",
                email="nodek@example.com",
                hashed_password=pwd_context.hash("password123"),
                encrypted_dek=None,
            )
            db.add(user)
            db.commit()
            db.refresh(user)

            link = _make_link(db, user, site="demo_site")

            # Create token with master-key encryption (no DEK)
            import uuid

            from src.database import encrypt_credential

            token = AccessToken(
                token=str(uuid.uuid4()),
                link_token=link.link_token,
                username_encrypted=encrypt_credential("nouser"),
                password_encrypted=encrypt_credential("nopass"),
                user_id=user.id,
                key_version=1,
            )
            db.add(token)
            db.commit()

            with patch.object(db_settings, "encryption_key_version", 2):
                count = re_encrypt_tokens(db)
                assert count == 0  # Skipped because no DEK
        finally:
            db.close()


# ── Tests: Full rotation flow ─────────────────────────────────────────────────


class TestFullRotationFlow:
    """Test the end-to-end key rotation flow."""

    def test_rotate_master_key_then_re_encrypt(self):
        """Full flow: encrypt → rotate master key → re-encrypt tokens."""
        db = TestSessionLocal()
        try:
            user = _make_user(db, "rotuser")
            link = _make_link(db, user)
            token = _make_access_token(db, user, link, "rotuser_site", "rotsecret", key_version=1)

            # Generate new master key
            new_key = base64.urlsafe_b64encode(os.urandom(32)).decode()
            old_key = db_settings.encryption_key

            # Rotate DEK wrappers
            dek_count = rotate_master_key(old_key, new_key, db)
            assert dek_count == 1

            # Now unwrap_dek needs the new key — patch settings
            with (
                patch.object(db_settings, "encryption_key", new_key),
                patch.object(db_settings, "encryption_key_previous", old_key),
                patch.object(db_settings, "encryption_key_version", 2),
            ):
                # Verify credentials are still decryptable (DEK is now wrapped with new key)
                db.refresh(user)
                db.refresh(token)
                assert decrypt_credential_for_user(user, token.username_encrypted) == "rotuser_site"

                # Re-encrypt tokens
                count = re_encrypt_tokens(db)
                assert count == 1

                db.refresh(token)
                assert token.key_version == 2
                assert decrypt_credential_for_user(user, token.username_encrypted) == "rotuser_site"
                assert decrypt_credential_for_user(user, token.password_encrypted) == "rotsecret"
        finally:
            db.close()

    def test_multiple_users_rotation(self):
        """Rotation works for multiple users with different DEKs."""
        db = TestSessionLocal()
        try:
            user1 = _make_user(db, "rot_alice")
            user2 = _make_user(db, "rot_bob")
            link1 = _make_link(db, user1)
            link2 = _make_link(db, user2)
            t1 = _make_access_token(db, user1, link1, "alice_site", "alice_secret", key_version=1)
            t2 = _make_access_token(db, user2, link2, "bob_site", "bob_secret", key_version=1)

            new_key = base64.urlsafe_b64encode(os.urandom(32)).decode()
            old_key = db_settings.encryption_key

            dek_count = rotate_master_key(old_key, new_key, db)
            assert dek_count == 2

            with (
                patch.object(db_settings, "encryption_key", new_key),
                patch.object(db_settings, "encryption_key_previous", old_key),
                patch.object(db_settings, "encryption_key_version", 2),
            ):
                count = re_encrypt_tokens(db)
                assert count == 2

                db.refresh(user1)
                db.refresh(user2)
                db.refresh(t1)
                db.refresh(t2)

                assert decrypt_credential_for_user(user1, t1.username_encrypted) == "alice_site"
                assert decrypt_credential_for_user(user2, t2.username_encrypted) == "bob_site"
                assert t1.key_version == 2
                assert t2.key_version == 2
        finally:
            db.close()


# ── Tests: CLI rotate-key command ─────────────────────────────────────────────


class TestCLIRotateKey:
    """Test the CLI rotate-key command."""

    def _import_cli(self):
        """Import CLI module without triggering the full SDK __init__.py."""
        import importlib.util
        import sys

        cli_path = os.path.join(os.path.dirname(__file__), "..", "sdk", "plaidify", "cli.py")
        spec = importlib.util.spec_from_file_location("plaidify_cli", os.path.abspath(cli_path))
        # Temporarily add a fake 'plaidify' to sys.modules so import inside cli.py works
        fake_plaidify = type(sys)("plaidify")
        fake_plaidify.__version__ = "0.3.0a1"
        old = sys.modules.get("plaidify")
        sys.modules["plaidify"] = fake_plaidify
        try:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        finally:
            if old is not None:
                sys.modules["plaidify"] = old
            else:
                sys.modules.pop("plaidify", None)
        return mod.cli

    def test_cli_rotate_key_rewraps_deks(self):
        """CLI rotate-key re-wraps DEKs without error."""
        from click.testing import CliRunner

        cli = self._import_cli()

        db = TestSessionLocal()
        try:
            user = _make_user(db, "cliuser")
            link = _make_link(db, user)
            _make_access_token(db, user, link, "clipass", "clisecret", key_version=1)
        finally:
            db.close()

        new_key = base64.urlsafe_b64encode(os.urandom(32)).decode()
        old_key = db_settings.encryption_key

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "rotate-key",
                "--old-key",
                old_key,
                "--new-key",
                new_key,
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Re-wrapped" in result.output
        assert "Key rotation complete" in result.output

    def test_cli_rotate_key_with_re_encrypt(self):
        """CLI rotate-key with --re-encrypt also re-encrypts tokens."""
        from click.testing import CliRunner

        cli = self._import_cli()

        db = TestSessionLocal()
        try:
            user = _make_user(db, "cliuser2")
            link = _make_link(db, user)
            _make_access_token(db, user, link, "clipass2", "clisecret2", key_version=1)
        finally:
            db.close()

        new_key = base64.urlsafe_b64encode(os.urandom(32)).decode()
        old_key = db_settings.encryption_key

        runner = CliRunner()

        # Need to patch key_version to 2 so re-encryption finds old tokens
        with (
            patch.object(db_settings, "encryption_key", new_key),
            patch.object(db_settings, "encryption_key_previous", old_key),
            patch.object(db_settings, "encryption_key_version", 2),
        ):
            result = runner.invoke(
                cli,
                [
                    "rotate-key",
                    "--old-key",
                    old_key,
                    "--new-key",
                    new_key,
                    "--re-encrypt",
                ],
            )

        assert result.exit_code == 0, result.output
        assert "Re-encrypted" in result.output
