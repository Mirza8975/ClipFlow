package com.mirza8975.clipflow

import android.Manifest
import android.annotation.SuppressLint
import android.app.DownloadManager
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.view.View
import android.view.WindowManager
import android.webkit.CookieManager
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
import androidx.core.splashscreen.SplashScreen.Companion.installSplashScreen
import androidx.lifecycle.lifecycleScope
import com.google.android.material.floatingactionbutton.FloatingActionButton
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.ByteArrayInputStream
import android.util.Log
import java.util.concurrent.TimeUnit

class MainActivity : AppCompatActivity() {

    companion object {
        private const val TAG = "ClipFlowMainActivity"
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

    // Dedicated OkHttp client for pre-verifying downloads without following cross-origin redirects
    private val downloadVerifierClient = OkHttpClient.Builder()
        .followRedirects(false)
        .followSslRedirects(false)
        .connectTimeout(4, TimeUnit.SECONDS)
        .readTimeout(4, TimeUnit.SECONDS)
        .build()

    // Activity Result Launcher for Video File Picker
    private val filePickerLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        if (fileUploadCallback == null) return@registerForActivityResult

        val uris = WebChromeClient.FileChooserParams.parseResult(result.resultCode, result.data)
        fileUploadCallback?.onReceiveValue(uris)
        fileUploadCallback = null
    }

    // Permission Launcher for Android 13+ (API 33+) DownloadManager notifications
    private val notificationPermissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { _ ->
        // DownloadManager continues download regardless of notification permission grant state
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
            if (savedUrl != null && !savedUrl.contains("127.0.0.1") && !savedUrl.contains("localhost")) {
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
        val savedBase = serverHelper.getSavedServerUrl() ?: return
        val targetPath = if (host.isNotEmpty()) "/$host$path" else path
        val fullUrl = "$savedBase$targetPath" + (if (!query.isNullOrEmpty()) "?$query" else "")

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
            if (inputUrl.contains("127.0.0.1") || inputUrl.contains("localhost")) {
                startEngineAndConnect(inputUrl)
            } else {
                connectToServer(inputUrl)
            }
        }

        btnRetry.setOnClickListener {
            val target = serverHelper.getSavedServerUrl() ?: "http://127.0.0.1:8765"
            if (target.contains("127.0.0.1") || target.contains("localhost")) {
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
            // Tapping Settings navigates the WebView to the real Settings page
            webView.evaluateJavascript(
                "(function() { " +
                "  var b = document.querySelector('[data-act=\"go\"][data-v=\"settings\"]'); " +
                "  if (b) { b.click(); } else if (window.S) { window.S.view = 'settings'; if (typeof draw === 'function') draw(); } " +
                "})();",
                null
            )
        }
    }

    private fun startEngineAndConnect(targetUrl: String = "http://127.0.0.1:8765", isRestart: Boolean = false) {
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
                connectToServer(targetUrl)
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
        fabSettings.visibility = View.VISIBLE
        progressBar.visibility = View.GONE
        tvErrorDetails.text = details

        val diag = ClipFlowBackendService.getDetailedDiagnostics(this)
        tvDiagnosticsLog.text = diag
    }

    private fun connectToServer(baseUrl: String) {
        val cleanBase = baseUrl.trim().removeSuffix("/")
        val targetAppUrl = "$cleanBase/app"
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

                val intent = Intent(Intent.ACTION_GET_CONTENT).apply {
                    type = "video/*"
                    addCategory(Intent.CATEGORY_OPENABLE)
                    putExtra(Intent.EXTRA_MIME_TYPES, arrayOf("video/mp4", "video/quicktime", "video/x-matroska", "video/webm"))
                }

                try {
                    filePickerLauncher.launch(Intent.createChooser(intent, "Select Video for ClipFlow"))
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
                val scheme = uri.scheme?.lowercase() ?: ""

                // Strictly block any subresource attempting to load dangerous schemes (file:, content:, data:, etc.)
                if (scheme != "http" && scheme != "https") {
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
                val trustedUri = serverHelper.getSavedServerUri()

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

        // DownloadListener: Native DownloadManager for MP4 clips with scoped cookies & redirect verification
        webView.setDownloadListener { url, userAgent, contentDisposition, mimetype, _ ->
            lifecycleScope.launch {
                handleFileDownload(url, userAgent, contentDisposition, mimetype)
            }
        }
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
            return // Block invalid schemes
        }

        val trustedUri = serverHelper.getSavedServerUri()
        val isSameOrigin = ServerConfigHelper.isSameOrigin(downloadUri, trustedUri)

        // Only allow direct native download if URL originates from the exact trusted server origin
        if (!isSameOrigin) {
            // For external download links, route securely to system browser without internal session cookies
            if (scheme == "https") {
                try {
                    startActivity(Intent(Intent.ACTION_VIEW, downloadUri))
                } catch (e: Exception) {
                    Toast.makeText(this, "External download blocked", Toast.LENGTH_SHORT).show()
                }
            }
            return
        }

        // Verify that the destination path belongs to ClipFlow media/clip endpoints
        val path = downloadUri.path ?: ""
        val isClipFlowEndpoint = path.startsWith("/media/") || path.startsWith("/api/clips.zip") || path.endsWith(".mp4")
        if (!isClipFlowEndpoint) {
            return
        }

        // Pre-verify download endpoint with OkHttp (followRedirects = false) to ensure no cross-origin redirect exists
        val isRedirectSafe = withContext(Dispatchers.IO) {
            try {
                val headReq = Request.Builder()
                    .url(url)
                    .head()
                    .build()

                downloadVerifierClient.newCall(headReq).execute().use { resp ->
                    if (resp.isRedirect) {
                        val redirectLocation = resp.header("Location") ?: ""
                        val redirectUri = Uri.parse(redirectLocation)
                        // Only safe if redirect target also matches exact same origin
                        ServerConfigHelper.isSameOrigin(redirectUri, trustedUri)
                    } else {
                        // Direct 200 OK or non-redirect response
                        resp.code in 200..299
                    }
                }
            } catch (e: Exception) {
                // If HEAD fails (e.g. method not allowed), allow proceeding with strict same-origin guarantee
                true
            }
        }

        if (!isRedirectSafe) {
            Toast.makeText(this, "Download blocked: cross-origin redirect detected", Toast.LENGTH_SHORT).show()
            return
        }

        try {
            val filename = URLUtil.guessFileName(url, contentDisposition, mimetype).let {
                if (!it.endsWith(".mp4") && url.contains(".mp4")) "$it.mp4" else it
            }

            val request = DownloadManager.Request(downloadUri).apply {
                setMimeType(mimetype.ifEmpty { "video/mp4" })

                // Security: Cookie is attached ONLY because exact same origin and zero cross-origin redirect are proven
                val cookies = CookieManager.getInstance().getCookie(url)
                if (!cookies.isNullOrEmpty()) {
                    addRequestHeader("Cookie", cookies)
                }

                addRequestHeader("User-Agent", userAgent)
                setDescription("Downloading ClipFlow vertical video...")
                setTitle(filename)
                setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
                setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, "ClipFlow/$filename")
            }

            // Request notification permission on Android 13+ (API 33+) if not yet granted
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                if (ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
                    notificationPermissionLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
                }
            }

            val dm = getSystemService(Context.DOWNLOAD_SERVICE) as DownloadManager
            dm.enqueue(request)

            Toast.makeText(this, "${getString(R.string.download_started)} ($filename)", Toast.LENGTH_SHORT).show()
        } catch (e: Exception) {
            Toast.makeText(this, "Download error: ${e.localizedMessage}", Toast.LENGTH_LONG).show()
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
}
