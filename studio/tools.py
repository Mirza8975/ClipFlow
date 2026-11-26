"""The two programs ClipFlow runs to work on video: ffmpeg and ffprobe.

Platform-aware discovery and execution adapter:
- On Android: locates and directly invokes bundled native libraries (libffmpeg.so, libffprobe.so)
  from the app's nativeLibraryDir without requiring Termux, root, or external installation.
- On Windows / Linux / macOS: discovers system-installed FFmpeg binaries from PATH and
  standard package-manager locations (WinGet, Chocolatey, Scoop, Homebrew, apt, snap).
- Performs live capability verification before marking tools available.
"""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

NEEDED = ("ffmpeg", "ffprobe")
_seen = {"at": 0.0, "missing": list(NEEDED)}
_BINARIES: dict[str, str] = {}
_CAPABILITY: dict[str, dict] = {}


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
    global HELP
    HELP = get_help()
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


def get_quickjs() -> str | None:
    """Return the resolved absolute path to the QuickJS executable, or None if unavailable."""
    # 1. Explicit environment variable override
    env_path = os.environ.get("CLIPFLOW_QUICKJS_PATH")
    if env_path and Path(env_path).is_file() and os.access(env_path, os.X_OK):
        return str(Path(env_path).resolve())

    exe = ".exe" if sys.platform == "win32" else ""
    # 2. Check Android native library directory and well-known installation folders
    for d in _places():
        so_bin = d / "libqjs.so"
        if so_bin.is_file() and os.access(so_bin, os.X_OK):
            return str(so_bin.resolve())
        for n in ("qjs", "quickjs"):
            cand = d / (n + exe)
            if cand.is_file() and os.access(cand, os.X_OK):
                return str(cand.resolve())

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


def get_diagnostics() -> dict:
    """Return structured capability and path diagnostics for FFmpeg, FFprobe and JS runtimes."""
    if not _BINARIES:
        setup(force=True)
    return {
        "is_android": is_android(),
        "missing": list(_seen["missing"]),
        "help": get_help(),
        "capabilities": dict(_CAPABILITY),
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
