"""ClipFlow Embedded Android Entry Point.
Runs the FastAPI/Uvicorn server inside Android using Chaquopy.
"""
import os
import sys
import threading
from pathlib import Path

_server = None
_thread = None


def setup_environment(data_dir: str, web_dir: str, ffmpeg_dir: str, port: int = 8765):
    """Sets up environment variables for studio.server before it imports config."""
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
        os.environ["CLIPFLOW_ROOT"] = str(Path(web_dir).parent)
        os.environ["PEAKCUT_ROOT"] = str(Path(web_dir).parent)

    # Add native library directory to PATH for ffmpeg and ffprobe
    if ffmpeg_dir:
        os.environ["CLIPFLOW_FFMPEG_DIR"] = ffmpeg_dir
        os.environ["PEAKCUT_FFMPEG_DIR"] = ffmpeg_dir
        cur_path = os.environ.get("PATH", "")
        if ffmpeg_dir not in cur_path:
            os.environ["PATH"] = ffmpeg_dir + os.pathsep + cur_path


def start_server(data_dir: str, web_dir: str, ffmpeg_dir: str, port: int = 8765):
    """Starts the Uvicorn server in a background daemon thread."""
    global _server, _thread

    if _server is not None and not _server.should_exit:
        print("[ClipFlow] Server is already running.")
        return True

    setup_environment(data_dir, web_dir, ffmpeg_dir, port)

    import uvicorn
    from studio.server import app

    class AndroidServer(uvicorn.Server):
        def install_signal_handlers(self):
            # In Android JVM background threads, signal handling must be bypassed
            pass

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
    )
    _server = AndroidServer(config=config)

    def _run():
        print(f"[ClipFlow] Starting embedded Uvicorn server on 127.0.0.1:{port}...")
        try:
            _server.run()
        except Exception as e:
            print(f"[ClipFlow] Server error: {e}")

    _thread = threading.Thread(target=_run, name="ClipFlow-Uvicorn", daemon=True)
    _thread.start()
    return True


def stop_server():
    """Signals the Uvicorn server to stop."""
    global _server
    if _server is not None:
        print("[ClipFlow] Stopping embedded Uvicorn server...")
        _server.should_exit = True
        _server = None
    return True
