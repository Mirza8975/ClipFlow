"""Tests for ClipFlow 2.0 Editor, FFmpeg Android Detection, and AI Speech/Loudness Fallback."""

import os
import subprocess
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


class TestQuickJSRuntimeAndYtdlpConfig(unittest.TestCase):
    def setUp(self):
        from studio import media
        self.media = media

    def test_quickjs_discovery_via_env(self):
        with tempfile.NamedTemporaryFile(suffix=".so") as tf:
            os.chmod(tf.name, 0o755)
            with patch.dict(os.environ, {"CLIPFLOW_QUICKJS_PATH": tf.name}):
                self.assertEqual(tools.get_quickjs(), str(Path(tf.name).resolve()))
                rt = tools.get_js_runtime()
                self.assertIsNotNone(rt)
                self.assertIn("quickjs", rt)
                self.assertEqual(rt["quickjs"]["path"], str(Path(tf.name).resolve()))
                self.assertTrue(tools.is_available("quickjs"))

    def test_quickjs_fallback_when_missing(self):
        with patch.dict(os.environ, {"CLIPFLOW_QUICKJS_PATH": "/nonexistent/libqjs.so"}):
            with patch("shutil.which", return_value=None):
                with patch.object(tools, "_places", return_value=[]):
                    self.assertIsNone(tools.get_quickjs())
                    self.assertFalse(tools.is_available("quickjs"))

    def test_ydl_includes_js_runtimes_and_extractor_args(self):
        mock_ydl_class = MagicMock()
        with patch("yt_dlp.YoutubeDL", mock_ydl_class):
            with patch.object(tools, "get_js_runtime", return_value={"quickjs": {"path": "/app/lib/libqjs.so"}}):
                self.media._ydl({"test_opt": 123})
                self.assertTrue(mock_ydl_class.called)
                call_args = mock_ydl_class.call_args[0][0]
                self.assertEqual(call_args["js_runtimes"], {"quickjs": {"path": "/app/lib/libqjs.so"}})
                self.assertIn("extractor_args", call_args)
                self.assertIn("youtube", call_args["extractor_args"])
                self.assertIn("player_client", call_args["extractor_args"]["youtube"])
                self.assertEqual(call_args["test_opt"], 123)

    def test_normalize_youtube_url(self):
        urls = [
            ("https://youtu.be/kR8bhkVAgBY", "https://www.youtube.com/watch?v=kR8bhkVAgBY"),
            ("https://youtu.be/kR8bhkVAgBY?si=abc123xyz", "https://www.youtube.com/watch?v=kR8bhkVAgBY"),
            ("https://www.youtube.com/watch?v=kR8bhkVAgBY&feature=youtu.be", "https://www.youtube.com/watch?v=kR8bhkVAgBY"),
            ("https://twitch.tv/streamer", "https://twitch.tv/streamer"),
            ("https://kick.com/creator", "https://kick.com/creator"),
        ]
        for inp, expected in urls:
            self.assertEqual(self.media.normalize_youtube_url(inp), expected)

    def test_clear_youtube_pause_and_copy(self):
        self.assertIn("ClipFlow", self.media.YT_BLOCKED)
        self.assertNotIn("PeakCut", self.media.YT_BLOCKED)
        self.media._yt_refused_until = 9999999999.0
        self.assertTrue(self.media.youtube_paused())
        self.media.clear_youtube_pause()
        self.assertFalse(self.media.youtube_paused())

    def test_non_youtube_isolation(self):
        self.media._yt_refused_until = 9999999999.0
        # Twitch and Kick URLs must NOT raise YouTubeBlocked even if circuit breaker is paused
        self.media.check_youtube("https://www.twitch.tv/ninja")
        self.media.check_youtube("https://kick.com/xqc")
        # But YouTube URL must raise YouTubeBlocked
        with self.assertRaises(self.media.YouTubeBlocked):
            self.media.check_youtube("https://youtu.be/kR8bhkVAgBY")
        self.media.clear_youtube_pause()


class TestFFmpegQualityAndEncoders(unittest.TestCase):
    def test_get_video_encoders_parsing(self):
        sample_output = (
            "Encoders:\n"
            " V..... = Video\n"
            " V..... libx264              libx264 H.264 / AVC / MPEG-4 AVC / MPEG-4 part 10 (codec h264)\n"
            " V....D h264_mediacodec       (codec h264)\n"
            " V.S..D mpeg4                \n"
            " A..... aac                  AAC (Advanced Audio Coding)\n"
        )
        mock_proc = MagicMock(returncode=0, stdout=sample_output.encode("utf-8"), stderr=b"")
        with patch("subprocess.run", return_value=mock_proc):
            encs = tools.get_video_encoders(ffmpeg_bin="/fake/ffmpeg")
            self.assertIn("libx264", encs)
            self.assertIn("h264_mediacodec", encs)
            self.assertIn("mpeg4", encs)
            self.assertNotIn("aac", encs)

    def test_get_render_encoder_candidates_with_x264(self):
        with patch.object(tools, "get_video_encoders", return_value={"libx264", "mpeg4"}):
            candidates = tools.get_render_encoder_candidates(crf_val=23, preset_val="fast")
            labels = [c[0] for c in candidates]
            self.assertIn("x264", labels)
            x264_args = next(c[1] for c in candidates if c[0] == "x264")
            self.assertIn("-crf", x264_args)
            self.assertIn("23", x264_args)
            self.assertIn("-preset", x264_args)
            self.assertIn("fast", x264_args)

    def test_get_render_encoder_candidates_mediacodec_and_mpeg4(self):
        # On Android ARM64 where libx264 is unavailable
        with patch.object(tools, "get_video_encoders", return_value={"h264_mediacodec", "mpeg4"}):
            candidates = tools.get_render_encoder_candidates(crf_val=21)
            labels = [c[0] for c in candidates]
            self.assertNotIn("x264", labels)
            self.assertIn("mediacodec", labels)
            self.assertIn("mpeg4", labels)

            # MediaCodec candidate must use -b:v and NOT -crf
            mc_args = next(c[1] for c in candidates if c[0] == "mediacodec")
            self.assertNotIn("-crf", mc_args)
            self.assertIn("-c:v", mc_args)
            self.assertIn("h264_mediacodec", mc_args)
            self.assertIn("-b:v", mc_args)
            self.assertIn("4500k", mc_args)

            # MPEG4 candidate must use -q:v and NOT -crf
            mp4_args = next(c[1] for c in candidates if c[0] == "mpeg4")
            self.assertNotIn("-crf", mp4_args)
            self.assertIn("-c:v", mp4_args)
            self.assertIn("mpeg4", mp4_args)
            self.assertIn("-q:v", mp4_args)
            self.assertIn("3", mp4_args)

    def test_crf_mapping_formula(self):
        # Lower CRF -> Higher bitrate / lower q:v
        with patch.object(tools, "get_video_encoders", return_value={"h264_mediacodec", "mpeg4"}):
            c_high = tools.get_render_encoder_candidates(crf_val=15)
            mc_high = next(c[1] for c in c_high if c[0] == "mediacodec")
            mp4_high = next(c[1] for c in c_high if c[0] == "mpeg4")
            bitrate_idx = mc_high.index("-b:v") + 1
            bitrate_num = int(mc_high[bitrate_idx].replace("k", ""))
            self.assertGreater(bitrate_num, 4500)
            q_idx = mp4_high.index("-q:v") + 1
            self.assertEqual(mp4_high[q_idx], "2")

            c_low = tools.get_render_encoder_candidates(crf_val=28)
            mc_low = next(c[1] for c in c_low if c[0] == "mediacodec")
            mp4_low = next(c[1] for c in c_low if c[0] == "mpeg4")
            bitrate_low_idx = mc_low.index("-b:v") + 1
            bitrate_low_num = int(mc_low[bitrate_low_idx].replace("k", ""))
            self.assertLess(bitrate_low_num, 4500)
            q_low_idx = mp4_low.index("-q:v") + 1
            self.assertEqual(mp4_low[q_low_idx], "5")

    def test_real_android_ffmpeg_execution_when_present(self):
        # Real execution test on actual Android FFmpeg binary
        arm64_ffmpeg = Path(__file__).resolve().parent.parent / "android/app/src/main/jniLibs/arm64-v8a/libffmpeg.so"
        if not arm64_ffmpeg.is_file() or not os.access(arm64_ffmpeg, os.X_OK):
            self.skipTest("Bundled arm64 libffmpeg.so not directly executable in this environment")

        # 1. Verify libx264 with -crf fails as expected with the exact known error
        p_fail = subprocess.run(
            [str(arm64_ffmpeg), "-f", "lavfi", "-i", "color=c=black:s=64x64:d=0.1", "-c:v", "libx264", "-crf", "21", "-f", "null", "-"],
            capture_output=True, text=True
        )
        self.assertNotEqual(p_fail.returncode, 0)
        self.assertIn("crf", p_fail.stderr.lower())

        # 2. Verify mpeg4 with -q:v succeeds completely without error
        p_mpeg4 = subprocess.run(
            [str(arm64_ffmpeg), "-f", "lavfi", "-i", "color=c=black:s=64x64:d=0.1", "-c:v", "mpeg4", "-q:v", "3", "-f", "null", "-"],
            capture_output=True, text=True
        )
        self.assertEqual(p_mpeg4.returncode, 0)


