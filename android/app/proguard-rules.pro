# ClipFlow ProGuard Rules

# Chaquopy / SpeechEngine JNI & Kotlin bridge
-keep class com.mirza8975.clipflow.SpeechEngine { *; }
-keep class com.k2fsa.sherpa.onnx.** { *; }
-dontwarn com.k2fsa.sherpa.onnx.**

# WebView JavaScript interfaces
-keepattributes JavascriptInterface,Annotation
-keepclassmembers class * {
    @android.webkit.JavascriptInterface <methods>;
}

# OkHttp rules
-dontwarn okhttp3.**
-dontwarn okio.**
-keep class okhttp3.** { *; }

# Material & AndroidX components
-keep class com.google.android.material.** { *; }
-dontwarn com.google.android.material.**
