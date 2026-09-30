# PlaidifyLinkKit

PlaidifyLinkKit is a first-party Swift package for embedding Plaidify's hosted Link flow in `WKWebView`.

It provides:

- Hosted-link URL building with token, origin, and theme parameters
- A web view locked to the Plaidify origin: bridge messages are accepted only
  from the Plaidify page's main frame, and any other link opens in the system
  browser instead of inside Link
- Parsing for Plaidify bridge events from `WKScriptMessage` bodies
- Terminal-event helpers for dismissing sheets at the right time
- `PlaidifyLinkViewController`, a drop-in UIKit controller

## Install

Not published yet. Add the `sdk-swift` folder of a checkout as a local package
(Xcode: File › Add Package Dependencies… › Add Local…, or
`.package(path: "../plaidify/sdk-swift")` in a `Package.swift`). The manifest is not at
the repository root, so a Git URL dependency on this repository does not work. iOS 15+ / macOS 13+.

## Example

```swift
import PlaidifyLinkKit

let configuration = PlaidifyHostedLinkConfiguration(
    serverURL: URL(string: "https://api.example.com")!,
    token: "lnk-123",
    theme: PlaidifyLinkTheme(accentColor: "#0b8f73")
)

// Drop-in controller: dismisses itself on CONNECTED and EXIT.
let link = PlaidifyLinkViewController(hostedConfiguration: configuration) { event in
    if event.name == .connected {
        print("Exchange on your backend:", event.publicToken ?? "")
    }
}
present(link, animated: true)

// Or host the web view yourself (keep a reference while it is shown):
let hosted = PlaidifyLinkWebViewFactory.makeHostedLinkWebView(hostedLink: configuration) { event in
    if event.shouldDismissSheet { /* close your sheet */ }
}
```

`shouldDismissSheet` is true for `CONNECTED` and for an exit (`EXIT`,
`DONE`, `CLOSE`). An `ERROR` is not terminal: the hosted page shows its retry
and choose-another-provider screens, and Link stays open.

## Native screens (experimental)

Every institution uses the hosted web view by default. The native SwiftUI
screens are experimental: they are used only when you pass
`experimentalNativeScreens: true` **and** list the institution's site in a
`PlaidifyLinkInstitutionRegistry(supportedSites:)`. They encrypt credentials
with RSA-OAEP (SHA-256) to the session key, call `/connect`, and follow the
session through MFA to completion (`PlaidifyLinkNativeSession`).

## Tests

```bash
swift test
xcodebuild -scheme PlaidifyLinkKit -destination 'generic/platform=iOS Simulator' build
```
