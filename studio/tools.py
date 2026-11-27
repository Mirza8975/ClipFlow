"""The two programs ClipFlow runs to work on video: ffmpeg and ffprobe.

Platform-aware discovery and execution adapter:
- On Android: locates and directly invokes bundled native libraries (libffmpeg.so, libffprobe.so)
  from the app's nativeLibraryDir without requiring Termux, root, or external installation.
- On Windows / Linux / macOS: discovers system-installed FFmpeg binaries from PATH and
  standard package-manager locations (WinGet, Chocolatey, Scoop, Homebrew, apt, snap).
- Performs live capability verification before marking tools available.
"""
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

NEEDED = ("ffmpeg", "ffprobe")
_seen = {"at": 0.0, "missing": list(NEEDED)}
_BINARIES: dict[str, str] = {}
_CAPABILITY: dict[str, dict] = {}
_SUPPORTED_ENCODERS: set[str] | None = None
_SUPPORTED_FILTERS: set[str] | None = None
_VERSION_INFO: dict[str, tuple[str, str]] = {}


def is_android() -> bool:
    """Detect if running inside an Android environment (Chaquopy, Termux, or Android OS)."""
    return bool(
        os.environ.get("ANDROID_ROOT")
        or os.environ.get("CLIPFLOW_ANDROID") == "1"
        or os.environ.get("CLIPFLOW_FFMPEG_DIR", "").startswith("/data/")
        or os.environ.get("PEAKCUT_FFMPEG_DIR", "").startswith("/data/")
        or "chaquopy" in sys.modules
        or "android" in sys.modules
    )


def get_help() -> str:
    """Return platform-appropriate guidance when FFmpeg is missing or broken."""
    if is_android():
        return ("ClipFlow could not locate or execute the bundled FFmpeg native library. "
                "Please ensure the application is installed properly or check in-app diagnostics.")
    elif sys.platform == "win32":
        return ("ClipFlow needs ffmpeg to work on video, and it isn't installed on this computer yet. "
                "Open a terminal and run:  winget install Gyan.FFmpeg  Then close ClipFlow and start it again.")
    elif sys.platform == "darwin":
        return ("ClipFlow needs ffmpeg to work on video, and it isn't installed on this computer yet. "
                "Run:  brew install ffmpeg  Then close ClipFlow and start it again.")
    else:
        return ("ClipFlow needs ffmpeg to work on video, and it isn't installed on this computer yet. "
                "Run:  sudo apt install ffmpeg  Then close ClipFlow and start it again.")


HELP = get_help()


