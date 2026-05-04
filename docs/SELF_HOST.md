# Self-Hosting Plaidify on Azure

This guide takes you from a fresh fork of the public repo to a running Plaidify deployment in your own Azure subscription. Nothing you configure here lands in the public repository — all secrets and tenant-specific values live in **your** GitHub Environment store and **your** Azure Key Vault.

> **Public-repo guarantee.** The repo contains only generic code, generic Bicep templates, and a generic CI/CD workflow. There are no embedded subscription IDs, hostnames, encryption keys, or passwords. Each self-hoster supplies their own values via GitHub Environment secrets and variables.

---

## Prerequisites

- An Azure subscription where you have **Owner** (or Contributor + User Access Administrator) rights.
- The [`gh` CLI](https://cli.github.com/) and [`az` CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) installed and signed in.
- A fork of `meetpandya27/plaidify` (or admin access to your own copy of the repo).

---

## 1. Fork (or clone) the repo

```bash
gh repo fork meetpandya27/plaidify --clone
cd plaidify
```

If you already cloned, just make sure you have admin access — you need to be able to set repo secrets and environments.

---

## 2. Create an Azure AD app for OIDC federation

The deploy workflow authenticates to Azure using GitHub OIDC, so no long-lived credentials are stored anywhere.

```bash
# Pick a name for your app registration
APP_NAME="plaidify-deploy-$(whoami)"
SUBSCRIPTION_ID="$(az account show --query id -o tsv)"
TENANT_ID="$(az account show --query tenantId -o tsv)"

# Create the app + service principal
APP_ID="$(az ad app create --display-name "$APP_NAME" --query appId -o tsv)"
az ad sp create --id "$APP_ID" >/dev/null

# Grant the SP rights at the subscription scope
az role assignment create --assignee "$APP_ID" --role "Contributor" --scope "/subscriptions/$SUBSCRIPTION_ID"
az role assignment create --assignee "$APP_ID" --role "User Access Administrator" --scope "/subscriptions/$SUBSCRIPTION_ID"

# Federate GitHub Actions -> Azure AD for the `production` environment
GH_OWNER="$(gh repo view --json owner --jq .owner.login)"
GH_REPO="$(gh repo view --json name --jq .name)"

az ad app federated-credential create --id "$APP_ID" --parameters "{
  \"name\": \"github-${GH_OWNER}-${GH_REPO}-production\",
  \"issuer\": \"https://token.actions.githubusercontent.com\",
  \"subject\": \"repo:${GH_OWNER}/${GH_REPO}:environment:production\",
  \"audiences\": [\"api://AzureADTokenExchange\"]
}"

echo "AZURE_CLIENT_ID=$APP_ID"
echo "AZURE_TENANT_ID=$TENANT_ID"
echo "AZURE_SUBSCRIPTION_ID=$SUBSCRIPTION_ID"
```

Copy the three IDs printed at the end — you'll paste them in step 4.

---

## 3. Create the `production` GitHub Environment

```bash
gh api -X PUT "repos/${GH_OWNER}/${GH_REPO}/environments/production" >/dev/null
echo "Created environment: production"
```

(Optional) Add required reviewers in the GitHub UI: **Settings → Environments → production → Required reviewers**. This forces a manual approval gate before each deploy.

---

## 4. Set environment **variables** (non-secret config)

These are public-ish: they describe *where* you're deploying, not *credentials*.

```bash
gh variable set AZURE_RESOURCE_GROUP --env production --body "rg-plaidify-prod"
gh variable set AZURE_LOCATION       --env production --body "eastus2"
gh variable set AZURE_NAME_PREFIX    --env production --body "plaidify"   # used for ACR/KV/Postgres names
gh variable set AZURE_APP_ENV        --env production --body "production"
gh variable set AZURE_CORS_ORIGINS   --env production --body "https://app.example.com"
gh variable set AZURE_LLM_PROVIDER   --env production --body "openai"
gh variable set AZURE_LLM_MODEL      --env production --body "gpt-4o-mini"
```

Tweak `AZURE_RESOURCE_GROUP`, `AZURE_LOCATION`, `AZURE_NAME_PREFIX`, and `AZURE_CORS_ORIGINS` to your needs.

---

## 5. Set environment **secrets**

Generate strong values locally — they never leave your machine until `gh secret set` uploads them to GitHub's encrypted store.

```bash
# Strong runtime secrets (generated locally)
ENCRYPTION_KEY="$(python3 -c 'import base64,os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())')"
JWT_SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(64))')"
HEALTH_CHECK_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
POSTGRES_ADMIN_PASSWORD="$(python3 -c 'import secrets,string; a=string.ascii_letters+string.digits+"!@#%^*-_=+"; print("".join(secrets.choice(a) for _ in range(32)))')"

# Push to GitHub
printf '%s' "$ENCRYPTION_KEY"             | gh secret set ENCRYPTION_KEY             --env production --body -
printf '%s' "$JWT_SECRET_KEY"             | gh secret set JWT_SECRET_KEY             --env production --body -
printf '%s' "$HEALTH_CHECK_TOKEN"         | gh secret set HEALTH_CHECK_TOKEN         --env production --body -
printf '%s' "$POSTGRES_ADMIN_PASSWORD"    | gh secret set AZURE_POSTGRES_ADMIN_PASSWORD --env production --body -

# Identifiers from step 2 (interactive prompts)
gh secret set AZURE_CLIENT_ID            --env production
gh secret set AZURE_TENANT_ID            --env production
gh secret set AZURE_SUBSCRIPTION_ID      --env production
gh secret set AZURE_POSTGRES_ADMIN_LOGIN --env production --body "plaidifyadmin"

# Provider key (paste your real OpenAI/Anthropic key)
gh secret set LLM_API_KEY                --env production
```

> Save `ENCRYPTION_KEY` somewhere safe (a password manager). Losing it means you cannot decrypt previously stored credentials. The other values can be rotated; `ENCRYPTION_KEY` rotation requires the dual-key migration documented in [docs/KMS_INTEGRATION.md](KMS_INTEGRATION.md).

---

## 6. Trigger the deployment

```bash
gh workflow run deploy-azure.yml -f github_environment=production
gh run watch
```

The workflow will:

1. Validate that every required secret/variable is present (fails fast otherwise).
2. Bicep-bootstrap the resource group: ACR, Key Vault, Postgres Flexible Server, Redis, Container Apps environment.
3. Populate Key Vault with runtime secrets.
4. Build the Plaidify Docker image and push to ACR.
5. Deploy the API Container App **and** the access-executor worker Container App.
6. Run `alembic upgrade head` via a Container Apps Job before the new revision serves traffic.
7. Hit `/health` to confirm the rollout.

When complete, `gh run view --log` will show the public FQDN of your Container App.

---

## 7. Post-deploy

- Point your DNS (`app.example.com`) at the Container App FQDN and re-run with the matching `AZURE_CORS_ORIGINS`.
- Configure alerts in Azure Monitor (see [docs/AZURE_DEPLOYMENT.md](AZURE_DEPLOYMENT.md)).
- Run a load test from a separate machine: [scripts/run-loadtest.sh](../scripts/run-loadtest.sh).
- Review the [docs/RUNBOOK.md](RUNBOOK.md) before going live.

---

## Local development without Azure

You don't need any of the above to run Plaidify locally:

```bash
cp .env.example .env
# Generate two values for the local file:
python3 -c 'import base64,os; print("ENCRYPTION_KEY=" + base64.urlsafe_b64encode(os.urandom(32)).decode())' >> .env
python3 -c 'import secrets; print("JWT_SECRET_KEY=" + secrets.token_urlsafe(64))' >> .env

docker compose up --build
curl http://localhost:8000/health
```

`.env` is gitignored, so your local secrets stay on your laptop.

---

## What lives where

| Layer | Where it lives | Visible to |
| --- | --- | --- |
| Source code, Bicep templates, CI workflow | The public repo | Everyone |
| Your `production` env vars (e.g. `AZURE_RESOURCE_GROUP`) | GitHub Environment store on **your** repo | You and workflow runs on your repo |
| Your `production` env secrets (e.g. `ENCRYPTION_KEY`) | GitHub Environment store on **your** repo (encrypted) | Workflow runs on your repo only — not even printable in logs |
| Runtime secrets at request time | Azure Key Vault → injected into Container App env | Your Container App's managed identity only |
| Local development values | `.env` on your laptop (gitignored) | You |

Multiple people can self-host from the same code without any coordination — each person's `production` environment is an independent island.
