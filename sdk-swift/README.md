# PlaidifyLinkKit

PlaidifyLinkKit is a first-party Swift package for embedding Plaidify's hosted Link flow in `WKWebView`.

It provides:

- Hosted-link URL building with token, origin, and theme parameters
- Parsing for Plaidify bridge events from `WKScriptMessage` bodies
- Terminal-event helpers for dismissing sheets at the right time
- A `WKScriptMessageHandler` and `WKWebView` factory for native iOS integration

## Install

Add the package in Xcode using the local folder or your published repository URL.

## Example

```swift
import SwiftUI
import WebKit
import PlaidifyLinkKit

let configuration = PlaidifyHostedLinkConfiguration(
    serverURL: URL(string: "https://api.example.com")!,
    token: "lnk-123",
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

When the hosted page posts a `CONNECTED`, `ERROR`, `EXIT`, or `DONE` event through `window.webkit.messageHandlers.plaidifyLink`, PlaidifyLinkKit parses the payload and exposes `shouldDismissSheet` to help native flows close cleanly.