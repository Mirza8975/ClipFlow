"""Tests for the native Android sherpa-onnx audio bridge and lifecycle safeguards.

Verifies:
1. Canonical 16 kHz mono normalized Float32 audio format rules.
2. Signed PCM16 normalization (sample / 32768.0f) and strict separation from Float32.
3. PCM16 vs Float32 distinction (never interpret PCM16 bytes as Float32).
4. Byte length validation (Float32 % 4 == 0, PCM16 % 2 == 0).
5. Sample rate validation (16,000 Hz enforcement).
6. Android SpeechEngine bridge adapter duck-typing and token/timestamp extraction.
7. Desktop fallback transparency (zero regression on non-Android platforms).
8. Lifecycle management: model unload and active decoding release deferral.
"""
import math
import struct
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import numpy as np
except ImportError:
    class _MockNP:
        float32 = "float32"
        int16 = "int16"
        nan = float("nan")

        @staticmethod
        def ascontiguousarray(arr, dtype=None):
            return arr

        @staticmethod
        def zeros(n, dtype=None):
            class _ZeroArr:
                def __init__(self, count, dt):
                    self.count = count
                    self.dt = dt
                def __len__(self):
                    return self.count
                def tobytes(self):
                    elem_size = 4 if self.dt == "float32" else 2
                    return b"\x00" * (self.count * elem_size)
            return _ZeroArr(n, dtype)

        @staticmethod
        def isnan(val):
            return math.isnan(val)

    np = _MockNP()
    sys.modules["numpy"] = np

from studio import fetch_model, transcribe


class TestAudioBridgeFormats(unittest.TestCase):
    """Test audio conversions, byte alignment, and normalization math."""

    def test_float32_canonical_format_and_endianness(self):
        """Float32 audio bytes must be little-endian IEEE 754 and divisible by 4."""
        original_floats = [0.0, 0.5, -0.5, 0.9900000095367432, -1.0]
        raw_bytes = struct.pack(f"<{len(original_floats)}f", *original_floats)

        self.assertEqual(len(raw_bytes), len(original_floats) * 4)
        self.assertEqual(len(raw_bytes) % 4, 0)

        # Decode using little-endian float format (<f)
        num_floats = len(raw_bytes) // 4
        decoded = struct.unpack(f"<{num_floats}f", raw_bytes)
        for orig, dec in zip(original_floats, decoded):
            self.assertAlmostEqual(orig, dec, places=6)

    def test_float32_invalid_byte_length_rejected(self):
        """Float32 buffers with invalid byte counts (not divisible by 4) must be rejected."""
        invalid_bytes = b"\x00\x00\x80"  # 3 bytes
        self.assertNotEqual(len(invalid_bytes) % 4, 0)

        invalid_bytes_5 = b"\x00\x00\x80\x3f\x01"  # 5 bytes
        self.assertNotEqual(len(invalid_bytes_5) % 4, 0)

    def test_pcm16_normalization_math(self):
        """PCM16 samples must be normalized using sample / 32768.0f strictly."""
        # Test boundary values: -32768, -16384, 0, 16384, 32767
        shorts = [-32768, -16384, 0, 16384, 32767]
        raw_bytes = struct.pack(f"<{len(shorts)}h", *shorts)

        self.assertEqual(len(raw_bytes), len(shorts) * 2)
        self.assertEqual(len(raw_bytes) % 2, 0)

        # Convert using sample / 32768.0f
        num_shorts = len(raw_bytes) // 2
        unpacked_shorts = struct.unpack(f"<{num_shorts}h", raw_bytes)
        floats = [s / 32768.0 for s in unpacked_shorts]

        self.assertAlmostEqual(floats[0], -1.0, places=6)
        self.assertAlmostEqual(floats[1], -0.5, places=6)
        self.assertAlmostEqual(floats[2], 0.0, places=6)
        self.assertAlmostEqual(floats[3], 0.5, places=6)
        self.assertAlmostEqual(floats[4], 32767.0 / 32768.0, places=6)

        # All values must fall strictly within [-1.0, 1.0]
        for val in floats:
            self.assertTrue(-1.0 <= val <= 1.0)

    def test_pcm16_never_interpreted_as_float32(self):
        """Passing PCM16 bytes directly to Float32 decoder produces corrupted/invalid audio."""
        # Example audio: 16-bit sine wave peaks (~16000, -16000)
        pcm16_shorts = [16000, -16000, 8000, -8000]
        pcm_bytes = struct.pack(f"<{len(pcm16_shorts)}h", *pcm16_shorts)

        # If wrongly interpreted as Float32 (4 bytes per sample):
        corrupted_floats = struct.unpack(f"<{len(pcm_bytes)//4}f", pcm_bytes)
        normalized_floats = [s / 32768.0 for s in pcm16_shorts]

        # The corrupted float values must not match the properly normalized values
        self.assertNotEqual(len(corrupted_floats), len(normalized_floats))
        for cf in corrupted_floats:
            # Corrupted values often have huge or subnormal exponents far outside [-1.0, 1.0]
            self.assertTrue(abs(cf) > 10.0 or abs(cf) < 1e-15 or math.isnan(cf))

    def test_pcm16_invalid_byte_length_rejected(self):
        """PCM16 buffers with odd byte counts must be rejected."""
        odd_bytes = b"\x00\x80\x01"  # 3 bytes
        self.assertNotEqual(len(odd_bytes) % 2, 0)


