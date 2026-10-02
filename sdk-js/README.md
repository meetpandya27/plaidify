# @plaidify/client

JavaScript and TypeScript client for the Plaidify service, with React and
React Native helpers for the hosted Link page.

## Install

The package is not on npm yet. Build it from a checkout of this repository
and install the folder:

```bash
cd sdk-js && npm ci && npm run build
cd ../your-app && npm install ../plaidify/sdk-js
```

## Basic Usage

```typescript
import { Plaidify } from "@plaidify/client";

const client = new Plaidify({ serverUrl: "http://localhost:8000" });
await client.login("alice", "password"); // OAuth2 form at POST /auth/token; keeps the token

const result = await client.connect("hydro_one", "your_username", "your_password");
console.log(result.status); // "connected", "mfa_required" (then submitMfa) or "pending" (then waitForAccessJob)
```

`register(username, email, password)` resolves to the new account's tokens
(and keeps its access token) when the server creates it at once. A server that
has sign-ups prove their email address first answers a `RegistrationPending`
(`{ status: "verification_sent", detail }`) whether or not the username or
address was free, and emails the address a one-time token:
`verifyEmail(token, password)` with that same password creates the account and
keeps its token. The token alone does not.

`token` (a user access token) is sent as `Authorization: Bearer`; `apiKey`
(`pk_…`, agent keys `pk_agent_…`) as `X-API-Key` — the server accepts API
keys only there, so a `pk_` value passed as `token` is sent as a key too.
Secrets always travel in request bodies: `connect`, `submitMfa` and
`fetchData` POST JSON, never query strings. `connect` sends the credentials as
plain JSON, so use it only over TLS and from a server.

Managing API keys, agents, webhooks and refresh schedules needs a user access
token (`login()`), not an API key. `registerWebhook(linkToken, url, secret)`
subscribes to a link's events; each delivery is signed:
`X-Plaidify-Signature: sha256=` + hex HMAC-SHA256 (keyed with `secret`) of
`` `${X-Plaidify-Timestamp}.${rawBody}` ``. Verify it over the raw body and
reject old timestamps.

## Hosted Link Bootstrap Flow

```typescript
// Server side (API key or user token):
const bootstrap = await client.createHostedLinkBootstrap({
  site: "hydro_one",
  allowedOrigin: "https://app.example.com",
  scopes: ["current_balance"],
});

// In the browser at https://app.example.com:
const publicClient = new Plaidify({ serverUrl: "https://api.example.com" });
const session = await publicClient.exchangeHostedLinkBootstrap(bootstrap.launch_token);
const hostedUrl = publicClient.getLinkUrl(session.link_token, {
  origin: "https://app.example.com",
});
```

## React Integration

`@plaidify/client/react` provides the hosted web modal helper (`usePlaidifyLink`,
`PlaidifyLink`). It only trusts messages from the Link iframe it opened, on
the Plaidify server's origin. `onSuccess` and `onExit` each fire at most
once per `open()`, and the modal closes only on `CONNECTED` or an exit — an
`ERROR` is recoverable (the page offers retry / another provider), so it
reaches `onEvent` and sets `status` to `"error"` without closing Link.

## React Native Integration

`@plaidify/client/react-native` provides hosted link helpers for webview-based mobile shells. Use the same production bootstrap flow and redeem the launch token before rendering the hosted page. `shouldDismissPlaidifySheet` is true for `CONNECTED` and exits, never for `ERROR`; messages from pages off the Plaidify origin are dropped.

Hosted link success callbacks return the browser-safe `public_token` as the first argument. Exchange it on your backend (`exchangePublicToken`, `POST /exchange/public_token`) when you need a durable access token.

`theme` (`accentColor`, `bgColor`, `borderRadius`, `logo` as a `data:image/…` URI of at most 32 KB) is applied by the hosted page.

## Notes

- Prefer `createHostedLinkBootstrap()` plus `exchangeHostedLinkBootstrap()` in production.
- `createPublicLinkSession()` is not the preferred production entrypoint; the server refuses it in production unless `PUBLIC_LINK_SESSIONS_ENABLED=true`.
- Keep hosted link origins explicit and environment-specific.

## Validation

```bash
npm ci
npm run lint
npm run typecheck
npm test
npm run build
```
