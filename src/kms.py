"""
Key Management Service (KMS) abstraction layer for Plaidify.

Provides a pluggable interface for key management operations:
- **LocalKMSProvider**: Software-based AES-256-GCM (default, current behavior).
- **AWSKMSProvider**: AWS KMS Customer Master Key for envelope key wrapping.
- **AzureKeyVaultProvider**: Azure Key Vault key wrapping (RSA-OAEP-256).
- **HashiCorpVaultProvider**: HashiCorp Vault Transit secrets engine.

The abstraction enables SOC 2 compliance by allowing operators to plug in
HSM-backed key storage without changing application code.

Usage:
    from src.kms import get_kms_provider, KMSProvider

    kms = get_kms_provider()
    wrapped = await kms.wrap_key(dek_bytes)
    dek = await kms.unwrap_key(wrapped)

Blocking and caching:
    The ``*_sync`` methods call the cloud SDKs directly and block; they are
    for synchronous code. The ``async`` methods of the external providers run
    those calls in a worker thread (``asyncio.to_thread``) so they never stall
    the event loop. The local provider is CPU-only AES-GCM and runs inline.

    Cached per process: the provider instance (``get_kms_provider``) and each
    provider's SDK client (boto3 client, hvac client, and for Azure one
    ``CryptographyClient`` per key version). Plaintext DEKs are never cached:
    every unwrap is a KMS round trip.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
from abc import ABC, abstractmethod
from typing import Optional

logger = logging.getLogger("plaidify.kms")


class KMSProvider(ABC):
    """Abstract interface for key management operations."""

    @abstractmethod
    async def wrap_key(self, plaintext_key: bytes) -> str:
        """Wrap (encrypt) a data encryption key.

        Args:
            plaintext_key: Raw key material (e.g. 32-byte DEK).

        Returns:
            Opaque string that can be stored in the database and later
            passed to ``unwrap_key`` to recover the original key.
        """

    @abstractmethod
    async def unwrap_key(self, wrapped_key: str) -> bytes:
        """Unwrap (decrypt) a previously wrapped key.

        Args:
            wrapped_key: The opaque string returned by ``wrap_key``.

        Returns:
            The original plaintext key bytes.
        """

    # ── Synchronous variants ────────────────────────────────────────────
    # Sync helpers exist because the database / credential-encryption hot
    # path is synchronous (SQLAlchemy 1.x style) and called from inside a
    # FastAPI event loop where ``asyncio.run`` is unsafe. Subclasses
    # override these to call their underlying synchronous SDKs (boto3,
    # azure-keyvault, hvac) directly.

    def wrap_key_sync(self, plaintext_key: bytes) -> str:
        raise NotImplementedError(f"{type(self).__name__} does not implement wrap_key_sync().")

    def unwrap_key_sync(self, wrapped_key: str) -> bytes:
        raise NotImplementedError(f"{type(self).__name__} does not implement unwrap_key_sync().")

    @abstractmethod
    async def generate_data_key(self) -> tuple[bytes, str]:
        """Generate a new data encryption key and return both plaintext and wrapped forms.

        Returns:
            A tuple of (plaintext_key_bytes, wrapped_key_string).
        """

    @abstractmethod
    async def rotate_master_key(self) -> str:
        """Trigger a master key rotation.

        Returns:
            A status message or new key identifier.
        """

    @abstractmethod
    async def health_check(self) -> dict:
        """Check KMS provider connectivity and status.

        Returns:
            Dict with ``provider``, ``status``, and optional details.
        """


# ── Local (Software) Provider ─────────────────────────────────────────────────


class LocalKMSProvider(KMSProvider):
    """Software-based KMS using AES-256-GCM.

    This is the default provider that wraps/unwraps keys using the
    local master encryption key from settings. It delegates to the
    existing ``database.py`` functions for backward compatibility.
    """

    def __init__(self) -> None:
        # Lazy import to avoid circular deps
        from src.database import _get_aesgcm, _get_encryption_key

        self._get_aesgcm = _get_aesgcm
        self._get_key = _get_encryption_key
        self._nonce_bytes = 12

    async def wrap_key(self, plaintext_key: bytes) -> str:
        return self.wrap_key_sync(plaintext_key)

    async def unwrap_key(self, wrapped_key: str) -> bytes:
        return self.unwrap_key_sync(wrapped_key)

    def wrap_key_sync(self, plaintext_key: bytes) -> str:
        nonce = os.urandom(self._nonce_bytes)
        ct = self._get_aesgcm().encrypt(nonce, plaintext_key, None)
        return base64.urlsafe_b64encode(nonce + ct).decode("ascii")

    def unwrap_key_sync(self, wrapped_key: str) -> bytes:
        raw = base64.urlsafe_b64decode(wrapped_key)
        nonce = raw[: self._nonce_bytes]
        ct = raw[self._nonce_bytes :]
        return self._get_aesgcm().decrypt(nonce, ct, None)

    async def generate_data_key(self) -> tuple[bytes, str]:
        dek = os.urandom(32)
        wrapped = await self.wrap_key(dek)
        return dek, wrapped

    async def rotate_master_key(self) -> str:
        return "Local provider: set ENCRYPTION_KEY_PREVIOUS to old key, update ENCRYPTION_KEY, then re-wrap all DEKs."

    async def health_check(self) -> dict:
        try:
            # Verify we can wrap/unwrap a test key
            test = os.urandom(32)
            wrapped = await self.wrap_key(test)
            unwrapped = await self.unwrap_key(wrapped)
            ok = test == unwrapped
            return {"provider": "local", "status": "healthy" if ok else "degraded"}
        except Exception as e:
            return {"provider": "local", "status": "unhealthy", "error": str(e)}


# ── AWS KMS Provider ─────────────────────────────────────────────────────────


class AWSKMSProvider(KMSProvider):
    """AWS KMS provider for HSM-backed key management.

    Requires:
        - ``pip install boto3`` (see requirements-kms.txt)
        - ``KMS_KEY_ID`` (CMK ARN or alias; ``KMS_AWS_KEY_ID`` also accepted).
        - AWS credentials configured (env vars, instance profile, SSO, etc.).

    Keys are wrapped/unwrapped directly by KMS (Encrypt/Decrypt) so plaintext
    DEKs never persist. See docs/KMS_INTEGRATION.md for setup and migration.
    """

    def __init__(self, key_id: Optional[str] = None, region: Optional[str] = None) -> None:
        # KMS_KEY_ID (via settings) first, then the older KMS_AWS_KEY_ID name.
        from src.config import get_settings

        settings = get_settings()
        self._key_id = key_id or settings.kms_key_id or os.environ.get("KMS_AWS_KEY_ID", "")
        self._region = region or settings.kms_region or os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
        self._client = None
        if not self._key_id:
            raise ValueError(
                "AWS KMS requires KMS_KEY_ID (or KMS_AWS_KEY_ID) environment variable "
                "(CMK ARN or alias). See docs/KMS_INTEGRATION.md for setup."
            )

    def _get_client(self):
        if self._client is None:
            try:
                import boto3

                self._client = boto3.client("kms", region_name=self._region)
            except ImportError:
                raise RuntimeError("boto3 is required for AWS KMS. Install with: pip install boto3")
        return self._client

    async def wrap_key(self, plaintext_key: bytes) -> str:
        return await asyncio.to_thread(self.wrap_key_sync, plaintext_key)

    async def unwrap_key(self, wrapped_key: str) -> bytes:
        return await asyncio.to_thread(self.unwrap_key_sync, wrapped_key)

    def wrap_key_sync(self, plaintext_key: bytes) -> str:
        client = self._get_client()
        response = client.encrypt(KeyId=self._key_id, Plaintext=plaintext_key)
        return base64.urlsafe_b64encode(response["CiphertextBlob"]).decode("ascii")

    def unwrap_key_sync(self, wrapped_key: str) -> bytes:
        # The ciphertext blob names the key version, so DEKs wrapped before an
        # automatic rotation still decrypt.
        client = self._get_client()
        response = client.decrypt(
            CiphertextBlob=base64.urlsafe_b64decode(wrapped_key),
            KeyId=self._key_id,
        )
        return response["Plaintext"]

    def _generate_data_key_sync(self) -> tuple[bytes, str]:
        response = self._get_client().generate_data_key(
            KeyId=self._key_id,
            KeySpec="AES_256",
        )
        plaintext = response["Plaintext"]
        wrapped = base64.urlsafe_b64encode(response["CiphertextBlob"]).decode("ascii")
        return plaintext, wrapped

    async def generate_data_key(self) -> tuple[bytes, str]:
        return await asyncio.to_thread(self._generate_data_key_sync)

    async def rotate_master_key(self) -> str:
        await asyncio.to_thread(lambda: self._get_client().enable_key_rotation(KeyId=self._key_id))
        return f"AWS KMS automatic rotation enabled for key {self._key_id}"

    async def health_check(self) -> dict:
        try:
            client = await asyncio.to_thread(self._get_client)
            response = await asyncio.to_thread(client.describe_key, KeyId=self._key_id)
            state = response["KeyMetadata"]["KeyState"]
            return {
                "provider": "aws-kms",
                "status": "healthy" if state == "Enabled" else "degraded",
                "key_state": state,
                "key_id": self._key_id,
            }
        except Exception as e:
            return {"provider": "aws-kms", "status": "unhealthy", "error": str(e)}


# ── Azure Key Vault Provider ─────────────────────────────────────────────────


_AZURE_WRAP_PREFIX = "azkv1:"  # azkv1:<key version>:<base64url(wrapped DEK)>


def _azure_key_version(key) -> Optional[str]:
    """Version of a KeyVaultKey (``properties.version``, else the last segment of its id)."""
    version = getattr(getattr(key, "properties", None), "version", None)
    if version:
        return version
    return _azure_version_from_kid(getattr(key, "id", None))


def _azure_version_from_kid(kid: Optional[str]) -> Optional[str]:
    """``https://<vault>/keys/<name>/<version>`` -> ``<version>``."""
    if not kid:
        return None
    parts = kid.rstrip("/").split("/")
    return parts[-1] if len(parts) >= 2 and parts[-2] != "keys" else None


