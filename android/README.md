# ClipFlow Android Application

Official standalone & companion Android client for **ClipFlow 2.0**, bundling an embedded Chaquopy Python 3.11 + FastAPI backend, ARM64 FFmpeg/FFprobe (with `libass` subtitle rendering), QuickJS-NG, and sherpa-onnx offline speech recognition, alongside configurable remote Wi-Fi/HTTPS server support.

- **Package ID**: `com.mirza8975.clipflow` (`.debug` suffix on Debug builds)
- **Minimum OS**: Android 7.0 (API 24)
- **Target OS**: Android 14 (API 34)
- **ABI**: `arm64-v8a`
- **Language**: Kotlin + embedded Python 3.11 (Chaquopy)

---

## Security & Storage Architecture

1. **Origin-Locked Share Bridge**: Only `ClipFlowAndroid.shareClip` is exposed via `@JavascriptInterface`, and both the active WebView page origin and requested media URL must pass `ServerConfigHelper.isSameOrigin` before caching to `cacheDir/shared_clips` and opening a `FileProvider` `ACTION_SEND` share intent.
2. **Exact Origin Validation**: Navigation, redirects, and downloads validate parsed scheme, lowercase host, and effective port (`ServerConfigHelper.isSameOrigin`).
3. **Scoped Downloads & Gallery Visibility**: Same-origin `OkHttp` streaming downloads (with cookie scoping and cross-origin redirect rejection) save `.mp4` video clips to `Movies/ClipFlow` (`MediaStore.Video` on API 29+, `DIRECTORY_MOVIES/ClipFlow` + `MediaScannerConnection` on API 24–28) so clips appear reliably in Gallery/Photos, while multi-clip `.zip` bundles save to `Downloads/ClipFlow` (`MediaStore.Downloads` on API 29+).
4. **Scheme & Subresource Interception**: `shouldOverrideUrlLoading` and `shouldInterceptRequest` block arbitrary `javascript:`, `file:`, `content:`, and non-raster `data:` schemes while permitting strictly validated same-origin `blob:` previews and raster thumbnails.
5. **Cleartext HTTP Policy**:
   - **Production Release Builds** (`assembleRelease`): `src/main/res/xml/network_security_config.xml` sets `cleartextTrafficPermitted="false"` globally and permits cleartext HTTP **only** for `127.0.0.1` and `localhost` (required for the embedded local backend). `ServerConfigHelper.validateUrlSecurity` also enforces HTTPS for all remote servers in Release builds.
   - **Debug / Local Wi-Fi Testing** (`assembleDebug`): `src/debug/res/xml/network_security_config.xml` enables cleartext HTTP and user CAs for local development only, and `ServerConfigHelper` restricts plain HTTP to verified RFC 1918 subnets (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`) and loopback.

---

## Connecting Over Local Wi-Fi (Debug Builds) or HTTPS (Release Builds)

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

---

## Production Release Signing Setup

Production releases must be signed with a dedicated private upload/release keystore (never with the Android Debug key, and never committed to source control — `*.keystore` and `*.jks` are ignored by `android/.gitignore`).

### 1. Generate a Release Keystore (Once, Offline)
```bash
keytool -genkeypair -v \
  -keystore clipflow-release.keystore \
  -alias clipflow-release \
  -keyalg RSA -keysize 4096 -validity 10000
```
Back up `clipflow-release.keystore` and its passwords in a secure password manager.

### 2. Configure GitHub Actions Secrets
Encode the keystore without line wrapping and store all four secrets in **Repository → Settings → Secrets and variables → Actions**:
```bash
base64 -w 0 clipflow-release.keystore
```
- `KEYSTORE_BASE64`: Base64 contents of `clipflow-release.keystore`
- `KEY_ALIAS`: Key alias (e.g. `clipflow-release`)
- `KEYSTORE_PASSWORD`: Keystore password
- `KEY_PASSWORD`: Key password

When all four secrets are configured, CI signs `app-release.apk`, verifies it with Android `apksigner`, confirms it does not use `CN=Android Debug`, and uploads `ClipFlow-release-apk`. Tagged releases (`v*.*.*`) and manual signed release dispatches fail if any signing secret is missing; PR verification builds without secrets produce an explicitly labeled `ClipFlow-release-unsigned-apk` artifact (`app-release-unsigned.apk`).

### 3. Local Signed Build & Signature Verification
```bash
cd android
./gradlew assembleRelease \
  -Pandroid.injected.signing.store.file=/path/to/clipflow-release.keystore \
  -Pandroid.injected.signing.store.password="$KEYSTORE_PASSWORD" \
  -Pandroid.injected.signing.key.alias="$KEY_ALIAS" \
  -Pandroid.injected.signing.key.password="$KEY_PASSWORD"

"$ANDROID_SDK_ROOT"/build-tools/*/apksigner verify --verbose --print-certs \
  app/build/outputs/apk/release/app-release.apk
```
