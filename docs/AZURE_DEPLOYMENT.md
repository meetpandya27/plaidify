# Plaidify Azure Deployment

This Azure deployment path is designed for a public repository. The committed files describe infrastructure and deployment mechanics, but they do not contain live Azure identifiers, tenant details, secrets, or environment-specific connection strings.

The current Azure path targets Azure Container Apps with a split control-plane and worker runtime:

- one public Container App for the Plaidify API
- one private Container App for the detached access-job executor
- one manual-trigger Container Apps Job for Alembic migrations

The infrastructure is defined in `infra/main.bicep`, parameter defaults live in `infra/main.bicepparam`, and `.github/workflows/deploy-azure.yml` orchestrates the deployment.

## What Is Tracked

- `infra/main.bicep`
- `infra/main.bicepparam`
- `.github/workflows/deploy-azure.yml`
- This document

## What Gets Provisioned

The Bicep template provisions these Azure resources at resource-group scope:

| Resource | Purpose | Default shape |
| --- | --- | --- |
| Azure Container Registry | Stores the Plaidify image built by the workflow | Basic SKU |
| Azure Key Vault | Holds runtime secrets referenced by Container Apps | RBAC-enabled, purge protection on |
| Azure Database for PostgreSQL Flexible Server | Primary relational store | PostgreSQL 16, Burstable `B_Standard_B1ms`, 32 GiB storage |
| Azure Cache for Redis | Shared state for detached jobs and runtime coordination | Basic C0, TLS-only |
| Log Analytics workspace | Container Apps logs and environment diagnostics | 30-day retention |
| User-assigned managed identity | Pulls from ACR and reads secrets from Key Vault | Shared by API, worker, and migration job |
| Azure Container Apps environment | Shared execution environment for the app, worker, and migration job | Log Analytics wired |
| Public Container App | Runs the Plaidify HTTP API | External ingress on port 8000, `/health` probes |
| Private Container App | Runs `python -m src.access_job_worker` | No external ingress |
| Container Apps Job | Runs `alembic upgrade head` during deployment | Manual trigger |

## What Must Stay Out Of Git

- Azure tenant IDs, subscription IDs, and service principal credentials
- Populated `.env` files
- Secret-bearing Bicep parameter files
- Publish profiles or deployment output files
- Runtime secrets such as `ENCRYPTION_KEY`, `JWT_SECRET_KEY`, `DATABASE_URL`, `REDIS_URL`, and provider API keys

## Authentication Model

The workflow uses GitHub OIDC with `azure/login`. That means Azure access is granted to the workflow through a federated identity, not through committed credentials.

Store the following as GitHub environment secrets:

- `AZURE_CLIENT_ID`
- `AZURE_TENANT_ID`
- `AZURE_SUBSCRIPTION_ID`
- `AZURE_POSTGRES_ADMIN_LOGIN`
- `AZURE_POSTGRES_ADMIN_PASSWORD`
- `ENCRYPTION_KEY`
- `JWT_SECRET_KEY`
- `LLM_API_KEY` (optional)
- `HEALTH_CHECK_TOKEN` (optional, only if you want bearer-token access to `GET /health/detailed` in addition to existing authenticated access)

Store the following as GitHub environment variables:

- `AZURE_RESOURCE_GROUP`
- `AZURE_LOCATION`
- `AZURE_NAME_PREFIX`
- `AZURE_APP_ENV`
- `AZURE_CORS_ORIGINS`
- `AZURE_LLM_PROVIDER`
- `AZURE_LLM_MODEL` (optional)

`AZURE_NAME_PREFIX` and the selected GitHub environment are combined with a deterministic suffix in `infra/main.bicep` to derive the final Azure resource names.

## Runtime Wiring

The Azure deployment uses Key Vault secret references rather than baking secrets into the image or the Bicep file.

Base runtime secrets wired into the Container Apps environment:

- `DATABASE_URL`
- `REDIS_URL`
- `ENCRYPTION_KEY`
- `JWT_SECRET_KEY`

Optional runtime secrets:

- `LLM_API_KEY`
- `HEALTH_CHECK_TOKEN`

