"""
Tests for envelope encryption — per-user DEKs (Issue #13).

Covers:
- DEK generation, wrapping, unwrapping
- Per-user credential encrypt/decrypt
- Fallback to master key for legacy data
- User isolation (one user's DEK can't decrypt another's data)
- Master key rotation
- Lazy DEK migration on login
- Registration creates DEK
"""

import base64
import os

import pytest


class TestDEKManagement:
    """Unit tests for DEK generation, wrapping, and unwrapping."""

    def test_generate_dek_is_32_bytes(self):
        from src.database import generate_dek

        dek = generate_dek()
        assert len(dek) == 32
        assert isinstance(dek, bytes)

    def test_generate_dek_unique(self):
        from src.database import generate_dek

        dek1 = generate_dek()
        dek2 = generate_dek()
        assert dek1 != dek2

    def test_wrap_unwrap_roundtrip(self):
        from src.database import generate_dek, unwrap_dek, wrap_dek

        dek = generate_dek()
        wrapped = wrap_dek(dek)
        assert isinstance(wrapped, str)
        unwrapped = unwrap_dek(wrapped)
        assert unwrapped == dek

    def test_create_user_dek(self):
        from src.database import create_user_dek, unwrap_dek

        wrapped = create_user_dek()
        assert isinstance(wrapped, str)
        dek = unwrap_dek(wrapped)
        assert len(dek) == 32

    def test_wrapped_dek_is_base64(self):
        from src.database import create_user_dek

        wrapped = create_user_dek()
        # Should be valid base64url
        raw = base64.urlsafe_b64decode(wrapped)
        assert len(raw) > 12  # At least nonce + some ciphertext


class TestPerUserEncryption:
    """Tests for encrypting/decrypting with per-user DEK."""

    def _make_user_with_dek(self):
        from src.database import User, create_user_dek

        user = User(
            id=999,
            username="testuser",
            email="test@example.com",
            encrypted_dek=create_user_dek(),
        )
        return user

    def _make_user_without_dek(self):
        from src.database import User

        user = User(
            id=998,
            username="legacyuser",
            email="legacy@example.com",
            encrypted_dek=None,
        )
        return user

    def test_encrypt_decrypt_with_dek(self):
        from src.database import decrypt_credential_for_user, encrypt_credential_for_user

        user = self._make_user_with_dek()
        plaintext = "my-secret-password"
        ciphertext = encrypt_credential_for_user(user, plaintext)
        decrypted = decrypt_credential_for_user(user, ciphertext)
        assert decrypted == plaintext

    def test_each_encryption_is_unique(self):
        from src.database import encrypt_credential_for_user

        user = self._make_user_with_dek()
        ct1 = encrypt_credential_for_user(user, "same-text")
        ct2 = encrypt_credential_for_user(user, "same-text")
        assert ct1 != ct2  # Different nonces

    def test_encrypt_unicode(self):
        from src.database import decrypt_credential_for_user, encrypt_credential_for_user

        user = self._make_user_with_dek()
        plaintext = "пароль-密码-🔑"
        ciphertext = encrypt_credential_for_user(user, plaintext)
        assert decrypt_credential_for_user(user, ciphertext) == plaintext

    def test_user_isolation(self):
        """One user's DEK cannot decrypt another user's data."""
        from src.database import decrypt_credential_for_user, encrypt_credential_for_user

        user_a = self._make_user_with_dek()
        user_b = self._make_user_with_dek()

        ciphertext = encrypt_credential_for_user(user_a, "secret-for-a")

        # User B should NOT be able to decrypt User A's data
        with pytest.raises(Exception):
            decrypt_credential_for_user(user_b, ciphertext)

    def test_fallback_to_master_key_for_legacy_user(self):
        """Users without a DEK should use master key encryption."""
        from src.database import decrypt_credential_for_user, encrypt_credential

        user = self._make_user_without_dek()
        # Encrypt with legacy master key
        ciphertext = encrypt_credential("legacy-secret")
        # Should decrypt via fallback
        decrypted = decrypt_credential_for_user(user, ciphertext)
        assert decrypted == "legacy-secret"

    def test_fallback_encrypt_uses_master_key(self):
        """encrypt_credential_for_user falls back to master key if no DEK."""
        from src.database import decrypt_credential, encrypt_credential_for_user

        user = self._make_user_without_dek()
        ciphertext = encrypt_credential_for_user(user, "fallback-test")
        # Should be decryptable with master key
        decrypted = decrypt_credential(ciphertext)
        assert decrypted == "fallback-test"


