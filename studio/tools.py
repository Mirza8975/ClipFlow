"""The two programs PeakCut runs to work on video: ffmpeg and ffprobe.

They are not part of PeakCut and have to be on the computer. On Windows a missing one shows up as the bare
"[WinError 2] The system cannot find the file specified", which tells nobody what to do. So PeakCut looks for them
itself (also in the places installers put them before Windows has told running programs), uses them from there,
and when they really are missing says so in plain words, before any video is started."""
import os
import shutil
import sys
import time
from pathlib import Path

NEEDED = ("ffmpeg", "ffprobe")
HELP = ("PeakCut needs ffmpeg to work on video, and it isn't installed on this computer yet. "
        + ("Open a terminal and run:  winget install Gyan.FFmpeg" if sys.platform == "win32" else
           "Run:  brew install ffmpeg" if sys.platform == "darwin" else "Run:  sudo apt install ffmpeg")
        + "  Then close PeakCut and start it again.")
_seen = {"at": 0.0, "missing": list(NEEDED)}
_BINARIES: dict[str, str] = {}


def _places() -> list:
    """Folders ffmpeg is usually installed into, for when it is not on the PATH of this running program."""
    root = Path(__file__).resolve().parent.parent
    out = [root / "ffmpeg" / "bin", root / "bin", root]
    env = os.environ.get
    if env("CLIPFLOW_FFMPEG_DIR") or env("PEAKCUT_FFMPEG_DIR"):                    # the folder that holds ffmpeg, if it lives somewhere unusual
        out.insert(0, Path(env("CLIPFLOW_FFMPEG_DIR") or env("PEAKCUT_FFMPEG_DIR")))
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


def setup() -> list:
    """Make ffmpeg and ffprobe reachable for everything PeakCut runs. Returns the ones that could not be found."""
    exe = ".exe" if sys.platform == "win32" else ""
    missing = []
    for name in NEEDED:
        found = None

        # 1. Direct explicit environment variable override
        env_var = os.environ.get(f"CLIPFLOW_{name.upper()}_PATH") or os.environ.get(f"PEAKCUT_{name.upper()}_PATH")
        if env_var and Path(env_var).is_file():
            found = str(Path(env_var).resolve())

        # 2. Check if already on system PATH
        if not found:
            which = shutil.which(name)
            if which:
                found = which

        # 3. Check well-known installation folders and Android nativeLibraryDir
        if not found:
            for d in _places():
                try:
                    # Standard executable binary (ffmpeg / ffmpeg.exe)
                    target = d / (name + exe)
                    if target.is_file():
                        found = str(target.resolve())
                        if str(d) not in os.environ.get("PATH", "").split(os.pathsep):
                            os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")
                        break

                    # Android native library packaging convention (libffmpeg.so / libffprobe.so)
                    # In Android APKs, ELF executables in nativeLibraryDir are named lib*.so.
                    # They MUST be executed directly by their absolute path: NEVER symlinked to app-data directories,
                    # because Android 10+ mounts /data/data with noexec, causing [Errno 13] Permission denied.
                    so_target = d / f"lib{name}.so"
                    if so_target.is_file():
                        found = str(so_target.resolve())
                        break
                except OSError:
                    pass

        if found:
            _BINARIES[name] = found
        else:
            _BINARIES[name] = name
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


def missing() -> list:
    """What is still missing. Looked at again every 20 seconds while something is, so installing ffmpeg is noticed
    without a restart whenever its folder is one of the usual ones."""
    if _seen["missing"] and time.time() - _seen["at"] > 20:
        setup()
    return list(_seen["missing"])


def explain(e) -> str:
    """The plain-words version of "the program could not be found", or "" if the error is something else."""
    text = str(e)
    not_found = isinstance(e, FileNotFoundError) or "WinError 2" in text or "No such file or directory: 'ff" in text
    return HELP if not_found and (missing() or "ffmpeg" in text or "ffprobe" in text or "WinError 2" in text) else ""


setup()
