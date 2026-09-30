# Plaidify Mobile Link Integration

Plaidify's hosted Link flow is embedded in mobile apps by loading the hosted
`/link` page in a web view and listening for the events it posts to the app.
The first-party SDKs do this for you: `@plaidify/client/react-native`,
[`sdk-swift/`](../sdk-swift/README.md) (`PlaidifyLinkKit`) and
[`sdk-android/`](../sdk-android/README.md). None is published to a package
registry yet; build them from this repository.

## Recommended Architecture

1. Your backend creates a signed one-time launch token with `POST /link/bootstrap`
   (authenticated with its API key or a user token).
2. The app redeems it with `POST /link/sessions/bootstrap {"launch_token": …}`
   and receives a `link_token` (and a relative `link_url`).
3. The app loads `https://<plaidify>/link?token=<link_token>` in a web view.
4. The hosted page posts JSON events to the app on every important state change.
5. On `CONNECTED`, the app receives a `public_token`, dismisses Link, and sends
   the token to its backend, which exchanges it with `POST /exchange/public_token`
   for a durable access token.

The page never returns extracted account data to the browser or web view, and
never shows the public token on screen. The browser-safe completion contract is
the `public_token` (one-time, 10 minutes) plus event metadata.

## Bridge Targets

The page posts every event to each of these that exists:

- React Native WebView: `window.ReactNativeWebView.postMessage(JSON.stringify(event))`
- iOS WKWebView: `window.webkit.messageHandlers.plaidifyLink.postMessage(event)` (an object)
- Android: `window.plaidifyLink.postMessage(JSON.stringify(event))` — injected by
  the Android SDK with `WebViewCompat.addWebMessageListener`, restricted to the
  Plaidify origin
- An embedding browser window: `postMessage`, only to the session's allowed origins

Accept messages only from the Plaidify page's origin and main frame; the SDKs do.

## Event Contract

Events the page sends (`event` field):

| Event | Payload fields | Meaning |
| --- | --- | --- |
| `OPEN` | — | The page loaded |
| `INSTITUTION_SELECTED` | `organization_id`, `organization_name`, `site` | The user picked a provider |
| `MFA_REQUIRED` | `mfa_type`, `session_id` | The provider asked for a code or approval |
| `MFA_SUBMITTED` | `session_id` | The user answered |
| `CONNECTED` | `public_token`, `job_id`, `site` | Done: exchange the public token on your backend |
| `ERROR` | `error`, `error_code`, `site` | Something failed; the page shows retry and choose-another-provider screens |
| `EXIT` | `reason` (`user_exit`, `invalid_link`, `page_closed`), `error_code` | The user left, or the page was torn down |
| `SUPPORT_REQUESTED` | `error_code` | The user asked for help from the error screen |
| `TELEMETRY` | `name`, `elapsed_ms`, … | UX analytics ([HOSTED_LINK_TELEMETRY.md](HOSTED_LINK_TELEMETRY.md)); ignore it unless you collect analytics |

Every message also carries `source: "plaidify-link"`. `ERROR` is not terminal.
`EXIT` is sent only when the user leaves Link or the page goes away, never on
an error by itself. (The SDKs also treat `DONE` and `CLOSE` as exits.)

## UX Guidance

- Use full-screen presentation on phones.
- Keep the native status bar visible, but let the web view own the rest of the screen.
- Dismiss Link on `CONNECTED` or `EXIT` — not on `ERROR`, which the page recovers from.
- Treat `public_token` as the only browser-safe completion token.

The Playwright tests in `tests/test_hosted_link_e2e.py` cover the hosted web
journey and the React Native and WKWebView bridge payloads.

## Security Notes

- Mint sessions from your backend (`/link/bootstrap`), not from the app.
- Name the origins that may redeem and embed a session (`allowed_origin` /
  `allowed_origins` on `/link/bootstrap`, or `allowed_origins` on
  `POST /link/sessions`); launch tokens that name them can only be redeemed
  from those origins.
- Treat the `link_token` as short-lived session state (10 minutes), not a reusable credential.
- `POST /link/sessions/public` (anonymous) is refused in production unless
  `PUBLIC_LINK_SESSIONS_ENABLED=true`; restrict it with `PUBLIC_LINK_ALLOWED_ORIGINS`.

## React Native Skeleton

```tsx
import { useEffect, useState } from "react";
import { View } from "react-native";
import { WebView } from "react-native-webview";
import { Plaidify } from "@plaidify/client";
import { PlaidifyReactNativeLink } from "@plaidify/client/react-native";

const client = new Plaidify({ serverUrl: "https://api.example.com" });

export function PlaidifyMobileSheet({ launchToken }: { launchToken: string }) {
  const [token, setToken] = useState<string | null>(null);

  useEffect(() => {
    client.exchangeHostedLinkBootstrap(launchToken).then((session) => setToken(session.link_token));
  }, [launchToken]);

  if (!token) return null;

  return (
    <View style={{ flex: 1 }}>
      <PlaidifyReactNativeLink
        WebViewComponent={WebView}
        serverUrl="https://api.example.com"
        token={token}
        theme={{ fullscreenOnMobile: true, accentColor: "#0b8f73" }}
        onSuccess={(publicToken, metadata) => {
          // Send publicToken to your backend; metadata has site and job_id.
        }}
        onExit={({ reason }) => {
          // Dismiss the sheet.
        }}
      />
    </View>
  );
}
```

`launchToken` comes from your backend's `POST /link/bootstrap`. Messages from
pages on other origins are dropped.

## Native iOS Skeleton

```swift
import PlaidifyLinkKit

let configuration = PlaidifyHostedLinkConfiguration(
  serverURL: URL(string: "https://api.example.com")!,
  token: linkToken,
  theme: PlaidifyLinkTheme(accentColor: "#0b8f73")
)

// Drop-in controller: dismisses itself on CONNECTED and on an exit.
let link = PlaidifyLinkViewController(hostedConfiguration: configuration) { event in
  if event.name == .connected {
    sendToBackend(event.publicToken ?? "")
  }
}
present(link, animated: true)
```

To host the web view yourself, use
`PlaidifyLinkWebViewFactory.makeHostedLinkWebView(hostedLink:onEvent:)` and
keep a reference to the returned object while it is on screen. Events arrive
as `PlaidifyLinkEvent` (`publicToken`, `jobID`, `organizationName`, `reason`,
`errorCode`, `shouldDismissSheet`). Details: [sdk-swift/README.md](../sdk-swift/README.md).

## Native Android Skeleton

```kotlin
private val link = registerForActivityResult(PlaidifyLinkActivity.Contract()) { result ->
    when (result) {
        is PlaidifyLinkResult.Connected -> sendToBackend(result.publicToken)
        is PlaidifyLinkResult.Exited -> Unit   // result.reason, result.errorCode
    }
}

link.launch(PlaidifyLinkActivity.Contract.Input(serverUrl = "https://api.example.com", linkToken = linkToken))
```

`PlaidifyLinkActivity` locks the web view to the Plaidify origin and receives
the page's `window.plaidifyLink` messages. With your own `WebView`, inject an
object named `plaidifyLink` with `WebViewCompat.addWebMessageListener`,
restricted to the Plaidify origin, and parse each message as JSON. Details:
[sdk-android/README.md](../sdk-android/README.md).
