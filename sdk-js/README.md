# @plaidify/client

JavaScript and TypeScript client for the Plaidify service.

## Install

```bash
npm install @plaidify/client
```

## Basic Usage

```typescript
import { Plaidify } from "@plaidify/client";

const client = new Plaidify({ serverUrl: "http://localhost:8000" });
await client.login("user@example.com", "password");

const result = await client.connect("hydro_one", "your_username", "your_password");
console.log(result.status);
```

## Hosted Link Bootstrap Flow

```typescript
const bootstrap = await client.createHostedLinkBootstrap({
  site: "hydro_one",
  allowedOrigin: "https://app.example.com",
  scopes: ["read_bill"],
});

const publicClient = new Plaidify({ serverUrl: "https://api.example.com" });
const session = await publicClient.exchangeHostedLinkBootstrap(bootstrap.launch_token);
const hostedUrl = publicClient.getLinkUrl(session.link_token, {
  origin: "https://app.example.com",
});
```

## React Integration

`@plaidify/client/react` provides the hosted web modal helper.

## React Native Integration

`@plaidify/client/react-native` provides hosted link helpers for webview-based mobile shells. Use the same production bootstrap flow and redeem the launch token before rendering the hosted page.

Hosted link success callbacks return the browser-safe `public_token` as the first argument. Exchange it on your backend when you need a durable access token.

## Notes

- Prefer `createHostedLinkBootstrap()` plus `exchangeHostedLinkBootstrap()` in production.
- `createPublicLinkSession()` is not the preferred production entrypoint.
- Keep hosted link origins explicit and environment-specific.

## Validation

```bash
npm run typecheck
npm test
```
