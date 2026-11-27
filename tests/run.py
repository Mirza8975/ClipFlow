"""Run the test suite against a throwaway copy of the app.

    python tests/run.py

Starts PeakCut on a spare port with an empty, temporary data folder, runs the tests against it, then stops it and
deletes the folder. Your real data and your running server are never touched. No AI key is needed: the tests cover accounts, permissions, limits and the queue, not clip quality."""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("CLIPFLOW_TEST_PORT") or os.environ.get("PEAKCUT_TEST_PORT", "8799"))
BASE = f"http://127.0.0.1:{PORT}"


def _normalize_path_entry(entry: str, base_dir: Path) -> str:
    """Normalize a PYTHONPATH entry relative to base_dir for duplicate detection."""
    p = Path(entry)
    if not p.is_absolute():
        p = base_dir / p
    try:
        resolved = str(p.resolve())
    except OSError:
        resolved = os.path.abspath(str(p))
    return os.path.normcase(resolved)


def _build_pythonpath(existing: str | None, repo_root: Path = ROOT) -> str:
    """Prepend repo_root to PYTHONPATH while preserving existing entries without duplicates."""
    root_resolved = str(repo_root.resolve())
    seen: set[str] = set()
    entries: list[str] = []

    raw_entries = [root_resolved]
    if existing:
        raw_entries.extend(existing.split(os.pathsep))

    for raw in raw_entries:
        if not raw or not raw.strip():
            continue
        key = _normalize_path_entry(raw, repo_root)
        if key in seen:
            continue
        seen.add(key)
        entries.append(raw)

    return os.pathsep.join(entries)


def _build_test_env(
    data: Path,
    base_env: Mapping[str, str] | None = None,
    repo_root: Path = ROOT,
) -> dict[str, str]:
    """Build subprocess environment pointing at repo_root and an isolated data folder."""
    source = os.environ if base_env is None else base_env
    env = {
        **source,
        "CLIPFLOW_DATA": str(data),
        "PEAKCUT_DATA": str(data),
        "CLIPFLOW_PORT": str(PORT),
        "PEAKCUT_PORT": str(PORT),
        "CLIPFLOW_PUBLIC_URL": BASE,
        "PEAKCUT_PUBLIC_URL": BASE,
        "CLIPFLOW_HOST": "127.0.0.1",
        "PEAKCUT_HOST": "127.0.0.1",
        "PEAKCUT_AUTO_RELOAD": "0",
        "DATABASE_URL": "",
        "CLIPFLOW_ACCOUNTS": "1",
        "PEAKCUT_ACCOUNTS": "1",
        "PEAKCUT_FETCH_MODEL": "0",
        "PEAKCUT_UPDATE_CHECK": "0",                   # these tests are about people signing in; tests/test_local.py covers the default
        # nothing outside this machine is contacted: no AI, no email, no Google sign-in
        "PEAKCUT_SMTP_HOST": "",
        "PEAKCUT_VERIFY_EMAIL": "1",   # the tests cover the confirm-your-email rule
        "GOOGLE_CLIENT_ID": "",
        "GOOGLE_CLIENT_SECRET": "",
        "GOOGLE_CLIENT_FILE": "",
        "PYTHONUNBUFFERED": "1",
        "PEAKCUT_AI_KEY": "",
        "PEAKCUT_AI_KEY_FILE": "",
        "PEAKCUT_AI_MODELS": "",
        "PEAKCUT_UPLOAD_PART_MB": "5",
        "PEAKCUT_MAX_UPLOAD_MB": "64",
    }
    env["PYTHONPATH"] = _build_pythonpath(source.get("PYTHONPATH"), repo_root=repo_root)
    return env


def _parse_suite_counts(lines: list[str], returncode: int) -> tuple[int, int, int, int]:
    """Return (executed, passed, failed, skipped) from script or unittest output."""
    text = "\n".join(lines)
    m_ut = re.search(r"^Ran (\d+) tests? in ", text, re.M)
    if m_ut:
        executed = int(m_ut.group(1))
        skipped = 0
        m_skip = re.search(r"skipped=(\d+)", text)
        if m_skip:
            skipped = int(m_skip.group(1))
        failures = 0
        errors = 0
        m_fail = re.search(r"failures=(\d+)", text)
        if m_fail:
            failures = int(m_fail.group(1))
        m_err = re.search(r"errors=(\d+)", text)
        if m_err:
            errors = int(m_err.group(1))
        failed = failures + errors
        if returncode != 0 and failed == 0:
            failed = 1
        passed = max(0, executed - skipped - failed)
        return executed, passed, failed, skipped

    m_summary = re.search(r"---\s*Test Results:\s*(\d+)\s+passed,\s*(\d+)\s+failed,\s*(\d+)\s+skipped\s*---", text)
    if m_summary:
        p, f, s = int(m_summary.group(1)), int(m_summary.group(2)), int(m_summary.group(3))
        if returncode != 0 and f == 0:
            f = 1
        return p + f + s, p, f, s

    passed = sum(1 for x in lines if x.startswith("PASS"))
    failed = sum(1 for x in lines if x.startswith("FAIL"))
    skipped = sum(1 for x in lines if x.startswith("SKIP"))
    m_checks = re.search(r"Result:\s*(\d+)\s+failures\s+out\s+of\s+(\d+)\s+checks", text)
    if m_checks:
        f_checks = int(m_checks.group(1))
        total_checks = int(m_checks.group(2))
        failed = max(failed, f_checks)
        passed = max(passed, total_checks - f_checks)
    if returncode != 0 and failed == 0:
        failed = 1
    return passed + failed + skipped, passed, failed, skipped


