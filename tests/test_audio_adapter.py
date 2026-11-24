"""Regression tests for the resilient audio adapter, lazy soundfile import,
and wave/FFmpeg audio decoding.

Follows the repository's check() testing standard.
"""
import os
import subprocess
import sys
import tempfile
import wave
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, ".")

fails = []
_counts = {"passed": 0, "skipped": 0}


def check(name, got, want=True):
    ok = want(got) if callable(want) else got == want
    if ok:
        _counts["passed"] += 1
        print(f"PASS {name}")
    else:
        fails.append(name)
        print(f"FAIL {name}   got {got!r}")


def skip(name, reason="numpy not installed in local environment"):
    _counts["skipped"] += 1
    print(f"SKIP {name} ({reason})")


try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False
    sys.modules["numpy"] = MagicMock()

# 1. Clean import without soundfile or libsndfile.so
with patch.dict(sys.modules, {"soundfile": None}):
    from studio import media
    check("studio.media imports cleanly when soundfile is missing", callable(media.load_audio), True)

# 2. Standard library wave decoding
if HAS_NUMPY:
    expected_samples = np.array([0, 500, -500, 15000, -15000, 32767, -32768], dtype=np.int16)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp_path = Path(tmp.name)
        try:
            with wave.open(str(tmp_path), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes(expected_samples.tobytes())

            with patch.dict(sys.modules, {"soundfile": None}):
                loaded = media.load_audio(tmp_path)
                check("wave reader returns numpy ndarray", isinstance(loaded, np.ndarray), True)
                check("wave reader returns int16 dtype", loaded.dtype == np.int16, True)
                check("wave reader returns 1D shape", loaded.ndim == 1, True)
                check("wave reader preserves exact sample count", len(loaded), len(expected_samples))
                check("wave reader decodes exact sample values", np.array_equal(loaded, expected_samples), True)
        finally:
            tmp_path.unlink(missing_ok=True)
else:
    skip("wave reader returns numpy ndarray")
    skip("wave reader returns int16 dtype")
    skip("wave reader returns 1D shape")
    skip("wave reader preserves exact sample count")
    skip("wave reader decodes exact sample values")

# 3. Wave reader rejects stereo and non-16kHz (falls through to Tier 2/3)
if HAS_NUMPY:
    stereo_samples = np.array([100, 200, 300, 400], dtype=np.int16)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp_path = Path(tmp.name)
        try:
            # Stereo 16kHz
            with wave.open(str(tmp_path), "wb") as wf:
                wf.setnchannels(2)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes(stereo_samples.tobytes())

            with patch.dict(sys.modules, {"soundfile": None}):
                with patch("subprocess.run") as mock_run:
                    mock_run.return_value = MagicMock(returncode=0, stdout=b"\x00\x00\x10\x00")
                    res = media.load_audio(tmp_path)
                    check("wave reader rejects stereo and triggers FFmpeg fallback", mock_run.called, True)

            # Mono 44.1kHz
            with wave.open(str(tmp_path), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(44100)
                wf.writeframes(stereo_samples.tobytes())

            with patch.dict(sys.modules, {"soundfile": None}):
                with patch("subprocess.run") as mock_run:
                    mock_run.return_value = MagicMock(returncode=0, stdout=b"\x00\x00\x10\x00")
                    res = media.load_audio(tmp_path)
                    check("wave reader rejects non-16kHz and triggers FFmpeg fallback", mock_run.called, True)
        finally:
            tmp_path.unlink(missing_ok=True)
else:
    skip("wave reader rejects stereo and triggers FFmpeg fallback")
    skip("wave reader rejects non-16kHz and triggers FFmpeg fallback")

# 4. Incomplete / corrupt WAV file handling
if HAS_NUMPY:
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp_path = Path(tmp.name)
        try:
            tmp.write(b"RIFF\x00\x00\x00\x00WAVEfmt \x10\x00\x00\x00corrupted")
            tmp.flush()

            with patch.dict(sys.modules, {"soundfile": None}):
                with patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, ["ffmpeg"], stderr=b"Invalid data")):
                    raised_cleanly = False
                    try:
                        media.load_audio(tmp_path)
                    except RuntimeError as e:
                        raised_cleanly = "FFmpeg failed to decode audio" in str(e)
                    check("corrupt WAV caught and raises clean RuntimeError", raised_cleanly, True)
        finally:
            tmp_path.unlink(missing_ok=True)
