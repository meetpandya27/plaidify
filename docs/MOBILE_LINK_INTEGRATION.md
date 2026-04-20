# Plaidify Mobile Link Integration

Plaidify's hosted Link flow can be embedded in mobile applications by loading the hosted `/link` page inside a native webview container and listening for bridge events.

## Recommended Architecture

1. Your backend creates a signed one-time launch token with `/link/bootstrap`.
2. The mobile client redeems that launch token with `/link/sessions/bootstrap`.
3. The client receives the `link_token` and builds the hosted link URL.
4. The app loads the URL in a native webview.
5. The hosted page emits JSON events back to the app shell on every important state transition.
6. On `CONNECTED`, the app exchanges the returned `public_token` if needed and dismisses the sheet.

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
- `access_token`
- `public_token`
- `error`

## UX Guidance

- Use full-screen presentation on phones.
- Keep the native status bar visible, but let the webview own the rest of the screen.
- Dismiss the native sheet when you receive `DONE`, `EXIT`, or `CONNECTED`.
- Treat `ERROR` as a terminal event and show a native retry affordance.

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
      const session = await client.createPublicLinkSession();
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
        onSuccess={(accessToken, metadata) => {
          console.log(accessToken, metadata.public_token, metadata.organization_name);
        }}
      />
    </View>
  );
}
```

Redeem the bootstrap token from your backend, then render the resulting hosted session in your native shell.

## Native iOS Skeleton

Register a `plaidifyLink` `WKScriptMessageHandler`, load the hosted URL in `WKWebView`, and parse `message.body` as the structured event payload.

## Native Android Skeleton

Expose a JavaScript interface named `PlaidifyLinkBridge` with a `postMessage(String json)` method, load the hosted URL in `WebView`, and parse the JSON payload into your native model.