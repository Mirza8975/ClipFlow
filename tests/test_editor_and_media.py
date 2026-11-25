"""Tests for ClipFlow 2.0 Editor, FFmpeg Android Detection, and AI Speech/Loudness Fallback."""

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure missing third-party packages in test harness are gracefully mocked
submodules = [
    "numpy", "httpx", "sqlalchemy", "yt_dlp", "yt_dlp.utils",
    "uvicorn", "fastapi", "fastapi.concurrency", "fastapi.responses", "fastapi.staticfiles",
    "starlette", "starlette.background", "starlette.types"
]
for mod in submodules:
    if mod not in sys.modules:
        try:
            __import__(mod)
        except ImportError:
            sys.modules[mod] = MagicMock()

if "yt_dlp" in sys.modules and not hasattr(sys.modules["yt_dlp"], "utils"):
    sys.modules["yt_dlp"].utils = sys.modules.get("yt_dlp.utils", MagicMock())
if "yt_dlp.utils" in sys.modules and not hasattr(sys.modules["yt_dlp.utils"], "download_range_func"):
    sys.modules["yt_dlp.utils"].download_range_func = MagicMock(return_value=lambda *args, **kwargs: None)
if "fastapi" in sys.modules:
    for sub in ["concurrency", "responses", "staticfiles"]:
        if not hasattr(sys.modules["fastapi"], sub):
            setattr(sys.modules["fastapi"], sub, sys.modules.get(f"fastapi.{sub}", MagicMock()))
    def _pass_thru_decorator(*args, **kwargs):
        return lambda fn: fn
    for method in ["get", "post", "put", "delete", "patch", "options", "head"]:
        setattr(sys.modules["fastapi"].FastAPI.return_value, method, _pass_thru_decorator)

import unittest
from studio import config, tools


class TestToolsFFmpeg(unittest.TestCase):
    def test_is_available(self):
        self.assertTrue(callable(getattr(tools, "is_available", None)))
        self.assertIsInstance(tools.is_available("ffmpeg"), bool)
        self.assertIsInstance(tools.is_available("ffprobe"), bool)
        self.assertFalse(tools.is_available("nonexistent_binary_xyz_123"))


class TestRenderFiltersAndOverlays(unittest.TestCase):
    def setUp(self):
        from studio import render
        self.render = render

    def test_filter_presets_exist(self):
        expected_filters = {"normal", "vivid", "dramatic", "vintage", "bw", "cyber", "warm", "cool"}
        self.assertTrue(hasattr(self.render, "FILTERS"))
        for f in expected_filters:
            self.assertIn(f, self.render.FILTERS)
        self.assertEqual(self.render.FILTERS["normal"], "")
        self.assertIn("eq=", self.render.FILTERS["vivid"])
        self.assertIn("colorchannelmixer=", self.render.FILTERS["vintage"])
        self.assertIn("hue=", self.render.FILTERS["cyber"])

    def test_build_ass_with_overlays_and_stickers(self):
        words = [
            {"w": "Hello", "s": 10.5, "e": 11.0},
            {"w": "world", "s": 11.1, "e": 11.8}
        ]
        text_overlays = [
            {"text": "Custom Subtitle", "x": 250, "y": 700, "color": "#00f0ff", "size": 28, "rotation": 5}
        ]
        stickers = [
            {"emoji": "🔥", "x": 800, "y": 150, "size": 48}
        ]
        ass_content = self.render.build_ass(
            words=words,
            start=10.0,
            end=20.0,
            hook="TEST HOOK",
            style_name="bounce",
            highlight=["TEST"],
            hook_y=300,
            cap_y=1400,
            text_overlays=text_overlays,
            stickers=stickers
        )
        self.assertIn("[Script Info]", ass_content)
        self.assertIn("TEST HOOK", ass_content)
        self.assertIn("Custom Subtitle", ass_content)
        self.assertIn("🔥", ass_content)
        self.assertIn(r"\frz5", ass_content)
        self.assertIn(r"\c&hfff000&", ass_content.lower())

    def test_filter_chain_includes_video_filter(self):
        chain = self.render._filter(
            layout="split",
            fg_h=1080,
            video_filter="vivid"
        )
        self.assertIn(self.render.FILTERS["vivid"], chain)


