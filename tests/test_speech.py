"""Speech models for other spoken languages (Hindi first): downloading one only when asked, removing it, choosing
the language for a video or a creator, and the second machine never using the wrong language's model.
Runs against the code directly, with its own empty data folder; nothing is downloaded."""
import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-speech-")
os.environ.pop("PEAKCUT_MODELS", None)          # models are made and removed: only ever inside this test's folder
os.environ.update(DATABASE_URL="", PEAKCUT_ACCOUNTS="0", PEAKCUT_FETCH_MODEL="0", PEAKCUT_AI_KEY="", PEAKCUT_AI_KEY_FILE="",
                  PEAKCUT_PUBLIC_URL="http://127.0.0.1:8765")
sys.path.insert(0, ".")
from fastapi.testclient import TestClient  # noqa: E402

from studio import brain, config, creators, db, fetch_model, jobs, live, server, transcribe  # noqa: E402

db.init()
fails = []
BASE = "http://127.0.0.1:8765"
ORIGIN = {"Origin": BASE}
c = TestClient(server.app, base_url=BASE, follow_redirects=False)


def check(name, got, want=True):
    ok = want(got) if callable(want) else got == want
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   got {ascii(got)}"))
    ok or fails.append(name)


assert str(config.MODELS_DIR).startswith(os.environ["PEAKCUT_DATA"]), "the test must only touch its own folder"
downloads = []


def archive(name):
    """A tiny stand-in for the real archive: the right folder name, the model files."""
    src = Path(tempfile.mkdtemp(prefix="pc-tar-")) / name
    src.mkdir()
    for n in ("tokens.txt", "encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx", "tiny-encoder.onnx", "tiny-decoder.onnx"):
        (src / n).write_bytes(b"model")
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:bz2") as t:
        t.add(src, arcname=name)
    return out.getvalue()


ARCHIVES = {k: archive(m[1]) for k, m in fetch_model.MODELS.items()}
tampered = set()      # languages whose download is swapped for a different file


class Answer(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}


def fake_open(url, timeout=None):
    downloads.append(url)
    lang = next(k for k, m in fetch_model.MODELS.items() if (url in m[2] if isinstance(m[2], list) else m[2] == url))
    return Answer(ARCHIVES[lang] + (b"changed" if lang in tampered else b""))


# ---- the list of models, and where Hindi comes from
check("English and Hindi are known", sorted(fetch_model.MODELS), ["en", "hi", "hi-IN"])
check("Hindi is downloaded from its own address, not English's", fetch_model.MODELS["hi"][2] != fetch_model.MODELS["en"][2], True)
check("Hindi's address points to whisper-tiny",
      fetch_model.MODELS["hi"][2], lambda u: any("whisper-tiny" in str(x) for x in (u if isinstance(u, list) else [u])))
check("Hindi's fingerprint is defined as string", isinstance(fetch_model.MODELS["hi"][5], str), True)
mirror = subprocess.run([sys.executable, "-c", "from studio import fetch_model as f; u = f.MODELS['hi'][2]; print(u[0] if isinstance(u, list) else u); print(f.MODELS['hi'][5])"],
                        env={**os.environ, "PEAKCUT_ASR_MODEL_HI_URL": "https://mirror.example/hi.tar.bz2",
                             "PEAKCUT_ASR_MODEL_HI_SHA256": "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"},
                        capture_output=True, text=True).stdout.split()
check("a mirror set with PEAKCUT_ASR_MODEL_HI_URL is used, and checked against the fingerprint",
      mirror, ["https://mirror.example/hi.tar.bz2", "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"])
real_hi = fetch_model.MODELS["hi"]
# From here on the stand-in archive is "the real file": its fingerprint is the one expected.
fetch_model.MODELS["hi"] = real_hi[:5] + (hashlib.sha256(ARCHIVES["hi"]).hexdigest(),)
fetch_model.urllib.request.urlopen = fake_open
check("each language has its own folder", fetch_model.folder("hi") != fetch_model.folder("en"), True)
check("nothing is here to begin with", (fetch_model.status("en"), fetch_model.status("hi")), ("missing", "missing"))

# ---- Hindi is never downloaded unless someone asks
try:
    fetch_model.ensure("hi", start=False)
    said = ""
except RuntimeError as e:
    said = str(e)
check("a video needing Hindi says what to do instead of downloading", ("Speech models" in said, downloads), (True, []))
try:
    transcribe._new("hi")
    said = ""
except RuntimeError as e:
    said = str(e)
check("...and so does loading the model for transcribing", ("Speech models" in said, downloads), (True, []))

# ---- the routes: list, download, remove
r = c.get("/api/speech").json()["models"]
check("the page lists all languages with their size", [(m["lang"], m["state"], m["size_mb"] > 0) for m in r],
      [("en", "missing", True), ("hi", "missing", True), ("hi-IN", "missing", True)])
