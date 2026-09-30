# sdk-android — Plaidify Link for Android

The hosted Link page in a locked-down WebView, with experimental native
Jetpack Compose screens behind an opt-in flag.

## Modules

- `core/` — Pure Kotlin/JVM library: REST client (`PlaidifyLinkClient`,
  with a default `HttpUrlConnectionClient`), RSA-OAEP credential encryption
  (`RsaOaepEncryptor`), the native state machine and its server driver
  (`PlaidifyLinkConnectFlow`, `PlaidifyLinkNativeSession`), the institution
  registry, and the WebView bridge contract (`PlaidifyLinkEvent`,
  `PlaidifyLinkResult`, `PlaidifyLinkOrigin`, `PlaidifyLinkNavigationPolicy`).
  No Android dependencies; unit-tested on the JVM.
- `ui/` — Android library (AGP + Compose): `PlaidifyLinkActivity` and the
  Compose screens. Included in the build only when an Android SDK is
  configured (`ANDROID_HOME`, `ANDROID_SDK_ROOT`, or `sdk.dir` in
  `local.properties`), so `:core` builds on a plain JDK.

## Quick start (host app)

Not published yet: include the modules from a checkout in your build
(e.g. in `settings.gradle.kts`: `include(":core", ":ui")` with each
`project(...).projectDir` pointing at `sdk-android/core` and `sdk-android/ui`).

```kotlin
dependencies {
    implementation(project(":ui"))   // brings :core
}

private val link = registerForActivityResult(PlaidifyLinkActivity.Contract()) { result ->
    when (result) {
        is PlaidifyLinkResult.Connected -> exchangeOnBackend(result.publicToken)
        is PlaidifyLinkResult.Exited -> Unit   // result.reason, result.errorCode
    }
}

link.launch(PlaidifyLinkActivity.Contract.Input(serverUrl = "https://api.example.com", linkToken = "lnk-abc"))
```

The Activity finishes on `CONNECTED` (`RESULT_OK`, public token in
`EXTRA_PUBLIC_TOKEN`) or on an exit (`RESULT_CANCELED`, reason in
`EXTRA_EXIT_REASON`). An `ERROR` is not the end: the hosted page shows its
retry and choose-another-provider screens and Link stays open.

## WebView bridge

The hosted page posts each event as a JSON string to
`window.plaidifyLink.postMessage(...)`. The Activity injects that object
with `WebViewCompat.addWebMessageListener`, restricted to the Plaidify
server's origin, and accepts messages only from that origin's main frame.
Navigation is locked to the same origin: other links open in the browser.
On WebViews older than 86 it falls back to a `@JavascriptInterface` that
re-checks the page origin on the main thread.

## Native screens (experimental)

Every institution uses the hosted WebView by default. The Compose screens
are used only with `experimentalNativeScreens = true` **and** for sites
listed in `nativeSites` (`PlaidifyLinkInstitutionRegistry.supportedSites`).
They encrypt credentials to the session key (RSA-OAEP, SHA-256 with
MGF1-SHA-256, like the server), call `/connect`, and follow the session
through MFA to completion.

## Build and test

Gradle 9.4 via the wrapper; the code compiles for Java 17 (a JDK 17
toolchain), and Gradle itself may run on JDK 17 or newer.

```bash
cd sdk-android
./gradlew :core:test                                  # JVM only
ANDROID_HOME=~/Library/Android/sdk ./gradlew :ui:compileDebugKotlin
```