class TestMediaAndroidPatch(unittest.TestCase):
    def test_patch_yt_dlp_ffmpeg(self):
        from studio import media
        self.assertTrue(callable(getattr(media, "_patch_yt_dlp_ffmpeg", None)))
        media._patch_yt_dlp_ffmpeg()
        try:
            import yt_dlp.postprocessor.ffmpeg as yt_ffmpeg
            inst = yt_ffmpeg.FFmpegPostProcessor()
            with patch.object(tools, "is_android", return_value=True):
                with patch.object(tools, "get_ffmpeg", return_value="/custom/libffmpeg.so"):
                    with patch.object(tools, "get_ffprobe", return_value="/custom/libffprobe.so"):
                        exe = inst._determine_executables()
                        self.assertEqual(exe.get("ffmpeg"), "/custom/libffmpeg.so")
                        self.assertEqual(exe.get("ffprobe"), "/custom/libffprobe.so")
        except (ImportError, AttributeError):
            pass


class TestBrainFallback(unittest.TestCase):
    def test_fallback_by_loudness_spoken_sentence(self):
        from studio import brain
        words = [
            {"w": "Hey", "s": 10.0, "e": 10.4},
            {"w": "guys", "s": 10.5, "e": 10.9},
            {"w": "watch", "s": 11.0, "e": 11.4},
            {"w": "this", "s": 11.5, "e": 11.8},
            {"w": "crazy", "s": 12.0, "e": 12.4},
            {"w": "shot", "s": 12.5, "e": 13.0},
        ]
        levels = [-30.0] * 30
        levels[10] = -5.0
        levels[11] = -4.0
        levels[12] = -5.0

        class FakeArr(list):
            def __neg__(self):
                return FakeArr([-x for x in self])

        mock_np = MagicMock()
        mock_np.argsort = lambda arr: sorted(range(len(arr)), key=lambda i: arr[i])
        with patch.object(brain, "np", mock_np):
            with patch.object(brain, "loud_zscore", return_value=FakeArr([0.0] * 10 + [3.0, 4.0, 3.0] + [0.0] * 17)):
                clips = brain.fallback_by_loudness(
                    db=levels,
                    words=words,
                    n=1,
                    max_len=15,
                    duration=30.0
                )
        self.assertEqual(len(clips), 1)
        c = clips[0]
        self.assertFalse(c["ai_detected"])
        self.assertEqual(c["detection_source"], "offline_speech_loudness")
        self.assertIn("offline", c["reason"].lower())
        self.assertIn("HEY GUYS WATCH", c["hook"])


class TestJobsRequestRenderValidation(unittest.TestCase):
    def test_request_render_fields(self):
        from studio import jobs
        mock_job = {
            "id": "job123",
            "dir": "/tmp/job123",
            "user": "u1",
            "clips": [{
                "id": "clip01",
                "start": 10.0,
                "end": 25.0,
                "hook": "Old",
                "title": "Old",
                "description": "",
                "status": "done"
            }]
        }
        with patch.dict(jobs._jobs, {"job123": mock_job}):
            with patch.object(jobs, "save"):
                with patch.object(jobs._q, "put"):
                    jobs.request_render("job123", "clip01", {
                        "start": 12.0,
                        "end": 28.0,
                        "filter": "vivid",
                        "volume": 1.5,
                        "muted": True,
                        "facecam": [5, 10, 30, 40],
                        "words": [{"w": "Test", "s": 12.0, "e": 13.0}],
                        "text_overlays": [{"text": "Hello", "x": 10, "y": 20}],
                        "stickers": [{"emoji": "⚡", "x": 50, "y": 50}]
                    })
                    updated = mock_job["clips"][0]
                    self.assertEqual(updated["start"], 12.0)
                    self.assertEqual(updated["end"], 28.0)
                    self.assertEqual(updated["filter"], "vivid")
                    self.assertEqual(updated["volume"], 1.5)
                    self.assertTrue(updated["muted"])
                    self.assertEqual(updated["facecam"], [5, 10, 30, 40])
                    self.assertEqual(updated["words"], [{"w": "Test", "s": 12.0, "e": 13.0}])
                    self.assertEqual(updated["text_overlays"], [{"text": "Hello", "x": 10, "y": 20}])
                    self.assertEqual(updated["stickers"], [{"emoji": "⚡", "x": 50, "y": 50}])
                    self.assertEqual(updated["status"], "queued")
                    self.assertTrue(updated["custom_words"])


