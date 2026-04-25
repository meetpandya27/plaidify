// Root build for the Plaidify Android SDK. The :core module is pure
// Kotlin/JVM so it builds without the Android SDK; the Compose UI
// sources under ui/src/main/kotlin are provided as a drop-in for host
// apps that depend on AGP and Jetpack Compose. See README.md for the
// recommended host-app build.gradle.kts wiring.

plugins {
    kotlin("jvm") version "1.9.25" apply false
}
