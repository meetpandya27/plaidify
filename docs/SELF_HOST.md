# Self-Hosting Plaidify on Azure

This guide takes you from a fresh fork of the public repo to a running Plaidify deployment in your own Azure subscription. Nothing you configure here lands in the public repository — all secrets and tenant-specific values live in **your** GitHub Environment store and **your** Azure Key Vault.

> **Public-repo guarantee.** The repo contains only generic code, generic Bicep templates, and a generic CI/CD workflow. There are no embedded subscription IDs, hostnames, encryption keys, or passwords. Each self-hoster supplies their own values via GitHub Environment secrets and variables.

---

## Prerequisites

- An Azure subscription where you can create a resource group and assign roles on it (**Owner** of the subscription or of the resource group, once, for the setup below). The deployment itself runs with rights on that one resource group only.
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

## 2. Create the resource group and the deployment identity

The deploy workflow authenticates to Azure using GitHub OIDC, so no long-lived credentials are stored anywhere. Its service principal gets rights on **one resource group**, nothing at subscription scope:

- **Contributor** on the resource group, to create and update the resources in `infra/main.bicep`;
- **Role Based Access Control Administrator** on the resource group, *restricted by a condition* to assigning exactly three roles — AcrPull and Key Vault Secrets User (for the app's managed identity) and Key Vault Secrets Officer (which the template grants the workflow itself on the new Key Vault, so it can write the runtime secrets). It can't grant anything else, to anyone.

```bash
# Pick names
APP_NAME="plaidify-deploy-$(whoami)"
RESOURCE_GROUP="rg-plaidify-prod"
LOCATION="eastus2"
SUBSCRIPTION_ID="$(az account show --query id -o tsv)"
TENANT_ID="$(az account show --query tenantId -o tsv)"

# One-time, subscription-level setup (needs subscription rights; the
# deployment identity can't do these itself)
for ns in Microsoft.App Microsoft.ContainerRegistry Microsoft.KeyVault Microsoft.DBforPostgreSQL \
          Microsoft.Cache Microsoft.Network Microsoft.OperationalInsights Microsoft.ManagedIdentity; do
  az provider register --namespace "$ns" --wait
done
RG_ID="$(az group create --name "$RESOURCE_GROUP" --location "$LOCATION" --query id -o tsv)"

# Create the app registration + service principal
APP_ID="$(az ad app create --display-name "$APP_NAME" --query appId -o tsv)"
SP_OBJECT_ID="$(az ad sp create --id "$APP_ID" --query id -o tsv)"

# Rights on the resource group only
az role assignment create --assignee-object-id "$SP_OBJECT_ID" --assignee-principal-type ServicePrincipal \
  --role "Contributor" --scope "$RG_ID"

# May only assign AcrPull (7f951dda…), Key Vault Secrets User (4633458b…)
# and Key Vault Secrets Officer (b86a8fe4…), and remove only those.
ALLOWED_ROLES="7f951dda-4ed3-4680-a7ca-43fe172d538d, 4633458b-17de-408a-b874-0445c86b69e6, b86a8fe4-44ce-4948-aee5-eccb2c155cd7"
CONDITION="((!(ActionMatches{'Microsoft.Authorization/roleAssignments/write'})) OR (@Request[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {${ALLOWED_ROLES}})) AND ((!(ActionMatches{'Microsoft.Authorization/roleAssignments/delete'})) OR (@Resource[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {${ALLOWED_ROLES}}))"
az role assignment create --assignee-object-id "$SP_OBJECT_ID" --assignee-principal-type ServicePrincipal \
  --role "Role Based Access Control Administrator" --scope "$RG_ID" \
  --condition "$CONDITION" --condition-version "2.0"

# Federate GitHub Actions -> Entra ID, for the `production` environment only
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

Copy the three IDs printed at the end — you'll paste them in step 5.

> Upgrading from an earlier version of this guide? Remove the old subscription-wide grants: `az role assignment delete --assignee "$APP_ID" --role "User Access Administrator" --scope "/subscriptions/$SUBSCRIPTION_ID"` and the same for `Contributor`, after adding the two resource-group assignments above.

---

## 3. Create and protect the `production` GitHub Environment

The environment holds the Azure credentials, so it is the deployment's gate: only `main` may deploy to it, and each deployment waits for your approval.

```bash
ME="$(gh api user --jq .id)"
gh api -X PUT "repos/${GH_OWNER}/${GH_REPO}/environments/production" --input - <<EOF
{
  "reviewers": [{ "type": "User", "id": ${ME} }],
  "deployment_branch_policy": { "protected_branches": false, "custom_branch_policies": true }
}
EOF
gh api -X POST "repos/${GH_OWNER}/${GH_REPO}/environments/production/deployment-branch-policies" \
  -f name=main -f type=branch
```

The workflow also refuses to run for anything but `main`, and only for a commit whose CI run passed.

---

## 4. Set environment **variables** (non-secret config)

These are public-ish: they describe *where* you're deploying, not *credentials*.

```bash
gh variable set AZURE_RESOURCE_GROUP --env production --body "$RESOURCE_GROUP"
gh variable set AZURE_LOCATION       --env production --body "$LOCATION"
gh variable set AZURE_NAME_PREFIX    --env production --body "plaidify"   # used for ACR/KV/Postgres names
gh variable set AZURE_APP_ENV        --env production --body "production"
gh variable set AZURE_CORS_ORIGINS   --env production --body "https://app.example.com"
gh variable set AZURE_LLM_PROVIDER   --env production --body "openai"
gh variable set AZURE_LLM_MODEL      --env production --body "gpt-4o-mini"

# The first administrator (registration is off in production); its password is a secret, step 5
gh variable set AZURE_BOOTSTRAP_USER_USERNAME --env production --body "admin"
gh variable set AZURE_BOOTSTRAP_USER_EMAIL    --env production --body "admin@example.com"

# Optional: password-reset mail (then SMTP_PASSWORD in step 5)
# gh variable set AZURE_SMTP_HOST     --env production --body "smtp.example.com"
# gh variable set AZURE_SMTP_FROM     --env production --body "no-reply@example.com"
# gh variable set AZURE_SMTP_USERNAME --env production --body "no-reply@example.com"
```

Tweak `AZURE_NAME_PREFIX` and `AZURE_CORS_ORIGINS` to your needs. `AZURE_RESOURCE_GROUP` must be the group from step 2.

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

# The first administrator's password (prompts)
gh secret set BOOTSTRAP_USER_PASSWORD    --env production

# Only with the SMTP variables from step 4 (prompts)
# gh secret set SMTP_PASSWORD            --env production
```

> Save `ENCRYPTION_KEY` somewhere safe (a password manager). Losing it means you cannot decrypt previously stored credentials. The other values can be rotated; rotating `ENCRYPTION_KEY` takes the two-key procedure in [SECURITY.md](../SECURITY.md#key-rotation-procedure).

`HEALTH_CHECK_TOKEN` is what makes `GET /health/detailed` answer in production (it is 404 without it). `AUDIT_HMAC_KEY`, `LINK_LAUNCH_SECRET` and `METRICS_TOKEN` are not GitHub secrets: the workflow generates them once and keeps them in Key Vault ([AZURE_DEPLOYMENT.md](AZURE_DEPLOYMENT.md#runtime-wiring)).

The PostgreSQL admin login is used by migrations only. The API and the executor connect as a separate role without DDL rights, whose password the workflow generates and keeps in Key Vault.

---

## 6. Trigger the deployment

Push to `main`, wait for CI to pass, then:

```bash
gh workflow run deploy-azure.yml --ref main -f github_environment=production
gh run watch
```

Approve the deployment when GitHub asks (the environment's required reviewer). The workflow will:

1. Check it runs for `main` and that CI passed for the commit.
2. Check the resource group exists, and read its own identity.
3. Bicep-deploy the infrastructure: virtual network, private PostgreSQL and Redis, ACR, Key Vault, Log Analytics, Container Apps environment — and give itself Key Vault Secrets Officer on the vault.
4. Populate Key Vault with runtime secrets.
5. Build the Plaidify image and push it to ACR.
6. Run `alembic upgrade head` (and refresh the application role's grants) in a Container Apps Job. If it fails, the deployment stops and the running revisions are untouched.
7. Deploy the API Container App **and** the access-executor Container App with the new image.
8. Wait for both apps to run a ready revision of the new image, and require exactly `200` from `https://<app>/health` (a redirect fails the deployment).

When complete, `gh run view --log` will show the public FQDN of your Container App.

---

## 7. Post-deploy

- Sign in as the bootstrap administrator and change its password, then delete `AZURE_BOOTSTRAP_USER_USERNAME`, `AZURE_BOOTSTRAP_USER_EMAIL` and `BOOTSTRAP_USER_PASSWORD` from the environment; the next deployment stops passing them. (Without them registration is off, so the first account is otherwise made [by hand](RUNBOOK.md#first-administrator-without-bootstrap_user_).)
- Point your DNS (`app.example.com`) at the Container App FQDN and re-run with the matching `AZURE_CORS_ORIGINS`.
- Configure alerts: the template creates none ([AZURE_DEPLOYMENT.md, "Alerts"](AZURE_DEPLOYMENT.md#alerts)).
- Load-test a separate, non-production deployment, not this one ([LOAD_TESTING.md](LOAD_TESTING.md)): the scenario registers users and calls `/connect`.
- Work through the owner checklist in [RUNBOOK.md](RUNBOOK.md#before-going-live-owner-checklist) before going live.

---

## Local development without Azure

You don't need any of the above to run Plaidify locally:

```bash
cp .env.example .env
# Generate two values for the local file:
python3 -c 'import base64,os; print("ENCRYPTION_KEY=" + base64.urlsafe_b64encode(os.urandom(32)).decode())' >> .env
python3 -c 'import secrets; print("JWT_SECRET_KEY=" + secrets.token_urlsafe(64))' >> .env

# Database and Redis passwords for the compose stack
mkdir -p .secrets && chmod 700 .secrets
openssl rand -base64 32 > .secrets/postgres_password
openssl rand -base64 32 > .secrets/redis_password
chmod 644 .secrets/*   # containers read these as non-root users (.secrets/README.md)

docker compose up --build
curl http://127.0.0.1:8000/health
```

`.env` and `.secrets/` are gitignored, so your local secrets stay on your laptop.

---

## What lives where

| Layer | Where it lives | Visible to |
| --- | --- | --- |
| Source code, Bicep templates, CI workflow | The public repo | Everyone |
| Your `production` env vars (e.g. `AZURE_RESOURCE_GROUP`) | GitHub Environment store on **your** repo | You and workflow runs on your repo |
| Your `production` env secrets (e.g. `ENCRYPTION_KEY`) | GitHub Environment store on **your** repo (encrypted) | Workflow runs on your repo only — not even printable in logs |
| Runtime secrets at request time | Azure Key Vault → injected into Container App env | Your Container App's managed identity only |
| Local development values | `.env` and `.secrets/` on your laptop (gitignored) | You |

Multiple people can self-host from the same code without any coordination — each person's `production` environment is an independent island.