class TestMediaDownloadSection(unittest.TestCase):
    def test_unrecoverable_errors_not_falling_back(self):
        from studio import media
        unrecoverable_exceptions = [
            Exception("getaddrinfo failed: Temporary failure in name resolution"),
            Exception("HTTP Error 403: Forbidden"),
            Exception("Sign in to confirm you’re not a bot"),
            Exception("no space left on device: ENOSPC"),
            KeyboardInterrupt("cancelled")
        ]
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            for exc in unrecoverable_exceptions:
                with patch("studio.media._ydl") as mock_ydl:
                    mock_ydl.return_value.__enter__.side_effect = exc
                    with patch("studio.media.download") as mock_full_download:
                        with self.assertRaises(type(exc)):
                            media.download_section("https://example.com/watch?v=abc", out_dir, "sec1", 0.0, 10.0)
                        mock_full_download.assert_not_called()

    def test_recoverable_range_error_triggers_fallback_and_cleans_up(self):
        from studio import media
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            dummy_src = out_dir / "temp_full_source.mp4"
            dummy_src.write_bytes(b"dummy video data")

            with patch("studio.media._ydl") as mock_ydl:
                mock_ydl.return_value.__enter__.side_effect = Exception("download_ranges error: byte ranges not supported")
                with patch("studio.load.disk_full", return_value=False):
                    with patch("shutil.disk_usage", return_value=MagicMock(free=10 * 1024**3)):
                        with patch("studio.media.download", return_value=dummy_src) as mock_dl:
                            with patch("studio.media.run") as mock_run:
                                # Mock ffmpeg run creating the target file
                                def make_target(args, **kwargs):
                                    (out_dir / "sec1.mp4").write_bytes(b"trimmed data")
                                mock_run.side_effect = make_target

                                res = media.download_section("https://example.com/watch?v=abc", out_dir, "sec1", 5.0, 15.0)
                                self.assertEqual(res, out_dir / "sec1.mp4")
                                mock_dl.assert_called_once()
                                # Verify full_src was cleaned up
                                self.assertFalse(dummy_src.exists())

    def test_storage_precheck_blocks_fallback_on_low_disk(self):
        from studio import media
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td)
            with patch("studio.media._ydl") as mock_ydl:
                mock_ydl.return_value.__enter__.side_effect = Exception("ffmpeg is not installed for range cut")
                with patch("studio.load.disk_full", return_value=True):
                    with patch("studio.media.download") as mock_dl:
                        with self.assertRaises(RuntimeError) as cm:
                            media.download_section("https://example.com/watch?v=abc", out_dir, "sec1", 0.0, 10.0)
                        self.assertIn("insufficient disk space", str(cm.exception).lower())
                        mock_dl.assert_not_called()


class TestJobsCustomWords(unittest.TestCase):
    def test_request_render_invalidates_fixed_words_cache(self):
        from studio import jobs
        mock_job = {
            "id": "j_cache",
            "dir": "/tmp/j_cache",
            "user": "u1",
            "clips": [{
                "id": "c1",
                "start": 10.0,
                "end": 20.0,
                "fixed_words": [{"w": "cached", "s": 10.0, "e": 11.0}],
                "fixed_for": [10.0, 20.0, "en2"],
                "status": "done"
            }]
        }
        with patch.dict(jobs._jobs, {"j_cache": mock_job}):
            with patch.object(jobs, "save"):
                with patch.object(jobs._q, "put"):
                    jobs.request_render("j_cache", "c1", {"start": 12.0})
                    c = mock_job["clips"][0]
                    self.assertNotIn("fixed_words", c)
                    self.assertNotIn("fixed_for", c)

    def test_caption_words_uses_custom_words_and_bypasses_ai(self):
        from studio import jobs
        clip = {
            "id": "c_edit",
            "start": 10.0,
            "end": 15.0,
            "custom_words": True,
            "words": [
                {"w": "Custom", "s": 10.2, "e": 10.8},
                {"w": "Edit", "s": 11.0, "e": 11.6}
            ],
            "fixed_words": [
                {"w": "OldStale", "s": 10.2, "e": 10.8}
            ],
            "fixed_for": [10.0, 15.0, "en2"]
        }
        with patch("studio.brain.english_captions") as mock_brain:
            result = jobs._caption_words({}, clip, words=[], off=10.0)
            mock_brain.assert_not_called()
            self.assertEqual(len(result), 2)
            self.assertEqual(result[0]["w"], "Custom")
            self.assertAlmostEqual(result[0]["s"], 0.2)
            self.assertEqual(result[1]["w"], "Edit")
            self.assertAlmostEqual(result[1]["s"], 1.0)


