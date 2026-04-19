# Plaidify Azure Deployment

This Azure deployment path is designed for a public repository. The committed files describe infrastructure and deployment mechanics, but they do not contain live Azure identifiers, tenant details, secrets, or environment-specific connection strings.

## What Is Tracked

- `infra/main.bicep`
- `infra/main.bicepparam`
- `.github/workflows/deploy-azure.yml`
- This document

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
- `HEALTH_CHECK_TOKEN` (optional)

Store the following as GitHub environment variables:

- `AZURE_RESOURCE_GROUP`
- `AZURE_LOCATION`
- `AZURE_NAME_PREFIX`
- `AZURE_APP_ENV`
- `AZURE_CORS_ORIGINS`
- `AZURE_LLM_PROVIDER`
- `AZURE_LLM_MODEL` (optional)

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

## Detached Access Jobs

Detached `/connect` flows are productionized in Azure by splitting execution across two Container Apps:

- The public web app accepts API requests, creates access jobs, and enqueues detached work into Redis.
- The `access-executor` app runs `python -m src.access_job_worker` and performs the browser automation.

That closes the web-process restart gap for detached jobs in Azure the same way the production Docker Compose stack now does.

## Migrations

The Azure workflow now runs migrations automatically through a dedicated manual-trigger Container Apps Job before the final application health check.

That keeps schema changes out of web container startup while still ensuring the release path applies Alembic revisions before the deployment is considered healthy.