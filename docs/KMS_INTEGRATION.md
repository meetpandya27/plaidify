# KMS Integration

Plaidify wraps every per-user Data Encryption Key (DEK) through a pluggable
Key Management Service (KMS) provider. This document covers configuration,
the supported providers, and the migration path from the default env-var
master key to a managed KMS.

## Providers

| `KMS_PROVIDER` | Backend | Settings |
|---|---|---|
| `local` (default) | Software AES-256-GCM with the master key from `ENCRYPTION_KEY`. | `ENCRYPTION_KEY` (32 random bytes, base64url); `ENCRYPTION_KEY_PREVIOUS` during rotation. |
| `aws`             | AWS KMS key (Encrypt/Decrypt). | `KMS_KEY_ID` (key ARN or alias; `KMS_AWS_KEY_ID` is read when it is unset), `KMS_REGION` (else `AWS_DEFAULT_REGION`, else `us-east-1`). Standard AWS credentials (env vars, instance profile, SSO). |
| `azure`           | Azure Key Vault key (RSA-OAEP-256 wrap). | `KMS_AZURE_VAULT_URL`, `KMS_AZURE_KEY_NAME` (default `plaidify-master`). The default Azure credential chain. `KMS_KEY_ID` is not read. |
| `vault`           | HashiCorp Vault Transit secrets engine. | `KMS_VAULT_ADDR` (default `http://127.0.0.1:8200`), `KMS_VAULT_TOKEN`, `KMS_VAULT_KEY_NAME` (default `plaidify-master`). `KMS_KEY_ID` is not read. |

### Installing the provider SDKs

The cloud SDKs (`boto3`, `azure-keyvault-keys` + `azure-identity`, `hvac`) are
not in the default image. Either:

- build the image variant that includes them, from the hash-locked
  `requirements-kms.lock`:

  ```bash
  docker build --build-arg INSTALL_KMS=true -t plaidify:kms .
  ```

- or, outside Docker, install the same lock after the app's (as the image does):

  ```bash
  pip install --require-hashes -r requirements.lock
  pip install --require-hashes -r requirements-kms.lock
  ```

## How it integrates

`database.wrap_dek` and `database.unwrap_dek` route every wrap/unwrap call
through `src.kms.get_kms_provider().wrap_key_sync()` / `.unwrap_key_sync()`.
Switching provider is a configuration-only change for new writes; existing
rows must be re-wrapped (see the migration below) before reads will succeed
against the new provider.

For the default `local` provider, `unwrap_dek` falls back to
`ENCRYPTION_KEY_PREVIOUS` when the current key fails to decrypt — the
master-key rotation path ([SECURITY.md](../SECURITY.md#key-rotation-procedure)).
External providers (AWS / Azure / Vault) handle key versions themselves and do
not consult that fallback; keep old key versions enabled.

`ENCRYPTION_KEY` stays required with every provider: it still encrypts access
jobs queued in Redis and decrypts rows written before the migration, and the
audit-chain key is derived from it unless `AUDIT_HMAC_KEY` is set.

## Migration: `local` → managed KMS

1. **Provision** the target key in your KMS. Grant the Plaidify runtime
   permission to call `kms:Encrypt` + `kms:Decrypt` (AWS) /
   `keys/wrapKey` + `keys/unwrapKey` (Azure) /
   `transit/encrypt/<key>` + `transit/decrypt/<key>` (Vault).

2. **Pause writes** (or run during a maintenance window) so user DEKs
   are not being created in the source provider while you re-wrap.

3. **Run the migration script** from a checkout with the server and KMS
   dependencies installed, with `KMS_PROVIDER` still set to the source and the
   target provider's settings in the environment:

   ```bash
   SOURCE_KMS_PROVIDER=local \
   TARGET_KMS_PROVIDER=aws \
   KMS_KEY_ID=arn:aws:kms:us-east-1:111111111111:key/abcd-... \
   KMS_REGION=us-east-1 \
   python -m scripts.migrate_to_kms
   ```

   It re-wraps every user's DEK under the target (creating one for users
   without a DEK), moves credentials and webhook secrets still encrypted
   directly under `ENCRYPTION_KEY` under their owner's DEK, and commits in
   batches. Rows already under the target are left alone, so it is safe to
   re-run. Exit status: 0 when everything moved, 1 when any row was skipped or
   failed (see the log, fix, re-run), 2 on a configuration error.

4. **Flip configuration** to the target provider:

   ```bash
   KMS_PROVIDER=aws
   KMS_KEY_ID=arn:aws:kms:us-east-1:...
   ```

5. **Restart the application** (API and executor). New writes go to the
   target, and existing reads succeed because every DEK row now holds a
   target-wrapped envelope.

The image does not ship `scripts/migrate_to_kms.py`, so run the migration
from a checkout of the deployed commit with the production environment, not
inside the API container.

### Roll-back

If the target provider misbehaves, run the script with `SOURCE` /
`TARGET` reversed *before* restarting on the new configuration. The
local provider also retains the old `ENCRYPTION_KEY` until you rotate
it, so a same-key roll-back to local is a no-op on the data plane.

## Health

`GET /health/detailed` includes a `kms` check from
`get_kms_provider().health_check()`: the local provider round-trips a test
key, AWS reads the key's state, Azure opens its key client, and Vault checks
its token. It reports `ok` or the provider's status; a failure or a timeout
marks the service `degraded` (503). The provider-level result looks like:

```json
{ "provider": "aws-kms", "status": "healthy", "key_state": "Enabled", "key_id": "arn:aws:kms:..." }
```
