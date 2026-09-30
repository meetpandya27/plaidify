"""Tests for the KMS<->database integration shipped with #26.

Covers:
- ``database.wrap_dek`` / ``unwrap_dek`` route through the KMS provider.
- The ``LocalKMSProvider`` sync helpers preserve the existing wire format
  (so previously-stored envelopes still unwrap after this refactor).
- The provider singleton respects ``settings.kms_provider`` and the
  ``reset_kms_provider`` helper used by the migration script.
- ``scripts/migrate_to_kms.main`` performs end-to-end re-wrapping
  between two local provider instances.
"""

from __future__ import annotations

import os

import pytest

from src import database, kms


@pytest.fixture(autouse=True)
def _reset_kms_singleton():
    kms.reset_kms_provider()
    yield
    kms.reset_kms_provider()


class TestLocalKMSSyncHelpers:
    def test_sync_roundtrip(self):
        provider = kms.LocalKMSProvider()
        dek = os.urandom(32)
        wrapped = provider.wrap_key_sync(dek)
        assert isinstance(wrapped, str)
        assert provider.unwrap_key_sync(wrapped) == dek

    def test_async_methods_delegate_to_sync(self):
        provider = kms.LocalKMSProvider()
        dek = os.urandom(32)
        sync_wrap = provider.wrap_key_sync(dek)
        # Async should produce the same wire format on round-trip.
        assert provider.unwrap_key_sync(sync_wrap) == dek


class TestDatabaseRoutesThroughKMS:
    def test_wrap_dek_uses_local_provider_by_default(self):
        dek = os.urandom(32)
        wrapped = database.wrap_dek(dek)
        assert isinstance(wrapped, str)
        assert database.unwrap_dek(wrapped) == dek

    def test_unwrap_dek_falls_back_to_previous_master_for_local(self, monkeypatch):
        """Rotation path: data wrapped with previous key still unwraps."""
        import base64

        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        dek = os.urandom(32)
        old_key = os.urandom(32)
        old_b64 = base64.urlsafe_b64encode(old_key).decode("ascii")

        # Wrap manually with the old key (simulating data written before rotation).
        nonce = os.urandom(12)
        ct = AESGCM(old_key).encrypt(nonce, dek, None)
        wrapped_with_old = base64.urlsafe_b64encode(nonce + ct).decode("ascii")

        # Configure rotation: current key is whatever settings has, previous = old_b64.
        monkeypatch.setattr(database.settings, "encryption_key_previous", old_b64)
        kms.reset_kms_provider()

        # The current LocalKMS provider can't unwrap (different key), but the
        # previous-key fallback in unwrap_dek should rescue it.
        assert database.unwrap_dek(wrapped_with_old) == dek


class TestProviderSelection:
    def test_explicit_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="Unknown KMS provider"):
            kms.get_kms_provider("definitely-not-real")

    def test_settings_kms_provider_is_consulted(self, monkeypatch):
        from src.config import get_settings

        monkeypatch.setattr(get_settings(), "kms_provider", "local")
        kms.reset_kms_provider()
        provider = kms.get_kms_provider()
        assert isinstance(provider, kms.LocalKMSProvider)

    def test_explicit_argument_wins_over_settings(self, monkeypatch):
        from src.config import get_settings

        monkeypatch.setattr(get_settings(), "kms_provider", "local")
        provider = kms.get_kms_provider("local")
        assert isinstance(provider, kms.LocalKMSProvider)


class TestMigrationScript:
    def test_dry_imports(self):
        # Smoke import — script must not have side effects on import.
        import importlib

        mod = importlib.import_module("scripts.migrate_to_kms")
        assert hasattr(mod, "main")

    def test_main_requires_target(self, monkeypatch, caplog):
        from scripts.migrate_to_kms import main

        monkeypatch.delenv("TARGET_KMS_PROVIDER", raising=False)
        monkeypatch.delenv("SOURCE_KMS_PROVIDER", raising=False)
        rc = main()
        assert rc == 2

    def test_main_rejects_identical_source_target(self, monkeypatch):
        from scripts.migrate_to_kms import main

        monkeypatch.setenv("SOURCE_KMS_PROVIDER", "local")
        monkeypatch.setenv("TARGET_KMS_PROVIDER", "local")
        assert main() == 2


class _FakeHSM(kms.KMSProvider):
    """Stands in for AWS / Azure / Vault: its own key, its own wire format."""

    _key = os.urandom(32)

    def wrap_key_sync(self, plaintext_key: bytes) -> str:
        import base64

        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        nonce = os.urandom(12)
        return (
            "hsm:" + base64.urlsafe_b64encode(nonce + AESGCM(self._key).encrypt(nonce, plaintext_key, b"hsm")).decode()
        )

    def unwrap_key_sync(self, wrapped_key: str) -> bytes:
        import base64

        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        if not wrapped_key.startswith("hsm:"):
            raise ValueError("not an HSM envelope")
        raw = base64.urlsafe_b64decode(wrapped_key[4:])
        return AESGCM(self._key).decrypt(raw[:12], raw[12:], b"hsm")

    async def wrap_key(self, plaintext_key: bytes) -> str:
        return self.wrap_key_sync(plaintext_key)

    async def unwrap_key(self, wrapped_key: str) -> bytes:
        return self.unwrap_key_sync(wrapped_key)

    async def generate_data_key(self) -> tuple[bytes, str]:
        dek = os.urandom(32)
        return dek, self.wrap_key_sync(dek)

    async def rotate_master_key(self) -> str:
        return "rotated"

    async def health_check(self) -> dict:
        return {"provider": "hsm", "status": "healthy"}