else:
    skip("corrupt WAV caught and raises clean RuntimeError")

# 5. FFmpeg fallback binary PCM stdout capture and odd-byte truncation
if HAS_NUMPY:
    fake_pcm = (np.array([1000, -2000, 3000, -4000], dtype=np.int16)).tobytes()
    fake_pcm_odd = fake_pcm + b"\xff"  # odd byte to test buffer truncation

    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
        tmp_path = Path(tmp.name)
        try:
            with patch.dict(sys.modules, {"soundfile": None}):
                with patch("subprocess.run") as mock_run:
                    mock_run.return_value = MagicMock(returncode=0, stdout=fake_pcm_odd)
                    loaded = media.load_audio(tmp_path)
                    check("FFmpeg fallback returns int16 dtype", loaded.dtype == np.int16, True)
                    check("FFmpeg fallback truncates trailing odd byte", len(loaded), 4)
                    check("FFmpeg fallback decodes correct values", np.array_equal(loaded, [1000, -2000, 3000, -4000]), True)
        finally:
            tmp_path.unlink(missing_ok=True)
else:
    skip("FFmpeg fallback returns int16 dtype")
    skip("FFmpeg fallback truncates trailing odd byte")
    skip("FFmpeg fallback decodes correct values")

# 6. Desktop soundfile 2D stereo downmix
if HAS_NUMPY:
    stereo_2d = np.array([[100, 200], [300, 400], [500, 600]], dtype=np.int16)
    mock_sf = MagicMock()
    mock_sf.read.return_value = (stereo_2d, 16000)

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp_path = Path(tmp.name)
        try:
            with patch("wave.open", side_effect=Exception("skip wave")):
                with patch.dict(sys.modules, {"soundfile": mock_sf}):
                    loaded = media.load_audio(tmp_path)
                    check("soundfile downmixes 2D stereo to 1D mono", loaded.ndim, 1)
                    check("soundfile downmix preserves sample count", len(loaded), 3)
                    check("soundfile downmix averages channels accurately", np.array_equal(loaded, [150, 350, 550]), True)
        finally:
            tmp_path.unlink(missing_ok=True)
else:
    skip("soundfile downmixes 2D stereo to 1D mono")
    skip("soundfile downmix preserves sample count")
    skip("soundfile downmix averages channels accurately")

# 7. Downstream audio functions
if HAS_NUMPY:
    sample_count = 16000 * 2
    audio = np.zeros(sample_count, dtype=np.int16)
    audio[16000:] = 10000
    loudness = media.loudness_per_second(audio)
    check("loudness_per_second returns correct second count", len(loudness), 2)
    check("loudness_per_second calculates second block louder", loudness[0] < loudness[1], True)

    full = np.zeros(16000 * 10, dtype=np.int16)
    full[16000 * 3: 16000 * 3 + 1000] = 20000
    part = full[16000 * 2: 16000 * 5]
    offset = media.find_offset(full, part, expected=2.0, search=2.0)
    check("find_offset correctly finds offset within tolerance", abs(offset - 2.0) < 0.1, True)
else:
    skip("loudness_per_second returns correct second count")
    skip("loudness_per_second calculates second block louder")
    skip("find_offset correctly finds offset within tolerance")

print(f"\n--- Test Results: {_counts['passed']} passed, {len(fails)} failed, {_counts['skipped']} skipped ---")

if fails:
    print(f"\n{len(fails)} audio adapter checks failed: {fails}")
    sys.exit(1)