class TestRenderCoordinatesAndFraming(unittest.TestCase):
    def test_percentage_coordinate_and_color_mapping(self):
        from studio import render
        text_overlays = [
            {"text": "Percent Text", "x": 20, "y": 30, "font_size": 24, "color": "#00f", "rotation": 10}
        ]
        stickers = [
            {"emoji": "🚀", "x": 50, "y": 50, "scale": 1.5, "rotation": -15}
        ]
        ass = render.build_ass([], 0.0, 10.0, "HOOK", "bounce", [], 300, 1400,
                               text_overlays=text_overlays, stickers=stickers)
        # x = 1080 * 0.20 = 216, y = 1920 * 0.30 = 576
        self.assertIn(r"\pos(216,576)", ass)
        # font_size 24 * 2.5 = 60
        self.assertIn(r"\fs60", ass)
        # color #00f -> #0000ff -> BGR: FF0000
        self.assertIn(r"\c&HFF0000&", ass)
        self.assertIn(r"\frz10", ass)

        # sticker: x = 1080 * 0.5 = 540, y = 1920 * 0.5 = 960, scale 1.5 * 96 = 144
        self.assertIn(r"\pos(540,960)", ass)
        self.assertIn(r"\fs144", ass)
        self.assertIn(r"\frz-15", ass)
        self.assertIn("🚀", ass)

    def test_facecam_normalization_and_clamping(self):
        from studio import render
        # Percentage input on 1920x1080
        norm = render._normalize_facecam([10, 10, 40, 40], src_w=1920, src_h=1080)
        self.assertEqual(norm[0], 192)   # fx = 10% of 1920
        self.assertEqual(norm[1], 108)   # fy = 10% of 1080
        self.assertEqual(norm[2], 768)   # fw = 40% of 1920
        self.assertEqual(norm[3], 432)   # fh = 40% of 1080

        # Out-of-bounds input
        clamped = render._normalize_facecam([-50, -50, 2500, 2000], src_w=1920, src_h=1080)
        self.assertEqual(clamped[0], 0)
        self.assertEqual(clamped[1], 0)
        self.assertEqual(clamped[2], 1920)
        self.assertEqual(clamped[3], 1080)

    def test_split_and_pip_filter_chains(self):
        from studio import render
        # Split layout with facecam
        split_fc = render._filter("split", fg_h=1080, facecam=[192, 108, 768, 432, 0, 0])
        self.assertIn("crop=768:432:192:108", split_fc)
        self.assertIn("vstack", split_fc)

        # PiP layout
        pip_fc = render._filter("pip", fg_h=1080, facecam=[192, 108, 768, 432, 0, 0])
        self.assertIn("overlay=", pip_fc)
        self.assertIn("crop=768:432:192:108", pip_fc)


class TestServerRenameSidecar(unittest.TestCase):
    def test_rename_clip_updates_sidecar_json(self):
        import json
        from studio import server, jobs
        with tempfile.TemporaryDirectory() as td:
            job_dir = Path(td)
            clips_dir = job_dir / "clips"
            clips_dir.mkdir()
            sidecar = clips_dir / "clip99.json"
            sidecar.write_text(json.dumps({"title": "Old Name", "score": 90}), encoding="utf-8")

            mock_job = {
                "id": "job99",
                "dir": str(job_dir),
                "source": {"title": "Test Video"},
                "clips": [{"id": "clip99", "title": "Old Name"}]
            }
            with patch.object(server, "_clip_or_404", return_value=(mock_job, mock_job["clips"][0])):
                with patch.object(jobs, "save"):
                    server.rename_clip("job99", "clip99", {"title": "Updated Title"})
                    self.assertEqual(mock_job["clips"][0]["title"], "Updated Title")
                    data = json.loads(sidecar.read_text(encoding="utf-8"))
                    self.assertEqual(data["title"], "Updated Title")
                    self.assertEqual(data["score"], 90)


class TestBrainFallbackMalformedTimestamps(unittest.TestCase):
    def test_fallback_by_loudness_handles_malformed_words(self):
        from studio import brain
        malformed_words = [
            {"w": "good", "s": 10.0, "e": 10.5},
            {"w": "missing_s", "e": 11.0},
            {"w": "none_s", "s": None, "e": 11.5},
            {"w": "bad_s", "s": "not-a-number", "e": 12.0},
            "not-even-a-dict",
            {"w": None, "s": 10.2, "e": 10.8}
        ]
        levels = [-20.0] * 30
        levels[10] = -5.0

        class FakeArr(list):
            def __neg__(self):
                return FakeArr([-x for x in self])

        mock_np = MagicMock()
        mock_np.argsort = lambda arr: sorted(range(len(arr)), key=lambda i: arr[i])
        with patch.object(brain, "np", mock_np):
            with patch.object(brain, "loud_zscore", return_value=FakeArr([0.0] * 10 + [5.0] + [0.0] * 19)):
                # Must not raise TypeError
                picks = brain.fallback_by_loudness(db=levels, words=malformed_words, n=1, max_len=15, duration=30.0)
                self.assertEqual(len(picks), 1)
                self.assertIn("start", picks[0])
                self.assertIn("end", picks[0])


if __name__ == "__main__":
    unittest.main()
