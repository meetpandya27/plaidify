pluginManagement {
    repositories {
        gradlePluginPortal()
        google()
        mavenCentral()
    }
}

dependencyResolutionManagement {
    repositories {
        google()
        mavenCentral()
    }
}

rootProject.name = "plaidify-link-android"

include(":core")
project(":core").projectDir = file("core")

// The Compose UI needs AGP and an Android SDK. Include it when one is
// configured (ANDROID_HOME / ANDROID_SDK_ROOT, or sdk.dir in
// local.properties) so `:core` still builds on a plain JDK.
val androidSdkConfigured = listOf("ANDROID_HOME", "ANDROID_SDK_ROOT").any { !System.getenv(it).isNullOrBlank() } ||
    file("local.properties").let { it.exists() && it.readText().contains("sdk.dir") }
if (androidSdkConfigured) {
    include(":ui")
    project(":ui").projectDir = file("ui")
}
