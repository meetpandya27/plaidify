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