def main() -> int:
    data = Path(tempfile.mkdtemp(prefix="clipflow-test-"))
    env = _build_test_env(data)
    log = data / "server.log"
    failed = []
    with log.open("w", encoding="utf-8") as out:
        server = subprocess.Popen([sys.executable, "-m", "studio.server"], cwd=ROOT, env=env, stdout=out, stderr=subprocess.STDOUT)
    try:
        for _ in range(90):
            try:
                urllib.request.urlopen(BASE + "/healthz", timeout=2)
                break
            except Exception:  # noqa: BLE001
                if server.poll() is not None:
                    print(log.read_text(encoding="utf-8", errors="replace")[-2000:])
                    return 1
                time.sleep(1)
        else:
            print("The test server did not start.")
            return 1
        runs = [("permissions", ["tests/test_permissions.py", BASE, str(ROOT), sys.executable]),
                ("abuse and limits", ["tests/test_abuse.py", BASE, str(ROOT)]),
                ("queue under load", ["tests/test_load_queue.py", str(ROOT)]),
                ("a day-long video in little memory", ["tests/test_longvideo.py"]),
                ("deleting and bulk actions", ["tests/test_delete.py"]),
                ("saving a video when Windows says busy", ["tests/test_jobsave.py"]),
                ("your own big text on the clips", ["tests/test_bigtext.py"]),
                ("hashtags made without an AI", ["tests/test_hashtags.py"]),
                ("big uploads in pieces", ["tests/test_upload_parts.py", BASE]),
                ("AI services", ["tests/test_ai.py"]),
                ("no sign-in, and the AI key from the dashboard", ["tests/test_local.py"]),
                ("Hindi and Urdu captions in Roman letters", ["tests/test_roman.py"]),
                ("turning captions off and back on", ["tests/test_captions.py"]),
                ("connecting YouTube, Instagram and TikTok", ["tests/test_connect.py"]),
                ("the new-version notice", ["tests/test_updates.py"]),
                ("no limits by default", ["tests/test_limits.py"]),
                ("captions never overlap, stuck clips, sentence endings", ["tests/test_stuck.py"]),
                ("ad breaks and frozen screens", ["tests/test_static.py"]),
                ("answers that are not quite right", ["tests/test_answer.py"]),
                ("waiting out AI rate limits", ["tests/test_retry.py"]),
                ("knowing which game a stream is", ["tests/test_games.py"]),
                ("finding the facecam in a gaming stream", ["tests/test_facecam.py"]),
                ("recording time: the buffer knows where each piece sits", ["tests/test_live_times.py"]),
                ("fresh installs: ffmpeg, the speech model, AI errors", ["tests/test_tools.py"]),
                ("the storage folder: full drive, open, move", ["tests/test_storage.py"]),
                ("security: visitor address, email addresses, Google sign-in, prompts", ["tests/test_hardening.py"]),
                ("long logs, the machine monitor, failed recordings, admin day totals", ["tests/test_robustness.py"]),
                ("speech models for other languages: Hindi, downloads, the second machine", ["tests/test_speech.py"]),
                ("post text: write again with AI, edit, post without re-making", ["tests/test_post_text.py"]),
                ("live recording stops and empty stream URLs", ["tests/test_live.py"]),
                ("environment precedence and FFmpeg tools", ["tests/test_config_precedence.py"]),
                ("audio adapter fallback and wave/ffmpeg reading", ["tests/test_audio_adapter.py"]),
                ("speech models and settings validation", ["tests/test_speech_and_settings.py"]),
                ("audio bridge and transcription serialization", ["tests/test_audio_bridge.py"]),
                ("editor rendering, media endpoints, and web JS parity", ["tests/test_editor_and_media.py"])]
        discovered = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "tests").glob("test_*.py"))
        configured = sorted(args[0] for _, args in runs)
        missing_from_runs = sorted(set(discovered) - set(configured))
        if missing_from_runs:
            print(f"ERROR: Unregistered test modules missing from tests/run.py: {missing_from_runs}")
            return 1

        total_executed = total_passed = total_failed = total_skipped = 0
        for name, args in runs:
            print(f"\n=== {name} ===", flush=True)
            r = subprocess.run([sys.executable, *args], cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
            lines = (r.stdout + "\n" + r.stderr).splitlines()
            executed, passed, failed_cnt, skipped = _parse_suite_counts(lines, r.returncode)
            total_executed += executed
            total_passed += passed
            total_failed += failed_cnt
            total_skipped += skipped
            for x in lines:
                if not x.startswith("PASS") and x.strip():
                    print("  " + x)
            print(f"  {executed} executed: {passed} passed, {failed_cnt} failed, {skipped} skipped")
            if r.returncode != 0 or failed_cnt > 0:
                failed.append(name)
        print(
            f"\n=== Suite Totals ===\n"
            f"Discovered modules: {len(discovered)} | Executed modules: {len(runs)} | "
            f"Executed checks/tests: {total_executed} (passed={total_passed}, failed={total_failed}, skipped={total_skipped})",
            flush=True,
        )
    finally:
        server.terminate()
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server.kill()
        shutil.rmtree(data, ignore_errors=True)
    print("\nFAILED: " + ", ".join(failed) if failed else "\nAll tests passed.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
