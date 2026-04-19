# @plaidify/client

Official JavaScript/TypeScript SDK for the [Plaidify](https://github.com/plaidify/plaidify) API.

## Installation

```bash
npm install @plaidify/client
```

## Quick Start

```typescript
import { Plaidify } from "@plaidify/client";

const client = new Plaidify({ serverUrl: "http://localhost:8000" });

// Authenticate
await client.login("user@example.com", "password");

// List available blueprints
const { blueprints } = await client.listBlueprints();

// Connect to a site
const result = await client.connect("greengrid_energy", "demo_user", "demo_pass");
console.log(result.data);

if (result.status === "pending" && result.job_id) {
  const job = await client.waitForAccessJob(result.job_id);
  console.log(job.result);
}
```

## React Integration

```tsx
import { usePlaidifyLink } from "@plaidify/client/react";

function ConnectButton() {
  const { open, ready } = usePlaidifyLink({
    serverUrl: "http://localhost:8000",
    token: linkToken,
    onSuccess: (publicToken) => {
      // Exchange public token for access token
      client.exchangePublicToken(publicToken);
    },
    onExit: () => console.log("User exited"),
  });

  return (
    <button onClick={open} disabled={!ready}>
      Connect Account
    </button>
  );
}
```

## API Reference

### Core

| Method | Description |
|--------|-------------|
| `health()` | Check server health |
| `listBlueprints()` | List available site connectors |
| `getBlueprint(name)` | Get blueprint details |
| `connect(site, username, password)` | Direct connect + extract |
| `listAccessJobs(options?)` | List tracked access jobs |
| `getAccessJob(jobId)` | Get one access job |
| `waitForAccessJob(jobId, options?)` | Poll until a job leaves pending/running |
| `submitMfa(sessionId, code)` | Submit MFA code |
| `fetchData(accessToken)` | Fetch data with access token |

### Detached Connect Jobs

`connect()` can return `{ status: "pending", job_id }` when the server detaches
the browser flow into a background access job. Poll the job until it reaches a
terminal state:

```typescript
const result = await client.connect("greengrid_energy", "demo_user", "demo_pass");

if (result.status === "pending" && result.job_id) {
  const job = await client.waitForAccessJob(result.job_id, {
    pollIntervalMs: 500,
    timeoutMs: 30000,
  });

  if (job.status === "completed") {
    console.log(job.result);
  }
}
```

### Auth

| Method | Description |
|--------|-------------|
| `register(email, password)` | Create account |
| `login(email, password)` | Login and get tokens |
| `me()` | Get current user profile |

### Link Flow

| Method | Description |
|--------|-------------|
| `createLinkSession(site)` | Start a link session |
| `getLinkUrl(linkToken)` | Get embeddable link URL |
| `exchangePublicToken(publicToken)` | Exchange public → access token |

### Agents

| Method | Description |
|--------|-------------|
| `registerAgent(name, options?)` | Register an AI agent |
| `listAgents()` | List agents |
| `getAgent(agentId)` | Get agent details |
| `updateAgent(agentId, updates)` | Update agent config |
| `deactivateAgent(agentId)` | Deactivate an agent |

### Consent

| Method | Description |
|--------|-------------|
| `requestConsent(accessToken, scopes, agentName)` | Request data consent |
| `approveConsent(consentId)` | Approve consent |
| `denyConsent(consentId)` | Deny consent |
| `listConsents()` | List active consents |
| `revokeConsent(consentToken)` | Revoke consent |

### Webhooks

| Method | Description |
|--------|-------------|
| `registerWebhook(linkToken, url)` | Register webhook |
| `listWebhooks()` | List webhooks |
| `deleteWebhook(webhookId)` | Delete webhook |
| `testWebhook(webhookId)` | Send test event |
| `getWebhookDeliveries(webhookId)` | Get delivery history |

### Scheduled Refresh

| Method | Description |
|--------|-------------|
| `scheduleRefresh(accessToken, intervalSeconds?)` | Schedule periodic refresh |
| `unscheduleRefresh(accessToken)` | Remove schedule |
| `listRefreshJobs()` | List active refresh jobs |

### API Keys

| Method | Description |
|--------|-------------|
| `createApiKey(name, options?)` | Create API key |
| `listApiKeys()` | List API keys |
| `revokeApiKey(keyId)` | Revoke API key |

### Audit

| Method | Description |
|--------|-------------|
| `getAuditLogs(options?)` | Query audit logs |
| `verifyAuditChain()` | Verify log integrity |

## Error Handling

```typescript
import { PlaidifyError, AuthenticationError } from "@plaidify/client";

try {
  await client.login("wrong@email.com", "bad_password");
} catch (err) {
  if (err instanceof AuthenticationError) {
    console.error("Invalid credentials");
  } else if (err instanceof PlaidifyError) {
    console.error(`API error (${err.statusCode}): ${err.detail}`);
  }
}
```