class TestGeminiAIModelIntegration(unittest.TestCase):
    def setUp(self):
        from studio import ai
        self.ai = ai

    def test_list_models_gemini_filters_non_generation(self):
        mock_response = MagicMock(
            status_code=200,
            json=lambda: {
                "models": [
                    {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent", "countTokens"]},
                    {"name": "models/gemini-2.5-pro", "supportedGenerationMethods": ["generateContent"]},
                    {"name": "models/gemini-2.5-flash-preview-tts", "supportedGenerationMethods": ["generateContent"]},
                    {"name": "models/gemini-2.5-flash-image-preview", "supportedGenerationMethods": ["generateContent"]},
                    {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]},
                    {"name": "models/aqa", "supportedGenerationMethods": ["generateContent"]},
                    {"name": "models/imagen-3.0-generate-002", "supportedGenerationMethods": ["generateImages"]},
                ]
            }
        )
        with patch("httpx.get", return_value=mock_response):
            models = self.ai.list_models(provider_id="gemini", with_key="fake-test-key")
            self.assertIn("gemini-2.5-flash", models)
            self.assertIn("gemini-2.5-pro", models)
            self.assertNotIn("gemini-2.5-flash-preview-tts", models)
            self.assertNotIn("gemini-2.5-flash-image-preview", models)
            self.assertNotIn("text-embedding-004", models)
            self.assertNotIn("aqa", models)
            self.assertNotIn("imagen-3.0-generate-002", models)

    def test_ai_test_with_explicit_parameters(self):
        with patch.object(self.ai, "call", return_value=(["ok"], 10, 5)) as mock_call:
            res = self.ai.test(model="gemini-2.5-flash", provider_id="gemini", key_val="custom-key-xyz")
            self.assertTrue(res["ok"])
            self.assertEqual(res["model"], "gemini-2.5-flash")
            self.assertTrue(mock_call.called)
            call_kwargs = mock_call.call_args[1]
            self.assertEqual(call_kwargs.get("provider_id"), "gemini")
            self.assertEqual(call_kwargs.get("api_key"), "custom-key-xyz")

    def test_ai_explain_diagnostics(self):
        # 1. Retired model message
        exp_retired = self.ai.explain("HTTP 404 models/gemini-2.5-flash-lite is no longer available to new users", model="gemini-2.5-flash-lite")
        self.assertIn("retired", exp_retired)
        self.assertIn("gemini-2.5-flash-lite", exp_retired)
        self.assertIn("Find models", exp_retired)

        # 2. Key refused message
        exp_key = self.ai.explain("HTTP 401 API_KEY_INVALID", model="gemini-2.5-flash")
        self.assertIn("refused the key", exp_key)

        # 3. Quota exceeded message
        exp_quota = self.ai.explain("HTTP 429 Resource has been exhausted (e.g. check quota)", model="gemini-2.5-flash")
        self.assertIn("hit its limit", exp_quota)

        # 4. ClipFlow branding on connection error
        exp_conn = self.ai.explain("HTTP 502 ConnectError: getaddrinfo failed", model="gemini-2.5-flash")
        self.assertIn("ClipFlow couldn't reach", exp_conn)
        self.assertNotIn("PeakCut", exp_conn)


class TestFrameLayoutsAndDimensions(unittest.TestCase):
    def setUp(self):
        from studio import render, jobs
        self.render = render
        self.jobs = jobs

    def test_layouts_contain_landscape_and_square(self):
        self.assertIn("landscape", self.render.LAYOUTS)
        self.assertIn("square", self.render.LAYOUTS)
        self.assertIn("blur", self.render.LAYOUTS)
        self.assertIn("crop", self.render.LAYOUTS)
        self.assertIn("split", self.render.LAYOUTS)
        self.assertIn("pip", self.render.LAYOUTS)

    def test_get_dimensions(self):
        self.assertEqual(self.render.get_dimensions("landscape"), (1920, 1080))
        self.assertEqual(self.render.get_dimensions("square"), (1080, 1080))
        self.assertEqual(self.render.get_dimensions("blur"), (1080, 1920))
        self.assertEqual(self.render.get_dimensions("crop"), (1080, 1920))

    def test_geometry_and_safe_margins(self):
        layout_l, fg_h_l, hook_y_l, cap_y_l = self.render.geometry(1920, 1080, "landscape")
        self.assertEqual(layout_l, "landscape")
        self.assertEqual(fg_h_l, 1080)
        self.assertLess(hook_y_l, cap_y_l)

        layout_s, fg_h_s, hook_y_s, cap_y_s = self.render.geometry(1920, 1080, "square")
        self.assertEqual(layout_s, "square")
        self.assertEqual(fg_h_s, 1080)
        self.assertLess(hook_y_s, cap_y_s)

    def test_build_ass_dynamic_dimensions(self):
        ass_16_9 = self.render.build_ass([], 0, 10, "LANDSCAPE HOOK", "bounce", [], 120, 900, out_w=1920, out_h=1080)
        self.assertIn("PlayResX: 1920", ass_16_9)
        self.assertIn("PlayResY: 1080", ass_16_9)

        ass_1_1 = self.render.build_ass([], 0, 10, "SQUARE HOOK", "bounce", [], 140, 920, out_w=1080, out_h=1080)
        self.assertIn("PlayResX: 1080", ass_1_1)
        self.assertIn("PlayResY: 1080", ass_1_1)

    def test_filter_chain_for_landscape_and_square(self):
        chain_l = self.render._filter("landscape", 1080)
        self.assertIn("scale=1920:1080", chain_l)

        chain_s = self.render._filter("square", 1080)
        self.assertIn("scale=1080:1080", chain_s)

    def test_jobs_options_accept_landscape_and_square(self):
        clean_l = self.jobs.clean_options({"layout": "landscape"})
        self.assertEqual(clean_l.get("layout"), "landscape")

        clean_s = self.jobs.clean_options({"layout": "square"})
        self.assertEqual(clean_s.get("layout"), "square")


