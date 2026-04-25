# sdk-android — Plaidify Link for Android

Native Jetpack Compose Link surface with WebView fallback.

## Modules

- `core/` — Pure Kotlin/JVM library: REST client (`PlaidifyLinkClient`),
  state machine (`PlaidifyLinkConnectFlow`), and institution registry
  (`PlaidifyLinkInstitutionRegistry`). Has no Android dependencies and
  is fully unit-testable on the JVM. Run with `gradle :core:test`.
- `ui/src/main/kotlin/` — Jetpack Compose screens
  (`PlaidifyLinkPicker`, `PlaidifyLinkCredentials`, `PlaidifyLinkMfa`,
  `PlaidifyLinkProgress`, `PlaidifyLinkErrorScreen`) plus a drop-in
  `PlaidifyLinkActivity` that hosts the Compose flow and falls back
  to a `WebView` for institutions outside the registry.

The `ui/` sources are not built by this repository's Gradle root; host
apps depend on `core` for logic and copy the Compose sources into
their own Android library module that pulls AGP, Compose, and OkHttp.
This keeps the published `core` artifact lightweight and lets each
host app pick its own HTTP transport.

## Quick start (host app)

```kotlin
// build.gradle.kts (Android library / app)
dependencies {
    implementation(project(":core"))
    implementation("androidx.activity:activity-compose:1.9.0")
    implementation("androidx.compose.material3:material3:1.2.1")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
}
```

```kotlin
// Launch the Activity
val intent = PlaidifyLinkActivity.intent(
    context = this,
    serverUrl = "https://api.example.com",
    linkToken = "lnk-abc",
)
startActivity(intent)
```

## Event contract

The native flow emits the same logical events as the WebView bridge
(`OPEN`, `INSTITUTION_SELECTED`, `CREDENTIALS_SUBMITTED`,
`MFA_REQUIRED`, `MFA_SUBMITTED`, `CONNECTED`, `EXIT`, `ERROR`) plus a
`FALLBACK_WEBVIEW` event when an institution is routed to the WebView
surface. Subscribe to events by setting `flow.onEvent = { ... }` on
`PlaidifyLinkConnectFlow` or by hosting the flow yourself.

## Tests

```bash
cd sdk-android
gradle :core:test
```

13 unit tests cover URL escaping, JSON decoding, typed HTTP errors,
state-machine transitions (happy path / MFA / error / reset), and
fallback decisions.
