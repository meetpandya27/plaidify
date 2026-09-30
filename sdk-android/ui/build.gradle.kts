// Compose screens + PlaidifyLinkActivity. Needs an Android SDK; included by
// settings.gradle.kts only when one is configured.
plugins {
    id("com.android.library")
    kotlin("plugin.compose")
}

android {
    namespace = "com.plaidify.link.ui"
    compileSdk = 35

    defaultConfig {
        // java.util.Base64 (RsaOaepEncryptor in :core) needs API 26.
        minSdk = 26
    }

    buildFeatures {
        compose = true
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

dependencies {
    // PlaidifyLinkResult and the rest of the public types live in :core.
    api(project(":core"))

    implementation(platform("androidx.compose:compose-bom:2026.09.00"))
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.ui:ui")
    implementation("androidx.activity:activity-compose:1.13.0")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.11.0")
    implementation("androidx.webkit:webkit:1.17.1")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.10.2")
}
