"""The speech-to-text models, one per spoken language, each downloaded once.

English comes first and by itself, the first time it is needed (about 480 MB). The others are only downloaded when
someone asks for them, from Settings -> Speech models or when adding a video in that language.
Supports both NeMo transducer models and Whisper models exported for sherpa-onnx.

A model is unpacked next to where it will live and moved into place only when it is whole, so a download that was
cut off can never leave half a model behind. When a model's fingerprint (SHA-256) is known, the downloaded file is
checked against it before anything is unpacked."""
import hashlib
import os
import re
import shutil
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config

_SHERPA = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"

# language -> (name shown on the pages, the model's folder name, list of download URLs/mirrors, size in MB, credit,
#              the archive's SHA-256 fingerprint, or "" when it is not checked)
MODELS = {
    "en": (
        "English",
        config.ASR_MODEL_NAME,
        [
            _SHERPA + config.ASR_MODEL_NAME + ".tar.bz2",
        ],
        480,
        "Parakeet TDT 0.6b v2 by NVIDIA (CC-BY-4.0)",
        config.ASR_MODEL_SHA256,
    ),
    "hi": (
        "Hindi (हिन्दी)",
        config.ASR_MODEL_HI,
        [
            config.ASR_MODEL_HI_URL,
        ],
        116,
        "Whisper Tiny (Multilingual / Hindi)",
        config.ASR_MODEL_HI_SHA256,
    ),
    "hi-IN": (
        "Hindi (हिन्दी)",
        config.ASR_MODEL_HI,
        [
            config.ASR_MODEL_HI_URL,
        ],
        116,
        "Whisper Tiny (Multilingual / Hindi)",
        config.ASR_MODEL_HI_SHA256,
    ),
}
DEFAULT = "en"
WAIT = 45   # seconds of silence from the connection before giving up
USER_AGENT = "ClipFlow/2.0 (Android; Mobile)"

_locks = {lang: threading.Lock() for lang in MODELS}
state = {lang: {"doing": False, "percent": 0, "error": "", "got_mb": 0.0, "total_mb": 0.0, "cancel": False} for lang in MODELS}


class WrongFile(Exception):
    """The downloaded file is not the one expected: its fingerprint does not match."""


def known(lang) -> bool:
    return lang in MODELS


def label(lang) -> str:
    return MODELS[lang][0] if lang in MODELS else str(lang)


def folder(lang: str = DEFAULT):
    return config.MODELS_DIR / MODELS[lang][1]


def ready(lang: str = DEFAULT) -> bool:
    """Is the whole model present: token list and required ONNX component parts?"""
    if lang not in MODELS:
        return False
    d = folder(lang)
    if not d.is_dir():
        return False
    has_tokens = (d / "tokens.txt").is_file() or any(d.glob("*tokens*.txt"))
    if not has_tokens:
        return False
    # 1. Transducer (encoder, decoder, joiner)
    has_transducer = all(any(d.glob(f"{part}*.onnx")) for part in ("encoder", "decoder", "joiner"))
    # 2. Whisper (encoder, decoder)
    has_whisper = any(d.glob("*encoder*.onnx")) and any(d.glob("*decoder*.onnx"))
    return has_transducer or has_whisper


def status(lang: str) -> str:
    """'ready', 'downloading' or 'missing'."""
    return "ready" if ready(lang) else "downloading" if state.get(lang, {}).get("doing") else "missing"


def check_storage(lang: str) -> tuple[bool, int, int]:
    """Check if free disk space is sufficient before downloading. Returns (ok, free_bytes, required_bytes)."""
    size_mb = MODELS[lang][3] if lang in MODELS else 480
    required_bytes = int(size_mb * 1024 * 1024 * 2.5)  # 2.5x download size for archive + unpack + safety buffer
    try:
        usage = shutil.disk_usage(config.MODELS_DIR)
        return usage.free >= required_bytes, usage.free, required_bytes
    except Exception:
        return True, 0, 0


def ensure(lang: str = DEFAULT, start: bool = True) -> None:
    """Make sure the model is on this computer, downloading it if it is not. One download per language at a time."""
    if ready(lang):
        return
    if not start:
        while state[lang]["doing"] and not ready(lang):
            time.sleep(1)
        if ready(lang):
            return
        raise RuntimeError(f"{label(lang)} needs its speech model, which isn't on this computer yet. "
                           "Add it in Settings → Speech models, then try again.")
    with _locks[lang]:
        if not ready(lang):
            _fetch(lang)