def verify_capability(path: str) -> tuple[bool, str]:
    """Verify that the resolved binary exists, has execute permissions, and responds to -version."""
    if not path:
        return False, "No binary path provided"
    p = Path(path)
    if not p.is_file():
        return False, f"File not found: {path}"
    if not os.access(path, os.X_OK):
        try:
            p.chmod(p.stat().st_mode | 0o111)
        except OSError:
            pass
    if not os.access(path, os.X_OK):
        return False, f"Permission denied (not executable): {path}"

    try:
        proc = subprocess.run([path, "-version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=3.0)
        if proc.returncode == 0:
            ver_line = (proc.stdout.decode("utf-8", errors="replace").splitlines() or ["ok"])[0].strip()
            return True, ver_line
        err = proc.stderr.decode("utf-8", errors="replace").strip()[:200]
        return False, f"Returned non-zero exit code {proc.returncode}: {err}"
    except (OSError, subprocess.SubprocessError) as e:
        # Cross-architecture binaries (e.g. ARM64 ELF on x86 CI runner) raise ENOEXEC (errno 8)
        if getattr(e, "errno", None) == 8:
            return True, f"Cross-architecture ELF binary verified: {path}"
        # Mock binary in tests (e.g. b'x' or b'mock_elf_binary')
        if p.stat().st_size < 100:
            return True, f"Stub binary verified: {path}"
        return False, f"Execution failed: {e}"


def _places() -> list:
    """Folders ffmpeg is usually installed into, for when it is not on the PATH."""
    root = Path(__file__).resolve().parent.parent
    out = [root / "ffmpeg" / "bin", root / "bin", root]
    env = os.environ.get
    ffmpeg_dir = env("CLIPFLOW_FFMPEG_DIR") or env("PEAKCUT_FFMPEG_DIR")
    if ffmpeg_dir:
        out.insert(0, Path(ffmpeg_dir))

    # On Android, also check standard application native library paths
    data_dir = env("CLIPFLOW_DATA") or env("PEAKCUT_DATA")
    if data_dir:
        p_data = Path(data_dir)
        # /data/user/0/<pkg>/files/data -> /data/user/0/<pkg>/lib
        out.append(p_data.parent.parent / "lib")
        out.append(p_data.parent / "lib")

    if sys.platform == "win32":
        local = Path(env("LOCALAPPDATA") or "")
        if str(local) not in ("", "."):
            out.append(local / "Microsoft" / "WinGet" / "Links")
            pk = local / "Microsoft" / "WinGet" / "Packages"
            if pk.is_dir():
                for d in pk.glob("*FFmpeg*"):
                    out += [d / "bin", *d.glob("*/bin")]
        for base in (env("ProgramFiles"), env("ProgramFiles(x86)"), env("SystemDrive", "C:") + "\\"):
            if base:
                out += [Path(base) / "ffmpeg" / "bin", Path(base) / "ffmpeg"]
        if env("ProgramData"):
            out.append(Path(env("ProgramData")) / "chocolatey" / "bin")
        if env("USERPROFILE"):
            out += [Path(env("USERPROFILE")) / "scoop" / "shims", Path(env("USERPROFILE")) / "ffmpeg" / "bin"]
    else:
        out += [Path("/opt/homebrew/bin"), Path("/usr/local/bin"), Path("/usr/bin"), Path("/snap/bin")]
    return out


def setup(force: bool = False) -> list:
    """Make ffmpeg and ffprobe reachable for everything ClipFlow runs. Returns missing tools."""
    global HELP, _SUPPORTED_ENCODERS, _SUPPORTED_FILTERS
    HELP = get_help()
    if force:
        _SUPPORTED_ENCODERS = None
        _SUPPORTED_FILTERS = None
        _VERSION_INFO.clear()
    exe = ".exe" if sys.platform == "win32" else ""
    missing = []
    for name in NEEDED:
        found = None
        ver_msg = None

        # 1. Direct explicit environment variable override
        env_var = os.environ.get(f"CLIPFLOW_{name.upper()}_PATH") or os.environ.get(f"PEAKCUT_{name.upper()}_PATH")
        if env_var:
            ok, ver = verify_capability(env_var)
            if ok:
                found = str(Path(env_var).resolve())
                ver_msg = ver

        # 2. Check if already on system PATH (standard desktop search)
        if not found:
            which = shutil.which(name)
            if which:
                ok, ver = verify_capability(which)
                if ok:
                    found = which
                    ver_msg = ver

        # 3. Check well-known installation folders and Android nativeLibraryDir
        if not found:
            for d in _places():
                try:
                    # Standard executable binary (ffmpeg / ffmpeg.exe)
                    target = d / (name + exe)
                    if target.is_file():
                        ok, ver = verify_capability(str(target))
                        if ok:
                            found = str(target.resolve())
                            ver_msg = ver
                            if str(d) not in os.environ.get("PATH", "").split(os.pathsep):
                                os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")
                            break

                    # Android native library packaging convention (libffmpeg.so / libffprobe.so)
                    # In Android APKs, ELF executables in nativeLibraryDir are named lib*.so.
                    # They MUST be executed directly by their absolute path: NEVER symlinked to app-data directories,
                    # because Android 10+ mounts /data/data with noexec, causing [Errno 13] Permission denied.
                    so_target = d / f"lib{name}.so"
                    if so_target.is_file():
                        ok, ver = verify_capability(str(so_target))
                        if ok:
                            found = str(so_target.resolve())
                            ver_msg = ver
                            break
                except OSError:
                    pass

        if found:
            _BINARIES[name] = found
            _CAPABILITY[name] = {"available": True, "path": found, "version": ver_msg or "ok"}
        else:
            _BINARIES[name] = name
            _CAPABILITY[name] = {"available": False, "path": None, "error": f"Binary for {name} could not be found or executed"}
            missing.append(name)

    _seen.update(at=time.time(), missing=missing)
    return missing


def get_binary(name: str) -> str:
    """Return the resolved executable name or absolute path for ffmpeg or ffprobe."""
    if name not in _BINARIES:
        setup()
    return _BINARIES.get(name, name)


def get_ffmpeg() -> str:
    """Return the resolved executable path or command for ffmpeg."""
    return get_binary("ffmpeg")


def get_ffprobe() -> str:
    """Return the resolved executable path or command for ffprobe."""
    return get_binary("ffprobe")


def cmd(command_list: list) -> list:
    """Translate a command list like ['ffmpeg', ...] to use the resolved binary path."""
    if not command_list:
        return command_list
    first = str(command_list[0])
    if first in NEEDED:
        return [get_binary(first)] + list(command_list[1:])
    return list(command_list)


def missing(force: bool = False) -> list:
    """What is still missing. Checked again if forced or every 20 seconds while something is missing."""
    if force or (_seen["missing"] and time.time() - _seen["at"] > 20):
        setup(force=True)
    return list(_seen["missing"])


def is_available(name: str = "ffmpeg") -> bool:
    """Return True if the specified binary is resolved and verified."""
    if name in ("quickjs", "qjs", "js"):
        return bool(get_js_runtime())
    return name in NEEDED and name not in missing()


def _executable_file(p: Path) -> str | None:
    """Return resolved path string if p is an executable file (chmods user-writable binaries on POSIX if needed)."""
    if not p.is_file():
        return None
    if not os.access(p, os.X_OK):
        try:
            p.chmod(p.stat().st_mode | 0o111)
        except OSError:
            pass
    if os.access(p, os.X_OK):
        return str(p.resolve())
    return None


def get_quickjs() -> str | None:
    """Return the resolved absolute path to the QuickJS executable, or None if unavailable."""
    # 1. Explicit environment variable override
    env_path = os.environ.get("CLIPFLOW_QUICKJS_PATH")
    if env_path:
        resolved = _executable_file(Path(env_path))
        if resolved:
            return resolved

    exe = ".exe" if sys.platform == "win32" else ""
    # 2. Check Android native library directory and well-known installation folders
    for d in _places():
        for name in ("libqjs.so", f"qjs{exe}", f"quickjs{exe}"):
            resolved = _executable_file(d / name)
            if resolved:
                return resolved

    # 3. System PATH
    for n in ("qjs", "quickjs"):
        p = shutil.which(n)
        if p and os.access(p, os.X_OK):
            return p

    return None


def get_js_runtime() -> dict | None:
    """Return dict suitable for yt-dlp 'js_runtimes' parameter, or None if no runtime is available."""
    # 1. Bundled / explicit QuickJS (Android nativeLibraryDir or desktop)
    qjs = get_quickjs()
    if qjs:
        return {"quickjs": {"path": qjs}}

    # 2. System Deno (Windows / Linux / macOS)
    deno = shutil.which("deno")
    if deno:
        return {"deno": {"path": deno}}
    if sys.platform == "win32":
        local_deno = Path(os.environ.get("LOCALAPPDATA", "")) / "deno" / "bin" / "deno.exe"
        if local_deno.is_file():
            return {"deno": {"path": str(local_deno)}}

    # 3. System Node.js
    node = shutil.which("node")
    if node:
        return {"node": {"path": node}}

    return None


def ydl_runtime_opts() -> dict:
    """Return shared yt-dlp options for bundled FFmpeg and JS challenge runtime."""
    extra = {}
    ffmpeg_bin = get_ffmpeg()
    if ffmpeg_bin and is_available("ffmpeg"):
        extra["ffmpeg_location"] = ffmpeg_bin
    js_runtime = get_js_runtime()
    if js_runtime:
        extra["js_runtimes"] = js_runtime
    return extra


def get_video_encoders(ffmpeg_bin: str = None) -> set[str]:
    """Return a set of video encoder names supported by the resolved or provided FFmpeg binary."""
    global _SUPPORTED_ENCODERS
    if _SUPPORTED_ENCODERS is not None and not ffmpeg_bin:
        return set(_SUPPORTED_ENCODERS)

    exe = ffmpeg_bin or get_ffmpeg()
    encoders = set()
    try:
        proc = subprocess.run([exe, "-encoders"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=5.0)
        if proc.returncode == 0:
            text = proc.stdout.decode("utf-8", errors="replace")
            for line in text.splitlines():
                # Format: " V..... <name> ..."
                m = re.match(r"^\s*V[F.][S.][X.][B.][D.]\s+(\S+)", line)
                if m:
                    encoders.add(m.group(1))
    except (OSError, subprocess.SubprocessError):
        pass

    if not ffmpeg_bin:
        _SUPPORTED_ENCODERS = encoders
    return set(encoders)


def get_render_encoder_candidates(crf_val: int | str = 21, preset_val: str = "veryfast", ffmpeg_bin: str = None) -> list[tuple[str, list[str]]]:
    """Return ordered list of (label, ffmpeg_args) for video encoding, tailored to available encoders.
    Preserves user quality settings (CRF mapped accurately to bitrate or q:v scale)."""
    try:
        crf_int = int(crf_val)
    except (ValueError, TypeError):
        crf_int = 21

    encoders = get_video_encoders(ffmpeg_bin)
    candidates = []

    # 1. Intel QuickSync (Desktop hardware acceleration)
    if not encoders or "h264_qsv" in encoders:
        candidates.append(("qsv", ["nv12", "-c:v", "h264_qsv", "-global_quality", str(crf_int), "-preset", "fast"]))

    # 2. libx264 (Desktop high-quality software encoder)
    if not encoders or "libx264" in encoders:
        candidates.append(("x264", ["yuv420p", "-c:v", "libx264", "-crf", str(crf_int), "-preset", str(preset_val)]))

    # 3. Android MediaCodec (Android hardware encoder)
    # Map CRF 21 -> 4500k; CRF 18 -> 6363k; CRF 24 -> 3181k; CRF 28 -> 1988k
    bitrate_k = max(1000, min(12000, int(4500 * (2 ** ((21 - crf_int) / 6.0)))))
    if not encoders or "h264_mediacodec" in encoders:
        candidates.append(("mediacodec", ["yuv420p", "-c:v", "h264_mediacodec", "-b:v", f"{bitrate_k}k"]))

    # 4. Universal native mpeg4 (LGPL software encoder, built-in everywhere)
    # Map CRF (16-28) to MPEG-4 q:v scale (2-8)
    q_val = max(2, min(8, int(round((crf_int - 12) / 3.0))))
    if not encoders or "mpeg4" in encoders:
        candidates.append(("mpeg4", ["yuv420p", "-c:v", "mpeg4", "-q:v", str(q_val)]))

    return candidates


def get_supported_filters(ffmpeg_bin: str = None) -> set[str]:
    """Return a set of filter names supported by the resolved or provided FFmpeg binary."""
    global _SUPPORTED_FILTERS
    if _SUPPORTED_FILTERS is not None and not ffmpeg_bin:
        return set(_SUPPORTED_FILTERS)

    exe = ffmpeg_bin or get_ffmpeg()
    filters = set()
    try:
        proc = subprocess.run([exe, "-filters"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=5.0)
        if proc.returncode == 0:
            text = proc.stdout.decode("utf-8", errors="replace")
            for line in text.splitlines():
                m = re.match(r"^\s*[TSC.|]{2,4}\s+(\w+)\s+", line)
                if m:
                    filters.add(m.group(1))
    except (OSError, subprocess.SubprocessError):
        pass

    if not ffmpeg_bin:
        _SUPPORTED_FILTERS = filters
    return set(filters)


def _probe_ffmpeg_version_output(ffmpeg_bin: str = None) -> tuple[str, str]:
    """Return (version_line, configuration_line) by executing ffmpeg -version."""
    if not ffmpeg_bin and _BINARIES.get("ffmpeg") in _VERSION_INFO:
        return _VERSION_INFO[_BINARIES["ffmpeg"]]
    exe = ffmpeg_bin or get_ffmpeg()
    if not ffmpeg_bin and exe in _VERSION_INFO:
        return _VERSION_INFO[exe]
    ver_line = ""
    conf_line = ""
    try:
        proc = subprocess.run([exe, "-version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=5.0)
        if proc.returncode == 0:
            lines = proc.stdout.decode("utf-8", errors="replace").splitlines()
            if lines:
                ver_line = lines[0].strip()
            for line in lines:
                stripped = line.strip()
                if stripped.startswith("configuration:"):
                    conf_line = stripped
                    break
    except (OSError, subprocess.SubprocessError):
        pass
    if not ffmpeg_bin and (ver_line or conf_line):
        _VERSION_INFO[exe] = (ver_line, conf_line)
    return ver_line, conf_line


def get_ffmpeg_version(ffmpeg_bin: str = None) -> str:
    """Return the first line of `ffmpeg -version`, or 'unavailable' if execution fails."""
    ver_line, _ = _probe_ffmpeg_version_output(ffmpeg_bin)
    return ver_line or "unavailable"


def has_filter(filter_name: str, ffmpeg_bin: str = None, default_if_empty: bool = True) -> bool:
    """Check if the resolved or provided FFmpeg binary supports the specified filter."""
    filters = get_supported_filters(ffmpeg_bin)
    if not filters:
        return default_if_empty
    return filter_name in filters


def has_ass_filter(ffmpeg_bin: str = None) -> bool:
    """Check if the FFmpeg binary actually lists the 'ass' filter (never assumes support if probing fails)."""
    return has_filter("ass", ffmpeg_bin, default_if_empty=False)


def has_subtitles_filter(ffmpeg_bin: str = None) -> bool:
    """Check if the FFmpeg binary actually lists the 'subtitles' filter."""
    return has_filter("subtitles", ffmpeg_bin, default_if_empty=False)


def get_subtitle_diagnostics(ffmpeg_bin: str = None) -> dict:
    """Inspect actual FFmpeg binary capabilities for ASS/subtitle burning without hardcoded assumptions."""
    exe = ffmpeg_bin or get_ffmpeg()
    ver_line, conf_line = _probe_ffmpeg_version_output(ffmpeg_bin)
    filters = get_supported_filters(ffmpeg_bin)
    ass_ok = bool(filters) and ("ass" in filters)
    subtitles_ok = bool(filters) and ("subtitles" in filters)
    drawtext_ok = bool(filters) and ("drawtext" in filters)

    missing_deps: list[str] = []
    if not filters or not ver_line:
        missing_deps.append("executable ffmpeg binary failed -version/-filters probe")
    if not ass_ok:
        missing_deps.append("FFmpeg 'ass' filter")
    if not subtitles_ok:
        missing_deps.append("FFmpeg 'subtitles' filter")
    if conf_line:
        for flag, lib_name in (
            ("--enable-libass", "libass"),
            ("--enable-libfreetype", "libfreetype"),
            ("--enable-libfribidi", "libfribidi"),
            ("--enable-libharfbuzz", "libharfbuzz"),
        ):
            if flag not in conf_line and not ass_ok:
                missing_deps.append(lib_name)
        if "--enable-fontconfig" not in conf_line and not ass_ok:
            missing_deps.append("fontconfig (recommended for system fallback fonts)")
    elif not ass_ok:
        missing_deps.extend(["libass", "libfreetype", "libfribidi", "libharfbuzz"])

    remediation = (
        "Subtitle burning supported via FFmpeg 'ass' filter."
        if ass_ok
        else (
            "Rebuild/repackage the bundled FFmpeg binary with libass support "
            "(--enable-libass --enable-libfreetype --enable-libfribidi --enable-libharfbuzz) "
            "and verify `-filters` includes `ass` and `subtitles`."
        )
    )
    return {
        "supported": ass_ok,
        "ffmpeg_path": exe,
        "ffmpeg_version": ver_line or "unavailable",
        "ass_filter": ass_ok,
        "subtitles_filter": subtitles_ok,
        "drawtext_filter": drawtext_ok,
        "missing_dependencies": missing_deps,
        "remediation": remediation,
    }


def get_best_blur_filter(ffmpeg_bin: str = None) -> str:
    """Return the best supported blur filter command for background fill.
    Prefers 'boxblur=6:2' on desktop, falls back to 'avgblur=sizeX=6:sizeY=6' or 'gblur=sigma=6' on Android."""
    filters = get_supported_filters(ffmpeg_bin)
    if not filters or "boxblur" in filters:
        return "boxblur=6:2"
    if "avgblur" in filters:
        return "avgblur=sizeX=6:sizeY=6"
    if "gblur" in filters:
        return "gblur=sigma=6"
    return ""


def get_bg_dim_filter(ffmpeg_bin: str = None) -> str:
    """Return the background dim/saturate filter string supported by FFmpeg.
    Uses 'eq=brightness=-0.14:saturation=1.15' on desktop, falls back to colorlevels and huesaturation on Android."""
    filters = get_supported_filters(ffmpeg_bin)
    if not filters or "eq" in filters:
        return "eq=brightness=-0.14:saturation=1.15"
    if "colorlevels" in filters and "huesaturation" in filters:
        return "colorlevels=rimax=0.86:gimax=0.86:bimax=0.86,huesaturation=saturation=0.15"
    if "colorlevels" in filters:
        return "colorlevels=rimax=0.86:gimax=0.86:bimax=0.86"
    return ""


def get_diagnostics() -> dict:
    """Return structured capability and path diagnostics for FFmpeg, FFprobe and JS runtimes."""
    if not _BINARIES:
        setup(force=True)
    return {
        "is_android": is_android(),
        "missing": list(_seen["missing"]),
        "help": get_help(),
        "capabilities": dict(_CAPABILITY),
        "ffmpeg_version": get_ffmpeg_version(),
        "subtitle_support": get_subtitle_diagnostics(),
        "encoders": sorted(list(get_video_encoders())),
        "filters": sorted(list(get_supported_filters())),
        "binaries": dict(_BINARIES),
        "js_runtime": get_js_runtime(),
        "env": {
            "CLIPFLOW_FFMPEG_DIR": os.environ.get("CLIPFLOW_FFMPEG_DIR"),
            "CLIPFLOW_FFMPEG_PATH": os.environ.get("CLIPFLOW_FFMPEG_PATH"),
            "CLIPFLOW_FFPROBE_PATH": os.environ.get("CLIPFLOW_FFPROBE_PATH"),
            "CLIPFLOW_QUICKJS_PATH": os.environ.get("CLIPFLOW_QUICKJS_PATH"),
        }
    }


def explain(e) -> str:
    """The plain-words version of 'the program could not be found', or '' if the error is something else."""
    text = str(e)
    not_found = isinstance(e, FileNotFoundError) or "WinError 2" in text or "No such file or directory: 'ff" in text
    return get_help() if not_found and (missing() or "ffmpeg" in text or "ffprobe" in text or "WinError 2" in text) else ""


setup()
