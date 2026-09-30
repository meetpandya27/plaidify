# Plaidify Azure Deployment

The template and workflow have not been deployed to a real subscription yet; treat the first deployment as a test.

This Azure deployment path is designed for a public repository. The committed files describe infrastructure and deployment mechanics, but they do not contain live Azure identifiers, tenant details, secrets, or environment-specific connection strings.

The current Azure path targets Azure Container Apps with a split control-plane and worker runtime:

- one public Container App for the Plaidify API
- one private Container App for the detached access-job executor
- one manual-trigger Container Apps Job for Alembic migrations

The infrastructure is defined in `infra/main.bicep` (compiled to `infra/main.json`, which CI checks is current), parameter defaults live in `infra/main.bicepparam`, and `.github/workflows/deploy-azure.yml` orchestrates the deployment. First-time setup of the identity, roles and GitHub environment: [SELF_HOST.md](SELF_HOST.md).

## What Is Tracked

- `infra/main.bicep` and its compiled `infra/main.json`
- `infra/main.bicepparam`
- `.github/workflows/deploy-azure.yml`
- This document

## What Gets Provisioned

The Bicep template provisions these Azure resources at resource-group scope:

| Resource | Purpose | Default shape |
| --- | --- | --- |
| Virtual network | Private network for the apps and the data tier | `10.40.0.0/16`: Container Apps `/23`, PostgreSQL `/28` (delegated), private endpoints `/27` |
| Azure Container Registry | Stores the Plaidify image built by the workflow | Basic SKU |
| Azure Key Vault | Holds runtime secrets referenced by Container Apps | RBAC-enabled, purge protection on |
| Azure Database for PostgreSQL Flexible Server | Primary relational store | PostgreSQL 16, Burstable `B_Standard_B1ms`, 32 GiB storage, **VNet-integrated, no public access** |
| Azure Cache for Redis | Access-job queue, link sessions, MFA state, RSA keys, rate limits | **Standard C1** (primary + replica), TLS-only, **private endpoint, no public access** |
| Private DNS zones | Resolve PostgreSQL and Redis to their private addresses inside the VNet | linked to the VNet |
| Log Analytics workspace | Container Apps logs and environment diagnostics | 30-day retention |
| User-assigned managed identity | Pulls from ACR and reads secrets from Key Vault | Shared by API, worker, and migration job |
| Azure Container Apps environment | Shared execution environment, injected into the VNet | Workload profiles (Consumption) |
| Public Container App | Runs the Plaidify HTTP API | External ingress on port 8000, `/health` startup/liveness/readiness probes |
| Private Container App | Runs `python -m src.access_job_worker` | No ingress; `/health` probes on its own port 9101 |
| Container Apps Job | Runs `alembic upgrade head`, then refreshes the application database role | Manual trigger |

## What Must Stay Out Of Git

- Azure tenant IDs, subscription IDs, and service principal credentials
- Populated `.env` files
- Secret-bearing Bicep parameter files
- Publish profiles or deployment output files (`bootstrap.json` and `app-deploy.json` are git-ignored, as is `.azure/` apart from the tracked plan)
- Runtime secrets such as `ENCRYPTION_KEY`, `JWT_SECRET_KEY`, `DATABASE_URL`, `REDIS_URL`, and provider API keys

## Authentication Model

The workflow uses GitHub OIDC with `azure/login`. That means Azure access is granted to the workflow through a federated identity, not through committed credentials.

The deployment identity has rights on the target resource group only (see [SELF_HOST.md](SELF_HOST.md), step 2):

- **Contributor** on the resource group;
- **Role Based Access Control Administrator** on the resource group, with a condition that lets it assign and remove only AcrPull, Key Vault Secrets User and Key Vault Secrets Officer;
- **Key Vault Secrets Officer** on the vault, granted by the template itself (`deployPrincipalObjectId`, which the workflow reads from its own token), so it can write the runtime secrets. Without a data-plane role an RBAC-mode vault refuses those writes, whatever the identity's other roles.

It has no subscription-level role. The resource group and the resource-provider registrations are created once by the subscription owner.

Store the following as GitHub environment secrets:

