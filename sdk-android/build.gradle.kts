// Root build for the Plaidify Android SDK. The :core module is pure
// Kotlin/JVM so it builds without the Android SDK; the :ui module (Compose
// screens + PlaidifyLinkActivity) needs AGP and an Android SDK and is only
// included when one is configured (see settings.gradle.kts).
//
// Every plugin is declared here, once, so the Kotlin and Android plugins
// share a classloader (AGP's built-in Kotlin needs that).
plugins {
    kotlin("jvm") version "2.4.20" apply false
    kotlin("plugin.serialization") version "2.4.20" apply false
    kotlin("plugin.compose") version "2.4.20" apply false
    id("com.android.library") version "9.2.1" apply false
}
