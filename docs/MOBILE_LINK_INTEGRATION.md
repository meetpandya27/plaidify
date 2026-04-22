# Plaidify Mobile Link Integration

Plaidify's hosted Link flow can be embedded in mobile applications by loading the hosted `/link` page inside a native webview container and listening for bridge events.

## Recommended Architecture

1. Your backend creates a signed one-time launch token with `/link/bootstrap`.
2. The mobile client redeems that launch token with `/link/sessions/bootstrap`.
3. The client receives the `link_token` and builds the hosted link URL.
4. The app loads the URL in a native webview.
5. The hosted page emits JSON events back to the app shell on every important state transition.
6. On `CONNECTED`, the app receives a `public_token`, dismisses the sheet, and exchanges that token server-side when it needs a durable access token.

The hosted page does not return extracted account payloads to the browser or webview shell. The browser-safe completion contract is `public_token` plus connection metadata only.

## Bridge Targets

The hosted page emits events to these targets automatically when present:

- React Native WebView: `window.ReactNativeWebView.postMessage(JSON.stringify(event))`
- iOS WKWebView: `window.webkit.messageHandlers.plaidifyLink.postMessage(event)`
- Android WebView JS interface: `window.PlaidifyLinkBridge.postMessage(JSON.stringify(event))`
- Android alternate interface: `window.PlaidifyLinkBridge.onEvent(JSON.stringify(event))`

## Event Contract

Common event names:

- `INSTITUTION_SELECTED`
- `CREDENTIALS_SUBMITTED`
- `MFA_REQUIRED`
- `MFA_SUBMITTED`
- `CONNECTED`
- `ERROR`
- `EXIT`
- `DONE`

Common payload fields:

- `source`: always `plaidify-link`
- `event`: event name
- `site`: connector runtime identifier
- `organization_id`
- `organization_name`
- `session_id`
- `mfa_type`
- `public_token`
- `job_id`
- `error`

## UX Guidance

- Use full-screen presentation on phones.
- Keep the native status bar visible, but let the webview own the rest of the screen.
- Dismiss the native sheet when you receive `DONE`, `EXIT`, or `CONNECTED`.
- Treat `public_token` as the only browser-safe completion token.
- Treat `ERROR` as a terminal event and show a native retry affordance.

The repository includes a Playwright E2E slice in `tests/test_hosted_link_e2e.py` that validates the hosted web journey plus the React Native and WKWebView bridge payload contract.

## Security Notes

- Prefer minting link sessions from your backend rather than directly from the mobile app.
- Treat the `link_token` as short-lived session state, not as a reusable credential.
- If using `/link/sessions/public`, issue sessions only from trusted server-side code in production.
- In production, set `PUBLIC_LINK_SESSIONS_ENABLED=true` only when you intentionally support anonymous hosted-link bootstrapping.
- Restrict anonymous session creation with `PUBLIC_LINK_ALLOWED_ORIGINS` so only trusted app origins can mint public hosted-link sessions.
- Prefer `/link/bootstrap` plus `/link/sessions/bootstrap` in production because the launch token is signed, short-lived, and one-time redeemable.

## React Native Skeleton

```tsx
import { useEffect, useState } from "react";
import { View } from "react-native";
import { WebView } from "react-native-webview";
import { Plaidify } from "@plaidify/client";
import { PlaidifyReactNativeLink } from "@plaidify/client/react-native";

const client = new Plaidify({ serverUrl: "https://api.example.com" });

export function PlaidifyMobileSheet() {
  const [token, setToken] = useState<string | null>(null);

  useEffect(() => {
    async function boot() {
      const session = await client.exchangeHostedLinkBootstrap(launchTokenFromBackend);
      setToken(session.link_token);
    }
    boot();
  }, []);

  if (!token) return null;

  return (
    <View style={{ flex: 1 }}>
      <PlaidifyReactNativeLink
        WebViewComponent={WebView}
        serverUrl="https://api.example.com"
        token={token}
        theme={{ fullscreenOnMobile: true, accentColor: "#0b8f73" }}
        onSuccess={(publicToken, metadata) => {
          console.log(publicToken, metadata.organization_name, metadata.job_id);
        }}
      />
    </View>
  );
}
```

Redeem the bootstrap token from your backend, then render the resulting hosted session in your native shell. Exchange the returned `public_token` on your backend when you need a durable Plaidify access token.

## Native iOS Skeleton

Use the first-party Swift package in `sdk-swift/` to build the hosted URL, parse bridge messages, and decide when to dismiss the native sheet.

```swift
import WebKit
import PlaidifyLinkKit

let configuration = PlaidifyHostedLinkConfiguration(
  serverURL: URL(string: "https://api.example.com")!,
  token: linkToken,
  origin: "myapp://callback",
  theme: PlaidifyLinkTheme(accentColor: "#0b8f73")
)

let bridge = PlaidifyLinkScriptMessageHandler { event in
  if event.shouldDismissSheet {
    print("Dismiss sheet with public token:", event.publicToken ?? "")
  }
}

let webView = PlaidifyLinkWebViewFactory.makeWebView(
  hostedLink: configuration,
  messageHandler: bridge
)
```

Bridge events from `window.webkit.messageHandlers.plaidifyLink` are parsed into `PlaidifyLinkEvent`, including `publicToken`, `jobID`, `organizationName`, and `shouldDismissSheet`.

## Native Android Skeleton

Expose a JavaScript interface named `PlaidifyLinkBridge` with a `postMessage(String json)` method, load the hosted URL in `WebView`, and parse the JSON payload into your native model.