class AzureKeyVaultProvider(KMSProvider):
    """Azure Key Vault provider for HSM-backed key management.

    Requires:
        - ``pip install azure-keyvault-keys azure-identity``
        - ``KMS_AZURE_VAULT_URL`` environment variable (e.g. https://myvault.vault.azure.net/).
        - ``KMS_AZURE_KEY_NAME`` environment variable.
        - Azure credentials (DefaultAzureCredential).

    New DEKs are wrapped with the key version that was current when this
    process first used the key. The version is stored in the wrapped value
    (``azkv1:<version>:...``) because RSA-wrapped keys only unwrap under the
    version that wrapped them; after a key rotation, older DEKs are unwrapped
    with a client for their own version (one cached client per version).
    """

    def __init__(
        self,
        vault_url: Optional[str] = None,
        key_name: Optional[str] = None,
    ) -> None:
        self._vault_url = vault_url or os.environ.get("KMS_AZURE_VAULT_URL", "")
        self._key_name = key_name or os.environ.get("KMS_AZURE_KEY_NAME", "plaidify-master")
        self._credential = None
        self._key_client = None
        self._client = None
        self._client_version: Optional[str] = None
        self._version_clients: dict = {}
        if not self._vault_url:
            raise ValueError(
                "Azure Key Vault requires KMS_AZURE_VAULT_URL environment variable "
                "(e.g. https://myvault.vault.azure.net/). See docs/DEPLOYMENT.md for setup."
            )

    def _get_key_client(self):
        if self._key_client is None:
            try:
                from azure.identity import DefaultAzureCredential
                from azure.keyvault.keys import KeyClient
            except ImportError:
                raise RuntimeError(
                    "azure-keyvault-keys and azure-identity are required. "
                    "Install with: pip install azure-keyvault-keys azure-identity"
                )
            self._credential = DefaultAzureCredential()
            self._key_client = KeyClient(vault_url=self._vault_url, credential=self._credential)
        return self._key_client

    def _crypto_client(self, key):
        from azure.keyvault.keys.crypto import CryptographyClient

        return CryptographyClient(key, credential=self._credential)

    def _get_client(self):
        """Client for the key version that wraps new DEKs."""
        if self._client is None:
            key = self._get_key_client().get_key(self._key_name)
            self._client = self._crypto_client(key)
            self._client_version = _azure_key_version(key)
            if self._client_version:
                self._version_clients[self._client_version] = self._client
        return self._client

    def _client_for_version(self, version: str):
        if version not in self._version_clients:
            key = self._get_key_client().get_key(self._key_name, version)
            self._version_clients[version] = self._crypto_client(key)
        return self._version_clients[version]

    async def wrap_key(self, plaintext_key: bytes) -> str:
        return await asyncio.to_thread(self.wrap_key_sync, plaintext_key)

    async def unwrap_key(self, wrapped_key: str) -> bytes:
        return await asyncio.to_thread(self.unwrap_key_sync, wrapped_key)

    def wrap_key_sync(self, plaintext_key: bytes) -> str:
        client = self._get_client()
        from azure.keyvault.keys.crypto import KeyWrapAlgorithm

        result = client.wrap_key(KeyWrapAlgorithm.rsa_oaep_256, plaintext_key)
        blob = base64.urlsafe_b64encode(result.encrypted_key).decode("ascii")
        version = _azure_version_from_kid(getattr(result, "key_id", None)) or self._client_version
        return f"{_AZURE_WRAP_PREFIX}{version}:{blob}" if version else blob

    def unwrap_key_sync(self, wrapped_key: str) -> bytes:
        from azure.keyvault.keys.crypto import KeyWrapAlgorithm

        if wrapped_key.startswith(_AZURE_WRAP_PREFIX):
            version, blob = wrapped_key[len(_AZURE_WRAP_PREFIX) :].split(":", 1)
            client = self._client_for_version(version)
        else:  # no recorded version: only the version current at first use can unwrap it
            client, blob = self._get_client(), wrapped_key
        result = client.unwrap_key(KeyWrapAlgorithm.rsa_oaep_256, base64.urlsafe_b64decode(blob))
        return result.key

    async def generate_data_key(self) -> tuple[bytes, str]:
        dek = os.urandom(32)
        wrapped = await self.wrap_key(dek)
        return dek, wrapped

    async def rotate_master_key(self) -> str:
        return (
            f"Azure Key Vault: create a new version of key '{self._key_name}' "
            f"in vault {self._vault_url}. Old versions remain accessible for unwrapping; "
            "keep them enabled while any DEK wrapped by them is stored."
        )

    async def health_check(self) -> dict:
        try:
            await asyncio.to_thread(self._get_client)
            return {
                "provider": "azure-keyvault",
                "status": "healthy",
                "vault_url": self._vault_url,
                "key_name": self._key_name,
            }
        except Exception as e:
            return {"provider": "azure-keyvault", "status": "unhealthy", "error": str(e)}


