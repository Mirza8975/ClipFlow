package com.mirza8975.clipflow

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.util.Log
import androidx.core.app.NotificationCompat
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import java.io.File
import java.io.FileOutputStream
import java.io.IOException
import java.util.concurrent.Executors

/**
 * Foreground Service hosting the embedded ClipFlow FastAPI/Uvicorn server.
 * Manages Python runtime lifecycle, background thread execution, and resource cleanup.
 */
class ClipFlowBackendService : Service() {

    sealed class BackendState {
        object Stopped : BackendState()
        data class Starting(val stage: String) : BackendState()
        object Ready : BackendState()
        data class Error(val message: String, val details: String? = null) : BackendState()
    }

    companion object {
        private const val TAG = "ClipFlowBackendService"
        private const val CHANNEL_ID = "clipflow_engine_channel"
        private const val NOTIFICATION_ID = 8765

        const val ACTION_START = "com.mirza8975.clipflow.action.START"
        const val ACTION_STOP = "com.mirza8975.clipflow.action.STOP"
        const val ACTION_RESTART = "com.mirza8975.clipflow.action.RESTART"

        @Volatile
        var currentState: BackendState = BackendState.Stopped
            private set

        @Volatile
        var lastErrorMessage: String? = null
            private set

        @Volatile
        var lastErrorDetails: String? = null
            private set

        val isServerRunning: Boolean
            get() = currentState is BackendState.Ready

        fun resetState() {
            currentState = BackendState.Stopped
            lastErrorMessage = null
            lastErrorDetails = null
        }

        fun restartService(context: Context) {
            resetState()
            val intent = Intent(context, ClipFlowBackendService::class.java).apply {
                action = ACTION_RESTART
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(intent)
            } else {
                context.startService(intent)
            }
        }

        fun startService(context: Context) {
            val intent = Intent(context, ClipFlowBackendService::class.java).apply {
                action = ACTION_START
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(intent)
            } else {
                context.startService(intent)
            }
        }

        fun stopService(context: Context) {
            val intent = Intent(context, ClipFlowBackendService::class.java).apply {
                action = ACTION_STOP
            }
            context.startService(intent)
        }

        fun getDetailedDiagnostics(context: Context): String {
            val sb = StringBuilder()
            sb.append("=== ClipFlow Engine Diagnostics ===\n")
            sb.append("App Version: ${BuildConfig.VERSION_NAME} (Build ${BuildConfig.VERSION_CODE})\n")
            sb.append("Android OS: ${Build.VERSION.RELEASE} (API ${Build.VERSION.SDK_INT})\n")
            sb.append("Device: ${Build.MANUFACTURER} ${Build.MODEL}\n")
            sb.append("Supported ABIs: ${Build.SUPPORTED_ABIS.joinToString(", ")}\n")
            sb.append("State: $currentState\n")

            if (lastErrorMessage != null) {
                sb.append("\nLast Failure: $lastErrorMessage\n")
            }
            if (lastErrorDetails != null) {
                sb.append("\n--- Service Stack Trace ---\n")
                sb.append(lastErrorDetails).append("\n")
            }

            try {
                if (Python.isStarted()) {
                    val py = Python.getInstance()
                    val entry = py.getModule("clipflow_entry")
                    val pyDiag = entry.callAttr("get_diagnostics").toString()
                    sb.append("\n--- Python Runtime Diagnostics ---\n")
                    sb.append(pyDiag).append("\n")
                } else {
                    sb.append("\nPython Runtime: Not started yet\n")
                }
            } catch (e: Exception) {
                sb.append("\n[Chaquopy Diagnostics Notice: ${e.localizedMessage}]\n")
            }

            try {
                val logFile = File(context.filesDir, "data/startup_diagnostics.log")
                if (logFile.exists()) {
                    sb.append("\n--- On-Disk Startup Log (Last 40 lines) ---\n")
                    val lines = logFile.readLines()
                    val tail = lines.takeLast(40)
                    sb.append(tail.joinToString("\n")).append("\n")
                }
            } catch (e: Exception) {
                // ignore
            }

            return sb.toString()
        }
    }

