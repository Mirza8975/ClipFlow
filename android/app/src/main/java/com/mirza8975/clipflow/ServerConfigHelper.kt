package com.mirza8975.clipflow

import android.content.Context
import android.net.Uri
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import java.util.concurrent.TimeUnit

sealed class ConnectionResult {
    object Success : ConnectionResult()
    data class Error(val message: String) : ConnectionResult()
}

class ServerConfigHelper(private val context: Context) {

    private val prefs = context.getSharedPreferences("clipflow_server_prefs", Context.MODE_PRIVATE)

    companion object {
        private const val KEY_SERVER_URL = "saved_server_url"

        /**
         * Compares two Uris by parsed scheme, lowercase host, and effective port.
         * Prevents string prefix spoofing (e.g. http://192.168.1.50:8765.attacker.com).
         */
        fun isSameOrigin(targetUri: Uri?, baseUri: Uri?): Boolean {
            if (targetUri == null || baseUri == null) return false

            val targetScheme = targetUri.scheme?.lowercase() ?: return false
            val baseScheme = baseUri.scheme?.lowercase() ?: return false
            if (targetScheme != baseScheme) return false

            val targetHost = targetUri.host?.lowercase() ?: return false
            val baseHost = baseUri.host?.lowercase() ?: return false
            if (targetHost != baseHost) return false

            val targetPort = if (targetUri.port != -1) targetUri.port else (if (targetScheme == "https") 443 else 80)
            val basePort = if (baseUri.port != -1) baseUri.port else (if (baseScheme == "https") 443 else 80)

            return targetPort == basePort
        }

        /**
         * Returns true when [url] is blank or targets the embedded on-device loopback engine.
         */
        fun isLocalEngineUrl(url: String?): Boolean {
            val raw = url?.trim() ?: return true
            if (raw.isEmpty()) return true
            val host = try {
                Uri.parse(raw).host?.lowercase()
            } catch (_: Exception) {
                null
            } ?: return false
            return host == "127.0.0.1" || host == "localhost" || host == "::1"
        }
    }

    private val client = OkHttpClient.Builder()
        .connectTimeout(4, TimeUnit.SECONDS)
        .readTimeout(4, TimeUnit.SECONDS)
        .build()

    fun getSavedServerUrl(): String? {
        return prefs.getString(KEY_SERVER_URL, null)?.trim()?.removeSuffix("/")
    }

    fun getSavedServerUri(): Uri? {
        val url = getSavedServerUrl() ?: return null
        return try {
            Uri.parse(url)
        } catch (e: Exception) {
            null
        }
    }

    fun saveServerUrl(url: String) {
        val cleanUrl = url.trim().removeSuffix("/")
        prefs.edit().putString(KEY_SERVER_URL, cleanUrl).apply()
    }

    fun clearSavedServerUrl() {
        prefs.edit().remove(KEY_SERVER_URL).apply()
    }

    /**
     * Validates URL syntax and security requirements.
     * Enforces HTTPS for all public domains and non-RFC1918 addresses.
     * Plain HTTP is strictly restricted to verified RFC1918 private IPv4 subnets and localhost.
     * Hostname suffixes (e.g. .local, .lan) are never blindly trusted.
     */
    fun validateUrlSecurity(rawUrl: String): Pair<Boolean, String?> {
        val trimmed = rawUrl.trim()
        if (trimmed.isEmpty()) {
            return Pair(false, "Server URL cannot be empty.")
        }

        val uri = try {
            Uri.parse(trimmed)
        } catch (e: Exception) {
            return Pair(false, "Invalid URL format.")
        }

        val scheme = uri.scheme?.lowercase() ?: ""
        if (scheme != "http" && scheme != "https") {
            return Pair(false, "URL must start with http:// or https://")
        }

        val host = uri.host?.lowercase() ?: ""
        if (host.isEmpty()) {
            return Pair(false, "Invalid host address.")
        }

        // Validate whether host is strictly an RFC1918 private IPv4 address or loopback
        val isRfc1918OrLoopback = isStrictRfc1918OrLoopback(host)

        if (scheme == "http" && !isRfc1918OrLoopback) {
            return Pair(
                false,
                "Security policy: Public domains and public IPs must use HTTPS (https://). Plain HTTP is only permitted for explicit private LAN IPs (192.168.x.x, 10.x.x.x, 172.16-31.x.x) or localhost."
            )
        }

        return Pair(true, null)
    }

    /**
     * Strictly validates whether a host string is a numeric RFC 1918 private IPv4 address
     * (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16) or standard loopback.
     * Arbitrary hostname suffixes (.local, .lan, etc.) are NOT accepted as private IPs.
     */
    private fun isStrictRfc1918OrLoopback(host: String): Boolean {
        // Standard loopback and Android emulator loopback
        if (host == "localhost" || host == "127.0.0.1" || host == "10.0.2.2") {
            return true
        }

        // Verify numeric IPv4 pattern
        val ipParts = host.split(".")
        if (ipParts.size != 4) {
            return false
        }

        val octets = ipParts.map { it.toIntOrNull() }
        if (octets.any { it == null || it !in 0..255 }) {
            return false
        }

        val o1 = octets[0]!!
        val o2 = octets[1]!!

        // RFC 1918: 10.0.0.0/8
        if (o1 == 10) return true

        // RFC 1918: 172.16.0.0/12 (172.16.0.0 to 172.31.255.255)
        if (o1 == 172 && o2 in 16..31) return true

        // RFC 1918: 192.168.0.0/16
        if (o1 == 192 && o2 == 168) return true

        // 127.0.0.0/8 loopback range
        if (o1 == 127) return true

        return false
    }

    /**
     * Checks if the server is healthy by calling GET /healthz
     */
    suspend fun checkHealth(serverUrl: String): ConnectionResult = withContext(Dispatchers.IO) {
        val cleanUrl = serverUrl.trim().removeSuffix("/")
        val healthzUrl = "$cleanUrl/healthz"

        try {
            val request = Request.Builder()
                .url(healthzUrl)
                .get()
                .build()

            client.newCall(request).execute().use { response ->
                if (response.isSuccessful) {
                    val body = response.body?.string() ?: ""
                    if (body.contains("\"ok\":true") || body.contains("\"ok\": true")) {
                        ConnectionResult.Success
                    } else {
                        ConnectionResult.Error("Server returned unexpected response: ${response.code}")
                    }
                } else {
                    ConnectionResult.Error("HTTP Error ${response.code}: ${response.message}")
                }
            }
        } catch (e: Exception) {
            ConnectionResult.Error(e.localizedMessage ?: "Connection timed out or failed.")
        }
    }
}