check("an unknown language is refused", c.post("/api/speech/xx", headers=ORIGIN).status_code, 400)
check("another website cannot start a download", c.post("/api/speech/hi", headers={"Origin": "https://evil.example"}).status_code, 403)
check("...and nothing was downloaded", downloads, [])
tampered.add("hi")         # the file at the address was changed: it must not be unpacked
try:
    fetch_model.ensure("hi")
    said = ""
except RuntimeError as e:
    said = str(e)
check("a downloaded file with the wrong fingerprint is refused, in plain words",
      ("not the one it expects" in said and "Nothing was unpacked" in said, fetch_model.ready("hi")), (True, False))
check("...it says which fingerprint it got and which it expected", fetch_model.MODELS["hi"][5] in said and "SHA-256" in said, True)
check("...the reason is shown on the Speech models page", "not the expected one" in fetch_model.state["hi"]["error"], True)
check("...and nothing is left behind", sorted(p.name for p in config.MODELS_DIR.iterdir()) if config.MODELS_DIR.exists() else [], [])
tampered.clear()
downloads.clear()
check("the owner can start the Hindi download", c.post("/api/speech/hi", headers=ORIGIN).status_code, 200)
for _ in range(100):
    if fetch_model.status("hi") == "ready":
        break
    time.sleep(0.05)
check("Hindi arrives whole, in its own folder", fetch_model.ready("hi"), True)
check("English was not touched", fetch_model.ready("en"), False)
check("nothing is left over from the download", sorted(p.name for p in config.MODELS_DIR.iterdir()), [fetch_model.MODELS["hi"][1]])
check("the status tells every page Hindi is ready", c.get("/api/status").json()["speech"]["hi"]["state"], "ready")
check("asking again downloads nothing more", (c.post("/api/speech/hi", headers=ORIGIN).status_code, len(downloads)), (200, 1))
check("English can't be removed", c.delete("/api/speech/en", headers=ORIGIN).status_code, 400)

transcribe._pool("hi").made += 1                  # pretend a video is transcribing in Hindi right now
check("a model in use is not removed", (c.delete("/api/speech/hi", headers=ORIGIN).status_code, fetch_model.ready("hi")), (409, True))
transcribe._pool("hi").made -= 1
check("Hindi can be removed when nothing uses it", (c.delete("/api/speech/hi", headers=ORIGIN).status_code, fetch_model.folder("hi").exists()), (200, False))

# ---- choosing the language
check("a video can be marked as Hindi", jobs.clean_options({"lang": "hi"}), {"lang": "hi"})
check("an unknown language becomes English", jobs.clean_options({"lang": "xx"}), {"lang": "en"})
check("videos are English unless chosen", jobs.DEFAULTS["lang"], "en")
check("a creator can speak Hindi", creators._clean({"lang": "hi"}), {"lang": "hi"})
check("...but not a made-up language", creators._clean({"lang": "<b>"}), {"lang": "en"})
r = c.post("/api/creators", json={"name": "Hindi speaker"}, headers=ORIGIN)
cid = r.json()["id"]
c.put(f"/api/creators/{cid}", json={"lang": "hi"}, headers=ORIGIN)
from studio import auth, ctx  # noqa: E402
with ctx.as_user(auth.local_user().id):
    cr = creators.get(cid)
    check("the creator's language is kept", cr.get("lang"), "hi")
    try:
        live.start_session(cr, "https://www.twitch.tv/someone")
        said = ""
    except ValueError as e:
        said = str(e)
    check("recording a Hindi creator without the model says what to do", "Speech models" in said, True)

# ---- the second machine: never the wrong language's model
class Reply:
    def __init__(self, code, body):
        self.status_code, self.body = code, body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)

    def json(self):
        return self.body


import httpx  # noqa: E402
real_post = httpx.post
piece = [transcribe.np.zeros(16000, dtype="float32")]
old = {"pieces": [{"tokens": ["▁hi"], "timestamps": [0.0]}]}
httpx.post = lambda *a, **k: Reply(200, old)               # an older machine: answers in English, says no language
try:
    transcribe._decode_remote(piece, "hi")
    got = "used"
except transcribe.RemoteLacks:
    got = "refused"
check("an older machine's English answer is not used for Hindi", got, "refused")
check("...but still used for English", transcribe._decode_remote(piece, "en"), [(["▁hi"], [0.0])])
httpx.post = lambda *a, **k: Reply(409, {})
try:
    transcribe._decode_remote(piece, "hi")
    got = "used"
except transcribe.RemoteLacks:
    got = "refused"
check("a machine without the Hindi model says so", got, "refused")
httpx.post = lambda *a, **k: Reply(200, dict(old, lang="hi"))
check("a machine with the Hindi model is used", transcribe._decode_remote(piece, "hi"), [(["▁hi"], [0.0])])
httpx.post = real_post

print("ALL PASSED" if not fails else f"{len(fails)} failed")
shutil.rmtree(os.environ["PEAKCUT_DATA"], ignore_errors=True)
sys.exit(1 if fails else 0)
