"""Regression tests for environment variable precedence (CLIPFLOW_* vs PEAKCUT_*)
and tools / FFmpeg system detection.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, ".")
from studio import config, tools

fails = []


def check(name, got, want=True):
    ok = want(got) if callable(want) else got == want
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   got {got!r}"))
    ok or fails.append(name)


# 1. Exact-match precedence over alternate prefix
os.environ["PEAKCUT_PRECEDENCE_TEST"] = "peakcut_value"
os.environ["CLIPFLOW_PRECEDENCE_TEST"] = "clipflow_value"

check("exact requested PEAKCUT key is prioritized when both exist",
      config.env("PEAKCUT_PRECEDENCE_TEST"), "peakcut_value")
check("exact requested CLIPFLOW key is prioritized when both exist",
      config.env("CLIPFLOW_PRECEDENCE_TEST"), "clipflow_value")

# 2. Transparent fallback when requested key is missing
os.environ.pop("PEAKCUT_PRECEDENCE_TEST", None)
check("PEAKCUT falls back to CLIPFLOW when PEAKCUT is absent",
      config.env("PEAKCUT_PRECEDENCE_TEST"), "clipflow_value")

os.environ["PEAKCUT_FALLBACK_TEST"] = "fallback_from_peakcut"
os.environ.pop("CLIPFLOW_FALLBACK_TEST", None)
check("CLIPFLOW falls back to PEAKCUT when CLIPFLOW is absent",
      config.env("CLIPFLOW_FALLBACK_TEST"), "fallback_from_peakcut")

# 3. Default value when neither is present
os.environ.pop("PEAKCUT_FALLBACK_TEST", None)
os.environ.pop("CLIPFLOW_PRECEDENCE_TEST", None)
check("default value returned when neither variant exists",
      config.env("CLIPFLOW_NONEXISTENT_XYZ", "default_val"), "default_val")
check("None returned when default omitted and neither exists",
      config.env("CLIPFLOW_NONEXISTENT_XYZ") is None, True)

# 4. Standard unprefixed environment variable
os.environ["DATABASE_URL_TEST"] = "sqlite:///test.db"
check("standard key returned directly",
      config.env("DATABASE_URL_TEST"), "sqlite:///test.db")
os.environ.pop("DATABASE_URL_TEST", None)

# 5. Tools detection regression
missing = tools.missing()
check("tools.missing() returns list", isinstance(missing, list), True)
check("tools.get_ffmpeg() returns non-empty string", bool(tools.get_ffmpeg()), True)
check("tools.get_ffprobe() returns non-empty string", bool(tools.get_ffprobe()), True)
check("tools.cmd(['ffmpeg', '-y']) replaces command name", tools.cmd(["ffmpeg", "-y"])[0] == tools.get_ffmpeg(), True)
check("tools.cmd(['ffprobe', '-i']) replaces command name", tools.cmd(["ffprobe", "-i"])[0] == tools.get_ffprobe(), True)
check("tools.cmd(['other', 'arg']) preserves other commands", tools.cmd(["other", "arg"]) == ["other", "arg"], True)

if fails:
    print(f"\n{len(fails)} checks failed.")
    sys.exit(1)
else:
    print("\nAll precedence and tools regression checks passed.")
    sys.exit(0)
