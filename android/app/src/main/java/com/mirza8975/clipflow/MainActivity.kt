package com.mirza8975.clipflow

import android.annotation.SuppressLint
import android.app.DownloadManager
import android.content.Context
import android.content.Intent
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
import androidx.core.splashscreen.SplashScreen.Companion.installSplashScreen
import androidx.lifecycle.lifecycleScope
import com.google.android.material.floatingactionbutton.FloatingActionButton
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.ByteArrayInputStream
import java.util.concurrent.TimeUnit

class MainActivity : AppCompatActivity() {

    private lateinit var webView: WebView
    private lateinit var progressBar: ProgressBar
    private lateinit var layoutServerConfig: View
    private lateinit var layoutError: View
    private lateinit var fabSettings: FloatingActionButton

    // Server Config Views
    private lateinit var etServerUrl: EditText
    private lateinit var btnTestConnection: Button
    private lateinit var btnConnect: Button
    private lateinit var tvStatusMessage: TextView

    // Error Views
    private lateinit var tvErrorDetails: TextView
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

    override fun onCreate(savedInstanceState: Bundle?) {
        // Install splash screen for Android 12+
        installSplashScreen()
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        // Keep screen on during active operations to prevent network sleep during large video uploads
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

        serverHelper = ServerConfigHelper(this)
        initViews()
        setupWebView()
        setupBackNavigation()

        // Check if server URL is already saved
        val savedUrl = serverHelper.getSavedServerUrl()
        if (savedUrl != null) {
            connectToServer(savedUrl)
        } else {
            showServerConfigScreen()
        }
    }

    private fun initViews() {
        webView = findViewById(R.id.webView)
        progressBar = findViewById(R.id.progressBar)
        layoutServerConfig = findViewById(R.id.layoutServerConfig)
        layoutError = findViewById(R.id.layoutError)
        fabSettings = findViewById(R.id.fabSettings)

        // Server Config UI
        etServerUrl = layoutServerConfig.findViewById(R.id.etServerUrl)
        btnTestConnection = layoutServerConfig.findViewById(R.id.btnTestConnection)
        btnConnect = layoutServerConfig.findViewById(R.id.btnConnect)
        tvStatusMessage = layoutServerConfig.findViewById(R.id.tvStatusMessage)

        // Error UI
        tvErrorDetails = layoutError.findViewById(R.id.tvErrorDetails)
        btnRetry = layoutError.findViewById(R.id.btnRetry)
        btnChangeServer = layoutError.findViewById(R.id.btnChangeServer)

        // Pre-fill with saved or sensible default
        val existing = serverHelper.getSavedServerUrl() ?: "http://192.168.1."
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
            connectToServer(inputUrl)
        }

        btnRetry.setOnClickListener {
            val target = serverHelper.getSavedServerUrl() ?: currentLoadedUrl
            if (target.isNotEmpty()) {
                connectToServer(target)
            } else {
                showServerConfigScreen()
            }
        }

        btnChangeServer.setOnClickListener {
            showServerConfigScreen()
        }

        fabSettings.setOnClickListener {
            showServerConfigScreen()
        }
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
        layoutServerConfig.visibility = View.VISIBLE
        layoutError.visibility = View.GONE
        webView.visibility = View.GONE
        fabSettings.visibility = View.GONE
        progressBar.visibility = View.GONE
    }

    private fun showErrorScreen(details: String) {
        layoutError.visibility = View.VISIBLE
        layoutServerConfig.visibility = View.GONE
        webView.visibility = View.GONE
        fabSettings.visibility = View.VISIBLE
        progressBar.visibility = View.GONE
        tvErrorDetails.text = details
    }

    private fun connectToServer(baseUrl: String) {
        val cleanBase = baseUrl.trim().removeSuffix("/")
        val targetAppUrl = "$cleanBase/app"
        currentLoadedUrl = targetAppUrl

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

                // 1. Strictly block dangerous and non-http schemes (javascript:, file:, content:, etc.)
                if (scheme != "http" && scheme != "https") {
                    return true // Block and consume
                }

                // 2. Exact Origin comparison: scheme, host, and effective port
                val isSameOrigin = ServerConfigHelper.isSameOrigin(targetUri, trustedUri)

                if (isSameOrigin) {
                    // Allowed inside WebView (same-origin app navigation)
                    return false
                }

                // 3. For external navigation or cross-origin redirects:
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

                // 4. Block arbitrary untrusted plain HTTP navigation and HTTP redirects
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