class TestMasterKeyRotation:
    """Tests for master key rotation."""

    def test_rotate_master_key(self):
        from src.config import get_settings
        from src.database import (
            User,
            create_user_dek,
            encrypt_credential_for_user,
            rotate_master_key,
        )
        from tests.conftest import TestSessionLocal

        db = TestSessionLocal()
        try:
            # Create a user with a DEK and encrypt some data
            user = User(
                username="rotateuser",
                email="rotate@example.com",
                hashed_password="ignored",
                encrypted_dek=create_user_dek(),
            )
            db.add(user)
            db.commit()
            db.refresh(user)

            ciphertext = encrypt_credential_for_user(user, "rotate-secret")

            # Generate a new master key
            new_key = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
            old_key = get_settings().encryption_key

            # Rotate
            count = rotate_master_key(old_key, new_key, db)
            assert count == 1
            db.refresh(user)

            # Now verify: unwrap with new key should work
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM

            new_key_bytes = base64.urlsafe_b64decode(new_key)
            new_aesgcm = AESGCM(new_key_bytes)
            raw = base64.urlsafe_b64decode(user.encrypted_dek)
            dek = new_aesgcm.decrypt(raw[:12], raw[12:], None)
            assert len(dek) == 32

            # The re-wrapped DEK should still decrypt the data
            aesgcm = AESGCM(dek)
            ct_raw = base64.urlsafe_b64decode(ciphertext)
            plaintext = aesgcm.decrypt(ct_raw[:12], ct_raw[12:], None).decode("utf-8")
            assert plaintext == "rotate-secret"
        finally:
            db.close()


class TestRegistrationCreatesDEK:
    """Verify that user registration creates a DEK."""

    def test_register_creates_dek(self, client):
        from src.database import User
        from tests.conftest import TestSessionLocal

        response = client.post(
            "/auth/register",
            json={
                "username": "dekuser",
                "email": "dek@example.com",
                "password": "Strong@pass123",
            },
        )
        assert response.status_code == 200

        db = TestSessionLocal()
        try:
            user = db.query(User).filter_by(username="dekuser").first()
            assert user is not None
            assert user.encrypted_dek is not None
            assert len(user.encrypted_dek) > 20
        finally:
            db.close()

    def test_oauth2_disabled_returns_403(self, client):
        """OAuth2 social login is disabled by default (OAUTH_ENABLED=false)."""
        response = client.post(
            "/auth/oauth2",
            json={
                "provider": "google",
                "oauth_token": "fake-google-token-dek-test",
            },
        )
        assert response.status_code == 403


class TestLazyDEKMigration:
    """Verify lazy DEK migration on login."""

    def test_login_creates_dek_for_legacy_user(self, client):
        from src.database import User
        from src.dependencies import get_password_hash
        from tests.conftest import TestSessionLocal

        # Manually create a user without DEK (simulates pre-migration user)
        db = TestSessionLocal()
        try:
            user = User(
                username="legacylogin",
                email="legacy@login.com",
                hashed_password=get_password_hash("pass12345678"),
                encrypted_dek=None,
            )
            db.add(user)
            db.commit()
            assert user.encrypted_dek is None
        finally:
            db.close()

        # Login should trigger lazy DEK generation
        response = client.post(
            "/auth/token",
            data={
                "username": "legacylogin",
                "password": "pass12345678",
            },
        )
        assert response.status_code == 200

        db = TestSessionLocal()
        try:
            user = db.query(User).filter_by(username="legacylogin").first()
            assert user.encrypted_dek is not None
        finally:
            db.close()


