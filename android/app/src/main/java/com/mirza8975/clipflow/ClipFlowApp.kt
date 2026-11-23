package com.mirza8975.clipflow

import android.app.Application
import android.webkit.WebView

class ClipFlowApp : Application() {
    override fun onCreate() {
        super.onCreate()
        // Enable Chrome DevTools inspection for debug builds
        if (BuildConfig.DEBUG) {
            WebView.setWebContentsDebuggingEnabled(true)
        }
    }
}
