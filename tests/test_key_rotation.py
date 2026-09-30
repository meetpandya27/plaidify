"""
Tests for Issue #14: Encryption key rotation with versioning.

Covers:
- key_version column stamped on new AccessTokens
- get_current_key_version reads from settings
- unwrap_dek / decrypt_credential fallback to the previous master key
- re_encrypt_tokens rotation sweep: DEK re-wrap, legacy credentials, webhook
  secrets, progress past undecryptable rows, no tokens in logs
- rotate_master_key + re_encrypt_tokens full flow
- the SECURITY.md rotation procedure, end to end, including removing the
  previous key
- CLI rotate-key command
"""

import base64
import logging
import os
import uuid
from contextlib import ExitStack
from unittest.mock import patch

import pytest

import src.database as _db_mod
from src import kms
from src.database import (
    AccessJob,
    AccessToken,
    CredentialDecryptionError,
    KeyRotationIncomplete,
    Link,
    User,
    Webhook,
    create_user_dek,
    decrypt_credential,
    decrypt_credential_for_user,
    decrypt_json_for_user,
    decrypt_webhook_secret,
    encrypt_credential,
    encrypt_credential_for_user,
    encrypt_json_for_user,
    encrypt_webhook_secret,
    generate_dek,
    get_current_key_version,
    key_rotation_status,
    re_encrypt_tokens,
    rotate_encryption_keys,
    rotate_master_key,
    unwrap_dek,
    wrap_dek,
)
from tests.conftest import TestSessionLocal

# The settings instance used inside src.database (must patch this one, not a local copy)
db_settings = _db_mod.settings


# ── Helpers ───────────────────────────────────────────────────────────────────


def _new_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


