"""Hermetic tests for the external KMS providers (AWS, Azure, Vault).

The cloud SDKs (boto3, azure-keyvault-keys / azure-identity, hvac) are optional
and not installed in CI, so each test injects a fake SDK module into
``sys.modules``. This proves the provider wiring — the SDK calls and the
wrap/unwrap round-trip semantics — without real credentials or network access.
"""

import asyncio
import base64
import sys
import types
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.kms import AWSKMSProvider, AzureKeyVaultProvider, HashiCorpVaultProvider

# ── AWS ───────────────────────────────────────────────────────────────────────


def _fake_boto3_module():
    mod = types.ModuleType("boto3")

    class _Client:
        def encrypt(self, KeyId, Plaintext):
            return {"CiphertextBlob": b"AWSCT|" + Plaintext}

        def decrypt(self, CiphertextBlob, KeyId=None):
            assert CiphertextBlob.startswith(b"AWSCT|")
            return {"Plaintext": CiphertextBlob[len(b"AWSCT|") :]}

        def generate_data_key(self, KeyId, KeySpec):
            pt = b"\x02" * 32
            return {"Plaintext": pt, "CiphertextBlob": b"AWSCT|" + pt}

        def describe_key(self, KeyId):
            return {"KeyMetadata": {"KeyState": "Enabled"}}

        def enable_key_rotation(self, KeyId):
            return {}

    mod.client = lambda service, region_name=None: _Client()
    return mod


@pytest.fixture
def fake_boto3():
    with patch.dict(sys.modules, {"boto3": _fake_boto3_module()}):
        yield


def test_aws_wrap_unwrap_roundtrip(fake_boto3):
    provider = AWSKMSProvider(key_id="arn:aws:kms:us-east-1:0:key/abc", region="us-east-1")
    dek = b"\x01" * 32
    wrapped = provider.wrap_key_sync(dek)
    assert isinstance(wrapped, str)
    assert provider.unwrap_key_sync(wrapped) == dek


def test_aws_generate_data_key(fake_boto3):
    provider = AWSKMSProvider(key_id="arn:aws:kms:us-east-1:0:key/abc")
    plaintext, wrapped = asyncio.run(provider.generate_data_key())
    assert len(plaintext) == 32
    assert isinstance(wrapped, str)
    assert provider.unwrap_key_sync(wrapped) == plaintext


def test_aws_health_check_enabled(fake_boto3):
    provider = AWSKMSProvider(key_id="arn:aws:kms:us-east-1:0:key/abc")
    result = asyncio.run(provider.health_check())
    assert result["provider"] == "aws-kms"
    assert result["status"] == "healthy"
    assert result["key_state"] == "Enabled"


def test_aws_requires_key_id(monkeypatch):
    monkeypatch.delenv("KMS_AWS_KEY_ID", raising=False)
    with patch("src.config.get_settings", return_value=SimpleNamespace(kms_key_id=None, kms_region=None)):
        with pytest.raises(ValueError):
            AWSKMSProvider()


# ── Azure Key Vault ───────────────────────────────────────────────────────────


def _fake_azure_modules():
    azure = types.ModuleType("azure")
    azure.__path__ = []
    identity = types.ModuleType("azure.identity")
    identity.DefaultAzureCredential = lambda *a, **k: object()

    keyvault = types.ModuleType("azure.keyvault")
    keyvault.__path__ = []
    keys = types.ModuleType("azure.keyvault.keys")
    keys.__path__ = []

    class _KeyClient:
        def __init__(self, vault_url=None, credential=None):
            self.vault_url = vault_url

        def get_key(self, name):
            return SimpleNamespace(name=name)

    keys.KeyClient = _KeyClient

    crypto = types.ModuleType("azure.keyvault.keys.crypto")

    class _KeyWrapAlgorithm:
        rsa_oaep_256 = "RSA-OAEP-256"

    class _CryptographyClient:
        def __init__(self, key, credential=None):
            self.key = key

        def wrap_key(self, algo, key):
            return SimpleNamespace(encrypted_key=b"AZ|" + key)

        def unwrap_key(self, algo, blob):
            assert blob.startswith(b"AZ|")
            return SimpleNamespace(key=blob[len(b"AZ|") :])

    crypto.CryptographyClient = _CryptographyClient
    crypto.KeyWrapAlgorithm = _KeyWrapAlgorithm

    return {
        "azure": azure,
        "azure.identity": identity,
        "azure.keyvault": keyvault,
        "azure.keyvault.keys": keys,
        "azure.keyvault.keys.crypto": crypto,
    }