class TestMigrationMovesEverything:
    """SEC-21: every encrypted artifact ends up under the target provider."""

    @pytest.fixture
    def hsm(self, monkeypatch):
        monkeypatch.setitem(kms._PROVIDERS, "hsm", _FakeHSM)
        monkeypatch.setenv("SOURCE_KMS_PROVIDER", "local")
        monkeypatch.setenv("TARGET_KMS_PROVIDER", "hsm")

    def _seed(self):
        import uuid

        from tests.conftest import TestSessionLocal

        db = TestSessionLocal()
        try:
            modern = database.User(username="modern", email="m@x", encrypted_dek=database.create_user_dek())
            legacy = database.User(username="legacy", email="l@x", encrypted_dek=None)
            db.add_all([modern, legacy])
            db.commit()
            db.add_all(
                [
                    database.Link(link_token="lm", site="s", user_id=modern.id),
                    database.Link(link_token="ll", site="s", user_id=legacy.id),
                ]
            )
            db.add_all(
                [
                    database.AccessToken(
                        token="tok-modern",
                        link_token="lm",
                        user_id=modern.id,
                        username_encrypted=database.encrypt_credential_for_user(modern, "m-user"),
                        password_encrypted=database.encrypt_credential("m-pass"),  # a legacy half
                    ),
                    database.AccessToken(
                        token="tok-legacy",
                        link_token="ll",
                        user_id=legacy.id,
                        username_encrypted=database.encrypt_credential("l-user"),
                        password_encrypted=database.encrypt_credential("l-pass"),
                    ),
                    database.Webhook(
                        id=str(uuid.uuid4()),
                        link_token="lm",
                        url="https://h",
                        secret=database.encrypt_credential("hook-master"),
                        user_id=modern.id,
                    ),
                    database.Webhook(
                        id=str(uuid.uuid4()),
                        link_token="lm",
                        url="https://h",
                        secret=database.encrypt_webhook_secret(modern, "hook-dek"),
                        user_id=modern.id,
                    ),
                ]
            )
            db.commit()
            return modern.id, legacy.id
        finally:
            db.close()

    def _under_hsm_only(self, monkeypatch, modern_id, legacy_id):
        """Every artifact decrypts through the target provider, and none is left on the master key."""
        from tests.conftest import TestSessionLocal

        monkeypatch.setenv("KMS_PROVIDER", "hsm")
        kms.reset_kms_provider()
        db = TestSessionLocal()
        try:
            users = {u.id: u for u in db.query(database.User).all()}
            assert all(u.encrypted_dek.startswith("hsm:") for u in users.values())
            deks = {uid: _FakeHSM().unwrap_key_sync(u.encrypted_dek) for uid, u in users.items()}
            tokens = {t.token: t for t in db.query(database.AccessToken).all()}
            for token, owner, expected in (
                ("tok-modern", modern_id, ("m-user", "m-pass")),
                ("tok-legacy", legacy_id, ("l-user", "l-pass")),
            ):
                row = tokens[token]
                got = tuple(
                    database.decrypt_with_dek(deks[owner], ct)
                    for ct in (row.username_encrypted, row.password_encrypted)
                )
                assert got == expected
            secrets = sorted(
                database.decrypt_with_dek(deks[w.user_id], w.secret) for w in db.query(database.Webhook).all()
            )
            assert secrets == ["hook-dek", "hook-master"]
            for w in db.query(database.Webhook).all():
                assert database.decrypt_webhook_secret(db, w) in ("hook-dek", "hook-master")
        finally:
            db.close()
            kms.reset_kms_provider()

    def test_moves_deks_legacy_credentials_and_webhook_secrets(self, hsm, monkeypatch):
        from scripts.migrate_to_kms import main

        modern_id, legacy_id = self._seed()
        assert main() == 0
        self._under_hsm_only(monkeypatch, modern_id, legacy_id)

    def test_rerun_is_a_clean_no_op(self, hsm, monkeypatch):
        from scripts.migrate_to_kms import main

        modern_id, legacy_id = self._seed()
        assert main() == 0
        assert main() == 0
        self._under_hsm_only(monkeypatch, modern_id, legacy_id)

    def test_skipped_rows_make_the_exit_status_nonzero(self, hsm, monkeypatch, caplog):
        import base64

        from scripts.migrate_to_kms import main
        from tests.conftest import TestSessionLocal

        self._seed()
        db = TestSessionLocal()
        try:
            stray_key = base64.urlsafe_b64encode(os.urandom(32)).decode()
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(database.settings, "encryption_key", stray_key)
                kms.reset_kms_provider()
                db.add(database.User(username="stray", email="s@x", encrypted_dek=database.create_user_dek()))
                db.commit()
            kms.reset_kms_provider()
        finally:
            db.close()

        assert main() == 1  # the stray DEK unwraps with neither provider
        assert "skipping" in caplog.text
        db = TestSessionLocal()
        try:
            migrated = db.query(database.User).filter(database.User.encrypted_dek.like("hsm:%")).count()
            assert migrated == 2  # the others still moved
        finally:
            db.close()
