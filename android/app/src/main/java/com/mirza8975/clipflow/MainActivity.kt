package com.mirza8975.clipflow

import android.Manifest
import android.annotation.SuppressLint
import android.content.ClipData
import android.content.ClipboardManager
import android.content.ContentValues
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.media.MediaScannerConnection
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.provider.MediaStore
import android.view.View
import android.view.ViewGroup
import android.view.WindowManager
import android.webkit.CookieManager
import android.webkit.JavascriptInterface
import android.webkit.URLUtil
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.EditText
import android.widget.ProgressBar
import android.widget.TextView
import android.widget.Toast
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.core.content.FileProvider
import androidx.core.splashscreen.SplashScreen.Companion.installSplashScreen
import androidx.lifecycle.lifecycleScope
import com.google.android.material.floatingactionbutton.FloatingActionButton
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import java.io.ByteArrayInputStream
import java.io.File
import java.io.FileOutputStream
import android.util.Log
import java.net.URLDecoder
import java.util.concurrent.TimeUnit

class MainActivity : AppCompatActivity() {

    companion object {
        private const val TAG = "ClipFlowMainActivity"
        private val SAFE_IMAGE_DATA_URI_REGEX = Regex(
            "^data:image/(jpeg|jpg|png|webp|gif);base64,[A-Za-z0-9+/=\\r\\n]+$",
            RegexOption.IGNORE_CASE
        )

        /**
         * Validates subresource URIs for WebView's shouldInterceptRequest security boundary:
         * - Allows HTTP/HTTPS requests cleanly (HTTPS & mixed-content rules enforced separately).
         * - Allows strict same-origin blob: subresources (e.g., URL.createObjectURL(file) video previews).
         * - Allows strict base64 raster image data: subresources (never HTML/SVG/scripts).
         * - Strictly blocks content://, file://, javascript:, cross-origin blob:, and all unknown schemes.
         */
        internal fun isAllowedSubresourceUri(
            uri: Uri?,
            trustedOriginUri: Uri?,
            isForMainFrame: Boolean
        ): Boolean {
            if (uri == null) return false
            val scheme = uri.scheme?.lowercase() ?: return false

            if (scheme == "http" || scheme == "https") {
                return true
            }

            if (isForMainFrame) {
                return false
            }

            if (scheme == "blob") {
                val innerRaw = uri.schemeSpecificPart?.trim() ?: return false
                if (!innerRaw.startsWith("http://", ignoreCase = true) &&
                    !innerRaw.startsWith("https://", ignoreCase = true)
                ) {
                    return false
                }
                val innerUri = try {
                    Uri.parse(innerRaw)
                } catch (_: Exception) {
                    return false
                }
                return ServerConfigHelper.isSameOrigin(innerUri, trustedOriginUri)
            }

            if (scheme == "data") {
                return SAFE_IMAGE_DATA_URI_REGEX.matches(uri.toString())
            }

            return false
        }
    }

    private lateinit var webView: WebView
    private lateinit var progressBar: ProgressBar
    private lateinit var layoutServerConfig: View
    private lateinit var layoutError: View
    private lateinit var layoutLoading: View
    private lateinit var tvLoadingStatus: TextView
    private lateinit var fabSettings: FloatingActionButton

    // Server Config Views
    private lateinit var etServerUrl: EditText
    private lateinit var btnTestConnection: Button
    private lateinit var btnConnect: Button
    private var btnUseLocalEngine: Button? = null
    private lateinit var tvStatusMessage: TextView

    // Error Views
    private lateinit var tvErrorDetails: TextView
    private var tvAppVersion: TextView? = null
    private var tvDiagnosticsLog: TextView? = null
    private lateinit var btnCopyDiagnostics: Button
    private lateinit var btnRetry: Button
    private lateinit var btnChangeServer: Button

    private lateinit var serverHelper: ServerConfigHelper
    private var fileUploadCallback: ValueCallback<Array<Uri>>? = null
    private var lastBackPressTime: Long = 0
    private var currentLoadedUrl: String = ""

    private data class PendingDownload(
        val url: String,
        val userAgent: String,
        val contentDisposition: String,
        val mimetype: String
    )
    private var pendingDownload: PendingDownload? = null

    // Dedicated OkHttp client for same-origin downloads/shares without following cross-origin redirects
    private val downloadStreamClient = OkHttpClient.Builder()
        .followRedirects(false)
        .followSslRedirects(false)
        .connectTimeout(8, TimeUnit.SECONDS)
        .readTimeout(120, TimeUnit.SECONDS)
        .build()

    // Activity Result Launcher for File Picker
    private val filePickerLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        if (fileUploadCallback == null) return@registerForActivityResult