class TestAndroidBlurAndSubtitleFilterHandling(unittest.TestCase):
    """Verifies blur filter fallback (avgblur/gblur vs boxblur) and subtitle handling when libass is absent."""

    def setUp(self):
        from studio import render, tools
        self.render = render
        self.tools = tools
        self.tools._SUPPORTED_FILTERS = None

    def tearDown(self):
        self.tools._SUPPORTED_FILTERS = None

    def test_filter_parsing_and_detection(self):
        sample_filters_output = (
            "Filters:\n"
            "  T.. = Timeline support\n"
            "  .S. = Slice threading\n"
            "  ------\n"
            " TS aap               AA->A      (null)\n"
            " T. avgblur           V->V       (null)\n"
            " TS gblur             V->V       (null)\n"
            " TS colorlevels       V->V       (null)\n"
            " TS huesaturation     V->V       (null)\n"
            " .. scale             V->V       (null)\n"
        )
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=sample_filters_output.encode("utf-8"))
            filters = self.tools.get_supported_filters("mock_ffmpeg")
            self.assertIn("avgblur", filters)
            self.assertIn("gblur", filters)
            self.assertIn("colorlevels", filters)
            self.assertIn("huesaturation", filters)
            self.assertNotIn("boxblur", filters)
            self.assertNotIn("ass", filters)

    def test_best_blur_filter_selection(self):
        # 1. Desktop with boxblur
        with patch.object(self.tools, "get_supported_filters", return_value={"boxblur", "avgblur", "gblur"}):
            self.assertEqual(self.tools.get_best_blur_filter(), "boxblur=6:2")

        # 2. Android with avgblur (boxblur missing)
        with patch.object(self.tools, "get_supported_filters", return_value={"avgblur", "gblur"}):
            self.assertEqual(self.tools.get_best_blur_filter(), "avgblur=sizeX=6:sizeY=6")

        # 3. Android with only gblur
        with patch.object(self.tools, "get_supported_filters", return_value={"gblur"}):
            self.assertEqual(self.tools.get_best_blur_filter(), "gblur=sigma=6")

        # 4. Unknown/probe failed -> fallback to desktop default
        with patch.object(self.tools, "get_supported_filters", return_value=set()):
            self.assertEqual(self.tools.get_best_blur_filter(), "boxblur=6:2")

    def test_bg_dim_filter_selection(self):
        # 1. Desktop with eq
        with patch.object(self.tools, "get_supported_filters", return_value={"eq", "colorlevels"}):
            self.assertEqual(self.tools.get_bg_dim_filter(), "eq=brightness=-0.14:saturation=1.15")

        # 2. Android with colorlevels and huesaturation (eq missing)
        with patch.object(self.tools, "get_supported_filters", return_value={"colorlevels", "huesaturation"}):
            self.assertEqual(
                self.tools.get_bg_dim_filter(),
                "colorlevels=rimax=0.86:gimax=0.86:bimax=0.86,huesaturation=saturation=0.15"
            )

        # 3. Android with only colorlevels
        with patch.object(self.tools, "get_supported_filters", return_value={"colorlevels"}):
            self.assertEqual(self.tools.get_bg_dim_filter(), "colorlevels=rimax=0.86:gimax=0.86:bimax=0.86")

        # 4. Unknown/probe failed -> fallback to desktop eq
        with patch.object(self.tools, "get_supported_filters", return_value=set()):
            self.assertEqual(self.tools.get_bg_dim_filter(), "eq=brightness=-0.14:saturation=1.15")

    def test_blur_filter_chain_construction(self):
        # Desktop chain
        with patch.object(self.tools, "get_best_blur_filter", return_value="boxblur=6:2"), \
             patch.object(self.tools, "get_bg_dim_filter", return_value="eq=brightness=-0.14:saturation=1.15"):
            chain_desktop = self.render._filter("blur", 608)
            self.assertIn("boxblur=6:2", chain_desktop)
            self.assertIn("eq=brightness=-0.14:saturation=1.15", chain_desktop)
            self.assertIn("overlay=0:(1920-h)/2", chain_desktop)

        # Android chain
        with patch.object(self.tools, "get_best_blur_filter", return_value="avgblur=sizeX=6:sizeY=6"), \
             patch.object(self.tools, "get_bg_dim_filter", return_value="colorlevels=rimax=0.86:gimax=0.86:bimax=0.86,huesaturation=saturation=0.15"):
            chain_android = self.render._filter("blur", 608)
            self.assertIn("avgblur=sizeX=6:sizeY=6", chain_android)
            self.assertIn("colorlevels=rimax=0.86:gimax=0.86:bimax=0.86,huesaturation=saturation=0.15", chain_android)
            self.assertIn("overlay=0:(1920-h)/2", chain_android)

    def test_has_ass_events(self):
        ass_with_dialogue = "[Script Info]\n[Events]\nDialogue: 1,0:00:00.00,0:00:01.00,Cap,,0,0,0,,Hello\n"
        self.assertTrue(self.render.has_ass_events(ass_with_dialogue))

        ass_without_dialogue = "[Script Info]\n[Events]\nFormat: Layer, Start, End, Style\n"
        self.assertFalse(self.render.has_ass_events(ass_without_dialogue))
        self.assertFalse(self.render.has_ass_events(""))

    def test_subtitle_handling_without_ass_raises_when_captions_requested(self):
        ass_content = "[Script Info]\n[Events]\nDialogue: 1,0:00:00.00,0:00:01.00,Cap,,0,0,0,,Hello\n"
        work = Path(tempfile.mkdtemp())
        out_dir = Path(tempfile.mkdtemp())
        src = work / "dummy.mp4"
        src.write_bytes(b"dummy")

        with patch.object(self.tools, "has_ass_filter", return_value=False):
            with self.assertRaises(RuntimeError) as ctx:
                self.render._render_in(
                    work, src, out_dir, "test1", 0.0, 1.0, "blur", 600, ass_content,
                    captions_requested=True
                )
            self.assertIn("Subtitle burning requires FFmpeg with 'ass' filter (libass) support", str(ctx.exception))
            self.assertIn("turn off captions", str(ctx.exception))

    def test_subtitle_handling_bypasses_ass_filter_when_captions_disabled(self):
        ass_content = "[Script Info]\n[Events]\nFormat: Layer, Start, End, Style\n"
        work = Path(tempfile.mkdtemp())
        out_dir = Path(tempfile.mkdtemp())
        src = work / "dummy.mp4"
        src.write_bytes(b"dummy")

        with patch.object(self.tools, "has_ass_filter", return_value=False), \
             patch("subprocess.run") as mock_run, \
             patch("studio.render.run") as mock_media_run, \
             patch("shutil.move") as mock_move:
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            res = self.render._render_in(
                work, src, out_dir, "test2", 0.0, 1.0, "blur", 600, ass_content,
                captions_requested=False
            )
            # Verify the command passed to subprocess.run used [v0]copy[v] rather than ass filter
            call_args = mock_run.call_args[0][0]
            filter_arg_idx = call_args.index("-filter_complex")
            filter_complex = call_args[filter_arg_idx + 1]
            self.assertIn("[v0]copy[v]", filter_complex)
            self.assertNotIn("ass=", filter_complex)
            self.assertEqual(res["file"], "test2.mp4")

    def test_real_android_blur_and_subtitle_execution(self):
        bin_path = Path(__file__).resolve().parent.parent / "android/app/src/main/jniLibs/arm64-v8a/libffmpeg.so"
        if not bin_path.is_file():
            self.skipTest("libffmpeg.so not found on disk")

        # 1. Verify filter detection on real binary
        filters = self.tools.get_supported_filters(str(bin_path))
        self.assertIn("avgblur", filters)
        self.assertIn("gblur", filters)
        self.assertNotIn("boxblur", filters)
        self.assertNotIn("ass", filters)
        self.assertFalse(self.tools.has_ass_filter(str(bin_path)))
        self.assertEqual(self.tools.get_best_blur_filter(str(bin_path)), "avgblur=sizeX=6:sizeY=6")

        # 2. Perform real render test with captions=False using actual libffmpeg.so
        with tempfile.TemporaryDirectory() as tmp_dir:
            work = Path(tmp_dir)
            src_vid = work / "test_src.mp4"
            out_dir = work / "out"
            out_dir.mkdir()

            # Generate synthetic 1-second video with audio
            gen = subprocess.run([
                str(bin_path), "-y",
                "-f", "lavfi", "-i", "testsrc=size=1920x1080:rate=25",
                "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000",
                "-t", "1",
                "-c:v", "mpeg4", "-q:v", "3",
                "-c:a", "aac", "-b:a", "128k",
                str(src_vid)
            ], capture_output=True, text=True)
            self.assertEqual(gen.returncode, 0, f"Source video creation failed: {gen.stderr}")

            # Set environment so studio.tools resolves libffmpeg.so
            old_ffmpeg = os.environ.get("CLIPFLOW_FFMPEG_PATH")
            try:
                os.environ["CLIPFLOW_FFMPEG_PATH"] = str(bin_path)
                self.tools._BINARIES.clear()
                self.tools._CAPABILITY.clear()
                self.tools._SUPPORTED_ENCODERS = None
                self.tools._SUPPORTED_FILTERS = None

                # Test A: Render without captions succeeds
                clip_no_caps = {"start": 0.0, "end": 1.0, "captions": False, "hook": "", "layout": "blur"}
                res = self.render.render_clip(src_vid, out_dir, "clip_real", clip_no_caps, [], 1920, 1080)
                self.assertEqual(res["file"], "clip_real.mp4")
                self.assertEqual(res["cover"], "clip_real.jpg")
                self.assertTrue((out_dir / "clip_real.mp4").is_file())
                self.assertGreater((out_dir / "clip_real.mp4").stat().st_size, 1000)
                self.assertTrue((out_dir / "clip_real.jpg").is_file())
                self.assertGreater((out_dir / "clip_real.jpg").stat().st_size, 1000)

                # Test B: Render with captions triggers subtitle error boundary
                clip_caps = {"start": 0.0, "end": 1.0, "captions": True, "hook": "Android Test", "layout": "blur"}
                words = [{"w": "Test", "s": 0.1, "e": 0.8}]
                with self.assertRaises(RuntimeError) as err_ctx:
                    self.render.render_clip(src_vid, out_dir, "clip_caps", clip_caps, words, 1920, 1080)
                self.assertIn("Subtitle burning requires FFmpeg with 'ass' filter", str(err_ctx.exception))
            finally:
                if old_ffmpeg is not None:
                    os.environ["CLIPFLOW_FFMPEG_PATH"] = old_ffmpeg
                else:
                    os.environ.pop("CLIPFLOW_FFMPEG_PATH", None)
                self.tools._BINARIES.clear()
                self.tools._CAPABILITY.clear()
                self.tools._SUPPORTED_ENCODERS = None
                self.tools._SUPPORTED_FILTERS = None