class TestSubmitCredentialsWithDEK:
    """Verify submit_credentials uses per-user DEK."""

    def test_submit_and_fetch_with_dek(self, client, auth_headers):
        """Full flow: create_link → submit_credentials → verify stored encrypted."""
        from src.database import AccessToken, User, decrypt_credential_for_user
        from tests.conftest import TestSessionLocal

        # Create link
        link_resp = client.post("/create_link?site=internal_bank", headers=auth_headers)
        assert link_resp.status_code == 200
        link_token = link_resp.json()["link_token"]

        # Submit credentials
        response = client.post(
            "/submit_credentials",
            json={
                "link_token": link_token,
                "username": "myuser",
                "password": "mypass",
            },
            headers=auth_headers,
        )
        assert response.status_code == 200
        access_token = response.json()["access_token"]

        # Verify stored credentials are encrypted with user's DEK
        db = TestSessionLocal()
        try:
            token_record = db.query(AccessToken).filter_by(token=access_token).first()
            user = db.query(User).filter_by(id=token_record.user_id).first()
            assert user.encrypted_dek is not None

            decrypted_user = decrypt_credential_for_user(user, token_record.username_encrypted)
            decrypted_pass = decrypt_credential_for_user(user, token_record.password_encrypted)
            assert decrypted_user == "myuser"
            assert decrypted_pass == "mypass"
        finally:
            db.close()