- `AZURE_CLIENT_ID`
- `AZURE_TENANT_ID`
- `AZURE_SUBSCRIPTION_ID`
- `AZURE_POSTGRES_ADMIN_LOGIN`
- `AZURE_POSTGRES_ADMIN_PASSWORD`
- `ENCRYPTION_KEY`
- `JWT_SECRET_KEY`
- `LLM_API_KEY` (optional)
- `HEALTH_CHECK_TOKEN` (optional, but without it `GET /health/detailed` answers 404 in production)
- `SMTP_PASSWORD` (optional: the SMTP login's password)
- `BOOTSTRAP_USER_PASSWORD` (optional: the first administrator's password, with the two bootstrap variables below)

Store the following as GitHub environment variables:

- `AZURE_RESOURCE_GROUP`
- `AZURE_LOCATION`
- `AZURE_NAME_PREFIX`
- `AZURE_APP_ENV`
- `AZURE_CORS_ORIGINS`
- `AZURE_LLM_PROVIDER`
- `AZURE_LLM_MODEL` (optional)
- `AZURE_SMTP_HOST`, `AZURE_SMTP_FROM` (optional, together: password-reset mail), `AZURE_SMTP_PORT` (default 587), `AZURE_SMTP_USERNAME`, `AZURE_PASSWORD_RESET_URL`
- `AZURE_BOOTSTRAP_USER_USERNAME`, `AZURE_BOOTSTRAP_USER_EMAIL` (optional, with `BOOTSTRAP_USER_PASSWORD`: the first administrator)
- `AZURE_AUDIT_HMAC_KEY_ROTATING` (`true` only while rotating the audit key: [RUNBOOK.md](RUNBOOK.md#audit-key-audit_hmac_key))

The workflow refuses a partial bootstrap (all three values or none) and SMTP settings without both `AZURE_SMTP_HOST` and `AZURE_SMTP_FROM`.

`AZURE_NAME_PREFIX` and the selected GitHub environment are combined with a deterministic suffix in `infra/main.bicep` to derive the final Azure resource names.

## Runtime Wiring

The Azure deployment uses Key Vault secret references rather than baking secrets into the image or the Bicep file.

| Key Vault secret | Used by | Contents |
| --- | --- | --- |
| `database-url` | API, executor | The **application role** (`appDatabaseRole`, default `plaidify_app`): data access only, no DDL |
| `database-admin-url` | migration job | The server admin, for schema changes |
| `database-app-password` | migration job | The application role's password (generated once by the workflow, then kept) |
| `redis-url` | API, executor | `rediss://` to the private endpoint |
| `encryption-key`, `jwt-secret-key` | all three | Application secrets |
| `audit-hmac-key` | API, executor | `AUDIT_HMAC_KEY`, which signs the audit chain (both processes append to it). Generated once by the workflow, then kept |
| `link-launch-secret`, `metrics-token` | API | `LINK_LAUNCH_SECRET` (signs hosted-link launch tokens) and `METRICS_TOKEN` (bearer token for `/metrics`). Generated once, then kept |
| `llm-api-key`, `health-check-token` | API, executor | Optional |
| `smtp-password`, `bootstrap-user-password` | API | Optional, from the GitHub secrets of the same purpose |
| `audit-hmac-key-previous` | API, executor | Passed only while `AZURE_AUDIT_HMAC_KEY_ROTATING` is `true` |

A missing generated secret is created; one that exists is never replaced, and a Key Vault read that fails for any reason other than "not found" fails the deployment rather than generate a new key. Read a value with `az keyvault secret show --vault-name <vault> --name metrics-token --query value -o tsv` (for example to configure a scraper). Registration stays off (the production default), so the first account comes from the bootstrap values; once that administrator has signed in and changed the password, remove the three values and the template stops passing them.

Base runtime environment variables pushed by the template include `APP_NAME`, `APP_VERSION`, `ENV`, `LOG_LEVEL`, `LOG_FORMAT`, `CORS_ORIGINS`, `ENFORCE_HTTPS`, `LLM_PROVIDER`, `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `ACCESS_JOB_EXECUTION_MODE=redis-worker`, `BROWSER_CHROMIUM_SANDBOX` and `AUDIT_HMAC_KEY`. The API additionally gets `GUNICORN_WORKERS`, `FORWARDED_ALLOW_IPS`, `LINK_LAUNCH_SECRET`, `METRICS_TOKEN` and, when configured, `SMTP_*`, `PASSWORD_RESET_URL` and `BOOTSTRAP_USER_*`; the executor gets `ACCESS_JOB_WORKER_CONCURRENCY` and `ACCESS_WORKER_METRICS_PORT=9101`.

### Proxy trust (`FORWARDED_ALLOW_IPS`)

The Container Apps ingress terminates TLS and forwards plain HTTP with `X-Forwarded-For` / `X-Forwarded-Proto`. The API only honours those headers from the peers in `FORWARDED_ALLOW_IPS` (parameter `forwardedAllowIps`, default: the private and carrier-grade-NAT ranges the environment's proxies use). Without it every request looks like plain HTTP, so with `ENFORCE_HTTPS` the API redirects each one to itself, and every client shares the ingress' address for rate limiting. Clients reach the app only through the ingress; the other peers on those private ranges are this deployment's own executor and migration job, so keep other workloads out of the VNet (or narrow `forwardedAllowIps` to the Container Apps subnet).

### Probes

- API: startup, liveness and readiness probes on `GET /health`, port 8000. The probes connect directly over plain HTTP; `/health` answers them with 200, never a redirect.
- Executor: startup and liveness probes on `GET /health`, port 9101 — the worker's own endpoint, which answers 503 once the worker's event loop stops running, so a hung worker is restarted. The parameter `accessExecutorProbes` turns them off for an image whose worker doesn't serve that endpoint.
- The image runs `tini` as PID 1 (Container Apps has no init option), so signals reach the processes and exited Chromium processes are reaped. Container `command` is never overridden; roles are chosen with `args`.

### Database connection budget

Every process has its own SQLAlchemy pool. With the defaults (`maxReplicas=3`, `gunicornWorkers=2`, `dbPoolSize=2`, `dbMaxOverflow=2`, one executor, the migration job) the peak is 3 × 2 × 4 + 4 + 2 = 30 connections, inside what a Burstable B1ms server (`max_connections` 50, some reserved) leaves for applications. Raise the pool, workers or replicas together with the PostgreSQL SKU.

## Resource Access Model

The shared user-assigned managed identity is granted:

- `AcrPull` on the Azure Container Registry
- `Key Vault Secrets User` on the Key Vault

That identity is attached to the API app, the access-executor app, and the migration job, so all three runtimes pull the same image and resolve the Key Vault-backed secrets they are given (the migration job alone gets the admin connection string).

Network access:

- PostgreSQL has no public endpoint and no firewall rules; it is reachable only from the VNet.
- Redis has `publicNetworkAccess: Disabled` and a private endpoint in the VNet.
- The Key Vault stays reachable over its public endpoint (the workflow writes secrets from a GitHub runner); every request is authorized by Entra ID RBAC.
- ACR stays public for the workflow's image push; pulls use the managed identity.

## Deployment Flow

1. Run the manual `Deploy Azure` workflow from `main` (it refuses any other ref, and any commit whose CI run did not pass).
2. The job waits for the environment's required reviewers, then signs into Azure using OIDC.
3. It checks the resource group exists and reads its own object ID.
4. A first Bicep deployment creates or updates the infrastructure only (network, data tier, Key Vault, registry, Container Apps environment) and grants the workflow Key Vault Secrets Officer on the vault.
5. The workflow writes the runtime secrets into Key Vault, retrying while a fresh role assignment propagates.
6. It builds the application image and pushes it to Azure Container Registry, tagged with the commit SHA.
7. A second Bicep deployment points the migration job at the new image; **the running apps are not touched**.
8. The migration job runs `alembic upgrade head` as the server admin, then creates or updates the application role and its grants. If it fails, the deployment stops and the old revisions keep serving.
9. A third Bicep deployment creates the new API and executor revisions with the new image.
10. The workflow waits until both apps' latest revision is ready and runs the new image, then requires exactly `200` from `https://<app>/health` through the public ingress; a redirect there means the proxy trust is wrong and fails the deployment.

## Container App Defaults

The default deployment posture from `infra/main.bicep` is:

- public API app: `0.5` CPU, `1Gi` memory, min replicas `1`, max replicas `3`, 2 gunicorn workers
- access executor: `0.5` CPU, `1Gi` memory, min replicas `1`, max replicas `1`
- migration job: `0.5` CPU, `1Gi` memory, 30-minute replica timeout
- external ingress is enabled only for the public API app
- termination grace period 45 seconds (gunicorn's graceful timeout is 30)

These values are parameterized in the Bicep template, so they can be overridden without changing application code.

### Chromium sandbox

`browserChromiumSandbox` (default `true`) sets `BROWSER_CHROMIUM_SANDBOX`. Chromium's sandbox needs user namespaces; Container Apps doesn't let you apply a custom seccomp profile. If the executor logs `No usable sandbox` on your environment, set the parameter to `false` — the trade-off being that the container boundary is then the only isolation between a malicious page's renderer and a process that holds `ENCRYPTION_KEY` and the database credentials — and consider running the executor on a dedicated workload profile.

## Alerts

The template creates no alert rules, and nothing scrapes Prometheus metrics:
until you add alerts, nothing notifies you. Container logs go to the Log
Analytics workspace. A minimum set:

- An availability check of `https://<app>/health` from outside (Azure Monitor
  availability test or any uptime monitor).
- Metric alerts on both Container Apps (restart count, replica count at zero)
  and on the PostgreSQL server (CPU, storage, connections) and Redis (server
  load, memory).
- Log-search alerts in Log Analytics on the API's and the executor's
  error-level lines, and on `Refresh token reuse detected` and
  `Redis is unreachable for access locks`.

The Prometheus rules in [monitoring/alert_rules.yml](../monitoring/alert_rules.yml)
need a scraper that can reach the API's `/metrics` (set `METRICS_TOKEN` first)
and the executor's port 9101, which has no ingress in this template.

## Upgrading An Existing Deployment

If you deployed an earlier version of this template (public PostgreSQL and Redis, no VNet), some changes cannot be applied in place:

- **Container Apps environment:** VNet injection can only be set when an environment is created. Delete the old environment (after deleting its apps and job) or deploy with a new `environmentName`.
- **PostgreSQL:** a server created with public access can't be switched to VNet integration. Create the new server (a new `environmentName`, or delete the old server after taking a dump), then restore your data into it ([DISASTER_RECOVERY.md](DISASTER_RECOVERY.md)).
- **Redis:** Basic → Standard and public → private endpoint are in-place changes; expect a brief reconnect.
- **Key Vault names** are deterministic, and a deleted vault keeps its name for the soft-delete retention period (90 days by default; purge protection is on, so it can't be purged). To start over, recover the vault (`az keyvault recover --name <name>`) or set `keyVaultGeneration` (e.g. `'2'`) to create a new name.

## Local Overrides

If you need environment-specific overrides while developing the infrastructure locally, use ignored files such as:

- `infra/main.local.bicepparam`
- `infra/main.override.bicepparam`

Those patterns are intentionally excluded by `.gitignore`. After editing `infra/main.bicep`, regenerate the compiled template: `az bicep build --file infra/main.bicep --outfile infra/main.json`.

## Secret Handling

The Bicep file does not carry committed application secrets. Instead:

- PostgreSQL admin credentials are passed in at deploy time and used only by the migration job.
- The workflow constructs the connection strings at deploy time from the Azure resource outputs and generates the application role's password once.
- Key Vault stores all runtime secrets used by the Container Apps; the apps and the job read them through the shared managed identity.

## Detached Access Jobs

Detached `/connect` flows are productionized in Azure by splitting execution across two Container Apps:

- The public web app accepts API requests, creates access jobs, and enqueues detached work into Redis.
- The `access-executor` app runs `python -m src.access_job_worker` and performs the browser automation.

That closes the web-process restart gap for detached jobs in Azure the same way the production Docker Compose stack does. Queued jobs, link sessions and MFA state live in Redis, which is why the template defaults to the Standard tier (replicated) instead of Basic.

## Migrations

Migrations run through a dedicated manual-trigger Container Apps Job **before** any new API or executor revision exists, so new code never runs against an old schema, and a failed migration leaves the previous release serving. The job connects as the server admin; after `alembic upgrade head` it runs `scripts/provision_app_db_role.py`, which (re)grants the application role data access to every table, including ones created by later migrations.

## Deployment Outputs

The bootstrap deployment exports values consumed by later workflow steps, including:

- ACR name and login server
- Key Vault name and URI
- PostgreSQL server FQDN, database name and application role
- Redis host name and SSL port
- Container App names for the API and access executor
- Migration job name

Those outputs are what let the workflow stay mostly declarative without hardcoding Azure resource names into the GitHub Actions file.
