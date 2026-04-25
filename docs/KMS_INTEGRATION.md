# KMS Integration

Plaidify wraps every per-user Data Encryption Key (DEK) through a pluggable
Key Management Service (KMS) provider. This document covers configuration,
the supported providers, and the zero-downtime migration path from the
default env-var master key to a managed KMS.

## Providers

| `KMS_PROVIDER` | Backend | Required env / settings |
|---|---|---|
| `local` (default) | Software AES-256-GCM with master key from `ENCRYPTION_KEY`. | `ENCRYPTION_KEY` (32 random bytes, base64url). Optional `ENCRYPTION_KEY_PREVIOUS` for rotation. |
| `aws`             | AWS KMS Customer Master Key (CMK). | `KMS_KEY_ID` (CMK ARN/alias), optional `KMS_REGION` (else `AWS_DEFAULT_REGION`). Standard AWS auth (env vars / instance profile / SSO). `pip install boto3`. |
| `azure`           | Azure Key Vault key (RSA-OAEP-256 wrap). | `KMS_AZURE_VAULT_URL`, `KMS_AZURE_KEY_NAME` (or `KMS_KEY_ID`). Default Azure credential chain. `pip install azure-keyvault-keys azure-identity`. |
| `vault`           | HashiCorp Vault Transit secrets engine. | `KMS_VAULT_ADDR`, `KMS_VAULT_TOKEN`, `KMS_VAULT_KEY_NAME` (or `KMS_KEY_ID`). `pip install hvac`. |

`KMS_PROVIDER`, `KMS_KEY_ID`, and `KMS_REGION` are first-class Pydantic
settings in [`src/config.py`](../src/config.py); environment variables can
also be used.

## How it integrates

`database.wrap_dek` and `database.unwrap_dek` route every wrap/unwrap call
through `src.kms.get_kms_provider().wrap_key_sync()` / `.unwrap_key_sync()`.
Switching provider is a configuration-only change for new writes; existing
rows must be re-wrapped (see migration script below) before reads will
succeed against the new provider.

For the default `local` provider, `unwrap_dek` falls back to
`ENCRYPTION_KEY_PREVIOUS` when the current key fails to decrypt — preserving
the existing rolling-master-key rotation path. External providers (AWS /
Azure / Vault) handle versioning natively and do not consult that env-var
fallback.

## Zero-downtime migration: `local` → managed KMS

1. **Provision** the target key in your KMS. Grant the Plaidify runtime
   permission to call `kms:Encrypt` + `kms:Decrypt` (AWS) /
   `keys/wrapKey` + `keys/unwrapKey` (Azure) /
   `transit/encrypt/<key>` + `transit/decrypt/<key>` (Vault).

2. **Pause writes** (or run during a maintenance window) so user DEKs
   are not being created in the source provider while you re-wrap.

3. **Run the re-wrap script** with `KMS_PROVIDER` still set to the
   source value:

   ```bash
   SOURCE_KMS_PROVIDER=local \
   TARGET_KMS_PROVIDER=aws \
   KMS_KEY_ID=arn:aws:kms:us-east-1:111111111111:key/abcd-... \
   KMS_REGION=us-east-1 \
   python -m scripts.migrate_to_kms
   ```

   The script iterates `users.encrypted_dek`, unwraps with the source,
   re-wraps with the target, and commits per user. Failures are logged
   and reported in the exit code (0 = clean, 1 = at least one failure,
   2 = bad arguments).

4. **Flip configuration** to the target provider:

   ```bash
   KMS_PROVIDER=aws
   KMS_KEY_ID=arn:aws:kms:us-east-1:...
   ```

5. **Restart the application**. New writes go to the target. Existing
   reads succeed because every DEK row now contains a target-wrapped
   envelope.

### Roll-back

If the target provider misbehaves, run the script with `SOURCE` /
`TARGET` reversed *before* restarting on the new configuration. The
local provider also retains the old `ENCRYPTION_KEY` until you rotate
it, so a same-key roll-back to local is a no-op on the data plane.

## Health

`get_kms_provider().health_check()` returns a per-provider status dict
useful for `/healthz` integrations:

```json
{ "provider": "aws-kms", "status": "healthy", "key_state": "Enabled", "key_id": "arn:aws:kms:..." }
```