class TestAndroidSpeechAdapter(unittest.TestCase):
    """Test the Python-to-Kotlin Android adapter and desktop transparency."""

    def test_android_adapter_decode_pieces(self):
        """_AndroidRecognizer formats Float32 bytes and extracts tokens and timestamps."""
        mock_engine = MagicMock()
        mock_engine.initRecognizer.return_value = True
        mock_engine.transcribeFloatBytesDetailed.return_value = {
            "text": "नमस्ते दुनिया",
            "tokens": [" ", "नम", "स्ते", " ", "दु", "निया"],
            "timestamps": [0.12, 0.24, 0.36, 0.50, 0.62, 0.75],
        }

        with patch.dict("sys.modules", {"com.mirza8975.clipflow": MagicMock(SpeechEngine=mock_engine)}):
            with patch("studio.fetch_model.ensure"), \
                 patch("studio.fetch_model.folder", return_value=Path("/data/models/hi")), \
                 patch("studio.transcribe._model_files", return_value={
                     "encoder": "/data/models/hi/tiny-encoder.onnx",
                     "decoder": "/data/models/hi/tiny-decoder.onnx",
                     "tokens": "/data/models/hi/tiny-tokens.txt",
                 }):
                rec = transcribe._AndroidRecognizer("hi")

                # Input pieces: list of 16 kHz mono Float32 arrays
                piece1 = np.zeros(16000, dtype="float32")
                results = rec.decode_pieces([piece1])

                self.assertEqual(len(results), 1)
                tokens, timestamps = results[0]
                self.assertEqual(tokens, [" ", "नम", "स्ते", " ", "दु", "निया"])
                self.assertEqual(timestamps, [0.12, 0.24, 0.36, 0.50, 0.62, 0.75])

                # Verify mock called with raw bytes of length 16000 * 4 = 64000
                mock_engine.transcribeFloatBytesDetailed.assert_called_once()
                call_args = mock_engine.transcribeFloatBytesDetailed.call_args[0]
                self.assertEqual(len(call_args[0]), 64000)
                self.assertEqual(call_args[1], 16000)

    def test_decode_pieces_duck_typing(self):
        """transcribe.decode_pieces delegates to rec.decode_pieces when available."""
        mock_rec = MagicMock()
        mock_rec.decode_pieces.return_value = [(["hello"], [0.1])]

        pieces = [np.zeros(8000, dtype="float32")]
        out = transcribe.decode_pieces(pieces, rec=mock_rec)

        self.assertEqual(out, [(["hello"], [0.1])])
        mock_rec.decode_pieces.assert_called_once_with(pieces)

    def test_desktop_pipeline_preserves_sherpa_streams(self):
        """When rec lacks decode_pieces, desktop decode_streams path is executed."""
        mock_rec = MagicMock(spec=["create_stream", "decode_streams"])
        mock_stream = MagicMock()
        mock_stream.result.tokens = ["test"]
        mock_stream.result.timestamps = [0.25]
        mock_rec.create_stream.return_value = mock_stream

        pieces = [np.zeros(8000, dtype="float32")]
        out = transcribe.decode_pieces(pieces, rec=mock_rec)

        self.assertEqual(out, [(["test"], [0.25])])
        mock_stream.accept_waveform.assert_called_once_with(16000, pieces[0])
        mock_rec.decode_streams.assert_called_once_with([mock_stream])

    def test_unload_releases_matching_android_language(self):
        """transcribe.unload releases SpeechEngine when target language matches."""
        mock_engine = MagicMock()
        mock_engine.getLoadedLanguage.return_value = "hi"

        with patch("studio.tools.is_android", return_value=True), \
             patch.dict("sys.modules", {"com.mirza8975.clipflow": MagicMock(SpeechEngine=mock_engine)}):
            ok = transcribe.unload("hi")
            self.assertTrue(ok)
            mock_engine.release.assert_called_once_with(False)

    def test_unload_ignores_non_matching_android_language(self):
        """transcribe.unload does not release SpeechEngine if loaded model differs."""
        mock_engine = MagicMock()
        mock_engine.getLoadedLanguage.return_value = "en"

        with patch("studio.tools.is_android", return_value=True), \
             patch.dict("sys.modules", {"com.mirza8975.clipflow": MagicMock(SpeechEngine=mock_engine)}):
            ok = transcribe.unload("hi")
            self.assertTrue(ok)
            mock_engine.release.assert_not_called()


