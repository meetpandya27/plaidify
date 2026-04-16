"""
Tests for the KMS abstraction layer.
"""

import os
import pytest
import asyncio
from unittest.mock import patch

from src.kms import (
    KMSProvider,
    LocalKMSProvider,
    AWSKMSProvider,
    AzureKeyVaultProvider,
    HashiCorpVaultProvider,
    get_kms_provider,
    _PROVIDERS,
)


class TestLocalKMSProvider:
    @pytest.fixture
    def provider(self):
        return LocalKMSProvider()

    @pytest.mark.asyncio
    async def test_wrap_unwrap_roundtrip(self, provider):
        dek = os.urandom(32)
        wrapped = await provider.wrap_key(dek)
        assert isinstance(wrapped, str)
        assert len(wrapped) > 0

        unwrapped = await provider.unwrap_key(wrapped)
        assert unwrapped == dek

    @pytest.mark.asyncio
    async def test_generate_data_key(self, provider):
        plaintext, wrapped = await provider.generate_data_key()
        assert len(plaintext) == 32
        assert isinstance(wrapped, str)

        # Verify the wrapped form decrypts to the same key
        unwrapped = await provider.unwrap_key(wrapped)
        assert unwrapped == plaintext

    @pytest.mark.asyncio
    async def test_different_keys_produce_different_wraps(self, provider):
        dek1 = os.urandom(32)
        dek2 = os.urandom(32)
        wrap1 = await provider.wrap_key(dek1)
        wrap2 = await provider.wrap_key(dek2)
        assert wrap1 != wrap2

    @pytest.mark.asyncio
    async def test_same_key_different_nonces(self, provider):
        """Same plaintext produces different ciphertext due to random nonce."""
        dek = os.urandom(32)
        wrap1 = await provider.wrap_key(dek)
        wrap2 = await provider.wrap_key(dek)
        assert wrap1 != wrap2  # Different nonces

        # But both decrypt to the same key
        assert await provider.unwrap_key(wrap1) == dek
        assert await provider.unwrap_key(wrap2) == dek

    @pytest.mark.asyncio
    async def test_health_check(self, provider):
        result = await provider.health_check()
        assert result["provider"] == "local"
        assert result["status"] == "healthy"

    @pytest.mark.asyncio
    async def test_rotate_master_key_message(self, provider):
        msg = await provider.rotate_master_key()
        assert "ENCRYPTION_KEY" in msg


class TestProviderRegistry:
    def test_default_provider_is_local(self):
        import src.kms
        src.kms._provider_instance = None  # Reset singleton
        provider = get_kms_provider("local")
        assert isinstance(provider, LocalKMSProvider)

    def test_all_providers_registered(self):
        assert "local" in _PROVIDERS
        assert "aws" in _PROVIDERS
        assert "azure" in _PROVIDERS
        assert "vault" in _PROVIDERS

    def test_unknown_provider_raises(self):
        import src.kms
        src.kms._provider_instance = None
        with pytest.raises(ValueError, match="Unknown KMS provider"):
            get_kms_provider("unknown_provider")


class TestAWSKMSProvider:
    def test_requires_boto3(self):
        provider = AWSKMSProvider(key_id="arn:aws:kms:us-east-1:123:key/abc")
        # If boto3 is not installed, _get_client should raise
        # If it IS installed, it will try to create a client (may fail with no creds)
        # Either way, we're testing the provider can be instantiated
        assert provider._key_id == "arn:aws:kms:us-east-1:123:key/abc"


class TestAzureKeyVaultProvider:
    def test_config_from_init(self):
        provider = AzureKeyVaultProvider(
            vault_url="https://myvault.vault.azure.net/",
            key_name="my-key",
        )
        assert provider._vault_url == "https://myvault.vault.azure.net/"
        assert provider._key_name == "my-key"


class TestHashiCorpVaultProvider:
    def test_config_from_init(self):
        provider = HashiCorpVaultProvider(
            vault_addr="http://vault.local:8200",
            token="s.mytoken",
            key_name="transit-key",
        )
        assert provider._vault_addr == "http://vault.local:8200"
        assert provider._key_name == "transit-key"


class TestKMSProviderInterface:
    """Verify all providers implement the abstract interface."""

    def test_all_providers_are_kms_provider(self):
        for name, cls in _PROVIDERS.items():
            assert issubclass(cls, KMSProvider), f"{name} does not subclass KMSProvider"

    def test_all_providers_have_required_methods(self):
        required = ["wrap_key", "unwrap_key", "generate_data_key", "rotate_master_key", "health_check"]
        for name, cls in _PROVIDERS.items():
            for method in required:
                assert hasattr(cls, method), f"{name} missing method: {method}"
