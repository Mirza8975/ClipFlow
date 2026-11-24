plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

android {
    namespace = "com.mirza8975.clipflow"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.mirza8975.clipflow"
        minSdk = 24
        targetSdk = 34
        versionCode = 1
        versionName = "1.0.0"

        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
        vectorDrawables {
            useSupportLibrary = true
        }

        ndk {
            abiFilters += listOf("arm64-v8a")
        }
    }

    signingConfigs {
        create("release") {
            val keystorePath = project.findProperty("android.injected.signing.store.file") as? String
                ?: System.getenv("KEYSTORE_FILE")
            if (keystorePath != null && file(keystorePath).exists()) {
                storeFile = file(keystorePath)
                storePassword = project.findProperty("android.injected.signing.store.password") as? String
                    ?: System.getenv("KEYSTORE_PASSWORD")
                keyAlias = project.findProperty("android.injected.signing.key.alias") as? String
                    ?: System.getenv("KEY_ALIAS")
                keyPassword = project.findProperty("android.injected.signing.key.password") as? String
                    ?: System.getenv("KEY_PASSWORD")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
            val releaseSigning = signingConfigs.getByName("release")
            if (releaseSigning.storeFile != null) {
                signingConfig = releaseSigning
            }
            // When releaseSigning.storeFile is not set, Gradle builds an unsigned release APK (app-release-unsigned.apk)
        }
        debug {
            applicationIdSuffix = ".debug"
            versionNameSuffix = "-debug"
            isDebuggable = true
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    buildFeatures {
        viewBinding = true
        buildConfig = true
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.12.0")
    implementation("androidx.appcompat:appcompat:1.6.1")
    implementation("com.google.android.material:material:1.11.0")
    implementation("androidx.webkit:webkit:1.10.0")
    implementation("androidx.core:core-splashscreen:1.0.1")
    implementation("androidx.activity:activity-ktx:1.8.2")

    // Lightweight networking for /healthz connection check
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.7.3")

    testImplementation("junit:junit:4.13.2")
}

chaquopy {
    defaultConfig {
        version = "3.11"
        pip {
            install("pydantic==1.10.19")
            install("fastapi==0.115.0")
            install("uvicorn==0.30.0")
            install("sqlalchemy==2.0.35")
            install("httpx==0.27.0")
            install("python-multipart==0.0.9")
            install("numpy")
            install("pillow")
            install("soundfile")
            install("yt-dlp")
        }
    }
}

val syncWebAssets = tasks.register<Sync>("syncWebAssets") {
    from(rootProject.file("../web"))
    into(file("src/main/assets/web"))
}

val syncStudioPython = tasks.register<Sync>("syncStudioPython") {
    from(rootProject.file("../studio"))
    into(file("src/main/python/studio"))
    exclude("__pycache__", "*.pyc")
}

tasks.matching { it.name.startsWith("merge") && it.name.endsWith("PythonSources") }.configureEach {
    dependsOn(syncStudioPython)
}

tasks.matching { it.name.startsWith("merge") && it.name.endsWith("Assets") }.configureEach {
    dependsOn(syncWebAssets)
}

tasks.named("preBuild") {
    dependsOn(syncWebAssets, syncStudioPython)
}