def cancel(lang: str) -> None:
    """Request cancellation of an active download."""
    if lang in state and state[lang]["doing"]:
        state[lang]["cancel"] = True


def _download_url_with_resume(url: str, dest, st: dict, digest) -> tuple[bool, str, int]:
    """Download single URL with HTTP Range resumption. Returns (success, err_msg, total_size)."""
    got_bytes = dest.stat().st_size if dest.exists() else 0
    if got_bytes > 0:
        headers = {"User-Agent": USER_AGENT, "Accept": "*/*", "Range": f"bytes={got_bytes}-"}
        req = urllib.request.Request(url, headers=headers)
    else:
        req = url
    try:
        with urllib.request.urlopen(req, timeout=WAIT) as r:
            status_code = getattr(r, "status", 200)
            content_len = int(r.headers.get("Content-Length") or 0)
            cr_header = r.headers.get("Content-Range", "").strip()

            valid_resume = False
            total_bytes = 0
            if status_code == 206 and got_bytes > 0:
                # Validate Content-Range header: bytes <start>-<end>/<total>
                m = re.match(r"^bytes\s+(\d+)-(\d+)/(?:\*|(\d+))$", cr_header)
                if m:
                    start_byte = int(m.group(1))
                    if start_byte == got_bytes:
                        valid_resume = True
                        if m.group(3):
                            total_bytes = int(m.group(3))
                        elif content_len > 0:
                            total_bytes = got_bytes + content_len

            if valid_resume:
                mode = "ab"
            else:
                # If status is 200, or Content-Range is invalid/missing/inconsistent, restart safely
                mode = "wb"
                got_bytes = 0
                total_bytes = content_len

            st["total_mb"] = round(total_bytes / (1024 * 1024), 1) if total_bytes > 0 else 0.0

            with open(dest, mode) as f:
                while True:
                    if st.get("cancel"):
                        return False, "Download cancelled by user", total_bytes
                    chunk = r.read(1 << 20)  # 1 MB chunk
                    if not chunk:
                        break
                    f.write(chunk)
                    got_bytes += len(chunk)
                    st["got_mb"] = round(got_bytes / (1024 * 1024), 1)
                    if total_bytes > 0:
                        st["percent"] = min(99, int(got_bytes * 100 // total_bytes))

        final_size = dest.stat().st_size if dest.exists() else 0
        if total_bytes > 0 and final_size != total_bytes:
            return False, f"Incomplete download: got {final_size} bytes, expected {total_bytes}", final_size

        # Re-compute digest of whole completed file
        if digest is not None:
            with open(dest, "rb") as f:
                while chunk := f.read(1 << 20):
                    digest.update(chunk)
        return True, "", final_size
    except Exception as e:
        curr = dest.stat().st_size if dest.exists() else 0
        return False, str(e), curr


def _save(lang: str, dest) -> str:
    """Save the archive to dest, supporting multiple mirrors and HTTP Range resumption."""
    st = state[lang]
    urls = MODELS[lang][2]
    if isinstance(urls, str):
        urls = [urls]

    last_err = ""
    for url in urls:
        if st.get("cancel"):
            raise RuntimeError("Download cancelled by user")
        digest = hashlib.sha256()
        ok, err, _ = _download_url_with_resume(url, dest, st, digest)
        if ok and dest.exists() and dest.stat().st_size > 0:
            return digest.hexdigest()
        last_err = err
        time.sleep(1)

    raise RuntimeError(last_err or "Failed to download model from available sources")


def _fetch(lang: str):
    name, size, want = MODELS[lang][1], MODELS[lang][3], MODELS[lang][5]
    st = state[lang]
    st.update(doing=True, percent=0, error="", got_mb=0.0, total_mb=float(size), cancel=False)
    print(f"\n  Downloading the {label(lang)} speech model (one time, about {size} MB)...\n", flush=True)

    # 1. Disk storage pre-check
    has_space, free_bytes, req_bytes = check_storage(lang)
    if not has_space:
        free_mb = free_bytes // (1024 * 1024)
        req_mb = req_bytes // (1024 * 1024)
        err = f"Not enough storage space to download {label(lang)} speech model. Required: {req_mb} MB, available: {free_mb} MB."
        st["error"] = err
        st["doing"] = False
        raise RuntimeError(err)

    config.MODELS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = config.MODELS_DIR / f"speech-model-{lang}.tar.bz2"
    part = config.MODELS_DIR / f"speech-model-{lang}.unpacking"

    try:
        got_sha = _save(lang, tmp)
        if want and got_sha != want:
            raise WrongFile(f"its fingerprint (SHA-256) is {got_sha}, but ClipFlow expects {want}")
        shutil.rmtree(part, ignore_errors=True)
        part.mkdir(parents=True, exist_ok=True)
        with tarfile.open(tmp, "r:bz2") as t:
            for m in t.getmembers():
                if (m.isfile() or m.isdir()) and not m.name.startswith(("/", "..")) and ".." not in m.name.split("/"):
                    t.extract(m, part)
        got = part / name
        if not got.is_dir():
            # In case archive extracts directly or with prefix
            subdirs = [d for d in part.iterdir() if d.is_dir()]
            if subdirs:
                got = subdirs[0]
            else:
                raise OSError("the downloaded archive did not contain the model directory")
        shutil.rmtree(folder(lang), ignore_errors=True)
        got.rename(folder(lang))
        st["percent"] = 100
        print(f"  {label(lang)} speech model ready.\n", flush=True)
    except WrongFile as e:
        st["error"] = f"The downloaded file is not the expected one: {e}."
        raise RuntimeError(
            f"ClipFlow downloaded the {label(lang)} speech model, but the file is not the one it expects, "
            "so it was not used: it may have been changed or damaged on the way. Nothing was unpacked. "
            "Try again later. If a different download address was set (a mirror), it must hold the very "
            f"same file. Details: {e}."
        ) from e
    except Exception as e:
        err_str = str(e)
        if "cancelled" in err_str.lower():
            friendly = f"Download of {label(lang)} speech model was cancelled."
        elif any(s in err_str for s in ("No address associated with hostname", "Name or service not known", "gaierror")):
            friendly = f"Could not connect to model download servers for {label(lang)}. Please check your internet connection and DNS settings."
        elif "timed out" in err_str.lower() or "timeout" in err_str.lower():
            friendly = f"Connection timed out while downloading the {label(lang)} speech model. Please try again on a faster connection."
        elif "storage space" in err_str.lower() or "no space left" in err_str.lower():
            friendly = err_str
        else:
            friendly = f"ClipFlow could not download the {label(lang)} speech model (about {size} MB). Please check your internet connection and free disk space, then try again."
        st["error"] = friendly
        raise RuntimeError(friendly) from e
    finally:
        st["doing"] = False
        tmp.unlink(missing_ok=True)
        shutil.rmtree(part, ignore_errors=True)


def start(lang: str) -> None:
    """Begin downloading in the background (from the UI); progress is in state[lang]."""
    if ready(lang) or state.get(lang, {}).get("doing"):
        return
    state[lang].update(doing=True, percent=0, error="", cancel=False)

    def run():
        try:
            with _locks[lang]:
                if not ready(lang):
                    _fetch(lang)
        except Exception:
            pass
        finally:
            state[lang]["doing"] = False
    threading.Thread(target=run, daemon=True, name=f"fetch-{lang}").start()


def remove(lang: str) -> None:
    """Delete a downloaded model to free storage space. English stays as default."""
    from . import transcribe
    if lang == DEFAULT:
        raise ValueError("The English speech model is always kept.")
    with _locks[lang]:
        if state[lang]["doing"]:
            raise RuntimeError(f"The {label(lang)} speech model is still downloading.")
        if not transcribe.unload(lang):
            raise RuntimeError(f"The {label(lang)} speech model is being used right now. Try again when videos in that language finish processing.")
        try:
            shutil.rmtree(folder(lang))
        except FileNotFoundError:
            pass
        except OSError as e:
            raise RuntimeError(f"Couldn't delete the {label(lang)} speech model: {e}") from e
        state[lang].update(percent=0, error="", got_mb=0.0)


def main():
    lang = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    if lang not in MODELS:
        raise SystemExit(f"Unknown language {lang!r}. Known: {', '.join(MODELS)}")
    if ready(lang):
        print(f"{label(lang)} speech model already present.")
        return
    ensure(lang)


if __name__ == "__main__":
    main()