Base runtime environment variables pushed by the template include:

- `APP_NAME`
- `APP_VERSION`
- `ENV`
- `LOG_LEVEL`
- `LOG_FORMAT`
- `CORS_ORIGINS`
- `ENFORCE_HTTPS`
- `LLM_PROVIDER`

The public web app is additionally forced into `ACCESS_JOB_EXECUTION_MODE=redis-worker`, and the private executor app receives both `ACCESS_JOB_EXECUTION_MODE=redis-worker` and `ACCESS_JOB_WORKER_CONCURRENCY`.

## Resource Access Model

The shared user-assigned managed identity is granted:

- `AcrPull` on the Azure Container Registry
- `Key Vault Secrets User` on the Key Vault

That identity is attached to the API app, the access-executor app, and the migration job, so all three runtimes pull the same image and resolve the same Key Vault-backed secrets.

## Deployment Flow

1. Run the manual `Deploy Azure` workflow.
2. The workflow signs into Azure using OIDC.
3. A first Bicep deployment creates shared infrastructure only: ACR, Key Vault, PostgreSQL, Redis, Log Analytics, managed identity, and Container Apps environment.
4. The workflow writes runtime secrets into Key Vault from GitHub environment secrets.
5. The workflow builds the application image and pushes it to Azure Container Registry.
6. A second Bicep deployment creates or updates two Container Apps using managed identity and Key Vault secret references:
	- the public Plaidify web API
	- the private `access-executor` worker that consumes detached access jobs from Redis
7. The workflow starts a manual-trigger Container Apps Job that runs `alembic upgrade head` against the production database and waits for it to succeed.
8. The workflow runs a public health check against `/health` and also verifies the executor app reports a ready revision.

The current workflow does not require `HEALTH_CHECK_TOKEN` because it probes the public `/health` endpoint. Set that secret only if you also want bearer-token access to `/health/detailed` for private diagnostics.

## Container App Defaults

The default deployment posture from `infra/main.bicep` is:

- public API app: `0.5` CPU, `1Gi` memory, min replicas `1`, max replicas `3`
- access executor: `0.5` CPU, `1Gi` memory, min replicas `1`, max replicas `1`
- migration job: `0.5` CPU, `1Gi` memory, 30-minute replica timeout
- external ingress is enabled only for the public API app
- liveness and readiness probes both hit `/health`

These values are parameterized in the Bicep template, so they can be overridden without changing application code.

## Local Overrides

If you need environment-specific overrides while developing the infrastructure locally, use ignored files such as:

- `infra/main.local.bicepparam`
- `infra/main.override.bicepparam`

Those patterns are intentionally excluded by `.gitignore`.

## Secret Handling

The Bicep file does not carry committed application secrets. Instead:

- PostgreSQL admin credentials are passed in at deploy time.
- The workflow constructs `DATABASE_URL` and `REDIS_URL` at deploy time.
- Key Vault stores all runtime secrets used by the Container App.
- Both Container Apps read those secrets through the shared managed identity.

The workflow also derives `DATABASE_URL` and `REDIS_URL` from the Azure resource outputs rather than expecting those full connection strings to exist in GitHub secrets.

## Detached Access Jobs

Detached `/connect` flows are productionized in Azure by splitting execution across two Container Apps:

- The public web app accepts API requests, creates access jobs, and enqueues detached work into Redis.
- The `access-executor` app runs `python -m src.access_job_worker` and performs the browser automation.

That closes the web-process restart gap for detached jobs in Azure the same way the production Docker Compose stack now does.

## Migrations

The Azure workflow now runs migrations automatically through a dedicated manual-trigger Container Apps Job before the final application health check.

That keeps schema changes out of web container startup while still ensuring the release path applies Alembic revisions before the deployment is considered healthy.

## Deployment Outputs

The bootstrap deployment exports values consumed by later workflow steps, including:

- ACR name and login server
- Key Vault name and URI
- PostgreSQL server FQDN and database name
- Redis host name and SSL port
- Container App names for the API and access executor
- Migration job name

Those outputs are what let the workflow stay mostly declarative without hardcoding Azure resource names into the GitHub Actions file.