def _make_user(db, username="keyrotuser", with_dek=True) -> User:
    """Create a user (with a DEK unless told otherwise) for testing."""
    from passlib.context import CryptContext

    pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
    user = User(
        username=username,
        email=f"{username}@example.com",
        hashed_password=pwd_context.hash("password123"),
        encrypted_dek=create_user_dek() if with_dek else None,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _make_link(db, user, site="internal_bank") -> Link:
    """Create a link for a user."""
    link = Link(link_token=str(uuid.uuid4()), site=site, user_id=user.id)
    db.add(link)
    db.commit()
    return link


def _make_access_token(db, user, link, plain_user="myuser", plain_pass="mypass", key_version=1, token=None):
    """Create an access token with encrypted credentials."""
    record = AccessToken(
        token=token or str(uuid.uuid4()),
        link_token=link.link_token,
        username_encrypted=encrypt_credential_for_user(user, plain_user),
        password_encrypted=encrypt_credential_for_user(user, plain_pass),
        user_id=user.id,
        key_version=key_version,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def _make_webhook(db, user, stored_secret: str) -> Webhook:
    webhook = Webhook(
        id=str(uuid.uuid4()),
        link_token="lnk",
        url="https://hooks.example.com/x",
        secret=stored_secret,
        user_id=user.id,
    )
    db.add(webhook)
    db.commit()
    db.refresh(webhook)
    return webhook


def _rotation(stack: ExitStack, new_key: str, previous: str | None, version: int) -> None:
    """Apply rotation settings for the rest of ``stack``'s lifetime."""
    stack.enter_context(patch.object(db_settings, "encryption_key", new_key))
    stack.enter_context(patch.object(db_settings, "encryption_key_previous", previous))
    stack.enter_context(patch.object(db_settings, "encryption_key_version", version))
    kms.reset_kms_provider()  # a restarted process builds its provider afresh


def _run_sweep_to_completion(db, batch_size=100) -> int:
    total = 0
    while True:
        count = re_encrypt_tokens(db, batch_size=batch_size)
        total += count
        if count < batch_size:
            return total


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
        resp = client.post("/create_link", params={"site": "internal_bank"}, headers=auth_headers)
        assert resp.status_code == 200
        link_token = resp.json()["link_token"]

        # Submit credentials
        resp = client.post(
            "/submit_credentials",
            json={
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

    def test_new_rows_record_the_key_version_that_protects_them(self):
        db = TestSessionLocal()
        try:
            with patch.object(db_settings, "encryption_key_version", 3):
                user = _make_user(db, "stamped")
                webhook = _make_webhook(db, user, encrypt_webhook_secret(user, "s"))
            assert user.dek_key_version == 3
            assert webhook.key_version == 3
            assert _make_user(db, "nodek", with_dek=False).dek_key_version is None
        finally:
            db.close()


# ── Tests: unwrap_dek / decrypt_credential with previous key fallback ────────


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
        old_master = _new_key()
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
        bogus_master = _new_key()
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        bogus_bytes = base64.urlsafe_b64decode(bogus_master)
        dek = generate_dek()
        nonce = os.urandom(12)
        ct = AESGCM(bogus_bytes).encrypt(nonce, dek, None)
        wrapped = base64.urlsafe_b64encode(nonce + ct).decode()

        with patch.object(db_settings, "encryption_key_previous", None), pytest.raises(CredentialDecryptionError):
            unwrap_dek(wrapped)


class TestDecryptCredentialFallback:
    """SEC-08: master-key ciphertexts stay readable during a rotation."""

    def test_falls_back_to_previous_key(self):
        old_key = db_settings.encryption_key
        ciphertext = encrypt_credential("legacy-secret")
        with patch.object(db_settings, "encryption_key", _new_key()):
            with patch.object(db_settings, "encryption_key_previous", None), pytest.raises(CredentialDecryptionError):
                decrypt_credential(ciphertext)
            with patch.object(db_settings, "encryption_key_previous", old_key):
                assert decrypt_credential(ciphertext) == "legacy-secret"

    def test_garbage_raises_a_clear_error(self):
        for garbage in ("not-base64-!!", base64.urlsafe_b64encode(os.urandom(40)).decode(), ""):
            with pytest.raises(CredentialDecryptionError):
                decrypt_credential(garbage)


class TestWebhookSecretDecryption:
    """SEC-08: a webhook secret that does not decrypt is an error, never the signing key."""

    def test_dek_and_master_key_formats_decrypt(self):
        db = TestSessionLocal()
        try:
            user = _make_user(db, "hookowner")
            under_dek = _make_webhook(db, user, encrypt_webhook_secret(user, "dek-secret"))
            under_master = _make_webhook(db, user, encrypt_credential("master-secret"))
            assert decrypt_webhook_secret(db, under_dek) == "dek-secret"
            assert decrypt_webhook_secret(db, under_master) == "master-secret"
        finally:
            db.close()

    def test_undecryptable_secret_raises_instead_of_returning_the_ciphertext(self):
        db = TestSessionLocal()
        try:
            user = _make_user(db, "hookowner2")
            with patch.object(db_settings, "encryption_key", _new_key()):
                stranger = encrypt_credential("someone-elses")
            webhook = _make_webhook(db, user, stranger)
            with pytest.raises(CredentialDecryptionError):
                decrypt_webhook_secret(db, webhook)
        finally:
            db.close()


# ── Tests: re_encrypt_tokens ─────────────────────────────────────────────────


class TestReEncryptTokens:
    """Test the re_encrypt_tokens background job."""

    def test_re_encrypts_old_version_tokens(self):
        """Tokens with key_version < current are brought to the current version."""
        db = TestSessionLocal()
        try:
            user = _make_user(db)
            link = _make_link(db, user)
            token = _make_access_token(db, user, link, "alice", "secret", key_version=1)
            old_username_enc = token.username_encrypted

            # Pretend we're on version 2
            with patch.object(db_settings, "encryption_key_version", 2):
                assert _run_sweep_to_completion(db) == 2  # the user's DEK + the token
                assert key_rotation_status(db)["complete"] is True

            db.refresh(token)
            db.refresh(user)
            assert token.key_version == 2
            assert user.dek_key_version == 2
            # Credentials under the DEK need no rewrite: the DEK is what the master key protects.
            assert token.username_encrypted == old_username_enc
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
        """re_encrypt_tokens respects batch_size and resumes where it stopped."""
        db = TestSessionLocal()
        try:
            user = _make_user(db)
            link = _make_link(db, user)
            for i in range(5):
                _make_access_token(db, user, link, f"user{i}", f"pass{i}", key_version=1)

            with patch.object(db_settings, "encryption_key_version", 2):
                counts = [re_encrypt_tokens(db, batch_size=2) for _ in range(4)]
                # One DEK + five tokens, two per call, then nothing left.
                assert counts == [2, 2, 2, 0]
                assert db.query(AccessToken).filter(AccessToken.key_version < 2).count() == 0
        finally:
            db.close()

    def test_legacy_master_key_tokens_move_under_a_new_dek(self):
        """Tokens of a user without a DEK (master-key encrypted) move into the envelope."""
        db = TestSessionLocal()
        try:
            user = _make_user(db, "nodekuser", with_dek=False)
            link = _make_link(db, user, site="internal_bank")
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
                assert re_encrypt_tokens(db) == 1

            db.refresh(user)
            db.refresh(token)
            assert user.encrypted_dek is not None
            dek = unwrap_dek(user.encrypted_dek)
            assert _db_mod.decrypt_with_dek(dek, token.username_encrypted) == "nouser"
            assert decrypt_credential_for_user(user, token.password_encrypted) == "nopass"
            assert token.key_version == 2
        finally:
            db.close()

    def test_undecryptable_rows_do_not_block_the_rest(self):
        """JOB-19: rows no key can decrypt are passed over, reported, and never logged in full."""
        db = TestSessionLocal()
        try:
            user = _make_user(db, "mixed")
            link = _make_link(db, user)
            with patch.object(db_settings, "encryption_key", _new_key()):
                stranger = encrypt_credential("lost")
            broken = []
            for i in range(3):
                broken_token = f"a-broken-{i}-{uuid.uuid4()}"  # sorts before the good ones
                db.add(
                    AccessToken(
                        token=broken_token,
                        link_token=link.link_token,
                        username_encrypted=stranger,
                        password_encrypted=stranger,
                        user_id=user.id,
                        key_version=1,
                    )
                )
                broken.append(broken_token)
            db.commit()
            good = [_make_access_token(db, user, link, f"u{i}", f"p{i}", token=f"z-good-{i}").token for i in range(5)]

            with patch.object(db_settings, "encryption_key_version", 2), pytest.raises(KeyRotationIncomplete) as info:
                for _ in range(10):  # bounded: the pass must end
                    re_encrypt_tokens(db, batch_size=2)

            assert len(info.value.failures) == 3
            message = str(info.value)
            assert all(raw not in message for raw in broken)
            assert "access_tokens:sha256:" in message
            rotated = {t.token: t.key_version for t in db.query(AccessToken).all()}
            assert all(rotated[t] == 2 for t in good)
            assert all(rotated[t] == 1 for t in broken)
        finally:
            db.close()

    def test_sweep_logs_never_contain_tokens(self, caplog):
        db = TestSessionLocal()
        try:
            user = _make_user(db, "loggy")
            link = _make_link(db, user)
            with patch.object(db_settings, "encryption_key", _new_key()):
                stranger = encrypt_credential("lost")
            raw = f"secret-token-{uuid.uuid4()}"
            db.add(
                AccessToken(
                    token=raw,
                    link_token=link.link_token,
                    username_encrypted=stranger,
                    password_encrypted=stranger,
                    user_id=user.id,
                    key_version=1,
                )
            )
            db.commit()
            with (
                caplog.at_level(logging.DEBUG),
                patch.object(db_settings, "encryption_key_version", 2),
                pytest.raises(KeyRotationIncomplete),
            ):
                re_encrypt_tokens(db)
            assert raw not in caplog.text
            assert "could not be re-encrypted" in caplog.text
        finally:
            db.close()

    def test_a_concurrent_credential_update_is_not_overwritten(self):
        """The sweep writes with a conditional UPDATE, so a reconnect that lands mid-sweep wins."""
        db = TestSessionLocal()
        try:
            # The owner already has a DEK, so the sweep holds no write lock while it
            # re-encrypts this legacy (master-key) row — the window a reconnect can hit.
            user = _make_user(db, "racy")
            link = _make_link(db, user)
            token = AccessToken(
                token="racy-token",
                link_token=link.link_token,
                username_encrypted=encrypt_credential("old-user"),
                password_encrypted=encrypt_credential("old-pass"),
                user_id=user.id,
                key_version=1,
            )
            db.add(token)
            db.commit()

            real = _db_mod._reencrypt_under_dek

            def reconnect_meanwhile(session, owner, ciphertexts):
                changed = real(session, owner, ciphertexts)
                other = TestSessionLocal()
                try:
                    row = other.get(AccessToken, "racy-token")
                    row.username_encrypted = encrypt_credential("new-user")
                    row.password_encrypted = encrypt_credential("new-pass")
                    other.commit()
                finally:
                    other.close()
                return changed

            with (
                patch.object(db_settings, "encryption_key_version", 2),
                patch.object(_db_mod, "_reencrypt_under_dek", reconnect_meanwhile),
            ):
                rotate_encryption_keys(db)

            db.expire_all()
            row = db.get(AccessToken, "racy-token")
            assert decrypt_credential_for_user(db.get(User, row.user_id), row.username_encrypted) == "new-user"
            assert decrypt_credential_for_user(db.get(User, row.user_id), row.password_encrypted) == "new-pass"
            assert row.key_version == 1  # left for the next pass
        finally:
            db.close()

    def test_external_kms_provider_leaves_deks_alone(self):
        db = TestSessionLocal()
        try:
            user = _make_user(db, "kmsuser")
            wrapped = user.encrypted_dek
            with (
                patch.object(db_settings, "encryption_key_version", 2),
                patch.object(_db_mod, "_uses_local_kms", return_value=False),
            ):
                re_encrypt_tokens(db)
                assert key_rotation_status(db)["stale_user_deks"] == 0
            db.refresh(user)
            assert user.encrypted_dek == wrapped
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
            new_key = _new_key()
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
                _run_sweep_to_completion(db)
                assert key_rotation_status(db)["complete"] is True

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

            new_key = _new_key()
            old_key = db_settings.encryption_key

            dek_count = rotate_master_key(old_key, new_key, db)
            assert dek_count == 2

            with (
                patch.object(db_settings, "encryption_key", new_key),
                patch.object(db_settings, "encryption_key_previous", old_key),
                patch.object(db_settings, "encryption_key_version", 2),
            ):
                _run_sweep_to_completion(db)

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

    def test_rotate_master_key_is_idempotent(self):
        db = TestSessionLocal()
        try:
            _make_user(db, "idem")
            old_key, new_key = db_settings.encryption_key, _new_key()
            assert rotate_master_key(old_key, new_key, db) == 1
            assert rotate_master_key(old_key, new_key, db) == 0  # already under new_key
        finally:
            db.close()

    def test_rotate_master_key_reports_deks_under_neither_key(self):
        db = TestSessionLocal()
        try:
            good = _make_user(db, "good")
            with patch.object(db_settings, "encryption_key", _new_key()):
                _make_user(db, "orphaned")  # DEK wrapped by a key nobody has any more
            old_key, new_key = db_settings.encryption_key, _new_key()
            with pytest.raises(KeyRotationIncomplete) as info:
                rotate_master_key(old_key, new_key, db)
            assert len(info.value.failures) == 1
            # The good user was still re-wrapped and committed.
            db.refresh(good)
            with patch.object(db_settings, "encryption_key", new_key):
                assert len(unwrap_dek(good.encrypted_dek)) == 32
        finally:
            db.close()


class TestDocumentedRotationProcedure:
    """SEC-08: SECURITY.md's "Key Rotation Procedure", step by step.

    The old runbook re-encrypted credentials under the same per-user DEK and
    then told operators to delete the previous master key, which still wrapped
    every DEK: all stored credentials became undecryptable.
    """

    def _seed(self, db, client):
        """Everything encrypted at rest, as a v1 deployment has it."""
        client.post(
            "/auth/register",
            json={"username": "rotaudit", "email": "rotaudit@example.com", "password": "Secure@pass123"},
        )
        user = _make_user(db, "rotowner")
        link = _make_link(db, user)
        token = _make_access_token(db, user, link, "site-user", "site-pass")
        legacy_user = _make_user(db, "legacyowner", with_dek=False)
        legacy_link = _make_link(db, legacy_user)
        legacy_token = _make_access_token(db, legacy_user, legacy_link, "legacy-user", "legacy-pass")
        hook_new = _make_webhook(db, user, encrypt_webhook_secret(user, "hook-dek"))
        hook_old = _make_webhook(db, user, encrypt_credential("hook-master"))
        job = AccessJob(id="ajob-1", user_id=user.id, site="internal_bank", job_type="connect", lock_scope="s")
        job.result_json = encrypt_json_for_user(db, user, {"balance": 12.5}, context="access_job:ajob-1:result")
        db.add(job)
        db.commit()
        return {
            "user": user.id,
            "token": token.token,
            "legacy_user": legacy_user.id,
            "legacy_token": legacy_token.token,
            "hooks": {hook_new.id: "hook-dek", hook_old.id: "hook-master"},
        }

    def _assert_everything_decrypts(self, db, seeded):
        from src.audit import verify_audit_chain

        db.expire_all()
        user = db.get(User, seeded["user"])
        token = db.get(AccessToken, seeded["token"])
        assert decrypt_credential_for_user(user, token.username_encrypted) == "site-user"
        assert decrypt_credential_for_user(user, token.password_encrypted) == "site-pass"
        legacy_user = db.get(User, seeded["legacy_user"])
        legacy_token = db.get(AccessToken, seeded["legacy_token"])
        assert decrypt_credential_for_user(legacy_user, legacy_token.password_encrypted) == "legacy-pass"
        for hook_id, secret in seeded["hooks"].items():
            assert decrypt_webhook_secret(db, db.get(Webhook, hook_id)) == secret
        job = db.get(AccessJob, "ajob-1")
        assert decrypt_json_for_user(user, job.result_json, context="access_job:ajob-1:result") == {"balance": 12.5}
        report = verify_audit_chain(db)
        assert report["valid"] is True, report["errors"]

    def test_following_the_runbook_keeps_every_secret_readable(self, client):
        from click.testing import CliRunner

        db = TestSessionLocal()
        try:
            seeded = self._seed(db, client)
            old_key = db_settings.encryption_key

            # 1. Generate a new 256-bit key.
            new_key = _new_key()
            with ExitStack() as stack:
                # 2. ENCRYPTION_KEY_PREVIOUS=<old>, ENCRYPTION_KEY=<new>, bump the version, restart.
                _rotation(stack, new_key, old_key, 2)
                self._assert_everything_decrypts(db, seeded)  # reads fall back to the old key

                # 3. plaidify rotate-key --re-encrypt (keys come from the same variables).
                result = CliRunner().invoke(
                    TestCLIRotateKey()._import_cli(),
                    ["rotate-key", "--re-encrypt"],
                    env={"ENCRYPTION_KEY_PREVIOUS": old_key, "ENCRYPTION_KEY": new_key},
                )
                assert result.exit_code == 0, result.output

                # 4. Confirm nothing depends on the previous key any more.
                status = key_rotation_status(db)
                assert status["complete"] is True, status

            with ExitStack() as stack:
                # 5. Remove ENCRYPTION_KEY_PREVIOUS and restart.
                _rotation(stack, new_key, None, 2)
                self._assert_everything_decrypts(db, seeded)
        finally:
            db.close()
            kms.reset_kms_provider()

    def test_removing_the_previous_key_without_step_3_breaks_decryption(self, client):
        """Control: the procedure above is what keeps the data readable."""
        db = TestSessionLocal()
        try:
            seeded = self._seed(db, client)
            with ExitStack() as stack:
                _rotation(stack, _new_key(), None, 2)
                with pytest.raises(CredentialDecryptionError):
                    self._assert_everything_decrypts(db, seeded)
        finally:
            db.close()
            kms.reset_kms_provider()


# ── Tests: CLI rotate-key command ─────────────────────────────────────────────


class TestCLIRotateKey:
    """Test the CLI rotate-key command."""

    def _import_cli(self):
        """Import CLI module without triggering the full SDK __init__.py."""
        import importlib.util
        import sys

        sdk_package = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "sdk", "plaidify"))
        spec = importlib.util.spec_from_file_location("plaidify_cli", os.path.join(sdk_package, "cli.py"))
        # A stand-in 'plaidify' package: cli.py's own imports (plaidify.config)
        # resolve from the SDK directory whatever ran before, and the SDK's
        # __init__.py never runs.
        fake_plaidify = type(sys)("plaidify")
        fake_plaidify.__version__ = "0.3.0a1"
        fake_plaidify.__path__ = [sdk_package]
        saved = {name: sys.modules.get(name) for name in ("plaidify", "plaidify.config")}
        sys.modules["plaidify"] = fake_plaidify
        try:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        finally:
            for name, module in saved.items():
                if module is not None:
                    sys.modules[name] = module
                else:
                    sys.modules.pop(name, None)
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

        new_key = _new_key()
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
        """CLI rotate-key with --re-encrypt also brings the stored rows to the current key version."""
        from click.testing import CliRunner

        cli = self._import_cli()

        db = TestSessionLocal()
        try:
            user = _make_user(db, "cliuser2")
            link = _make_link(db, user)
            _make_access_token(db, user, link, "clipass2", "clisecret2", key_version=1)
        finally:
            db.close()

        new_key = _new_key()
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
        assert "row(s) to the current key version" in result.output

    def test_cli_fails_when_rows_cannot_be_rotated(self):
        """A partial rotation must not look complete (exit status 1)."""
        from click.testing import CliRunner

        cli = self._import_cli()
        db = TestSessionLocal()
        try:
            user = _make_user(db, "cliuser3")
            link = _make_link(db, user)
            with patch.object(db_settings, "encryption_key", _new_key()):
                stranger = encrypt_credential("lost")
            db.add(
                AccessToken(
                    token="unreadable",
                    link_token=link.link_token,
                    username_encrypted=stranger,
                    password_encrypted=stranger,
                    user_id=user.id,
                    key_version=1,
                )
            )
            db.commit()
        finally:
            db.close()

        old_key, new_key = db_settings.encryption_key, _new_key()
        with (
            patch.object(db_settings, "encryption_key", new_key),
            patch.object(db_settings, "encryption_key_previous", old_key),
            patch.object(db_settings, "encryption_key_version", 2),
        ):
            result = CliRunner().invoke(cli, ["rotate-key", "--old-key", old_key, "--new-key", new_key, "--re-encrypt"])
        assert result.exit_code == 1, result.output
        assert "could not be brought to key_version=2" in result.output
        assert "unreadable" not in result.output
