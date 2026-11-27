package com.mirza8975.clipflow

import android.app.Application
import android.util.Log
import android.webkit.WebView

class ClipFlowApp : Application() {
    override fun onCreate() {
        super.onCreate()
        // Enable Chrome DevTools inspection for debug builds
        if (BuildConfig.DEBUG) {
            WebView.setWebContentsDebuggingEnabled(true)
        }
        // Only auto-start the local Python engine when configured for standalone mode
        val savedUrl = ServerConfigHelper(this).getSavedServerUrl()
        if (ServerConfigHelper.isLocalEngineUrl(savedUrl)) {
            try {
                ClipFlowBackendService.startService(this)
            } catch (e: Exception) {
                // On Android 12+ background app launches, MainActivity will start the FGS once in foreground
                Log.w("ClipFlowApp", "Deferred backend service start until Activity foreground: ${e.message}")
            }
        }
    }
}
