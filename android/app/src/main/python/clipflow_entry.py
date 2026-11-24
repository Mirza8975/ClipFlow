"""ClipFlow Embedded Android Entry Point.
Runs the FastAPI/Uvicorn server inside Android using Chaquopy.
"""
import os
import sys
import threading
import time
import traceback
import urllib.request
from pathlib import Path

_server = None
_thread = None
_server_status = {
    "started": False,
    "ready": False,
    "error": None,
    "port": 8765,
}

_LOG_BUFFER = []
_DIAGNOSTICS_FILE = None
TAG = "ClipFlowBackend"


def _log(level: str, msg: str):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    formatted = f"[{timestamp}] [{level.upper()}] [{TAG}] {msg}"
    print(formatted, flush=True)

    _LOG_BUFFER.append(formatted)
    if len(_LOG_BUFFER) > 200:
        _LOG_BUFFER.pop(0)

    if _DIAGNOSTICS_FILE:
        try:
            with open(_DIAGNOSTICS_FILE, "a", encoding="utf-8") as f:
                f.write(formatted + "\n")
        except Exception:
            pass

    try:
        from android.util import Log
        if level == "e":
            Log.e(TAG, msg)
        elif level == "w":
            Log.w(TAG, msg)
        else:
            Log.i(TAG, msg)
    except Exception:
        pass


def setup_environment(data_dir: str, web_dir: str, ffmpeg_dir: str, port: int = 8765):
    """Sets up environment variables for studio.server before it imports config."""
    global _DIAGNOSTICS_FILE
    if data_dir:
        _DIAGNOSTICS_FILE = Path(data_dir) / "startup_diagnostics.log"

    os.environ["CLIPFLOW_HOST"] = "127.0.0.1"
    os.environ["PEAKCUT_HOST"] = "127.0.0.1"
    os.environ["CLIPFLOW_PORT"] = str(port)
    os.environ["PEAKCUT_PORT"] = str(port)

    # Local single-user mode: open dashboard directly without password prompts
    os.environ["CLIPFLOW_ACCOUNTS"] = "0"
    os.environ["PEAKCUT_ACCOUNTS"] = "0"

    # Map persistent data directory
    os.environ["CLIPFLOW_DATA"] = data_dir
    os.environ["PEAKCUT_DATA"] = data_dir

    # Root directory pointing to web assets
    if web_dir:
        parent_dir = str(Path(web_dir).parent)
        os.environ["CLIPFLOW_ROOT"] = parent_dir
        os.environ["PEAKCUT_ROOT"] = parent_dir
        if parent_dir not in sys.path:
            sys.path.insert(0, parent_dir)

    this_dir = str(Path(__file__).resolve().parent)
    if this_dir not in sys.path:
        sys.path.insert(0, this_dir)

    # Add native library directory to PATH for ffmpeg and ffprobe
    os.environ["CLIPFLOW_ANDROID"] = "1"
    if ffmpeg_dir:
        os.environ["CLIPFLOW_FFMPEG_DIR"] = ffmpeg_dir
        os.environ["PEAKCUT_FFMPEG_DIR"] = ffmpeg_dir
        cur_path = os.environ.get("PATH", "")
        if ffmpeg_dir not in cur_path:
            os.environ["PATH"] = ffmpeg_dir + os.pathsep + cur_path

        p_dir = Path(ffmpeg_dir)
        ff_bin = p_dir / "libffmpeg.so"
        if not ff_bin.is_file():
            ff_bin = p_dir / "ffmpeg"
        if ff_bin.is_file():
            os.environ["CLIPFLOW_FFMPEG_PATH"] = str(ff_bin.resolve())
            os.environ["PEAKCUT_FFMPEG_PATH"] = str(ff_bin.resolve())
            _log("i", f"[FFMPEG_RESOLVED] Found bundled ffmpeg binary at: {ff_bin}")

        probe_bin = p_dir / "libffprobe.so"
        if not probe_bin.is_file():
            probe_bin = p_dir / "ffprobe"
        if probe_bin.is_file():
            os.environ["CLIPFLOW_FFPROBE_PATH"] = str(probe_bin.resolve())
            os.environ["PEAKCUT_FFPROBE_PATH"] = str(probe_bin.resolve())
            _log("i", f"[FFPROBE_RESOLVED] Found bundled ffprobe binary at: {probe_bin}")