class TestStabilityFixBatch(unittest.TestCase):
    """Regression suite for ClipFlow 2.0 Stability Fix Batch (Audio, Overlays, Layouts, AI, URI Policy)."""

    def setUp(self):
        from studio import ai, render, tools
        self.ai = ai
        self.render = render
        self.tools = tools

    @staticmethod
    def _mean_max_db(bin_path: Path, media_path: Path) -> tuple[float, float, str]:
        import re
        p = subprocess.run(
            [str(bin_path), "-i", str(media_path), "-af", "volumedetect", "-f", "null", "-"],
            capture_output=True, text=True,
        )
        txt = p.stderr + p.stdout
        mean_m = re.search(r"mean_volume:\s*(-?[\d.]+|inf|-inf)\s*dB", txt)
        max_m = re.search(r"max_volume:\s*(-?[\d.]+|inf|-inf)\s*dB", txt)
        audio_m = re.search(r"Audio:\s*([^\n]+)", txt)
        mean_v = -100.0 if (not mean_m or "inf" in mean_m.group(1)) else float(mean_m.group(1))
        max_v = -100.0 if (not max_m or "inf" in max_m.group(1)) else float(max_m.group(1))
        return mean_v, max_v, (audio_m.group(1) if audio_m else "")

    @staticmethod
    def _video_dims(bin_path: Path, media_path: Path) -> tuple[int, int]:
        import re
        p = subprocess.run([str(bin_path), "-i", str(media_path)], capture_output=True, text=True)
        m = re.search(r"Video:.*?,\s*(\d{2,5})x(\d{2,5})", p.stderr + p.stdout)
        if not m:
            raise AssertionError(f"Could not parse video dimensions from stderr: {p.stderr[-400:]}")
        return int(m.group(1)), int(m.group(2))

    # --- Priority 1: Audio Pipeline ---
    def test_audio_filter_ordering_and_mute_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            out_dir = work / "out"
            out_dir.mkdir()
            src = work / "dummy.mp4"
            src.write_bytes(b"dummy")
            ass_empty = "[Script Info]\n[Events]\nFormat: Layer, Start, End, Style\n"

            cases = [
                (dict(volume=0.0, muted=False), "volume=0"),
                (dict(volume=1.0, muted=True), "volume=0"),
                (dict(volume=0.2, muted=False), "loudnorm=I=-14:TP=-1.5:LRA=11,volume=0.20"),
                (dict(volume=1.0, muted=False), "loudnorm=I=-14:TP=-1.5:LRA=11"),
                (dict(volume=1.8, muted=False), "loudnorm=I=-14:TP=-1.5:LRA=11,volume=1.80"),
            ]
            for i, (kwargs, expected_af) in enumerate(cases):
                with patch.object(self.tools, "has_ass_filter", return_value=False), \
                     patch("subprocess.run", return_value=MagicMock(returncode=0, stderr="")) as mock_run, \
                     patch("studio.render.run"), \
                     patch("shutil.move"):
                    self.render._render_in(
                        work, src, out_dir, f"aud_{i}", 0.0, 1.0, "blur", 608, ass_empty,
                        captions_requested=False, **kwargs
                    )
                    cmd = mock_run.call_args[0][0]
                    af_val = cmd[cmd.index("-af") + 1]
                    self.assertEqual(af_val, expected_af)
                    self.assertEqual(cmd[cmd.index("-ar") + 1], "48000")

    def test_real_android_audio_mute_and_volume_levels(self):
        bin_path = Path(__file__).resolve().parent.parent / "android/app/src/main/jniLibs/arm64-v8a/libffmpeg.so"
        if not bin_path.is_file():
            self.skipTest("libffmpeg.so not found on disk")

        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            src = work / "src.mp4"
            out_dir = work / "out"
            out_dir.mkdir()
            gen = subprocess.run([
                str(bin_path), "-y",
                "-f", "lavfi", "-i", "color=c=blue:size=1280x720:rate=25",
                "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000",
                "-t", "1.5", "-c:v", "mpeg4", "-q:v", "4", "-c:a", "aac", "-b:a", "128k",
                str(src)
            ], capture_output=True, text=True)
            self.assertEqual(gen.returncode, 0, gen.stderr)

            old_ffmpeg = os.environ.get("CLIPFLOW_FFMPEG_PATH")
            try:
                os.environ["CLIPFLOW_FFMPEG_PATH"] = str(bin_path)
                self.tools._BINARIES.clear()
                self.tools._CAPABILITY.clear()
                self.tools._SUPPORTED_ENCODERS = None
                self.tools._SUPPORTED_FILTERS = None

                vols = {}
                for tag, clip_cfg in [
                    ("muted", {"start": 0.0, "end": 1.2, "captions": False, "layout": "landscape", "volume": 1.0, "muted": True}),
                    ("vol0", {"start": 0.0, "end": 1.2, "captions": False, "layout": "landscape", "volume": 0.0, "muted": False}),
                    ("vol20", {"start": 0.0, "end": 1.2, "captions": False, "layout": "landscape", "volume": 0.2, "muted": False}),
                    ("vol100", {"start": 0.0, "end": 1.2, "captions": False, "layout": "landscape", "volume": 1.0, "muted": False}),
                    ("vol180", {"start": 0.0, "end": 1.2, "captions": False, "layout": "landscape", "volume": 1.8, "muted": False}),
                ]:
                    res = self.render.render_clip(src, out_dir, tag, clip_cfg, [], 1280, 720)
                    mean_v, max_v, a_info = self._mean_max_db(bin_path, out_dir / res["file"])
                    self.assertIn("aac", a_info.lower())
                    self.assertIn("48000 hz", a_info.lower())
                    vols[tag] = (mean_v, max_v)

                self.assertLessEqual(vols["muted"][0], -85.0)
                self.assertLessEqual(vols["vol0"][0], -85.0)
                self.assertLess(vols["vol20"][0] + 6.0, vols["vol100"][0])
                self.assertLess(vols["vol100"][0] + 3.0, vols["vol180"][0])
            finally:
                if old_ffmpeg is not None:
                    os.environ["CLIPFLOW_FFMPEG_PATH"] = old_ffmpeg
                else:
                    os.environ.pop("CLIPFLOW_FFMPEG_PATH", None)
                self.tools._BINARIES.clear()
                self.tools._CAPABILITY.clear()
                self.tools._SUPPORTED_ENCODERS = None
                self.tools._SUPPORTED_FILTERS = None

    # --- Priority 2: Prevent Silent Overlay Loss ---
    def test_overlay_elements_never_silently_dropped_without_ass(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            out_dir = work / "out"
            out_dir.mkdir()
            src = work / "dummy.mp4"
            src.write_bytes(b"dummy")

            with patch.object(self.tools, "has_ass_filter", return_value=False):
                with self.assertRaises(RuntimeError) as ctx_hook:
                    self.render.render_clip(src, out_dir, "h1", {
                        "start": 0.0, "end": 1.0, "captions": False, "hook": "BIG HOOK", "layout": "blur"
                    }, [], 1920, 1080)
                self.assertIn("title hook", str(ctx_hook.exception))

                with self.assertRaises(RuntimeError) as ctx_txt:
                    self.render.render_clip(src, out_dir, "t1", {
                        "start": 0.0, "end": 1.0, "captions": False, "hook": "", "layout": "blur",
                        "text_overlays": [{"text": "Keep me", "x": 20, "y": 30}]
                    }, [], 1920, 1080)
                self.assertIn("text overlays", str(ctx_txt.exception))

                with self.assertRaises(RuntimeError) as ctx_stk:
                    self.render.render_clip(src, out_dir, "s1", {
                        "start": 0.0, "end": 1.0, "captions": False, "hook": "", "layout": "blur",
                        "stickers": [{"emoji": "🔥", "x": 50, "y": 50}]
                    }, [], 1920, 1080)
                self.assertIn("stickers", str(ctx_stk.exception))

            # Desktop behavior: with ass filter available, ASS burn-in filter is attached
            with patch.object(self.tools, "has_ass_filter", return_value=True), \
                 patch("subprocess.run", return_value=MagicMock(returncode=0, stderr="")) as mock_run, \
                 patch("studio.render.run"), \
                 patch("shutil.move"):
                self.render.render_clip(src, out_dir, "desk1", {
                    "start": 0.0, "end": 1.0, "captions": False, "hook": "Desktop Hook", "layout": "blur"
                }, [], 1920, 1080)
                cmd = mock_run.call_args[0][0]
                self.assertIn("ass=desk1.ass:fontsdir=f", cmd[cmd.index("-filter_complex") + 1])

    # --- Priority 3: Frame Layout Preservation & Non-GPL Filters ---
    def test_layouts_preserved_across_all_orientations_and_filters(self):
        for sw, sh in [(1080, 1920), (1080, 1080), (1920, 1080)]:
            for lay in ("blur", "fit", "split", "pip", "gameplay", "crop", "left", "right", "zoom", "landscape", "square"):
                got_lay, fg_h, _, _ = self.render.geometry(sw, sh, lay)
                self.assertEqual(got_lay, lay)
                self.assertGreater(fg_h, 0)

        fit_fc = self.render._filter("fit", 1080, out_w=1080, out_h=1920)
        self.assertIn("force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black", fit_fc)

        def fake_has_filter(name, path=None):
            return name in {"colorlevels", "huesaturation", "hue", "avgblur"}

        with patch.object(self.tools, "has_filter", side_effect=fake_has_filter):
            self.assertEqual(
                self.render._resolve_video_filter("dramatic"),
                "colorlevels=rimin=0.08:gimin=0.08:bimin=0.08:rimax=0.92:gimax=0.92:bimax=0.92,huesaturation=saturation=-0.12",
            )
            self.assertIn("hue=h=15", self.render._resolve_video_filter("cyber"))
            self.assertIn("huesaturation=saturation=0.35", self.render._resolve_video_filter("vivid"))

    def test_real_android_vertical_square_horizontal_layouts_and_filters(self):
        bin_path = Path(__file__).resolve().parent.parent / "android/app/src/main/jniLibs/arm64-v8a/libffmpeg.so"
        if not bin_path.is_file():
            self.skipTest("libffmpeg.so not found on disk")

        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            out_dir = work / "out"
            out_dir.mkdir()
            old_ffmpeg = os.environ.get("CLIPFLOW_FFMPEG_PATH")
            try:
                os.environ["CLIPFLOW_FFMPEG_PATH"] = str(bin_path)
                self.tools._BINARIES.clear()
                self.tools._CAPABILITY.clear()
                self.tools._SUPPORTED_ENCODERS = None
                self.tools._SUPPORTED_FILTERS = None

                sources = {}
                for tag, sw, sh in [("vert", 1080, 1920), ("sq", 1080, 1080), ("horiz", 1920, 1080)]:
                    p_src = work / f"{tag}.mp4"
                    gen = subprocess.run([
                        str(bin_path), "-y",
                        "-f", "lavfi", "-i", f"testsrc=size={sw}x{sh}:rate=25",
                        "-f", "lavfi", "-i", "sine=frequency=500:sample_rate=48000",
                        "-t", "0.8", "-c:v", "mpeg4", "-q:v", "4", "-c:a", "aac", str(p_src)
                    ], capture_output=True, text=True)
                    self.assertEqual(gen.returncode, 0, gen.stderr)
                    sources[tag] = (p_src, sw, sh)

                for tag, (p_src, sw, sh) in sources.items():
                    for lay, exp_w, exp_h in [
                        ("blur", 1080, 1920),
                        ("fit", 1080, 1920),
                        ("split", 1080, 1920),
                        ("pip", 1080, 1920),
                        ("landscape", 1920, 1080),
                        ("square", 1080, 1080),
                    ]:
                        res = self.render.render_clip(p_src, out_dir, f"{tag}_{lay}", {
                            "start": 0.0, "end": 0.8, "captions": False, "hook": "", "layout": lay,
                            "facecam": [10, 10, 35, 35, 0, 0],
                            "filter": "dramatic" if lay == "blur" else ("cyber" if lay == "fit" else "normal"),
                        }, [], sw, sh)
                        self.assertEqual(res["layout"], lay)
                        got_w, got_h = self._video_dims(bin_path, out_dir / res["file"])
                        self.assertEqual((got_w, got_h), (exp_w, exp_h))
            finally:
                if old_ffmpeg is not None:
                    os.environ["CLIPFLOW_FFMPEG_PATH"] = old_ffmpeg
                else:
                    os.environ.pop("CLIPFLOW_FFMPEG_PATH", None)
                self.tools._BINARIES.clear()
                self.tools._CAPABILITY.clear()
                self.tools._SUPPORTED_ENCODERS = None
                self.tools._SUPPORTED_FILTERS = None

    # --- Priority 6: Gemini Fallback List, Provider Label & Key Redaction ---
    def test_ai_fallback_models_and_redacted_provider_errors(self):
        self.assertEqual(self.ai.parse_models("gemini-2.5-flash, gemini-2.5-pro , "), ["gemini-2.5-flash", "gemini-2.5-pro"])
        self.assertEqual(self.ai.parse_models(["gemini-2.5-flash", "gemini-2.5-flash", "gemini-2.5-pro"]),
                         ["gemini-2.5-flash", "gemini-2.5-pro"])

        with tempfile.TemporaryDirectory() as tmp:
            cfg_file = Path(tmp) / "ai.json"
            with patch.object(self.ai, "_file", return_value=cfg_file):
                self.ai._saved["at"] = None
                self.ai.save("google", "gemini-2.5-pro", "", "AIzaSavedDummySecret1234")

                calls = []
                def fake_call(model, prompt, schema, provider_id=None, base=None, api_key=None):
                    calls.append((model, provider_id, api_key))
                    if model == "retired-flash-001":
                        err = self.ai.AIError("HTTP 404 model retired-flash-001 is no longer available")
                        err.model = model
                        raise err
                    return (["ok"], 5, 2)

                with patch.object(self.ai, "call", side_effect=fake_call):
                    res = self.ai.test(model="retired-flash-001, gemini-2.5-flash", provider_id="google", key_val="AIzaCustomKey999")
                    self.assertTrue(res["ok"])
                    self.assertEqual(res["model"], "gemini-2.5-flash")
                    self.assertEqual(res["models_tested"], ["retired-flash-001", "gemini-2.5-flash"])
                    self.assertEqual(calls[0], ("retired-flash-001", "gemini", "AIzaCustomKey999"))
                    self.assertEqual(calls[1], ("gemini-2.5-flash", "gemini", "AIzaCustomKey999"))

                # Auth failures abort immediately without hammering remaining fallbacks
                calls.clear()
                def fake_401(model, *args, **kwargs):
                    calls.append(model)
                    err = self.ai.AIError("HTTP 401 API key invalid")
                    err.model = model
                    raise err

                with patch.object(self.ai, "call", side_effect=fake_401):
                    with self.assertRaises(self.ai.AIError):
                        self.ai.test(model="m1, m2", provider_id="openai", key_val="sk-bad")
                    self.assertEqual(calls, ["m1"])

                # explain() uses the tested provider_id rather than saved Gemini provider
                msg = self.ai.explain(self.ai.AIError("HTTP 404 model foo is not found for api version"),
                                      model="foo", provider_id="openai")
                self.assertIn("OpenAI", msg)
                self.assertNotIn("Gemini", msg)

                # _fail redacts untyped/custom key passed in request
                fake_resp = MagicMock(status_code=400, headers={}, text="bad key AIzaCustomKey999 in request")
                with self.assertRaises(self.ai.AIError) as fail_ctx:
                    self.ai._fail(fake_resp, "gemini-2.5-flash", used_key="AIzaCustomKey999")
                self.assertNotIn("AIzaCustomKey999", str(fail_ctx.exception))
                self.assertIn("<key>", str(fail_ctx.exception))
                self.assertEqual(fail_ctx.exception.model, "gemini-2.5-flash")

                from studio import server
                clean_err = server._ai_error(RuntimeError("failed with AIzaCustomKey999"), extra_key="AIzaCustomKey999")
                self.assertNotIn("AIzaCustomKey999", clean_err)
                self.assertIn("<key>", clean_err)

                class _HTTPExc(Exception):
                    def __init__(self, status_code, detail):
                        self.status_code = status_code
                        self.detail = detail

                with patch.object(server, "_admin"), patch.object(server, "_throttle"), \
                     patch.object(server.ctx, "uid", return_value="test_admin"), \
                     patch.object(server, "HTTPException", _HTTPExc), \
                     patch.object(self.ai, "call", return_value=(["ok"], 3, 2)):
                    ok_srv = server.ai_test(MagicMock(), {"provider": "google", "model": ["m1", "gemini-2.5-flash"], "key": "AIzaSrv"})
                    self.assertTrue(ok_srv["ok"])
                    self.assertEqual(ok_srv["models_tested"], ["m1", "gemini-2.5-flash"])

                with patch.object(server, "_admin"), patch.object(server, "_throttle"), \
                     patch.object(server.ctx, "uid", return_value="test_admin"), \
                     patch.object(server, "HTTPException", _HTTPExc), \
                     patch.object(self.ai, "call", side_effect=self.ai.AIError("HTTP 404 model gpt-missing is not found for api version")):
                    with self.assertRaises(_HTTPExc) as srv_ctx:
                        server.ai_test(MagicMock(), {"provider": "openai", "model": "gpt-missing", "key": "sk-test"})
                    self.assertEqual(srv_ctx.exception.status_code, 502)
                    self.assertIn("OpenAI", srv_ctx.exception.detail)
                    self.assertNotIn("Gemini", srv_ctx.exception.detail)

    # --- Priority 5: Android WebView Subresource URI Policy Source + Parity Check ---
    def test_android_webview_subresource_uri_policy_parity(self):
        import re
        from urllib.parse import urlparse
        kt_path = Path(__file__).resolve().parent.parent / "android/app/src/main/java/com/mirza8975/clipflow/MainActivity.kt"
        kt_src = kt_path.read_text(encoding="utf-8")
        self.assertIn("SAFE_IMAGE_DATA_URI_REGEX", kt_src)
        self.assertIn("isAllowedSubresourceUri", kt_src)

        safe_img_re = re.compile(r"^data:image/(jpeg|jpg|png|webp|gif);base64,[A-Za-z0-9+/=\r\n]+$", re.I)

        def is_same_origin(a: str, b: str) -> bool:
            pa, pb = urlparse(a), urlparse(b)
            def eff_port(p):
                if p.port is not None:
                    return p.port
                return 443 if p.scheme == "https" else (80 if p.scheme == "http" else -1)
            return (pa.scheme.lower() == pb.scheme.lower()
                    and (pa.hostname or "").lower() == (pb.hostname or "").lower()
                    and eff_port(pa) == eff_port(pb))

        def py_is_allowed(raw_uri: str, trusted_origin: str, is_main: bool) -> bool:
            parsed = urlparse(raw_uri)
            scheme = (parsed.scheme or "").lower()
            if scheme in ("http", "https"):
                return True
            if is_main:
                return False
            if scheme == "blob":
                inner = raw_uri.split(":", 1)[1].strip()
                if not inner.lower().startswith(("http://", "https://")):
                    return False
                return is_same_origin(inner, trusted_origin)
            if scheme == "data":
                return bool(safe_img_re.match(raw_uri))
            return False

        origin = "http://127.0.0.1:8765"
        self.assertTrue(py_is_allowed("http://127.0.0.1:8765/api/jobs", origin, True))
        self.assertTrue(py_is_allowed("blob:http://127.0.0.1:8765/550e8400-e29b-41d4-a716-446655440000", origin, False))
        self.assertFalse(py_is_allowed("blob:http://127.0.0.1:8765/550e8400-e29b-41d4-a716-446655440000", origin, True))
        self.assertFalse(py_is_allowed("blob:http://evil.example:8765/uuid", origin, False))
        self.assertFalse(py_is_allowed("blob:null/uuid", origin, False))
        self.assertTrue(py_is_allowed("data:image/png;base64,iVBORw0KGgo=", origin, False))
        self.assertFalse(py_is_allowed("data:image/svg+xml;base64,PHN2Zz4=", origin, False))
        self.assertFalse(py_is_allowed("data:text/html;base64,PGgxPmg8L2gxPg==", origin, False))
        self.assertFalse(py_is_allowed("content://media/external/video/media/42", origin, False))
        self.assertFalse(py_is_allowed("file:///etc/passwd", origin, False))
        self.assertFalse(py_is_allowed("javascript:alert(1)", origin, False))

    def test_media_probe_ffmpeg_fallback_real_binary(self):
        from studio import media
        bin_path = Path(__file__).resolve().parent.parent / "android/app/src/main/jniLibs/arm64-v8a/libffmpeg.so"
        if not bin_path.is_file():
            self.skipTest("libffmpeg.so not found on disk")

        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            av_src = work / "av.mp4"
            no_audio = work / "no_audio.mp4"
            self.assertEqual(subprocess.run([
                str(bin_path), "-y",
                "-f", "lavfi", "-i", "testsrc=size=1080x1920:rate=25",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                "-t", "1.4", "-c:v", "mpeg4", "-q:v", "4", "-c:a", "aac", str(av_src)
            ], capture_output=True, text=True).returncode, 0)
            self.assertEqual(subprocess.run([
                str(bin_path), "-y",
                "-f", "lavfi", "-i", "testsrc=size=640x480:rate=25",
                "-t", "0.6", "-c:v", "mpeg4", "-q:v", "4", str(no_audio)
            ], capture_output=True, text=True).returncode, 0)

            old_ff = os.environ.get("CLIPFLOW_FFMPEG_PATH")
            try:
                os.environ["CLIPFLOW_FFMPEG_PATH"] = str(bin_path)
                self.tools._BINARIES.clear()
                self.tools._CAPABILITY.clear()
                orig_is_avail = self.tools.is_available
                with patch.object(self.tools, "is_available", side_effect=lambda name: False if name == "ffprobe" else orig_is_avail(name)):
                    meta = media.probe(av_src)
                    self.assertEqual(meta["width"], 1080)
                    self.assertEqual(meta["height"], 1920)
                    self.assertGreaterEqual(meta["duration"], 1.2)
                    self.assertLessEqual(meta["duration"], 1.8)

                    with self.assertRaises(RuntimeError) as no_aud_ctx:
                        media.probe(no_audio)
                    self.assertIn("no audio track", str(no_aud_ctx.exception))
            finally:
                if old_ff is not None:
                    os.environ["CLIPFLOW_FFMPEG_PATH"] = old_ff
                else:
                    os.environ.pop("CLIPFLOW_FFMPEG_PATH", None)
                self.tools._BINARIES.clear()
                self.tools._CAPABILITY.clear()

    def test_jobs_request_render_sanitizes_and_preserves_empty_custom_words(self):
        from studio import jobs
        mock_job = {
            "id": "j_san",
            "dir": "/tmp/j_san",
            "user": "u1",
            "clips": [{
                "id": "c1",
                "start": 1.0,
                "end": 10.0,
                "captions": True,
                "status": "done",
            }]
        }
        with patch.dict(jobs._jobs, {"j_san": mock_job}), \
             patch.object(jobs, "save"), \
             patch.object(jobs._q, "put"):
            jobs.request_render("j_san", "c1", {
                "facecam": ["10.4", 20.6, "40.2", 40.8],
                "words": [],
                "captions": True,
            })
            c = mock_job["clips"][0]
            self.assertEqual(c["facecam"], [10, 21, 40, 41])
            self.assertEqual(c["words"], [])
            self.assertTrue(c["custom_words"])
            with patch("studio.brain.english_captions") as mock_ai:
                out_words = jobs._caption_words(mock_job, c, words=[{"w": "Old", "s": 2.0, "e": 3.0}], off=0.0)
                mock_ai.assert_not_called()
                self.assertEqual(out_words, [])

            with self.assertRaises(ValueError):
                jobs.request_render("j_san", "c1", {"facecam": [float("nan"), 10, 40, 40]})

    def test_facecam_size_ffmpeg_fallback_real_binary(self):
        from studio import facecam
        bin_path = Path(__file__).resolve().parent.parent / "android/app/src/main/jniLibs/arm64-v8a/libffmpeg.so"
        if not bin_path.is_file():
            self.skipTest("libffmpeg.so not found on disk")

        with tempfile.TemporaryDirectory() as tmp:
            vid = Path(tmp) / "sq.mp4"
            self.assertEqual(subprocess.run([
                str(bin_path), "-y",
                "-f", "lavfi", "-i", "testsrc=size=720x720:rate=25",
                "-t", "0.5", "-c:v", "mpeg4", "-q:v", "4", str(vid)
            ], capture_output=True, text=True).returncode, 0)

            old_ff = os.environ.get("CLIPFLOW_FFMPEG_PATH")
            try:
                os.environ["CLIPFLOW_FFMPEG_PATH"] = str(bin_path)
                self.tools._BINARIES.clear()
                self.tools._CAPABILITY.clear()
                orig_is_avail = self.tools.is_available
                with patch.object(self.tools, "is_available", side_effect=lambda name: False if name == "ffprobe" else orig_is_avail(name)):
                    self.assertEqual(facecam._size(vid), (720, 720))
            finally:
                if old_ff is not None:
                    os.environ["CLIPFLOW_FFMPEG_PATH"] = old_ff
                else:
                    os.environ.pop("CLIPFLOW_FFMPEG_PATH", None)
                self.tools._BINARIES.clear()
                self.tools._CAPABILITY.clear()

    def test_android_kotlin_fixes_present(self):
        root = Path(__file__).resolve().parent.parent
        main_kt = (root / "android/app/src/main/java/com/mirza8975/clipflow/MainActivity.kt").read_text(encoding="utf-8")
        svc_kt = (root / "android/app/src/main/java/com/mirza8975/clipflow/ClipFlowBackendService.kt").read_text(encoding="utf-8")
        # fabSettings uses go('settings') and closes open <dialog>, not dead window.S/draw()
        self.assertIn("typeof go === 'function'", main_kt)
        self.assertNotIn("typeof draw === 'function'", main_kt)
        # Cold-start deep link starts embedded backend when local server isn't up yet
        self.assertIn("loadUrlOverride: String? = null", main_kt)
        # File chooser includes extended video MIME types & multiple selection
        self.assertIn("video/x-m4v", main_kt)
        self.assertIn("EXTRA_ALLOW_MULTIPLE", main_kt)
        # onDestroy clears lingering fileUploadCallback and detaches/destroys WebView safely
        self.assertIn("override fun onDestroy()", main_kt)
        self.assertIn("removeView(webView)", main_kt)
        # Web assets cache invalidates on package lastUpdateTime even when versionCode stays unchanged
        self.assertIn("lastUpdateTime", svc_kt)

    def test_web_editor_js_helpers_execution_in_node(self):
        import re
        import shutil as _sh
        node_bin = _sh.which("node")
        if not node_bin:
            self.skipTest("node not installed")
        root = Path(__file__).resolve().parent.parent
        html = (root / "web/index.html").read_text(encoding="utf-8")
        # Structural checks for live UI bindings & offline-LAN clipboard fallback
        for marker in ("edVolVal", "edTextSz", "edTextRot", "edStkSz", "updateFileProgress", "copyTextSafe", "aipick", "#t=0.1"):
            self.assertIn(marker, html)

        # Extract editor helper functions and execute them under Node vm
        start_idx = html.index("const ED = {")
        end_idx = html.index("function updateStagePreview()", start_idx)
        helpers_src = html[start_idx:end_idx]
        driver = f"""
const assert = require("assert");
const els = {{
  "#edVideo": {{ currentTime: 5.0, duration: 15.0 }},
  "#edTextSz0": {{ textContent: "" }},
  "#edTextRot0": {{ textContent: "" }},
  "#edStkSz0": {{ textContent: "" }},
  "#edLiveWords": {{ innerHTML: "" }},
  "#edLiveCaptions": {{ style: {{ display: "none" }} }},
  "#edUndoBtn": {{ disabled: true }},
  "#edRedoBtn": {{ disabled: true }},
}};
const $ = s => els[s] || null;
const esc = s => String(s ?? "");
const updateStagePreview = () => {{ els._prev = (els._prev || 0) + 1; }};
const drawTabContent = () => {{ els._tab = (els._tab || 0) + 1; }};
{helpers_src}

ED.srcW = 1920; ED.srcH = 1080; ED.origStart = 10.0; ED.origEnd = 25.0; ED.start = 10.0; ED.end = 25.0; ED.srcDuration = 60.0;
// Pixel -> Pct -> Pixel roundtrip
const pct = facecamToPct([192, 108, 768, 432, 0, 0], 1920, 1080);
assert.deepStrictEqual(pct, [10, 10, 40, 40, 0, 0]);
assert.deepStrictEqual(pctToSourceFacecam(pct, 1920, 1080), [192, 108, 768, 432, 0, 0]);

// Unknown source dimensions keep % coordinates instead of falsely scaling to 1920x1080
ED.srcW = 0; ED.srcH = 0;
assert.deepStrictEqual(pctToSourceFacecam([10, 20, 35, 45, 0, 0], 0, 0), [10, 20, 35, 45, 0, 0]);

// Trim helpers clamp playhead and use ED.origStart offset
els["#edVideo"].currentTime = 4.0;
setTrimStartToCurrent();
assert.strictEqual(ED.start, 14.0);
els["#edVideo"].currentTime = 14.0;
setTrimEnd(20.0);
assert.strictEqual(ED.end, 20.0);
assert.strictEqual(els["#edVideo"].currentTime, 10.0);

// Text overlay and sticker live updates update labels
addTextOverlay();
updateTextOverlay(0, "font_size", 42);
updateTextOverlay(0, "rotation", -30);
assert.strictEqual(els["#edTextSz0"].textContent, "42px");
assert.strictEqual(els["#edTextRot0"].textContent, "-30°");
ED.stickers = [{{ emoji: "🔥", x: 50, y: 30, scale: 1.0 }}];
updateSticker(0, "scale", 1.8);
assert.strictEqual(els["#edStkSz0"].textContent, "180%");

// Word editing live-updates caption preview using ED.origStart + relT
ED.captions = true;
ED.words = [{{ w: "Hello", s: 12.0, e: 13.0 }}, {{ w: "World", s: 13.1, e: 14.0 }}];
els["#edVideo"].currentTime = 2.5; // abs time 12.5s
updateWordText(0, "Edited", false);
assert.ok(els["#edLiveWords"].innerHTML.includes("Edited"));
assert.strictEqual(els["#edLiveCaptions"].style.display, "flex");
console.log("NODE_EDITOR_HELPERS_OK");
"""
        r = subprocess.run([node_bin, "-e", driver], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, f"Node editor test failed:\n{r.stdout}\n{r.stderr}")
        self.assertIn("NODE_EDITOR_HELPERS_OK", r.stdout)


if __name__ == "__main__":
    unittest.main()
