# ClipFlow ProGuard Rules

# OkHttp rules
-dontwarn okhttp3.**
-dontwarn okio.**
-keep class okhttp3.** { *; }

# Material & AndroidX components
-keep class com.google.android.material.** { *; }
-dontwarn com.google.android.material.**