def start_server(data_dir: str, web_dir: str, ffmpeg_dir: str, port: int = 8765, timeout: float = 35.0) -> bool:
    """Starts the Uvicorn server in a background thread and waits until it responds to /healthz."""
    global _server, _thread

    if _server is not None:
        if getattr(_server, "started", False) and not getattr(_server, "should_exit", False):
            _log("i", f"[FASTAPI_SERVER_READY] Server is already running on 127.0.0.1:{port}.")
            _server_status["started"] = True
            _server_status["ready"] = True
            return True
        else:
            stopped = stop_server(timeout=5.0)
            if not stopped or (_thread is not None and _thread.is_alive()):
                err_msg = (
                    _server_status.get("error")
                    or "Previous Uvicorn server thread is still alive. Refusing to start new server on port 8765."
                )
                _log("e", f"[FASTAPI_SERVER_FAILED] {err_msg}")
                raise RuntimeError(err_msg)

    _server_status["started"] = False
    _server_status["ready"] = False
    _server_status["error"] = None
    _server_status["port"] = port

    setup_environment(data_dir, web_dir, ffmpeg_dir, port)
    _log("i", "[PYTHON_BACKEND_INITIALIZED] Environment variables and paths configured.")

    try:
        import uvicorn
        from studio.server import app
        from studio import tools as _tools
        missing_tools = _tools.setup(force=True)
        if not missing_tools:
            _log("i", f"[FFMPEG_CAPABILITY_VERIFIED] Tools ready: ffmpeg={_tools.get_ffmpeg()}, ffprobe={_tools.get_ffprobe()}")
        else:
            _log("w", f"[FFMPEG_CAPABILITY_WARNING] Missing tools after setup: {missing_tools}. Help: {_tools.get_help()}")
    except Exception as e:
        tb = traceback.format_exc()
        err_msg = f"Failed to import studio.server: {e}\n{tb}"
        _log("e", f"[FASTAPI_SERVER_FAILED] {err_msg}")
        _server_status["error"] = err_msg
        raise RuntimeError(err_msg) from e

    class AndroidServer(uvicorn.Server):
        def install_signal_handlers(self):
            # In Android JVM background threads, signal handling must be bypassed
            pass

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="info",
        access_log=False,
    )
    _server = AndroidServer(config=config)

    def _run():
        _log("i", f"[FASTAPI_SERVER_STARTING] Starting Uvicorn event loop on 127.0.0.1:{port}...")
        try:
            _server.run()
        except Exception as e:
            tb = traceback.format_exc()
            err_msg = f"Uvicorn runtime error: {e}\n{tb}"
            _log("e", f"[FASTAPI_SERVER_FAILED] {err_msg}")
            _server_status["error"] = err_msg
        finally:
            if getattr(_server, "should_exit", False) and not getattr(_server, "started", False):
                if not _server_status.get("error"):
                    _server_status["error"] = "FastAPI application lifespan startup failed. Check server logs."
                    _log("e", f"[FASTAPI_SERVER_FAILED] {_server_status['error']}")

    _thread = threading.Thread(target=_run, name="ClipFlow-Uvicorn", daemon=True)
    _thread.start()

    # Bounded wait for server socket bind and /healthz readiness
    start_time = time.time()
    while time.time() - start_time < timeout:
        if getattr(_server, "started", False) and not getattr(_server, "should_exit", False):
            # Verify healthz endpoint responds directly
            try:
                req = urllib.request.Request(f"http://127.0.0.1:{port}/healthz")
                with urllib.request.urlopen(req, timeout=1.0) as resp:
                    if resp.status == 200:
                        _log("i", f"[FASTAPI_SERVER_READY] Verified healthz on 127.0.0.1:{port}")
                        _server_status["started"] = True
                        _server_status["ready"] = True
                        _server_status["error"] = None
                        return True
            except urllib.error.HTTPError as he:
                try:
                    err_body = he.read().decode("utf-8", errors="replace")
                    _server_status["error"] = f"Health check returned HTTP {he.code}: {err_body}"
                except Exception:
                    _server_status["error"] = f"Health check returned HTTP {he.code}"
            except Exception:
                pass

        if getattr(_server, "should_exit", False) or not _thread.is_alive():
            err_msg = _server_status.get("error") or "Server thread exited prematurely without binding."
            _log("e", f"[FASTAPI_SERVER_FAILED] {err_msg}")
            raise RuntimeError(err_msg)

        time.sleep(0.15)

    err_msg = _server_status.get("error") or f"Server did not become ready on 127.0.0.1:{port} within {timeout}s."
    _log("e", f"[FASTAPI_SERVER_FAILED] {err_msg}")
    raise TimeoutError(err_msg)


def is_server_ready() -> bool:
    """Returns whether the embedded server is actively serving requests."""
    return bool(_server_status.get("ready", False))


def get_last_error() -> str:
    """Returns the last recorded startup error or empty string."""
    return str(_server_status.get("error") or "")


def get_diagnostics() -> str:
    """Returns formatted diagnostics with server state, last error, startup state, and recent logs."""
    lines = [
        f"Server State: started={_server_status.get('started')}, ready={_server_status.get('ready')}, port={_server_status.get('port')}"
    ]
    if _server_status.get("error"):
        lines.append(f"\nLast Error Details:\n{_server_status['error']}")

    try:
        from studio.server import STARTUP_STATE
        if STARTUP_STATE.get("critical_error"):
            lines.append(f"\nCritical Startup Failure: {STARTUP_STATE['critical_error']}")
        if STARTUP_STATE.get("warnings"):
            lines.append("\nStartup Warnings:")
            for w in STARTUP_STATE["warnings"]:
                lines.append(f"  - {w}")
    except Exception:
        pass

    lines.append("\nRecent Python Logs:")
    if _LOG_BUFFER:
        lines.extend(_LOG_BUFFER[-40:])
    else:
        lines.append("(No logs buffered yet)")
    return "\n".join(lines)


def stop_server(timeout: float = 5.0) -> bool:
    """Signals the Uvicorn server to stop and waits for clean exit."""
    global _server, _thread
    if _server is not None:
        _log("i", "Stopping embedded Uvicorn server...")
        _server.should_exit = True
        if _thread is not None and _thread.is_alive():
            _thread.join(timeout=timeout)
            if _thread.is_alive():
                err_msg = (
                    f"Uvicorn server thread ({_thread.name}) did not terminate within {timeout}s timeout. "
                    "References preserved; cannot rebind port 8765."
                )
                _log("e", f"[FASTAPI_SERVER_STOP_FAILED] {err_msg}")
                _server_status["error"] = err_msg
                return False

        # Only clear references if the thread has cleanly terminated
        _server = None
        _thread = None
        _server_status["started"] = False
        _server_status["ready"] = False
        _log("i", "Embedded Uvicorn server stopped cleanly.")
    return True