    private val executor = Executors.newSingleThreadExecutor()
    private var wakeLock: PowerManager.WakeLock? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
        val notification = buildForegroundNotification("Initializing ClipFlow Studio engine...")
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
                startForeground(
                    NOTIFICATION_ID,
                    notification,
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC
                )
            } else {
                startForeground(NOTIFICATION_ID, notification)
            }
        } else {
            startForeground(NOTIFICATION_ID, notification)
        }

        acquireWakeLock()
        Log.i(TAG, "[FOREGROUND_SERVICE_STARTED] Foreground service active.")
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val action = intent?.action ?: ACTION_START
        when (action) {
            ACTION_STOP -> {
                shutdownServer()
                stopForeground(STOP_FOREGROUND_REMOVE)
                stopSelf()
                return START_NOT_STICKY
            }
            ACTION_RESTART -> {
                executor.execute {
                    shutdownServer()
                    bootPythonBackend()
                }
            }
            ACTION_START -> {
                val state = currentState
                if (state !is BackendState.Ready && state !is BackendState.Starting) {
                    executor.execute {
                        bootPythonBackend()
                    }
                }
            }
        }
        return START_STICKY
    }

    private fun bootPythonBackend() {
        acquireWakeLock()
        try {
            currentState = BackendState.Starting("Preparing environment and assets...")
            updateNotification("Preparing web assets and studio files...")

            // 1. Prepare directories in app-private storage
            val dataDir = File(filesDir, "data").apply { mkdirs() }
            val webDir = File(filesDir, "web")
            val binDir = File(dataDir, "bin").apply { mkdirs() }
            File(dataDir, "models").mkdirs()
            File(dataDir, "tmp").mkdirs()
            File(dataDir, "fonts").mkdirs()

            // 2. Extract web assets if needed
            extractWebAssetsIfNeeded(webDir)

            // 3. Setup Chaquopy Python runtime
            currentState = BackendState.Starting("Initializing Chaquopy Python 3.11...")
            updateNotification("Starting embedded Python 3.11 runtime...")
            if (!Python.isStarted()) {
                Python.start(AndroidPlatform(applicationContext))
            }
            Log.i(TAG, "[CHAQUOPY_INITIALIZED] Embedded Python runtime ready.")
            val py = Python.getInstance()

            // 4. Start Uvicorn server via clipflow_entry module
            currentState = BackendState.Starting("Launching ClipFlow Studio engine on 127.0.0.1:8765...")
            updateNotification("Launching ClipFlow Studio (127.0.0.1:8765)...")
            Log.i(TAG, "[FASTAPI_SERVER_STARTING] Invoking clipflow_entry.start_server...")
            val entry = py.getModule("clipflow_entry")
            val nativeLibDir = applicationInfo.nativeLibraryDir
            val nativeLibDirFile = File(nativeLibDir)
            Log.i(TAG, "[NATIVE_LIBS] nativeLibraryDir path: $nativeLibDir (exists: ${nativeLibDirFile.exists()})")

            val ffmpegSo = File(nativeLibDirFile, "libffmpeg.so")
            val ffprobeSo = File(nativeLibDirFile, "libffprobe.so")
            val qjsSo = File(nativeLibDirFile, "libqjs.so")
            if (ffmpegSo.exists()) {
                ffmpegSo.setExecutable(true, false)
                Log.i(TAG, "[NATIVE_LIBS] libffmpeg.so found: ${ffmpegSo.absolutePath} (size=${ffmpegSo.length()}B, executable=${ffmpegSo.canExecute()})")
            } else {
                Log.w(TAG, "[NATIVE_LIBS_WARNING] libffmpeg.so not found in $nativeLibDir")
            }
            if (ffprobeSo.exists()) {
                ffprobeSo.setExecutable(true, false)
                Log.i(TAG, "[NATIVE_LIBS] libffprobe.so found: ${ffprobeSo.absolutePath} (size=${ffprobeSo.length()}B, executable=${ffprobeSo.canExecute()})")
            } else {
                Log.w(TAG, "[NATIVE_LIBS_WARNING] libffprobe.so not found in $nativeLibDir")
            }
            if (qjsSo.exists()) {
                qjsSo.setExecutable(true, false)
                Log.i(TAG, "[NATIVE_LIBS] libqjs.so found: ${qjsSo.absolutePath} (size=${qjsSo.length()}B, executable=${qjsSo.canExecute()})")
            } else {
                Log.w(TAG, "[NATIVE_LIBS_WARNING] libqjs.so not found in $nativeLibDir")
            }

            val dirContents = nativeLibDirFile.listFiles()?.map { it.name } ?: emptyList()
            Log.i(TAG, "[NATIVE_LIBS] All libraries in nativeLibraryDir: $dirContents")

            entry.callAttr(
                "start_server",
                dataDir.absolutePath,
                webDir.absolutePath,
                nativeLibDir,
                8765
            )

            currentState = BackendState.Ready
            lastErrorMessage = null
            updateNotification("ClipFlow Studio Engine is active on 127.0.0.1:8765")
            Log.i(TAG, "[FASTAPI_SERVER_READY] ClipFlow Python backend successfully started and verified.")
        } catch (e: Exception) {
            val errorMsg = e.localizedMessage ?: "Unknown startup exception"
            val stackTrace = Log.getStackTraceString(e)
            Log.e(TAG, "[FASTAPI_SERVER_FAILED] Failed to start Python backend: $errorMsg\n$stackTrace", e)
            currentState = BackendState.Error(errorMsg, stackTrace)
            lastErrorMessage = errorMsg
            lastErrorDetails = stackTrace
            updateNotification("Engine error: $errorMsg")
        }
    }

    private fun shutdownServer() {
        Log.i(TAG, "Shutting down ClipFlow Python backend...")
        try {
            if (Python.isStarted()) {
                val py = Python.getInstance()
                val entry = py.getModule("clipflow_entry")
                entry.callAttr("stop_server")
            }
        } catch (e: Exception) {
            Log.e(TAG, "Error stopping Python backend", e)
        } finally {
            currentState = BackendState.Stopped
            releaseWakeLock()
        }
    }

    private fun extractWebAssetsIfNeeded(destDir: File) {
        val versionFile = File(destDir, ".version")
        val lastUpdateTime = try {
            @Suppress("DEPRECATION")
            packageManager.getPackageInfo(packageName, 0).lastUpdateTime
        } catch (_: Exception) {
            0L
        }
        val currentStamp = "${BuildConfig.VERSION_CODE}:$lastUpdateTime"
        val indexHtml = File(destDir, "index.html")

        if (destDir.exists() && versionFile.exists() && versionFile.readText().trim() == currentStamp && indexHtml.exists() && indexHtml.length() > 0) {
            return // Up to date
        }

        Log.i(TAG, "Extracting web assets to: ${destDir.absolutePath}")
        destDir.deleteRecursively()
        destDir.mkdirs()

        copyAssetFolder("web", destDir)
        try {
            versionFile.writeText(currentStamp)
        } catch (e: IOException) {
            Log.w(TAG, "Failed to write web assets version file", e)
        }
    }

    private fun copyAssetFolder(assetPath: String, destDir: File) {
        val assetManager = assets
        val files = assetManager.list(assetPath) ?: return

        if (files.isEmpty()) {
            // Leaf file
            try {
                assetManager.open(assetPath).use { input ->
                    FileOutputStream(destDir).use { output ->
                        input.copyTo(output)
                    }
                }
            } catch (e: IOException) {
                Log.w(TAG, "Error copying asset file: $assetPath", e)
            }
        } else {
            // Directory
            destDir.mkdirs()
            for (file in files) {
                val subAssetPath = "$assetPath/$file"
                val subDestFile = File(destDir, file)
                val subFiles = assetManager.list(subAssetPath)
                if (subFiles != null && subFiles.isNotEmpty()) {
                    copyAssetFolder(subAssetPath, subDestFile)
                } else {
                    try {
                        assetManager.open(subAssetPath).use { input ->
                            FileOutputStream(subDestFile).use { output ->
                                input.copyTo(output)
                            }
                        }
                    } catch (e: IOException) {
                        subDestFile.mkdirs()
                    }
                }
            }
        }
    }

    private fun acquireWakeLock() {
        try {
            if (wakeLock?.isHeld == true) {
                wakeLock?.release()
            }
            val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
            wakeLock = pm.newWakeLock(
                PowerManager.PARTIAL_WAKE_LOCK,
                "ClipFlow:BackendServiceWakeLock"
            ).apply {
                setReferenceCounted(false)
                acquire(30 * 60 * 1000L) // 30 minutes timeout per boot/restart cycle
            }
        } catch (e: Exception) {
            Log.w(TAG, "Could not acquire WakeLock", e)
        }
    }

    private fun releaseWakeLock() {
        try {
            if (wakeLock?.isHeld == true) {
                wakeLock?.release()
            }
        } catch (e: Exception) {
            Log.w(TAG, "Error releasing WakeLock", e)
        }
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID,
                "ClipFlow Studio Engine",
                NotificationManager.IMPORTANCE_LOW
            ).apply {
                description = "Runs the local video and speech processing engine"
                setShowBadge(false)
            }
            val manager = getSystemService(NotificationManager::class.java)
            manager?.createNotificationChannel(channel)
        }
    }

    private fun buildForegroundNotification(statusText: String): Notification {
        val launchIntent = Intent(this, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_SINGLE_TOP
        }
        val pendingIntent = PendingIntent.getActivity(
            this,
            0,
            launchIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("ClipFlow Studio Engine")
            .setContentText(statusText)
            .setSmallIcon(R.mipmap.ic_launcher)
            .setOngoing(true)
            .setContentIntent(pendingIntent)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .build()
    }

    private fun updateNotification(statusText: String) {
        val manager = getSystemService(Context.NOTIFICATION_SERVICE) as? NotificationManager
        manager?.notify(NOTIFICATION_ID, buildForegroundNotification(statusText))
    }

    override fun onTaskRemoved(rootIntent: Intent?) {
        super.onTaskRemoved(rootIntent)
        Log.i(TAG, "Task removed, stopping backend service...")
        shutdownServer()
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    override fun onDestroy() {
        super.onDestroy()
        shutdownServer()
        try {
            SpeechEngine.release(force = false)
        } catch (e: Exception) {
            Log.w(TAG, "Error releasing SpeechEngine on service destruction", e)
        }
        executor.shutdown()
    }
}