class TestEncryptedJsonDocuments:
    """ENG-15: per-user encryption of stored JSON (access-job results)."""

    def _user(self, db, username="docowner", with_dek=True):
        from src.database import User, create_user_dek

        user = User(
            username=username,
            email=f"{username}@example.com",
            hashed_password="x",
            encrypted_dek=create_user_dek() if with_dek else None,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user

    def test_roundtrip_and_nothing_readable_at_rest(self):
        from src.database import decrypt_json_for_user, encrypt_json_for_user, is_encrypted_json
        from tests.conftest import TestSessionLocal

        db = TestSessionLocal()
        try:
            user = self._user(db)
            document = {"data": {"account_number": "000123456789", "balance": 1234.56}, "status": "ok"}
            stored = encrypt_json_for_user(db, user, document, context="access_job:j1:result")
            assert is_encrypted_json(stored)
            assert "000123456789" not in stored and "balance" not in stored
            assert decrypt_json_for_user(user, stored, context="access_job:j1:result") == document
        finally:
            db.close()

    def test_ciphertext_is_bound_to_its_context(self):
        from src.database import CredentialDecryptionError, decrypt_json_for_user, encrypt_json_for_user
        from tests.conftest import TestSessionLocal

        db = TestSessionLocal()
        try:
            user = self._user(db)
            stored = encrypt_json_for_user(db, user, {"a": 1}, context="access_job:j1:result")
            with pytest.raises(CredentialDecryptionError):
                decrypt_json_for_user(user, stored, context="access_job:j2:result")
        finally:
            db.close()

    def test_other_users_key_cannot_decrypt(self):
        from src.database import CredentialDecryptionError, decrypt_json_for_user, encrypt_json_for_user
        from tests.conftest import TestSessionLocal

        db = TestSessionLocal()
        try:
            alice, bob = self._user(db, "alice_doc"), self._user(db, "bob_doc")
            stored = encrypt_json_for_user(db, alice, {"secret": True}, context="c")
            with pytest.raises(CredentialDecryptionError):
                decrypt_json_for_user(bob, stored, context="c")
        finally:
            db.close()

    def test_legacy_plaintext_rows_and_none_still_read(self):
        from src.database import decrypt_json_for_user
        from tests.conftest import TestSessionLocal

        db = TestSessionLocal()
        try:
            user = self._user(db)
            assert decrypt_json_for_user(user, '{"status": "completed"}', context="c") == {"status": "completed"}
            assert decrypt_json_for_user(user, None, context="c") is None
        finally:
            db.close()

    def test_user_without_a_dek_gets_one_in_the_callers_transaction(self):
        from src.database import User, decrypt_json_for_user, encrypt_json_for_user
        from tests.conftest import TestSessionLocal

        db = TestSessionLocal()
        try:
            user = self._user(db, "nodek_doc", with_dek=False)
            stored = encrypt_json_for_user(db, user, {"x": 1}, context="c")
            assert user.encrypted_dek is not None
            db.commit()

            other = TestSessionLocal()
            try:
                fresh = other.get(User, user.id)
                assert fresh.encrypted_dek == user.encrypted_dek
                assert decrypt_json_for_user(fresh, stored, context="c") == {"x": 1}
            finally:
                other.close()
        finally:
            db.close()

    def test_a_concurrently_created_dek_is_reused_not_overwritten(self):
        """Two writers racing to give a user a DEK must end up on the same one."""
        from src.database import User, create_user_dek, decrypt_json_for_user, encrypt_json_for_user
        from tests.conftest import TestSessionLocal

        db = TestSessionLocal()
        try:
            user = self._user(db, "race_doc", with_dek=False)
            winner = TestSessionLocal()
            try:
                row = winner.get(User, user.id)
                row.encrypted_dek = create_user_dek()
                winner.commit()
                winning_dek = row.encrypted_dek
            finally:
                winner.close()

            stored = encrypt_json_for_user(db, user, {"y": 2}, context="c")  # still sees no DEK in memory
            db.commit()
            assert user.encrypted_dek == winning_dek
            assert decrypt_json_for_user(user, stored, context="c") == {"y": 2}
        finally:
            db.close()

    def test_context_is_required(self):
        from src.database import encrypt_json_for_user
        from tests.conftest import TestSessionLocal

        db = TestSessionLocal()
        try:
            with pytest.raises(ValueError):
                encrypt_json_for_user(db, self._user(db), {}, context="")
        finally:
            db.close()


class TestJobResultRetention:
    """ENG-15: stored access-job results are erased after RESULT_RETENTION_DAYS."""

    def test_old_results_are_erased_and_rows_kept(self):
        from datetime import datetime, timedelta, timezone

        from src.database import AccessJob, purge_expired_job_results
        from tests.conftest import TestSessionLocal

        now = datetime.now(timezone.utc)
        db = TestSessionLocal()
        try:
            db.add_all(
                [
                    AccessJob(
                        id="old",
                        site="s",
                        job_type="t",
                        lock_scope="x",
                        result_json="enc:v1:aaa",
                        created_at=now - timedelta(days=40),
                        completed_at=now - timedelta(days=31),
                    ),
                    AccessJob(
                        id="recent",
                        site="s",
                        job_type="t",
                        lock_scope="x",
                        result_json="enc:v1:bbb",
                        created_at=now - timedelta(days=40),
                        completed_at=now - timedelta(days=29),
                    ),
                    AccessJob(
                        id="never-finished",
                        site="s",
                        job_type="t",
                        lock_scope="x",
                        result_json="{}",
                        created_at=now - timedelta(days=45),
                    ),
                ]
            )
            db.commit()

            assert purge_expired_job_results(db, retention_days=30, now=now) == 2
            db.expire_all()
            assert db.get(AccessJob, "old").result_json is None
            assert db.get(AccessJob, "never-finished").result_json is None
            assert db.get(AccessJob, "recent").result_json == "enc:v1:bbb"
            assert db.get(AccessJob, "old").status == "pending"  # the row itself stays
            assert purge_expired_job_results(db, retention_days=30, now=now) == 0
        finally:
            db.close()

    def test_default_retention_comes_from_settings(self):
        from src.config import get_settings

        assert get_settings().result_retention_days == 30