# ── HashiCorp Vault Provider ─────────────────────────────────────────────────


class HashiCorpVaultProvider(KMSProvider):
    """HashiCorp Vault Transit secrets engine for key management.

    Requires:
        - ``pip install hvac``
        - ``KMS_VAULT_ADDR`` environment variable (Vault address).
        - ``KMS_VAULT_TOKEN`` environment variable.
        - ``KMS_VAULT_KEY_NAME`` environment variable.
    """

    def __init__(
        self,
        vault_addr: Optional[str] = None,
        token: Optional[str] = None,
        key_name: Optional[str] = None,
    ) -> None:
        self._vault_addr = vault_addr or os.environ.get("KMS_VAULT_ADDR", "http://127.0.0.1:8200")
        self._token = token or os.environ.get("KMS_VAULT_TOKEN", "")
        self._key_name = key_name or os.environ.get("KMS_VAULT_KEY_NAME", "plaidify-master")
        self._client = None
        if not self._token:
            raise ValueError(
                "HashiCorp Vault requires KMS_VAULT_TOKEN environment variable. See docs/DEPLOYMENT.md for setup."
            )

    def _get_client(self):
        if self._client is None:
            try:
                import hvac

                self._client = hvac.Client(url=self._vault_addr, token=self._token)
            except ImportError:
                raise RuntimeError("hvac is required for HashiCorp Vault. Install with: pip install hvac")
        return self._client

    async def wrap_key(self, plaintext_key: bytes) -> str:
        return await asyncio.to_thread(self.wrap_key_sync, plaintext_key)

    async def unwrap_key(self, wrapped_key: str) -> bytes:
        return await asyncio.to_thread(self.unwrap_key_sync, wrapped_key)

    def wrap_key_sync(self, plaintext_key: bytes) -> str:
        client = self._get_client()
        result = client.secrets.transit.encrypt_data(
            name=self._key_name,
            plaintext=base64.b64encode(plaintext_key).decode("ascii"),
        )
        return result["data"]["ciphertext"]

    def unwrap_key_sync(self, wrapped_key: str) -> bytes:
        client = self._get_client()
        result = client.secrets.transit.decrypt_data(
            name=self._key_name,
            ciphertext=wrapped_key,
        )
        return base64.b64decode(result["data"]["plaintext"])

    def _generate_data_key_sync(self) -> tuple[bytes, str]:
        result = self._get_client().secrets.transit.generate_data_key(
            name=self._key_name,
            key_type="plaintext",
        )
        plaintext = base64.b64decode(result["data"]["plaintext"])
        ciphertext = result["data"]["ciphertext"]
        return plaintext, ciphertext

    async def generate_data_key(self) -> tuple[bytes, str]:
        return await asyncio.to_thread(self._generate_data_key_sync)

    async def rotate_master_key(self) -> str:
        await asyncio.to_thread(lambda: self._get_client().secrets.transit.rotate_encryption_key(name=self._key_name))
        return f"Vault Transit key '{self._key_name}' rotated. Old versions retained for decryption."

    async def health_check(self) -> dict:
        try:
            client = self._get_client()
            if await asyncio.to_thread(client.is_authenticated):
                return {
                    "provider": "hashicorp-vault",
                    "status": "healthy",
                    "vault_addr": self._vault_addr,
                    "key_name": self._key_name,
                }
            return {"provider": "hashicorp-vault", "status": "unhealthy", "error": "Not authenticated"}
        except Exception as e:
            return {"provider": "hashicorp-vault", "status": "unhealthy", "error": str(e)}


