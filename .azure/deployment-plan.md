# Azure Deployment Plan

Status: In Progress

## 1. Project Overview
- Project: Plaidify
- Workspace root: /Users/meetpandya/Documents/Plaidify/plaidify
- Current state: FastAPI backend with Playwright browser automation, PostgreSQL, Redis, Dockerized deployment, production hardening in progress.
- Deployment goal: Production-ready Azure hosting with managed infrastructure, secure secrets handling, observability, and a public-repo-safe deployment workflow.

## 2. Mode
- Mode: MODIFY / MODERNIZE existing application for Azure production deployment.

## 3. Public Repo Guardrails
- Do not commit Azure tenant IDs, subscription IDs, account identifiers, publish profiles, or any environment-specific Azure context to this repository.
- Do not commit secret-bearing parameter files, populated `.env` files, service principal credentials, or generated deployment outputs.
- Keep runtime secrets in GitHub environment secrets and Azure Key Vault.
- Use GitHub OIDC for Azure authentication instead of storing Azure credentials in the repository.
- Keep any local Azure parameter overrides in ignored files only.

## 4. Application Analysis
- Backend: FastAPI / Gunicorn / Python
- Runtime pattern: Containerized web API with Playwright browser automation
- State dependencies: PostgreSQL, Redis
- Security dependencies: JWT, encryption key management, webhook secrets, rate limiting
- Ops dependencies: health checks, Prometheus metrics, structured logs

## 5. Recommended Azure Architecture
- App hosting: Azure Container Apps (public web API + private access-executor worker)
- Container registry: Azure Container Registry
- Database: Azure Database for PostgreSQL Flexible Server
- Cache/shared state: Azure Cache for Redis
- Secrets and key material: Azure Key Vault
- Observability: Log Analytics
- Security model: Managed identity for ACR pulls and Key Vault secret access

## 6. Why This Architecture
- Container Apps fits the existing Dockerized FastAPI service without a platform rewrite.
- Managed PostgreSQL and Redis match the app’s production requirements and avoid self-managed infrastructure.
- Key Vault is the correct home for `ENCRYPTION_KEY`, `JWT_SECRET_KEY`, and runtime connection strings.
- Splitting the public API from the access executor closes the detached job restart gap for production deployments.
- A manual GitHub Actions workflow keeps Azure deployment explicit and avoids accidental pushes from a public repository.

## 7. Tracked Azure Artifacts
- `infra/main.bicep`
- `infra/main.bicepparam`
- `.github/workflows/deploy-azure.yml`
- `docs/AZURE_DEPLOYMENT.md`

## 8. Required Inputs
- GitHub environment name holding Azure secrets/variables
- Azure resource group name
- Azure region
- Public CORS origins
- Preferred domain / TLS approach if already known
- Production traffic and scaling expectations

## 8a. Azure Context Handling
- Live Azure account context is intentionally excluded from tracked files in this public repository.
- The deployment workflow expects Azure authentication and environment-specific values to come from GitHub environment secrets and variables.
- Local experimentation can use ignored `infra/*.local.bicepparam` or similar untracked files.

## 9. Execution Plan
1. Sanitize tracked Azure planning artifacts for public visibility.
2. Generate Bicep infrastructure with no committed secret values.
3. Add a manual GitHub Actions Azure deployment workflow using OIDC.
4. Deploy separate web and access-executor Container Apps backed by shared Redis and Key Vault secrets.
5. Run database migrations through a dedicated manual-trigger Container Apps Job during deployment.
6. Populate Key Vault secrets at deploy time from GitHub environment secrets.
7. Document deployment expectations, including the executor and migration topology.
8. Validate infrastructure definitions before first Azure deployment.

## 10. Risks / Open Items
- Playwright runtime dependencies inside Azure Container Apps still need explicit runtime validation in Azure.
- Redis remains mandatory in production for shared state; degraded in-memory mode should not be used.
- PostgreSQL is initially exposed via Azure-managed public access rules for simplicity; private networking can be layered in later.
- Executor process crash recovery depends on Redis stream reclaim plus MFA session persistence; this path is now implemented but still needs real Azure runtime exercise.

## 11. Approval Gate
- Public-safe Azure artifacts may be committed.
- Environment-specific values must remain outside git in GitHub environment secrets, Key Vault, or ignored local files.