        val data = result.data
        val clipData = data?.clipData
        val uris: Array<Uri>? = if (result.resultCode == RESULT_OK && clipData != null && clipData.itemCount > 0) {
            (0 until clipData.itemCount)
                .mapNotNull { i -> clipData.getItemAt(i)?.uri }
                .takeIf { it.isNotEmpty() }
                ?.toTypedArray()
                ?: WebChromeClient.FileChooserParams.parseResult(result.resultCode, data)
        } else {
            WebChromeClient.FileChooserParams.parseResult(result.resultCode, data)
        }
        fileUploadCallback?.onReceiveValue(uris)
        fileUploadCallback = null
    }

    private val storagePermissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted ->
        val pending = pendingDownload
        pendingDownload = null
        if (granted && pending != null) {
            lifecycleScope.launch {
                handleFileDownload(pending.url, pending.userAgent, pending.contentDisposition, pending.mimetype)
            }
        } else if (!granted && pending != null) {
            Toast.makeText(
                this,
                "Storage permission is required to save downloaded files on this Android version",
                Toast.LENGTH_LONG
            ).show()
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        // Install splash screen for Android 12+
        installSplashScreen()
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        Log.i(TAG, "[ANDROID_APP_STARTED] Activity created.")

        // Keep screen on during active operations to prevent network sleep during large video uploads
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

        serverHelper = ServerConfigHelper(this)
        initViews()
        setupWebView()
        setupBackNavigation()

        // Check deep link or server configuration
        val deepLink = intent?.data
        if (deepLink?.scheme?.lowercase() == "clipflow") {
            handleCustomSchemeUri(deepLink)
        } else {
            val savedUrl = serverHelper.getSavedServerUrl()
            if (savedUrl != null && !ServerConfigHelper.isLocalEngineUrl(savedUrl)) {
                // User explicitly saved a custom remote server URL
                connectToServer(savedUrl)
            } else {
                // Standalone mode: automatically start local backend and connect
                startEngineAndConnect("http://127.0.0.1:8765")
            }
        }
    }

    override fun onNewIntent(intent: Intent?) {
        super.onNewIntent(intent)
        setIntent(intent)
        handleIntent(intent)
    }

    private fun handleIntent(intent: Intent?) {
        val uri = intent?.data ?: return
        if (uri.scheme?.lowercase() == "clipflow") {
            handleCustomSchemeUri(uri)
        }
    }

    private fun handleCustomSchemeUri(uri: Uri) {
        val host = uri.host ?: ""
        val path = uri.path ?: ""
        val query = uri.query
        val savedBase = serverHelper.getSavedServerUrl()?.trim()?.removeSuffix("/") ?: "http://127.0.0.1:8765"
        val targetPath = if (host.isNotEmpty()) "/$host$path" else path
        val fullUrl = "$savedBase$targetPath" + (if (!query.isNullOrEmpty()) "?$query" else "")
        val isLocal = ServerConfigHelper.isLocalEngineUrl(savedBase)

        if (isLocal && !ClipFlowBackendService.isServerRunning) {
            startEngineAndConnect(savedBase, loadUrlOverride = fullUrl)
            return
        }

        layoutServerConfig.visibility = View.GONE
        layoutError.visibility = View.GONE
        webView.visibility = View.VISIBLE
        fabSettings.visibility = View.VISIBLE
        currentLoadedUrl = fullUrl
        webView.loadUrl(fullUrl)
    }

    private fun initViews() {
        webView = findViewById(R.id.webView)
        progressBar = findViewById(R.id.progressBar)
        layoutServerConfig = findViewById(R.id.layoutServerConfig)
        layoutError = findViewById(R.id.layoutError)
        layoutLoading = findViewById(R.id.layoutLoading)
        tvLoadingStatus = findViewById(R.id.tvLoadingStatus)
        fabSettings = findViewById(R.id.fabSettings)

        // Server Config UI
        etServerUrl = layoutServerConfig.findViewById(R.id.etServerUrl)
        btnTestConnection = layoutServerConfig.findViewById(R.id.btnTestConnection)
        btnConnect = layoutServerConfig.findViewById(R.id.btnConnect)
        btnUseLocalEngine = layoutServerConfig.findViewById(R.id.btnUseLocalEngine)
        tvStatusMessage = layoutServerConfig.findViewById(R.id.tvStatusMessage)

        // Error UI
        tvErrorDetails = layoutError.findViewById(R.id.tvErrorDetails)
        tvAppVersion = layoutError.findViewById(R.id.tvAppVersion)
        tvDiagnosticsLog = layoutError.findViewById(R.id.tvDiagnosticsLog)
        btnCopyDiagnostics = layoutError.findViewById(R.id.btnCopyDiagnostics)
        btnRetry = layoutError.findViewById(R.id.btnRetry)
        btnChangeServer = layoutError.findViewById(R.id.btnChangeServer)

        tvAppVersion?.text = "ClipFlow v${BuildConfig.VERSION_NAME} (Build ${BuildConfig.VERSION_CODE})"

        btnCopyDiagnostics.setOnClickListener {
            val diag = ClipFlowBackendService.getDetailedDiagnostics(this)
            val clipboard = getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
            val clip = ClipData.newPlainText("ClipFlow Diagnostics", diag)
            clipboard.setPrimaryClip(clip)
            Toast.makeText(this, getString(R.string.diagnostics_copied), Toast.LENGTH_SHORT).show()
        }

        // Pre-fill with saved or sensible default
        val existing = serverHelper.getSavedServerUrl() ?: "http://127.0.0.1:8765"
        etServerUrl.setText(existing)
        etServerUrl.setSelection(etServerUrl.text.length)

        btnTestConnection.setOnClickListener {
            testCurrentUrl()
        }

        btnConnect.setOnClickListener {
            val inputUrl = etServerUrl.text.toString().trim()
            val (valid, errorMsg) = serverHelper.validateUrlSecurity(inputUrl)
            if (!valid) {
                showConfigStatus(errorMsg ?: getString(R.string.invalid_url_error), isError = true)
                return@setOnClickListener
            }

            serverHelper.saveServerUrl(inputUrl)
            if (ServerConfigHelper.isLocalEngineUrl(inputUrl)) {
                startEngineAndConnect(inputUrl)
            } else {
                connectToServer(inputUrl)
            }
        }

        btnUseLocalEngine?.setOnClickListener {
            val localUrl = "http://127.0.0.1:8765"
            etServerUrl.setText(localUrl)
            etServerUrl.setSelection(localUrl.length)
            serverHelper.saveServerUrl(localUrl)
            startEngineAndConnect(localUrl)
        }

        btnRetry.setOnClickListener {
            val target = serverHelper.getSavedServerUrl() ?: "http://127.0.0.1:8765"
            if (ServerConfigHelper.isLocalEngineUrl(target)) {
                showLoadingScreen("Restarting ClipFlow Studio Engine...")
                ClipFlowBackendService.restartService(this)
                startEngineAndConnect(target, isRestart = true)
            } else {
                connectToServer(target)
            }
        }

        btnChangeServer.setOnClickListener {
            showServerConfigScreen()
        }

        fabSettings.setOnClickListener {
            // Close any open modal dialog first, then navigate the WebView to Settings
            webView.evaluateJavascript(
                "(function() { " +
                "  var dlg = document.querySelector('dialog[open]'); if (dlg && dlg.close) dlg.close(); " +
                "  var b = document.querySelector('[data-act=\"go\"][data-v=\"settings\"]'); " +
                "  if (b) { b.click(); } else if (typeof go === 'function') { go('settings'); } else { window.location.href = '/app/settings'; } " +
                "})();",
                null
            )
        }
    }

    private fun startEngineAndConnect(
        targetUrl: String = "http://127.0.0.1:8765",
        isRestart: Boolean = false,
        loadUrlOverride: String? = null
    ) {
        showLoadingScreen()
        if (!isRestart) {
            ClipFlowBackendService.startService(this)
        }

        lifecycleScope.launch {
            val startTime = System.currentTimeMillis()
            val timeoutMs = 45000L
            var pollDelay = 200L
            var connected = false

            while (System.currentTimeMillis() - startTime < timeoutMs) {
                // 1. Inspect backend service lifecycle state
                val serviceState = ClipFlowBackendService.currentState
                when (serviceState) {
                    is ClipFlowBackendService.BackendState.Starting -> {
                        Log.i(TAG, "[BACKEND_STARTING_STAGE] ${serviceState.stage}")
                    }
                    is ClipFlowBackendService.BackendState.Error -> {
                        Log.e(TAG, "[FASTAPI_SERVER_FAILED] Backend reported failure: ${serviceState.message}")
                        hideLoadingScreen()
                        val details = serviceState.details?.let { "\n\n$it" } ?: ""
                        showErrorScreen("ClipFlow studio engine failed to start:\n${serviceState.message}$details")
                        return@launch
                    }
                    else -> {}
                }

                // 2. Perform health check on the local endpoint
                val isHealthy = withContext(Dispatchers.IO) {
                    val result = serverHelper.checkHealth(targetUrl)
                    result is ConnectionResult.Success
                }

                if (isHealthy) {
                    connected = true
                    break
                }

                delay(pollDelay)
                pollDelay = (pollDelay * 1.2).toLong().coerceAtMost(500L)
            }

            if (connected) {
                hideLoadingScreen()
                serverHelper.saveServerUrl(targetUrl)
                connectToServer(targetUrl, loadUrlOverride)
            } else {
                hideLoadingScreen()
                val lastErr = ClipFlowBackendService.lastErrorMessage
                val msg = if (lastErr != null) {
                    "ClipFlow studio engine took too long to start.\n\n$lastErr"
                } else {
                    "Unable to reach the ClipFlow studio engine.\nThe local engine did not respond on 127.0.0.1:8765 within 45 seconds."
                }
                showErrorScreen(msg)
            }
        }
    }

    private fun showLoadingScreen(message: String = "") {
        layoutLoading.visibility = View.VISIBLE
        val subtitle = layoutLoading.findViewById<TextView?>(R.id.tvLoadingSubtitle)
        if (message.isNotEmpty() && subtitle != null) {
            subtitle.text = message
        } else if (subtitle != null) {
            subtitle.setText(R.string.startup_loading)
        }
        layoutServerConfig.visibility = View.GONE
        layoutError.visibility = View.GONE
        webView.visibility = View.GONE
        fabSettings.visibility = View.GONE
        progressBar.visibility = View.GONE
    }

    private fun hideLoadingScreen() {
        layoutLoading.visibility = View.GONE
    }

    private fun testCurrentUrl() {
        val inputUrl = etServerUrl.text.toString().trim()
        val (valid, errorMsg) = serverHelper.validateUrlSecurity(inputUrl)
        if (!valid) {
            showConfigStatus(errorMsg ?: getString(R.string.invalid_url_error), isError = true)
            return
        }

        showConfigStatus("Testing connection to $inputUrl...", isError = false)
        btnTestConnection.isEnabled = false

        lifecycleScope.launch {
            val result = serverHelper.checkHealth(inputUrl)
            btnTestConnection.isEnabled = true
            when (result) {
                is ConnectionResult.Success -> {
                    showConfigStatus(getString(R.string.connection_success), isError = false)
                }
                is ConnectionResult.Error -> {
                    showConfigStatus("Failed: ${result.message}", isError = true)
                }
            }
        }
    }

    private fun showConfigStatus(message: String, isError: Boolean) {
        tvStatusMessage.visibility = View.VISIBLE
        tvStatusMessage.text = message
        val colorRes = if (isError) R.color.status_error else R.color.status_success
        tvStatusMessage.setTextColor(getColor(colorRes))
    }

    private fun showServerConfigScreen() {
        hideLoadingScreen()
        layoutServerConfig.visibility = View.VISIBLE
        layoutError.visibility = View.GONE
        webView.visibility = View.GONE
        fabSettings.visibility = View.GONE
        progressBar.visibility = View.GONE
    }

    private fun showErrorScreen(details: String) {
        hideLoadingScreen()
        layoutError.visibility = View.VISIBLE
        layoutServerConfig.visibility = View.GONE
        webView.visibility = View.GONE
        fabSettings.visibility = View.GONE
        progressBar.visibility = View.GONE
        tvErrorDetails.text = details

        val diag = ClipFlowBackendService.getDetailedDiagnostics(this)
        tvDiagnosticsLog?.text = diag
    }

    private fun connectToServer(baseUrl: String, loadUrlOverride: String? = null) {
        val cleanBase = baseUrl.trim().removeSuffix("/")
        val targetAppUrl = loadUrlOverride?.takeIf { it.isNotEmpty() } ?: "$cleanBase/app"
        currentLoadedUrl = targetAppUrl

        Log.i(TAG, "[WEBVIEW_CONNECTED] Connecting WebView to $targetAppUrl")
        hideLoadingScreen()
        layoutServerConfig.visibility = View.GONE
        layoutError.visibility = View.GONE
        webView.visibility = View.VISIBLE
        fabSettings.visibility = View.VISIBLE

        webView.loadUrl(targetAppUrl)
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun setupWebView() {
        val settings = webView.settings
        settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            databaseEnabled = true
            loadWithOverviewMode = true
            useWideViewPort = true
            allowFileAccess = false       // Block file:// access
            allowContentAccess = false    // Block content:// access
            mediaPlaybackRequiresUserGesture = false
            mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW
            cacheMode = WebSettings.LOAD_DEFAULT
        }

        // Normalize User-Agent by removing Version/X.X so Google OAuth does not reject WebView with disallowed_useragent
        val defaultUa = settings.userAgentString
        settings.userAgentString = defaultUa.replace(Regex("Version/\\d+\\.\\d+\\s?"), "")

        // Enable cookies and session persistence; disallow 3rd-party cookies
        val cookieManager = CookieManager.getInstance()
        cookieManager.setAcceptCookie(true)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
            cookieManager.setAcceptThirdPartyCookies(webView, false)
        }

        // Expose secure native share bridge strictly validated by page & target origin
        webView.addJavascriptInterface(ClipFlowAndroidBridge(), "ClipFlowAndroid")

        // WebChromeClient: file picker & progress
        webView.webChromeClient = object : WebChromeClient() {
            override fun onProgressChanged(view: WebView?, newProgress: Int) {
                if (newProgress < 100) {
                    progressBar.visibility = View.VISIBLE
                    progressBar.progress = newProgress
                } else {
                    progressBar.visibility = View.GONE
                }
            }

            override fun onShowFileChooser(
                webView: WebView?,
                filePathCallback: ValueCallback<Array<Uri>>?,
                fileChooserParams: FileChooserParams?
            ): Boolean {
                fileUploadCallback?.onReceiveValue(null)
                fileUploadCallback = filePathCallback

                val videoMimes = arrayOf(
                    "video/mp4", "video/quicktime", "video/x-matroska", "video/webm",
                    "video/x-m4v", "video/mp2t", "video/x-flv", "video/3gpp", "video/x-msvideo"
                )
                val acceptTypes = fileChooserParams?.acceptTypes
                    ?.flatMap { it.split(",") }
                    ?.map { it.trim() }
                    ?.filter { it.isNotEmpty() && it != "*/*" }
                    ?: emptyList()
                val wantsOnlyVideo = acceptTypes.isEmpty() || acceptTypes.all {
                    it.startsWith("video/", ignoreCase = true) ||
                        it.equals(".mp4", ignoreCase = true) ||
                        it.equals(".mov", ignoreCase = true) ||
                        it.equals(".mkv", ignoreCase = true) ||
                        it.equals(".webm", ignoreCase = true)
                }

                val intent = Intent(Intent.ACTION_GET_CONTENT).apply {
                    addCategory(Intent.CATEGORY_OPENABLE)
                    if (wantsOnlyVideo) {
                        type = "video/*"
                        putExtra(Intent.EXTRA_MIME_TYPES, videoMimes)
                    } else {
                        type = "*/*"
                        val resolvedMimes = acceptTypes.map { t ->
                            when {
                                t.contains("/") -> t
                                t.equals(".txt", ignoreCase = true) -> "text/plain"
                                t.equals(".json", ignoreCase = true) -> "application/json"
                                t.equals(".jpg", ignoreCase = true) || t.equals(".jpeg", ignoreCase = true) -> "image/jpeg"
                                t.equals(".png", ignoreCase = true) -> "image/png"
                                else -> "*/*"
                            }
                        }.distinct().toTypedArray()
                        putExtra(Intent.EXTRA_MIME_TYPES, resolvedMimes)
                    }
                    if (fileChooserParams?.mode == FileChooserParams.MODE_OPEN_MULTIPLE) {
                        putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true)
                    }
                }

                try {
                    val title = if (wantsOnlyVideo) "Select Video for ClipFlow" else "Select File for ClipFlow"
                    filePickerLauncher.launch(Intent.createChooser(intent, title))
                    return true
                } catch (e: Exception) {
                    fileUploadCallback = null
                    return false
                }
            }
        }

        // WebViewClient: URL navigation, errors, cookies, redirect protection, subresource interception
        webView.webViewClient = object : WebViewClient() {
            override fun onPageStarted(view: WebView?, url: String?, favicon: Bitmap?) {
                super.onPageStarted(view, url, favicon)
                progressBar.visibility = View.VISIBLE
            }

            override fun onPageFinished(view: WebView?, url: String?) {
                super.onPageFinished(view, url)
                progressBar.visibility = View.GONE
                CookieManager.getInstance().flush()
            }

            override fun onReceivedError(
                view: WebView?,
                request: WebResourceRequest?,
                error: WebResourceError?
            ) {
                super.onReceivedError(view, request, error)
                if (request?.isForMainFrame == true) {
                    val desc = error?.description?.toString() ?: "Connection failed"
                    showErrorScreen("$desc\n\nTarget: ${request.url}")
                }
            }

            override fun shouldInterceptRequest(
                view: WebView?,
                request: WebResourceRequest?
            ): WebResourceResponse? {
                val uri = request?.url ?: return null
                val trustedOriginUri = resolveTrustedUri()

                // Allow http/https, strictly verified same-origin blob: media previews, and safe raster data:image/* thumbnails;
                // block content://, file://, javascript:, cross-origin blob:, HTML/SVG data:, and unknown schemes.
                if (!isAllowedSubresourceUri(uri, trustedOriginUri, request.isForMainFrame)) {
                    return WebResourceResponse(
                        "text/plain",
                        "UTF-8",
                        403,
                        "Forbidden",
                        null,
                        ByteArrayInputStream(ByteArray(0))
                    )
                }

                return super.shouldInterceptRequest(view, request)
            }

            override fun shouldOverrideUrlLoading(view: WebView?, request: WebResourceRequest?): Boolean {
                val targetUri = request?.url ?: return false
                val scheme = targetUri.scheme?.lowercase() ?: ""
                val trustedUri = resolveTrustedUri()

                // 1. Handle custom scheme clipflow://
                if (scheme == "clipflow") {
                    handleCustomSchemeUri(targetUri)
                    return true
                }

                // 2. Strictly block dangerous and non-http schemes (javascript:, file:, content:, etc.)
                if (scheme != "http" && scheme != "https") {
                    return true // Block and consume
                }

                // 3. Exact Origin comparison: scheme, host, and effective port
                val isSameOrigin = ServerConfigHelper.isSameOrigin(targetUri, trustedUri)

                if (isSameOrigin) {
                    // Allowed inside WebView (same-origin app navigation)
                    return false
                }

                // 4. Allowed OAuth identity providers during sign-in
                val host = targetUri.host?.lowercase() ?: ""
                val isGoogleAuth = scheme == "https" && (
                    host == "accounts.google.com" ||
                    host.endsWith(".accounts.google.com") ||
                    host == "accounts.youtube.com"
                )
                if (isGoogleAuth) {
                    // Allow OAuth navigation inside WebView to preserve cookies & return to app
                    return false
                }

                // 5. For external navigation or cross-origin redirects:
                // Never allow cross-origin navigation inside the privileged WebView container
                if (scheme == "https") {
                    return try {
                        val intent = Intent(Intent.ACTION_VIEW, targetUri)
                        startActivity(intent)
                        true
                    } catch (e: Exception) {
                        false
                    }
                }

                // 6. Block arbitrary untrusted plain HTTP navigation and HTTP redirects
                return true
            }
        }

        // DownloadListener: direct same-origin streaming to Downloads/ClipFlow with scoped cookies & redirect protection
        webView.setDownloadListener { url, userAgent, contentDisposition, mimetype, _ ->
            lifecycleScope.launch {
                handleFileDownload(url, userAgent, contentDisposition, mimetype)
            }
        }
    }

    private fun resolveTrustedUri(): Uri? {
        return serverHelper.getSavedServerUri()
            ?: currentLoadedUrl.takeIf { it.isNotEmpty() }?.let {
                try { Uri.parse(it) } catch (_: Exception) { null }
            }
    }

    private inner class ClipFlowAndroidBridge {
        @JavascriptInterface
        fun shareClip(rawUrl: String, fileName: String, title: String) {
            lifecycleScope.launch {
                val pageOrigin = withContext(Dispatchers.Main) {
                    webView.url?.let { try { Uri.parse(it) } catch (_: Exception) { null } }
                }
                val trustedUri = resolveTrustedUri()
                val targetUri = try { Uri.parse(rawUrl) } catch (_: Exception) { null }
                if (!ServerConfigHelper.isSameOrigin(pageOrigin, trustedUri) ||
                    !ServerConfigHelper.isSameOrigin(targetUri, trustedUri)
                ) {
                    Toast.makeText(this@MainActivity, "Share blocked for untrusted origin", Toast.LENGTH_SHORT).show()
                    return@launch
                }
                val ua = withContext(Dispatchers.Main) { webView.settings.userAgentString }
                downloadAndShareClip(rawUrl, ua, fileName, title)
            }
        }
    }

    private fun sanitizeFileName(raw: String, fallback: String): String {
        val cleaned = raw.trim().replace(Regex("[/\\\\:*?\"<>|\\x00-\\x1F]+"), "_").trim('.')
        return if (cleaned.isNotEmpty()) cleaned else fallback
    }

    private fun extractDispositionFilename(header: String?): String? {
        if (header.isNullOrBlank()) return null
        val utf8Match = Regex("filename\\*\\s*=\\s*UTF-8''([^;\\s]+)", RegexOption.IGNORE_CASE).find(header)
        if (utf8Match != null) {
            return try {
                URLDecoder.decode(utf8Match.groupValues[1], "UTF-8")
            } catch (_: Exception) {
                utf8Match.groupValues[1]
            }
        }
        val quoted = Regex("filename\\s*=\\s*\"([^\"]+)\"", RegexOption.IGNORE_CASE).find(header)
        if (quoted != null) return quoted.groupValues[1]
        val plain = Regex("filename\\s*=\\s*([^;\\s]+)", RegexOption.IGNORE_CASE).find(header)
        return plain?.groupValues?.getOrNull(1)?.trim('"')
    }

    private fun resolveMimeType(fileName: String, headerContentType: String?, fallbackMime: String): String {
        val lower = fileName.lowercase()
        return when {
            lower.endsWith(".mp4") -> "video/mp4"
            lower.endsWith(".zip") -> "application/zip"
            lower.endsWith(".jpg") || lower.endsWith(".jpeg") -> "image/jpeg"
            !headerContentType.isNullOrBlank() -> headerContentType.substringBefore(';').trim()
            fallbackMime.isNotBlank() -> fallbackMime
            else -> "application/octet-stream"
        }
    }

    private fun executeSameOriginGet(
        rawUrl: String,
        userAgent: String,
        trustedUri: Uri?
    ): Response {
        var currentUrl = rawUrl
        for (hop in 0..1) {
            val currentUri = Uri.parse(currentUrl)
            if (!ServerConfigHelper.isSameOrigin(currentUri, trustedUri)) {
                throw SecurityException("Download blocked: cross-origin redirect detected")
            }
            val reqBuilder = Request.Builder().url(currentUrl).get()
            if (userAgent.isNotBlank()) {
                reqBuilder.header("User-Agent", userAgent)
            }
            val cookies = CookieManager.getInstance().getCookie(currentUrl)
            if (!cookies.isNullOrEmpty()) {
                reqBuilder.header("Cookie", cookies)
            }
            val resp = downloadStreamClient.newCall(reqBuilder.build()).execute()
            if (resp.isRedirect) {
                val loc = resp.header("Location") ?: ""
                resp.close()
                if (loc.isBlank() || hop == 1) {
                    throw SecurityException("Download blocked: unexpected redirect")
                }
                val resolved = java.net.URI(currentUrl).resolve(loc).toString()
                if (!ServerConfigHelper.isSameOrigin(Uri.parse(resolved), trustedUri)) {
                    throw SecurityException("Download blocked: cross-origin redirect detected")
                }
                currentUrl = resolved
                continue
            }
            return resp
        }
        throw IllegalStateException("Failed to resolve download")
    }

    private suspend fun handleFileDownload(
        url: String,
        userAgent: String,
        contentDisposition: String,
        mimetype: String
    ) {
        val downloadUri = try {
            Uri.parse(url)
        } catch (e: Exception) {
            return
        }

        val scheme = downloadUri.scheme?.lowercase() ?: ""
        if (scheme != "http" && scheme != "https") {
            return
        }

        val trustedUri = resolveTrustedUri()
        val isSameOrigin = ServerConfigHelper.isSameOrigin(downloadUri, trustedUri)

        if (!isSameOrigin) {
            if (scheme == "https") {
                try {
                    startActivity(Intent(Intent.ACTION_VIEW, downloadUri))
                } catch (e: Exception) {
                    Toast.makeText(this, "External download blocked", Toast.LENGTH_SHORT).show()
                }
            }
            return
        }

        val path = downloadUri.path ?: ""
        val isClipFlowEndpoint = path.startsWith("/media/") || path.startsWith("/api/clips.zip") || path.endsWith(".mp4")
        if (!isClipFlowEndpoint) {
            return
        }

        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) {
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.WRITE_EXTERNAL_STORAGE) != PackageManager.PERMISSION_GRANTED) {
                pendingDownload = PendingDownload(url, userAgent, contentDisposition, mimetype)
                storagePermissionLauncher.launch(Manifest.permission.WRITE_EXTERNAL_STORAGE)
                return
            }
        }

        val initialGuess = URLUtil.guessFileName(url, contentDisposition, mimetype).let {
            if (path.endsWith(".mp4") && !it.endsWith(".mp4", ignoreCase = true)) "$it.mp4" else it
        }
        Toast.makeText(this, "${getString(R.string.download_started)} ($initialGuess)", Toast.LENGTH_SHORT).show()

        val result: Result<String> = withContext(Dispatchers.IO) {
            runCatching {
                executeSameOriginGet(url, userAgent, trustedUri).use { resp ->
                    if (!resp.isSuccessful) {
                        throw IllegalStateException("HTTP ${resp.code}")
                    }
                    val body = resp.body ?: throw IllegalStateException("Empty response body")
                    val serverDisp = resp.header("Content-Disposition")
                    val rawName = extractDispositionFilename(serverDisp)
                        ?: extractDispositionFilename(contentDisposition)
                        ?: initialGuess
                    val safeName = sanitizeFileName(rawName, if (path.contains("clips.zip")) "clipflow-clips.zip" else "clipflow-clip.mp4")
                    val finalName = if (path.contains("clips.zip") && !safeName.endsWith(".zip", ignoreCase = true)) {
                        "$safeName.zip"
                    } else if (path.endsWith(".mp4") && !safeName.endsWith(".mp4", ignoreCase = true)) {
                        "$safeName.mp4"
                    } else {
                        safeName
                    }
                    val mime = resolveMimeType(finalName, resp.header("Content-Type"), mimetype)
                    val isVideoDownload = mime.startsWith("video/", ignoreCase = true) ||
                        finalName.endsWith(".mp4", ignoreCase = true)
                    val folderLabel = if (isVideoDownload) "Movies/ClipFlow" else "Downloads/ClipFlow"

                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                        val resolver = contentResolver
                        val collectionUri = if (isVideoDownload) {
                            MediaStore.Video.Media.EXTERNAL_CONTENT_URI
                        } else {
                            MediaStore.Downloads.EXTERNAL_CONTENT_URI
                        }
                        val relativePath = if (isVideoDownload) {
                            "${Environment.DIRECTORY_MOVIES}/ClipFlow"
                        } else {
                            "${Environment.DIRECTORY_DOWNLOADS}/ClipFlow"
                        }
                        val values = ContentValues().apply {
                            put(MediaStore.MediaColumns.DISPLAY_NAME, finalName)
                            put(MediaStore.MediaColumns.MIME_TYPE, mime)
                            put(MediaStore.MediaColumns.RELATIVE_PATH, relativePath)
                            put(MediaStore.MediaColumns.IS_PENDING, 1)
                        }
                        val itemUri = resolver.insert(collectionUri, values)
                            ?: throw IllegalStateException("Could not create file in $folderLabel")
                        try {
                            resolver.openOutputStream(itemUri)?.use { out ->
                                body.byteStream().copyTo(out)
                            } ?: throw IllegalStateException("Could not open $folderLabel stream")
                            val readyValues = ContentValues().apply {
                                put(MediaStore.MediaColumns.IS_PENDING, 0)
                            }
                            resolver.update(itemUri, readyValues, null, null)
                        } catch (t: Throwable) {
                            resolver.delete(itemUri, null, null)
                            throw t
                        }
                        "$folderLabel: $finalName"
                    } else {
                        @Suppress("DEPRECATION")
                        val baseDir = Environment.getExternalStoragePublicDirectory(
                            if (isVideoDownload) Environment.DIRECTORY_MOVIES else Environment.DIRECTORY_DOWNLOADS
                        )
                        val dir = File(baseDir, "ClipFlow").apply { mkdirs() }
                        var dest = File(dir, finalName)
                        if (dest.exists()) {
                            val dot = finalName.lastIndexOf('.')
                            val stem = if (dot > 0) finalName.substring(0, dot) else finalName
                            val ext = if (dot > 0) finalName.substring(dot) else ""
                            var n = 2
                            while (dest.exists() && n < 100) {
                                dest = File(dir, "$stem ($n)$ext")
                                n++
                            }
                        }
                        FileOutputStream(dest).use { out ->
                            body.byteStream().copyTo(out)
                        }
                        MediaScannerConnection.scanFile(
                            this@MainActivity,
                            arrayOf(dest.absolutePath),
                            arrayOf(mime),
                            null
                        )
                        "$folderLabel: ${dest.name}"
                    }
                }
            }
        }

        result.onSuccess { savedLocation ->
            Toast.makeText(this, "Saved to $savedLocation", Toast.LENGTH_LONG).show()
        }.onFailure { err ->
            val msg = if (err is SecurityException) {
                err.message ?: "Download blocked: cross-origin redirect detected"
            } else {
                "Download error: ${err.localizedMessage ?: err.message ?: "Failed"}"
            }
            Toast.makeText(this, msg, Toast.LENGTH_LONG).show()
        }
    }

    private suspend fun downloadAndShareClip(
        url: String,
        userAgent: String,
        fallbackName: String,
        title: String
    ) {
        val trustedUri = resolveTrustedUri()
        val result: Result<Pair<File, String>> = withContext(Dispatchers.IO) {
            runCatching {
                val shareDir = File(cacheDir, "shared_clips").apply { mkdirs() }
                val cutoff = System.currentTimeMillis() - 60 * 60 * 1000L
                shareDir.listFiles()?.forEach { f ->
                    if (f.isFile && f.lastModified() < cutoff) {
                        f.delete()
                    }
                }
                executeSameOriginGet(url, userAgent, trustedUri).use { resp ->
                    if (!resp.isSuccessful) {
                        throw IllegalStateException("HTTP ${resp.code}")
                    }
                    val body = resp.body ?: throw IllegalStateException("Empty clip response")
                    val headerName = extractDispositionFilename(resp.header("Content-Disposition"))
                    val safeName = sanitizeFileName(headerName ?: fallbackName, "clipflow-clip.mp4")
                    val finalName = if (!safeName.endsWith(".mp4", ignoreCase = true) && url.contains(".mp4")) {
                        "$safeName.mp4"
                    } else {
                        safeName
                    }
                    val mime = resolveMimeType(finalName, resp.header("Content-Type"), "video/mp4")
                    val outFile = File(shareDir, finalName)
                    FileOutputStream(outFile).use { out ->
                        body.byteStream().copyTo(out)
                    }
                    Pair(outFile, mime)
                }
            }
        }

        result.onSuccess { (file, mime) ->
            try {
                val contentUri = FileProvider.getUriForFile(this, "${packageName}.fileprovider", file)
                val shareIntent = Intent(Intent.ACTION_SEND).apply {
                    type = mime
                    putExtra(Intent.EXTRA_STREAM, contentUri)
                    putExtra(Intent.EXTRA_SUBJECT, title.ifBlank { "ClipFlow Clip" })
                    addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                }
                startActivity(Intent.createChooser(shareIntent, title.ifBlank { "Share Clip" }))
            } catch (e: Exception) {
                Toast.makeText(this, "Could not open Android share sheet: ${e.localizedMessage}", Toast.LENGTH_LONG).show()
            }
        }.onFailure { err ->
            Toast.makeText(this, "Share failed: ${err.localizedMessage ?: err.message}", Toast.LENGTH_LONG).show()
        }
    }

    private fun setupBackNavigation() {
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                // 0. If engine is starting up, minimize app rather than interrupting
                if (layoutLoading.visibility == View.VISIBLE) {
                    moveTaskToBack(true)
                    return
                }

                // 1. If Server Config is showing but a server is already loaded, close config
                if (layoutServerConfig.visibility == View.VISIBLE && currentLoadedUrl.isNotEmpty()) {
                    layoutServerConfig.visibility = View.GONE
                    webView.visibility = View.VISIBLE
                    fabSettings.visibility = View.VISIBLE
                    return
                }

                // 2. If Error layout is showing, go to server config
                if (layoutError.visibility == View.VISIBLE) {
                    showServerConfigScreen()
                    return
                }

                // 3. If WebView has an open dialog (e.g. #clipdlg, #cropdlg), close it via JS
                if (webView.visibility == View.VISIBLE) {
                    webView.evaluateJavascript(
                        """
                        (function() {
                            const dlg = document.querySelector('dialog[open]');
                            if (dlg) { dlg.close(); return true; }
                            const closeBtn = document.querySelector('.modal.open .close, .dialog.open .close');
                            if (closeBtn) { closeBtn.click(); return true; }
                            return false;
                        })();
                        """.trimIndent()
                    ) { result ->
                        if (result == "true") {
                            // Dialog was closed by JavaScript
                            return@evaluateJavascript
                        }

                        // 4. Otherwise standard web history navigation
                        if (webView.canGoBack()) {
                            webView.goBack()
                        } else {
                            // 5. Double tap to exit
                            val now = System.currentTimeMillis()
                            if (now - lastBackPressTime < 2000) {
                                isEnabled = false
                                finish()
                            } else {
                                lastBackPressTime = now
                                Toast.makeText(this@MainActivity, getString(R.string.press_again_to_exit), Toast.LENGTH_SHORT).show()
                            }
                        }
                    }
                } else {
                    finish()
                }
            }
        })
    }

    override fun onDestroy() {
        fileUploadCallback?.onReceiveValue(null)
        fileUploadCallback = null
        if (::webView.isInitialized) {
            webView.stopLoading()
            (webView.parent as? ViewGroup)?.removeView(webView)
            webView.destroy()
        }
        super.onDestroy()
    }
}
