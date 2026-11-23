# ClipFlow Android Application

Official Android native shell for **ClipFlow**, packaged with modern Android WebView, native video file chooser, secure `DownloadManager` clip downloads, and configurable Wi-Fi PC server connection.

- **Package ID**: `com.mirza8975.clipflow`
- **Minimum OS**: Android 7.0 (API 24)
- **Target OS**: Android 14 (API 34)
- **Language**: Kotlin

---

## Security Architecture & Policies

1. **Zero JavaScript Bridge**: No `@JavascriptInterface` or `addJavascriptInterface` is used anywhere in the project. This completely eliminates iframe injection, reflective method invocation, and cross-document context vulnerabilities.
2. **Exact Origin Validation**: Navigation, redirects, and downloads are strictly validated using parsed scheme, lowercase host, and effective port (`ServerConfigHelper.isSameOrigin`). String-based prefix matching is avoided to prevent prefix spoofing.
3. **Download Cookie Scoping & Redirect Defense**: Session cookies (`peakcut_session`) are attached to `DownloadManager.Request` **only** after verifying that the download destination matches the exact configured server origin and that no cross-origin redirects exist.
4. **Scheme & Subresource Interception**: `shouldOverrideUrlLoading` and `shouldInterceptRequest` strictly block non-HTTP(S) schemes (`javascript:`, `file:`, `content:`, `data:`). External HTTPS links (e.g. Google OAuth) open in the system browser.
5. **Cleartext HTTP Policy**:
   - **Production Release Builds** (`assembleRelease`): Cleartext HTTP is **strictly disabled** at the platform level (`src/main/res/xml/network_security_config.xml` has `cleartextTrafficPermitted="false"`). All production traffic must use verified TLS / HTTPS.
   - **Debug / Local Wi-Fi Testing** (`assembleDebug`): Cleartext HTTP is enabled in `src/debug/res/xml/network_security_config.xml` to allow connecting to PC servers (`http://192.168.x.x:8765`) over local Wi-Fi. In addition, application code (`ServerConfigHelper`) strictly restricts plain HTTP to RFC 1918 private IPv4 subnets (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`) and localhost.

---

## Connecting Over Local Wi-Fi

1. **Start ClipFlow on your PC**:
   ```bash
   docker run -d -p 8765:8765 -v clipflow_data:/data ghcr.io/mirza8975/clipflow:latest
   ```
2. **Find your PC's local IP address**:
   - **Windows**: Run `ipconfig` (e.g., `192.168.1.50`).
   - **Linux / macOS**: Run `ip a` or `ifconfig` (e.g., `192.168.1.50`).
3. **Ensure PC Firewall allows inbound port 8765**:
   - **Windows**: Add an inbound firewall rule allowing TCP port `8765`.
   - **Linux**: `sudo ufw allow 8765/tcp`.
4. **Launch ClipFlow on your Android phone**:
   - Connect your phone to the same Wi-Fi network.
   - Enter your PC's address: `http://192.168.1.50:8765` (replace with your actual PC IP).
   - Tap **Test Connection** (pings `/healthz`; confirms "Connected! Server is healthy").
   - Tap **Connect**.

---

## Building the APK

### Method 1: GitHub Actions (Recommended, Automated)
Every push or manual trigger on GitHub builds the APK on Ubuntu runners and uploads it as a downloadable artifact:
1. Go to repository: `https://github.com/Mirza8975/ClipFlow/actions`
2. Select **ClipFlow Android Build**.
3. Download the **`ClipFlow-debug-apk`** artifact containing `app-debug.apk`.

### Method 2: Android Studio
1. Open Android Studio.
2. Select **Open** and choose the `/android` directory.
3. Allow Gradle to sync.
4. Select **Build → Build Bundle(s) / APK(s) → Build APK(s)**.

### Method 3: Command Line (Gradle)
```bash
cd android
./gradlew assembleDebug
```
Output APK location:
`android/app/build/outputs/apk/debug/app-debug.apk`