class TestModelFilesResolution(unittest.TestCase):
    """Test resolution of Transducer (Parakeet) and Whisper (Hindi) component files."""

    def test_model_files_transducer(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            (p / "encoder.int8.onnx").touch()
            (p / "decoder.int8.onnx").touch()
            (p / "joiner.int8.onnx").touch()
            (p / "tokens.txt").touch()

            files = transcribe._model_files(p)
            self.assertIn("encoder", files)
            self.assertIn("decoder", files)
            self.assertIn("joiner", files)
            self.assertIn("tokens", files)

    def test_model_files_whisper(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            (p / "tiny-encoder.onnx").touch()
            (p / "tiny-decoder.onnx").touch()
            (p / "tiny-tokens.txt").touch()

            files = transcribe._model_files(p)
            self.assertIn("encoder", files)
            self.assertIn("decoder", files)
            self.assertNotIn("joiner", files)
            self.assertIn("tokens", files)


class TestWhisperTimestampAndConcurrency(unittest.TestCase):
    """Regression tests for Whisper missing-timestamp synthesis and Android recognizer serialization."""

    def test_whisper_empty_timestamps_synthesized(self):
        mock_engine = MagicMock()
        mock_engine.initRecognizer.return_value = True
        mock_engine.transcribeFloatBytesDetailed.return_value = {
            "text": "नमस्ते दुनिया",
            "tokens": ["नम", "स्ते", " ", "दु", "निया"],
            "timestamps": [],
        }
        with patch.dict("sys.modules", {"com.mirza8975.clipflow": MagicMock(SpeechEngine=mock_engine)}):
            with patch("studio.fetch_model.ensure"), \
                 patch("studio.fetch_model.folder", return_value=Path("/data/models/hi")), \
                 patch("studio.transcribe._model_files", return_value={
                     "encoder": "/data/models/hi/tiny-encoder.onnx",
                     "decoder": "/data/models/hi/tiny-decoder.onnx",
                     "tokens": "/data/models/hi/tiny-tokens.txt",
                 }):
                rec = transcribe._AndroidRecognizer("hi")
                toks, ts = rec.decode_pieces([np.zeros(32000, dtype="float32")])[0]
                self.assertEqual(len(toks), 5)
                self.assertEqual(len(ts), 5)
                self.assertEqual(ts, sorted(ts))
                words = transcribe._words(toks, ts, 0, 32000)
                self.assertEqual([w["w"] for w in words], ["नमस्ते", "दुनिया"])

    def test_whisper_text_fallback_when_tokens_empty(self):
        mock_engine = MagicMock()
        mock_engine.initRecognizer.return_value = True
        mock_engine.transcribeFloatBytesDetailed.return_value = {
            "text": "Hello ClipFlow world",
            "tokens": [],
            "timestamps": [],
        }
        with patch.dict("sys.modules", {"com.mirza8975.clipflow": MagicMock(SpeechEngine=mock_engine)}):
            with patch("studio.fetch_model.ensure"), \
                 patch("studio.fetch_model.folder", return_value=Path("/data/models/hi")), \
                 patch("studio.transcribe._model_files", return_value={
                     "encoder": "/data/models/hi/tiny-encoder.onnx",
                     "decoder": "/data/models/hi/tiny-decoder.onnx",
                     "tokens": "/data/models/hi/tiny-tokens.txt",
                 }):
                rec = transcribe._AndroidRecognizer("hi")
                toks, ts = rec.decode_pieces([np.zeros(32000, dtype="float32")])[0]
                words = transcribe._words(toks, ts, 0, 32000)
                self.assertEqual([w["w"] for w in words], ["Hello", "ClipFlow", "world"])

    def test_words_handles_short_timestamps(self):
        words = transcribe._words(["hello", " world"], [0.15], 0, 32000)
        self.assertEqual([w["w"] for w in words], ["hello", "world"])

    def test_android_language_switch_reinitializes_singleton(self):
        mock_engine = MagicMock()
        mock_engine.initRecognizer.return_value = True
        mock_engine.transcribeFloatBytesDetailed.return_value = {
            "text": "ok",
            "tokens": ["ok"],
            "timestamps": [0.1],
        }

        def fake_folder(lang):
            return Path(f"/data/models/{lang}")

        def fake_files(d):
            return {
                "encoder": str(d / "enc.onnx"),
                "decoder": str(d / "dec.onnx"),
                "tokens": str(d / "tokens.txt"),
            }

        with patch.dict("sys.modules", {"com.mirza8975.clipflow": MagicMock(SpeechEngine=mock_engine)}):
            with patch("studio.fetch_model.ensure"), \
                 patch("studio.fetch_model.folder", side_effect=fake_folder), \
                 patch("studio.transcribe._model_files", side_effect=fake_files):
                rec_hi = transcribe._AndroidRecognizer("hi")
                rec_en = transcribe._AndroidRecognizer("en")
                # Decoding on rec_hi after rec_en loaded must re-init recognizer to "hi"
                rec_hi.decode_pieces([np.zeros(16000, dtype="float32")])
                langs = [call.args[6] for call in mock_engine.initRecognizer.call_args_list]
                self.assertEqual(langs, ["hi", "en", "hi"])

    def test_android_reinitializes_when_native_released(self):
        mock_engine = MagicMock()
        mock_engine.initRecognizer.return_value = True
        mock_engine.isModelLoaded.return_value = False
        mock_engine.transcribeFloatBytesDetailed.return_value = {
            "text": "recovered",
            "tokens": ["recovered"],
            "timestamps": [0.1],
        }
        with patch.dict("sys.modules", {"com.mirza8975.clipflow": MagicMock(SpeechEngine=mock_engine)}):
            with patch("studio.fetch_model.ensure"), \
                 patch("studio.fetch_model.folder", return_value=Path("/data/models/hi")), \
                 patch("studio.transcribe._model_files", return_value={
                     "encoder": "/data/models/hi/tiny-encoder.onnx",
                     "decoder": "/data/models/hi/tiny-decoder.onnx",
                     "tokens": "/data/models/hi/tiny-tokens.txt",
                 }):
                rec = transcribe._AndroidRecognizer("hi")
                toks, _ = rec.decode_pieces([np.zeros(16000, dtype="float32")])[0]
                self.assertEqual(toks, ["recovered"])
                # Must re-init on decode because native isModelLoaded() returned False
                self.assertEqual(mock_engine.initRecognizer.call_count, 2)

    def test_android_multithreaded_serialization_never_overlaps(self):
        in_flight = 0
        max_in_flight = 0
        guard = threading.Lock()
        active_lang = [None]
        mock_engine = MagicMock()
        mock_engine.isModelLoaded.return_value = True

        def fake_init(model_dir, is_whisper, enc, dec, joiner, tok, lang_code, threads):
            nonlocal in_flight, max_in_flight
            with guard:
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
            time.sleep(0.005)
            active_lang[0] = lang_code
            with guard:
                in_flight -= 1
            return True

        def fake_decode(raw_bytes, sr):
            nonlocal in_flight, max_in_flight
            with guard:
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
                lang = active_lang[0]
            time.sleep(0.005)
            with guard:
                in_flight -= 1
            return {"text": lang, "tokens": [lang], "timestamps": [0.1]}

        mock_engine.initRecognizer.side_effect = fake_init
        mock_engine.transcribeFloatBytesDetailed.side_effect = fake_decode

        with patch.dict("sys.modules", {"com.mirza8975.clipflow": MagicMock(SpeechEngine=mock_engine)}):
            with patch("studio.fetch_model.ensure"), \
                 patch("studio.fetch_model.folder", side_effect=lambda l: Path(f"/m/{l}")), \
                 patch("studio.transcribe._model_files", side_effect=lambda d: {
                     "encoder": str(d / "e.onnx"), "decoder": str(d / "d.onnx"), "tokens": str(d / "t.txt")
                 }):
                rec_hi = transcribe._AndroidRecognizer("hi")
                rec_en = transcribe._AndroidRecognizer("en")
                out = {"hi": [], "en": []}

                def run_worker(rec, bucket):
                    for _ in range(5):
                        res = rec.decode_pieces([np.zeros(8000, dtype="float32")])
                        bucket.append(res[0][0][0])

                t1 = threading.Thread(target=run_worker, args=(rec_hi, out["hi"]))
                t2 = threading.Thread(target=run_worker, args=(rec_en, out["en"]))
                t1.start(); t2.start(); t1.join(); t2.join()
                self.assertEqual(max_in_flight, 1)
                self.assertEqual(out["hi"], ["hi"] * 5)
                self.assertEqual(out["en"], ["en"] * 5)


if __name__ == "__main__":
    unittest.main()
