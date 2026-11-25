"""Regression tests for Hindi speech model support, download resilience, and language settings.

Runs locally without external servers or heavy dependencies:
    python tests/test_speech_and_settings.py
"""
import io
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import numpy as np
except ImportError:
    from unittest.mock import MagicMock
    sys.modules["numpy"] = MagicMock()

from studio import config, fetch_model, transcribe

total_checks = [0]
fails = []


def check(name: str, got, want=True):
    total_checks[0] += 1
    ok = want(got) if callable(want) else got == want
    status = "PASS" if ok else "FAIL"
    print(f"{status} {name}" + ("" if ok else f"   got {ascii(got)} expected {ascii(want)}"))
    if not ok:
        fails.append(name)


def run_tests():
    print("=== Hindi Speech Model and Settings Regression Tests ===")

    # 1. Configuration tests
    check("ASR_MODEL_HI defined in config", hasattr(config, "ASR_MODEL_HI") and bool(config.ASR_MODEL_HI))
    check("ASR_MODEL_HI is whisper-tiny", "whisper-tiny" in config.ASR_MODEL_HI)
    check("ASR_MODEL_HI_URL is valid URL", config.ASR_MODEL_HI_URL.startswith("http"))
    check("ASR_MODEL_HI_SHA256 is str", isinstance(config.ASR_MODEL_HI_SHA256, str))

    # 2. fetch_model.MODELS checks
    check("English 'en' in fetch_model.MODELS", "en" in fetch_model.MODELS)
    check("Hindi 'hi' in fetch_model.MODELS", "hi" in fetch_model.MODELS)
    check("Hindi 'hi-IN' in fetch_model.MODELS", "hi-IN" in fetch_model.MODELS)
    check("Arabic 'ar' removed from fetch_model.MODELS", "ar" not in fetch_model.MODELS)

    en_info = fetch_model.MODELS["en"]
    hi_info = fetch_model.MODELS["hi"]
    check("English size is approx 480 MB", en_info[3] == 480)
    check("Hindi size is approx 116 MB", hi_info[3] == 116)
    check("English URLs configured as list", isinstance(en_info[2], list) and len(en_info[2]) >= 1)
    check("Hindi URLs configured as list", isinstance(hi_info[2], list) and len(hi_info[2]) >= 1)
    check("Hindi archive URL points to genuine sherpa-onnx release", any("sherpa-onnx-whisper-tiny" in u for u in hi_info[2]))

    # 3. Storage pre-check
    has_space, free_b, needed_b = fetch_model.check_storage("hi")
    check("check_storage('hi') returns tuple(bool, int, int)", isinstance(has_space, bool) and isinstance(free_b, int))
    check("free_b is non-negative", free_b >= 0)
    check("needed_b is positive", needed_b > 0)

    # Simulate insufficient disk space
    with patch("shutil.disk_usage", return_value=MagicMock(free=10 * 1024 * 1024)):
        low_space, low_free, low_need = fetch_model.check_storage("en")
        check("check_storage detects insufficient disk space", low_space is False)
        check("check_storage reports free space", low_free == 10 * 1024 * 1024)

    # 4. Transcribe model architecture detection
    tmp_dir = Path(tempfile.mkdtemp(prefix="cf-test-model-"))
    try:
        # Transducer structure: tokens.txt, encoder.int8.onnx, decoder.int8.onnx, joiner.int8.onnx
        tdt_dir = tmp_dir / "tdt"
        tdt_dir.mkdir()
        for f in ("tokens.txt", "encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx"):
            (tdt_dir / f).write_bytes(b"dummy")
        tdt_files = transcribe._model_files(tdt_dir)
        check("Transducer model files found", tdt_files is not None)
        check("Transducer has joiner", "joiner" in tdt_files)

        # Whisper structure: tokens.txt, tiny-encoder.onnx, tiny-decoder.onnx
        whisper_dir = tmp_dir / "whisper"
        whisper_dir.mkdir()
        for f in ("tokens.txt", "tiny-encoder.onnx", "tiny-decoder.onnx"):
            (whisper_dir / f).write_bytes(b"dummy")
        w_files = transcribe._model_files(whisper_dir)
        check("Whisper model files found", w_files is not None)
        check("Whisper has encoder and decoder", "encoder" in w_files and "decoder" in w_files)
        check("Whisper has no joiner", "joiner" not in w_files)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    # 5. Language options validation via fetch_model
    check("fetch_model recognizes 'hi'", fetch_model.known("hi") is True)
    check("fetch_model recognizes 'hi-IN'", fetch_model.known("hi-IN") is True)
    check("fetch_model recognizes 'en'", fetch_model.known("en") is True)
    check("fetch_model rejects unknown language 'xx'", fetch_model.known("xx") is False)
    check("fetch_model rejects removed 'ar'", fetch_model.known("ar") is False)

    # 6. HTTP Range header resume and integrity tests
    tmp_out = Path(tempfile.mkdtemp(prefix="cf-test-dl-")) / "test.tar.bz2"
    try:
        # Case A: Valid HTTP 206 with consistent Content-Range (start byte matches got_bytes)
        tmp_out.write_bytes(b"x" * 50)
        req_headers = {}

        def mock_valid_206(req, timeout=None):
            if hasattr(req, "headers"):
                req_headers.update(req.headers)
            resp = MagicMock()
            resp.__enter__.return_value = resp
            resp.status = 206
            resp.headers = {"Content-Length": "50", "Content-Range": "bytes 50-99/100"}
            resp.read.side_effect = [b"y" * 50, b""]
            return resp

        st = {"cancel": False, "percent": 0, "got_mb": 0.0, "total_mb": 0.0}
        with patch("urllib.request.urlopen", side_effect=mock_valid_206):
            ok, err, sz = fetch_model._download_url_with_resume("http://example.com/model.tar.bz2", tmp_out, st, None)
            check("HTTP Range header sent for partial file", "Range" in req_headers)
            check("HTTP Range specifies bytes=50-", req_headers.get("Range") == "bytes=50-")
            check("Valid HTTP 206 appends to existing file", ok is True and tmp_out.stat().st_size == 100)

        # Case B: HTTP 206 with mismatched Content-Range (start byte != got_bytes) -> safe restart from 0
        tmp_out.write_bytes(b"x" * 50)

        def mock_mismatched_206(req, timeout=None):
            resp = MagicMock()
            resp.__enter__.return_value = resp
            resp.status = 206
            # Server incorrectly reports starting at byte 0 instead of requested byte 50
            resp.headers = {"Content-Length": "60", "Content-Range": "bytes 0-59/100"}
            resp.read.side_effect = [b"z" * 60, b""]
            return resp

        st = {"cancel": False, "percent": 0, "got_mb": 0.0, "total_mb": 0.0}
        with patch("urllib.request.urlopen", side_effect=mock_mismatched_206):
            ok, err, sz = fetch_model._download_url_with_resume("http://example.com/model.tar.bz2", tmp_out, st, None)
            check("Mismatched Content-Range restarts from 0", ok is True and tmp_out.stat().st_size == 60)

        # Case C: HTTP 200 returned when partial file exists -> safe restart and overwrite
        tmp_out.write_bytes(b"x" * 50)

        def mock_http_200(req, timeout=None):
            resp = MagicMock()
            resp.__enter__.return_value = resp
            resp.status = 200
            resp.headers = {"Content-Length": "80"}
            resp.read.side_effect = [b"w" * 80, b""]
            return resp

        st = {"cancel": False, "percent": 0, "got_mb": 0.0, "total_mb": 0.0}
        with patch("urllib.request.urlopen", side_effect=mock_http_200):
            ok, err, sz = fetch_model._download_url_with_resume("http://example.com/model.tar.bz2", tmp_out, st, None)
            check("Unexpected HTTP 200 restarts from 0 and overwrites", ok is True and tmp_out.stat().st_size == 80)

        # Case D: Incomplete download (server ends connection prematurely)
        tmp_out.write_bytes(b"")

        def mock_incomplete(req, timeout=None):
            resp = MagicMock()
            resp.__enter__.return_value = resp
            resp.status = 200
            resp.headers = {"Content-Length": "100"}
            # Only returns 30 bytes then EOF
            resp.read.side_effect = [b"a" * 30, b""]
            return resp

        st = {"cancel": False, "percent": 0, "got_mb": 0.0, "total_mb": 0.0}
        with patch("urllib.request.urlopen", side_effect=mock_incomplete):
            ok, err, sz = fetch_model._download_url_with_resume("http://example.com/model.tar.bz2", tmp_out, st, None)
            check("Incomplete download is rejected", ok is False and "Incomplete download" in err)

    finally:
        shutil.rmtree(tmp_out.parent, ignore_errors=True)

    # 7. Checksum pinning check
    check("ASR_MODEL_HI_SHA256 matches official upstream checksum",
          config.ASR_MODEL_HI_SHA256 == "c46116994e539aa165266d96b325252728429c12535eb9d8b6a2b10f129e66b1")

    print(f"\nResult: {len(fails)} failures out of {total_checks[0]} checks")
    if fails:
        print("Failed checks:", fails)
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(run_tests())