@pytest.fixture
def fake_azure():
    with patch.dict(sys.modules, _fake_azure_modules()):
        yield


def test_azure_wrap_unwrap_roundtrip(fake_azure):
    provider = AzureKeyVaultProvider(vault_url="https://v.vault.azure.net/", key_name="plaidify-master")
    dek = b"\x05" * 32
    wrapped = provider.wrap_key_sync(dek)
    assert isinstance(wrapped, str)
    assert provider.unwrap_key_sync(wrapped) == dek


def test_azure_health_check(fake_azure):
    provider = AzureKeyVaultProvider(vault_url="https://v.vault.azure.net/", key_name="plaidify-master")
    result = asyncio.run(provider.health_check())
    assert result["provider"] == "azure-keyvault"
    assert result["status"] == "healthy"


def test_azure_requires_vault_url(monkeypatch):
    monkeypatch.delenv("KMS_AZURE_VAULT_URL", raising=False)
    with pytest.raises(ValueError):
        AzureKeyVaultProvider()


# ── HashiCorp Vault ───────────────────────────────────────────────────────────


def _fake_hvac_module():
    mod = types.ModuleType("hvac")

    class _Transit:
        def encrypt_data(self, name, plaintext):
            return {"data": {"ciphertext": "vault:v1:" + plaintext}}

        def decrypt_data(self, name, ciphertext):
            assert ciphertext.startswith("vault:v1:")
            return {"data": {"plaintext": ciphertext[len("vault:v1:") :]}}

        def generate_data_key(self, name, key_type):
            pt = base64.b64encode(b"\x03" * 32).decode()
            return {"data": {"plaintext": pt, "ciphertext": "vault:v1:" + pt}}

        def rotate_encryption_key(self, name):
            return {}

    class _Secrets:
        def __init__(self):
            self.transit = _Transit()

    class _Client:
        def __init__(self, url=None, token=None):
            self.secrets = _Secrets()

        def is_authenticated(self):
            return True

    mod.Client = _Client
    return mod


@pytest.fixture
def fake_hvac():
    with patch.dict(sys.modules, {"hvac": _fake_hvac_module()}):
        yield


def test_vault_wrap_unwrap_roundtrip(fake_hvac):
    provider = HashiCorpVaultProvider(vault_addr="http://v:8200", token="t", key_name="plaidify-master")
    dek = b"\x07" * 32
    wrapped = provider.wrap_key_sync(dek)
    assert isinstance(wrapped, str)
    assert wrapped.startswith("vault:")
    assert provider.unwrap_key_sync(wrapped) == dek


def test_vault_generate_data_key(fake_hvac):
    provider = HashiCorpVaultProvider(vault_addr="http://v:8200", token="t", key_name="plaidify-master")
    plaintext, ciphertext = asyncio.run(provider.generate_data_key())
    assert len(plaintext) == 32
    assert provider.unwrap_key_sync(ciphertext) == plaintext


def test_vault_health_check(fake_hvac):
    provider = HashiCorpVaultProvider(vault_addr="http://v:8200", token="t", key_name="plaidify-master")
    result = asyncio.run(provider.health_check())
    assert result["provider"] == "hashicorp-vault"
    assert result["status"] == "healthy"


def test_vault_requires_token(monkeypatch):
    monkeypatch.delenv("KMS_VAULT_TOKEN", raising=False)
    with pytest.raises(ValueError):
        HashiCorpVaultProvider()