# ── Provider Registry ─────────────────────────────────────────────────────────

_PROVIDERS = {
    "local": LocalKMSProvider,
    "aws": AWSKMSProvider,
    "azure": AzureKeyVaultProvider,
    "vault": HashiCorpVaultProvider,
}

_provider_instance: Optional[KMSProvider] = None


def get_kms_provider(provider_name: Optional[str] = None) -> KMSProvider:
    """Get or create the configured KMS provider.

    Resolution order:
      1. Explicit ``provider_name`` argument.
      2. ``settings.kms_provider`` (from ``KMS_PROVIDER`` env var).
      3. Legacy ``KMS_PROVIDER`` env var.
      4. ``"local"`` (software AES-256-GCM, env-var master key).

    Returns:
        A configured KMSProvider instance.
    """
    global _provider_instance
    if _provider_instance is not None and provider_name is None:
        return _provider_instance

    name = provider_name
    if name is None:
        try:
            from src.config import get_settings

            name = get_settings().kms_provider
        except Exception:
            name = os.environ.get("KMS_PROVIDER", "local")
    name = (name or "local").lower()
    cls = _PROVIDERS.get(name)
    if cls is None:
        raise ValueError(f"Unknown KMS provider: {name!r}. Available: {', '.join(_PROVIDERS.keys())}")

    instance = cls()
    if provider_name is None:
        _provider_instance = instance
    logger.info("KMS provider initialized: %s", name)
    return instance


def reset_kms_provider() -> None:
    """Reset the cached singleton. Tests use this between configuration changes."""
    global _provider_instance
    _provider_instance = None
