"""Turns a time range into a vertical clip: 9:16 layout, bouncy word captions, hook title, cover."""
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import config, roman, tools as _tools
from .media import NO_WINDOW, run

W, H = config.OUT_W, config.OUT_H

# ASS colours are &HBBGGRR. `size` is the ASS font size (line height); _Font turns it into real pixels.
STYLES = {
    "bounce": {"label": "Bounce", "font": "Poppins ExtraBold", "size": 132, "base": "FFFFFF", "active": "00F0FF",
               "strong": "FFFFFF", "outline": 7, "pop": 112, "upper": True, "per": 3},
    "hype": {"label": "Hype", "font": "Anton", "size": 152, "base": "FFFFFF", "active": "3C3CFF",
             "strong": "00D7FF", "outline": 7, "pop": 114, "upper": True, "per": 2},
    "clean": {"label": "Clean", "font": "Poppins ExtraBold", "size": 100, "base": "FFFFFF", "active": "6BD7FF",
              "strong": "FFFFFF", "outline": 5, "pop": 106, "upper": False, "per": 4},
    "neon": {"label": "Neon", "font": "Archivo Black", "size": 128, "base": "FFFFFF", "active": "00FF66",
             "strong": "00E5FF", "outline": 8, "pop": 115, "upper": True, "per": 2},
    "minimal": {"label": "Minimal", "font": "Poppins ExtraBold", "size": 92, "base": "F5F5F5", "active": "FFD700",
                "strong": "FFFFFF", "outline": 4, "pop": 105, "upper": False, "per": 4},
}
HOOK = {"font": "Poppins ExtraBold", "size": 96, "ink": "14100E", "box": "FFFFFF", "pad_x": 36, "pad_y": 22, "radius": 26,
        "max_w": 900}
FONT_FILES = {"Poppins ExtraBold": "Poppins-ExtraBold.ttf", "Anton": "Anton-Regular.ttf", "Archivo Black": "ArchivoBlack-Regular.ttf"}
CAP_MAX_W = int(getattr(config, "OUT_W", 1080) * 0.82)   # 885px on 1080w: stays within Instagram Reels/TikTok right-side action icons


class _Font:
    """Measures text exactly as the renderer will draw it, so every word can be placed by hand: the highlighted
    word can grow without pushing its neighbours, and the heading box fits its text."""
    _cache: dict = {}

    def __init__(self, name):
        import struct
        path = Path(__file__).resolve().parent / "fonts" / FONT_FILES.get(name, FONT_FILES["Poppins ExtraBold"])
        d = path.read_bytes()
        tables = {}
        for i in range(struct.unpack(">H", d[4:6])[0]):
            tag, _, off, _ = struct.unpack(">4sIII", d[12 + 16 * i: 28 + 16 * i])
            tables[tag] = off
        self.upm = struct.unpack(">H", d[tables[b"head"] + 18: tables[b"head"] + 20])[0]
        o = tables[b"OS/2"]
        self.asc, self.desc = struct.unpack(">HH", d[o + 74: o + 78])      # what the renderer sizes the font by
        self.cap = struct.unpack(">h", d[o + 88: o + 90])[0] or int(self.upm * 0.7)
        self.path = str(path)
        self._pil = {}

    @classmethod
    def get(cls, name):
        if name not in cls._cache:
            cls._cache[name] = cls(name)
        return cls._cache[name]

    def em(self, size):
        return size * self.upm / (self.asc + self.desc)

    def width(self, text, size):
        """Pixels wide at this ASS size."""
        try:
            from PIL import ImageFont
            if "f" not in self._pil:
                self._pil["f"] = ImageFont.truetype(self.path, 200)
            return self._pil["f"].getlength(text) * self.em(size) / 200
        except Exception:  # noqa: BLE001 - no measuring library: a fair estimate keeps clips rendering
            return len(text) * self.em(size) * 0.62

    def cap_shift(self, size):
        """How far below the anchor the middle of a capital letter sits when text is centred on a point."""
        return ((self.asc - self.desc) / 2 - self.cap / 2) * size / (self.asc + self.desc)


CAPTION_LEAD = 0.0  # measured: word stamps are within 0.15 s of the voice, so no shift is needed
def install_fonts():
    """Copy the bundled caption fonts next to the data, so captions look the same on every machine and server."""
    try:
        src = Path(__file__).resolve().parent / "fonts"
        if src.is_dir():
            for f in src.glob("*.ttf"):
                target = config.FONTS_DIR / f.name
                if not target.exists():
                    try:
                        shutil.copyfile(f, target)
                    except OSError:
                        pass
    except Exception as e:
        print(f"[ClipFlow] Font installation notice: {e}", flush=True)


LAYOUTS = {"blur": "Full frame + blurred fill", "zoom": "Zoomed in + blurred fill", "crop": "Fill the screen (centre crop)",
           "left": "Fill the screen, keep the left side", "right": "Fill the screen, keep the right side",
           "fit": "Full frame on black", "split": "Split screen: left half on top, right half below (two people)",
           "gameplay": "Gameplay fills the screen, facecam in the corner",
           "pip": "Picture-in-picture with facecam"}
SAFE_MARGINS = {"reels": "Instagram Reels (9:16 safe)", "tiktok": "TikTok (9:16 safe)", "shorts": "YouTube Shorts", "none": "Full Frame (No Margin)"}
FILTERS = {
    "normal": "",
    "vivid": "eq=contrast=1.18:saturation=1.35",
    "dramatic": "eq=contrast=1.35:saturation=0.88",
    "vintage": "colorchannelmixer=.393:.769:.189:0:.349:.686:.168:0:.272:.534:.131",
    "bw": "hue=s=0",
    "cyber": "eq=contrast=1.25:saturation=1.45,hue=h=15",
    "warm": "colorbalance=rs=.12:gs=.06:bs=-.1",
    "cool": "colorbalance=rs=-.1:gs=.0:bs=.15",
}
_FILLS = ("crop", "left", "right", "gameplay", "pip")      # layouts where the picture covers the whole frame


def _ts(t):
    t = max(0.0, t)
    h, m = int(t // 3600), int(t % 3600 // 60)
    return f"{h}:{m:02d}:{t % 60:05.2f}"


def _clean(text):
    return re.sub(r"[\x00-\x1f\x7f]+", " ", re.sub(r"[{}\\]", "", str(text))).strip()


def _key(word):
    return re.sub(r"[^a-z0-9']", "", word.lower())


def _groups(words, per):
    out, cur = [], []
    for w in words:
        if cur and (len(cur) >= per or sum(len(x["w"]) for x in cur) + len(w["w"]) > 16
                    or w["s"] - cur[-1]["e"] > 0.45 or cur[-1]["w"][-1:] in ".?!"):
            out.append(cur)
            cur = []
        cur.append(w)
    if cur:
        out.append(cur)
    return out


def geometry(src_w, src_h, layout, safe_margin=None):
    """Where the video sits in the vertical frame, and where hook and captions go, respecting social safe zones."""
    if src_h >= src_w and layout != "crop":
        layout = "crop"  # already vertical or square: nothing to gain from a blurred fill
    if layout not in LAYOUTS:
        layout = "blur"
    if layout in ("blur", "fit"):
        fg_h = min(H, int(round(W * src_h / src_w / 2)) * 2)
    elif layout == "zoom":
        fg_h = 1000
    else:
        fg_h = H
    top = (H - fg_h) // 2

    # Social safe margins (Instagram Reels, TikTok, YouTube Shorts)
    mode = str(safe_margin or getattr(config, "SAFE_MARGIN_MODE", "reels")).lower()
    if mode == "reels":
        safe_top, safe_bottom = 270, 1360
    elif mode == "tiktok":
        safe_top, safe_bottom = 240, 1320
    elif mode == "shorts":
        safe_top, safe_bottom = 250, 1420
    else:
        safe_top, safe_bottom = 230, 1640

    if layout in _FILLS:
        return layout, fg_h, safe_top + 30, safe_bottom
    if layout == "split":
        return layout, fg_h, safe_top, H // 2        # captions sit on the line between the two people
    return layout, fg_h, max(safe_top, top - 190), min(safe_bottom, top + fg_h + 180)


def _hook_events(hook, dur, y):
    """The heading: dark text in a white rounded box that fits it, on one line or two balanced ones."""
    f, size = _Font.get(HOOK["font"]), HOOK["size"]
    words = hook.split()
    if not words:
        return []
    lines = [" ".join(words)]
    if f.width(lines[0], size) > HOOK["max_w"] and len(words) > 1:      # two lines, split where they come out most even
        cut = min(range(1, len(words)), key=lambda i: max(f.width(" ".join(words[:i]), size), f.width(" ".join(words[i:]), size)))
        lines = [" ".join(words[:cut]), " ".join(words[cut:])]
    widest = max(f.width(l, size) for l in lines)
    if widest > HOOK["max_w"]:
        size = max(40, int(size * HOOK["max_w"] / widest))
        widest = max(f.width(l, size) for l in lines)
    line_h = f.em(size) * 1.16
    bw, bh = widest + 2 * HOOK["pad_x"], line_h * len(lines) + 2 * HOOK["pad_y"]
    x0, y0, r = (W - bw) / 2, y - bh / 2, min(HOOK["radius"], bh / 2)
    x1, y1 = x0 + bw, y0 + bh
    n = lambda v: f"{v:.0f}"                                                         # noqa: E731
    box = (f"m {n(x0 + r)} {n(y0)} l {n(x1 - r)} {n(y0)} b {n(x1)} {n(y0)} {n(x1)} {n(y0)} {n(x1)} {n(y0 + r)} "
           f"l {n(x1)} {n(y1 - r)} b {n(x1)} {n(y1)} {n(x1)} {n(y1)} {n(x1 - r)} {n(y1)} "
           f"l {n(x0 + r)} {n(y1)} b {n(x0)} {n(y1)} {n(x0)} {n(y1)} {n(x0)} {n(y1 - r)} "
           f"l {n(x0)} {n(y0 + r)} b {n(x0)} {n(y0)} {n(x0)} {n(y0)} {n(x0 + r)} {n(y0)}")
    span = f"{_ts(0)},{_ts(dur)}"
    ev = [f"Dialogue: 3,{span},Hook,,0,0,0,,{{\\an7\\pos(0,0)\\fad(160,0)\\c&H{HOOK['box']}&\\p1}}{box}{{\\p0}}"]
    for i, line in enumerate(lines):
        cy = y - line_h * (len(lines) - 1) / 2 + i * line_h - f.cap_shift(size)
        ev.append(f"Dialogue: 4,{span},Hook,,0,0,0,,{{\\an5\\pos({W // 2},{cy:.0f})\\fad(160,0)\\fs{size}}}{line}")
    return ev


def build_ass(words, start, end, hook, style_name, highlight, hook_y, cap_y, text_overlays=None, stickers=None) -> str:
    st = STYLES.get(style_name, STYLES["bounce"])
    words = [dict(w, s=w["s"] - CAPTION_LEAD, e=w["e"] - CAPTION_LEAD) for w in words]
    strong = {_key(h) for h in highlight if _key(h)}
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{st['font']},{st['size']},&H00{st['base']},&H00{st['base']},&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,{st['outline']},0,5,0,0,0,1
Style: Hook,{HOOK['font']},{HOOK['size']},&H00{HOOK['ink']},&H00{HOOK['ink']},&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,5,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    dur = end - start
    ev = _hook_events(_clean(hook), dur, hook_y)
    f = _Font.get(st["font"])
    inside = [dict(w, w=_clean(w["w"])) for w in words if w["s"] >= start - 0.05 and w["s"] < end]
    inside = sorted((w for w in inside if w["w"]), key=lambda w: w["s"])
    for i, w in enumerate(inside[:-1]):
        w["e"] = max(w["s"], min(w["e"], inside[i + 1]["s"]))
    groups = _groups(inside, st["per"])
    for gi, g in enumerate(groups):
        g_start = max(0.0, g[0]["s"] - start)
        g_end = min(g[-1]["e"] + 0.25, groups[gi + 1][0]["s"] if gi + 1 < len(groups) else end) - start
        if g_end - g_start < 0.02:
            continue
        texts = []
        for x in g:
            t = re.sub(r"[.,;:]+$", "", x["w"])
            texts.append(t.upper() if st["upper"] else t)
        if not all(roman.drawable(t) for t in texts):
            # A script the caption font cannot draw (Japanese, Thai, Chinese...): its words cannot be measured, so
            # they are not placed one by one. The line is drawn whole and centred, in a font the system has for it.
            line = " ".join(texts)
            fs = max(44, min(int(st["size"] * 0.7), int(1500 / max(1, len(line)) * 1.6)))
            ev.append(f"Dialogue: 1,{_ts(g_start)},{_ts(g_end)},Cap,,60,60,0,,{{\\an5\\pos({W // 2},{cap_y:.0f})\\q0\\fs{fs}}}{line}")
            continue
        size = st["size"]
        gap = f.width("n n", size) - 2 * f.width("n", size) + (2 * st["outline"] + 6) * size / st["size"]   # a space, plus room for both outlines
        total = sum(f.width(t, size) for t in texts) + gap * (len(texts) - 1)
        if total > CAP_MAX_W:                                  # a long phrase: shrink it to fit, never run off the frame
            size = max(36, int(size * CAP_MAX_W / total))
            gap = f.width("n n", size) - 2 * f.width("n", size) + (2 * st["outline"] + 6) * size / st["size"]   # a space, plus room for both outlines
            total = sum(f.width(t, size) for t in texts) + gap * (len(texts) - 1)
        cy = cap_y - f.cap_shift(size)
        x = (W - total) / 2
        p = st["pop"]
        for i, (w, t) in enumerate(zip(g, texts)):
            wd = f.width(t, size)
            p = max(104, min(st["pop"], 100 + int(2200 / max(wd, 1))))   # long words grow less, so they never touch their neighbours
            pos = f"\\an5\\pos({x + wd / 2:.0f},{cy:.0f})\\fs{size}"
            x += wd + gap
            a = max(g_start, w["s"] - start)
            b = (g[i + 1]["s"] - start) if i + 1 < len(g) else g_end
            base = st["strong"] if _key(w["w"]) in strong else st["base"]
            if a - g_start >= 0.02:                                                   # waiting to be said
                ev.append(f"Dialogue: 1,{_ts(g_start)},{_ts(a)},Cap,,0,0,0,,{{{pos}\\c&H{base}&}}{t}")
            if b - a >= 0.02:                                                         # being said: colour and a quick pop, in front
                ev.append(f"Dialogue: 2,{_ts(a)},{_ts(b)},Cap,,0,0,0,,{{{pos}\\c&H{st['active']}&\\fscx94\\fscy94"
                          f"\\t(0,90,\\fscx{p}\\fscy{p})\\t(90,200,\\fscx{p - 5}\\fscy{p - 5})}}{t}")
            if g_end - b >= 0.02:                                                     # said
                ev.append(f"Dialogue: 1,{_ts(b)},{_ts(g_end)},Cap,,0,0,0,,{{{pos}\\c&H{base}&\\fscx{p - 5}\\fscy{p - 5}"
                          f"\\t(0,90,\\fscx100\\fscy100)}}{t}")

    def _parse_coord(val, total):
        if val is None:
            return total // 2
        if isinstance(val, str) and val.endswith("%"):
            try:
                return int(round(total * float(val[:-1]) / 100.0))
            except (ValueError, TypeError):
                return total // 2
        try:
            v = float(val)
        except (ValueError, TypeError):
            return total // 2
        if 0 <= v <= 100:
            return int(round(total * v / 100.0))
        return int(max(0, min(total, round(v))))

    # Custom text layers
    for item in (text_overlays or []):
        t_text = _clean(item.get("text", ""))
        if not t_text:
            continue
        t_s = max(0.0, float(item.get("start", 0.0)))
        t_e = min(dur, float(item.get("end", dur))) if item.get("end") else dur
        if t_e <= t_s:
            t_e = dur
        t_x = _parse_coord(item.get("x"), W)
        t_y = _parse_coord(item.get("y"), H)
        raw_fs = item.get("font_size") if item.get("font_size") is not None else item.get("size", 72)
        try:
            fs_val = float(raw_fs)
        except (ValueError, TypeError):
            fs_val = 72.0
        if fs_val <= 64:
            t_fs = max(24, int(round(fs_val * 2.5)))
        else:
            t_fs = max(24, int(round(fs_val)))
        t_col = str(item.get("color", "FFFFFF")).lstrip("#")
        if len(t_col) == 3:
            t_col = "".join(c * 2 for c in t_col)
        ass_col = f"{t_col[4:6]}{t_col[2:4]}{t_col[0:2]}".upper() if len(t_col) == 6 else "FFFFFF"
        rot = int(item.get("rotation", 0))
        rot_tag = f"\\frz{rot}" if rot else ""
        ev.append(f"Dialogue: 5,{_ts(t_s)},{_ts(t_e)},Cap,,0,0,0,,{{\\an5\\pos({t_x},{t_y})\\fs{t_fs}\\c&H{ass_col}&{rot_tag}\\bord3\\3c&H000000&}}{t_text}")

    # Stickers / Emojis
    for item in (stickers or []):
        s_emoji = _clean(str(item.get("emoji") or item.get("text") or ""))
        if not s_emoji:
            continue
        s_s = max(0.0, float(item.get("start", 0.0)))
        s_e = min(dur, float(item.get("end", dur))) if item.get("end") else dur
        if s_e <= s_s:
            s_e = dur
        s_x = _parse_coord(item.get("x"), W)
        s_y = _parse_coord(item.get("y"), H)
        scale = float(item.get("scale", 1.0))
        if item.get("size"):
            s_fs = max(32, int(item.get("size")))
        else:
            s_fs = max(32, int(round(96 * scale)))
        rot = int(item.get("rotation", 0))
        rot_tag = f"\\frz{rot}" if rot else ""
        ev.append(f"Dialogue: 6,{_ts(s_s)},{_ts(s_e)},Cap,,0,0,0,,{{\\an5\\pos({s_x},{s_y})\\fs{s_fs}{rot_tag}}}{s_emoji}")

    return head + "\n".join(ev) + "\n"


PIP_W = 380            # how wide the facecam is in the 1080-wide frame
PIP_MARGIN = 26


def _normalize_facecam(facecam, src_w: int, src_h: int):
    """Converts percentage or pixel facecam coordinates to valid source-pixel crop parameters."""
    if not facecam or not isinstance(facecam, (list, tuple)) or len(facecam) < 4:
        return None
    try:
        raw_fx, raw_fy, raw_fw, raw_fh = (float(facecam[i]) for i in range(4))
    except (ValueError, TypeError):
        return None

    if raw_fw <= 100 and raw_fh <= 100:
        fx = int(round(src_w * raw_fx / 100.0))
        fy = int(round(src_h * raw_fy / 100.0))
        fw = int(round(src_w * raw_fw / 100.0))
        fh = int(round(src_h * raw_fh / 100.0))
    else:
        fx = int(round(raw_fx))
        fy = int(round(raw_fy))
        fw = int(round(raw_fw))
        fh = int(round(raw_fh))

    fw = max(16, min(src_w, fw))
    fh = max(16, min(src_h, fh))
    fw = fw - (fw % 2)
    fh = fh - (fh % 2)

    fx = max(0, min(src_w - fw, fx))
    fy = max(0, min(src_h - fh, fy))

    ox = 0
    oy = 0
    if len(facecam) >= 6:
        try:
            raw_ox = float(facecam[4])
            raw_oy = float(facecam[5])
            if 0 < raw_ox <= 100:
                ox = int(round(W * raw_ox / 100.0))
            else:
                ox = int(round(raw_ox))
            if 0 < raw_oy <= 100:
                oy = int(round(H * raw_oy / 100.0))
            else:
                oy = int(round(raw_oy))
        except (ValueError, TypeError):
            ox, oy = 0, 0

    return [fx, fy, fw, fh, ox, oy]


def _filter(layout, fg_h, facecam=None, video_filter=None):
    vf = FILTERS.get(str(video_filter or "").lower(), "")
    if layout in ("gameplay", "pip") and facecam:
        fx, fy, fw, fh = (int(facecam[i]) for i in range(4))
        if fw > 8 and fh > 8:
            pip_h = max(2, int(round(PIP_W * fh / fw)))
            ox = int(facecam[4]) if len(facecam) >= 6 and int(facecam[4]) > 0 else (W - PIP_W - PIP_MARGIN)
            oy = int(facecam[5]) if len(facecam) >= 6 and int(facecam[5]) > 0 else PIP_MARGIN
            base_chain = (f"[0:v]fps={config.OUT_FPS},split=2[base][fc];"
                          f"[base]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1[b];"
                          f"[fc]crop={fw}:{fh}:{fx}:{fy},scale={PIP_W}:{pip_h},setsar=1[p];"
                          f"[b][p]overlay={ox}:{oy}[v_raw];")
            return base_chain + (f"[v_raw]{vf}[v0];" if vf else "[v_raw]copy[v0];")
    fill = f"[0:v]fps={config.OUT_FPS},scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}"
    if layout == "crop" or (layout in ("gameplay", "pip") and not facecam):
        core = fill + ",setsar=1[v_raw];"
    elif layout == "left":
        core = fill + ":0:0,setsar=1[v_raw];"
    elif layout == "right":
        core = fill + ":in_w-out_w:0,setsar=1[v_raw];"
    elif layout == "fit":
        core = f"[0:v]fps={config.OUT_FPS},scale={W}:{fg_h},pad={W}:{H}:0:(oh-ih)/2:black,setsar=1[v_raw];"
    elif layout == "split":
        half = f"scale={W}:{H // 2}:force_original_aspect_ratio=increase,crop={W}:{H // 2}"
        if facecam and len(facecam) >= 4 and int(facecam[2]) > 8 and int(facecam[3]) > 8:
            fx, fy, fw, fh = (int(facecam[i]) for i in range(4))
            core = (f"[0:v]fps={config.OUT_FPS},split=2[fc][main];"
                    f"[fc]crop={fw}:{fh}:{fx}:{fy},{half}[t];"
                    f"[main]{half}[u];[t][u]vstack,setsar=1[v_raw];")
        else:
            core = (f"[0:v]fps={config.OUT_FPS},split=2[l][r];[l]crop=iw/2:ih:0:0,{half}[t];"
                    f"[r]crop=iw/2:ih:iw/2:0,{half}[u];[t][u]vstack,setsar=1[v_raw];")
    else:
        core = (f"[0:v]fps={config.OUT_FPS},split=2[a][b];"
                f"[a]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=6:2,"
                f"scale={W}:{H},eq=brightness=-0.14:saturation=1.15[bg];"
                f"[b]scale={W}:{fg_h}:force_original_aspect_ratio=increase,crop={W}:{fg_h}[fg];"
                f"[bg][fg]overlay=0:(H-h)/2,setsar=1[v_raw];")
    return core + (f"[v_raw]{vf}[v0];" if vf else "[v_raw]copy[v0];")


def _hook_y(layout, hook_y, facecam=None):
    """Where the heading goes. In the gameplay layout the webcam sits in the top corner, so a heading
    across the top would run under it: it goes just below the picture instead."""
    if layout in ("gameplay", "pip") and facecam and len(facecam) >= 4 and facecam[2] > 8:
        return PIP_MARGIN + int(round(PIP_W * facecam[3] / facecam[2])) + 32
    return hook_y


_qsv_ok = None
RENDER_SECONDS = 600      # one clip's video encode; far beyond normal, so a frozen encoder is stopped, not waited on


def render_clip(src: Path, out_dir: Path, name: str, clip: dict, words: list, src_w: int, src_h: int) -> dict:
    """Writes <name>.mp4, <name>.jpg and <name>.ass into out_dir. Returns file names."""
    out_dir.mkdir(parents=True, exist_ok=True)
    start, end = clip["start"], clip["end"]
    layout, fg_h, hook_y, cap_y = geometry(src_w, src_h, clip.get("layout", "blur"), clip.get("safe_margin"))
    face = _normalize_facecam(clip.get("facecam"), src_w, src_h)
    if layout in ("gameplay", "pip") and not (face and len(face) >= 4):
        layout = "crop"     # no webcam found: the game fills the screen, which is still the right shape
    hook_y = _hook_y(layout, hook_y, face)
    text_overlays = clip.get("text_overlays") or []
    stickers = clip.get("stickers") or []
    v_filter = clip.get("filter")
    vol = float(clip.get("volume", 1.0))
    muted = bool(clip.get("muted", False))
    ass = build_ass(words if clip.get("captions", True) else [], start, end, clip.get("hook", ""), clip.get("style", "bounce"),
                    clip.get("highlight_words", []), hook_y, cap_y, text_overlays=text_overlays, stickers=stickers)
    # The clip is made in a short work folder and moved into place afterwards.
    work = Path(tempfile.mkdtemp(prefix="cf"))
    try:
        meta = {"title": clip.get("title") or "", "description": clip.get("description") or "", "comment": clip.get("description") or ""}
        return _render_in(work, src, out_dir, name, start, end, layout, fg_h, ass, face=face, meta=meta,
                          video_filter=v_filter, volume=vol, muted=muted)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _render_in(work: Path, src: Path, out_dir: Path, name: str, start, end, layout, fg_h, ass: str, face=None, meta=None,
               video_filter=None, volume=1.0, muted=False) -> dict:
    global _qsv_ok
    (work / f"{name}.ass").write_text(ass, encoding="utf-8")
    (work / "f").mkdir()
    for f in config.FONTS_DIR.glob("*.ttf"):
        shutil.copyfile(f, work / "f" / f.name)
    fc = _filter(layout, fg_h, face, video_filter=video_filter) + f"[v0]ass={name}.ass:fontsdir=f[v]"
    af_filters = []
    if muted or volume <= 0.001:
        af_filters.append("volume=0")
    elif abs(volume - 1.0) > 0.01:
        af_filters.append(f"volume={volume:.2f}")
    af_filters.append("loudnorm=I=-14:TP=-1.5:LRA=11")
    af_chain = ",".join(af_filters)

    base = [_tools.get_ffmpeg(), "-y", "-v", "error", "-ss", f"{start:.2f}", "-t", f"{end - start:.2f}", "-i", str(src),
            "-filter_complex", fc, "-map", "[v]", "-map", "0:a:0", "-af", af_chain,
            "-c:a", "aac", "-b:a", "160k", "-ar", "48000"]
    for k, v in (meta or {}).items():
        if v:
            base += ["-metadata", f"{k}={v}"]
    base += ["-movflags", "+faststart", "-pix_fmt"]
    encoders = []
    if _qsv_ok is not False:
        encoders.append(("qsv", ["nv12", "-c:v", "h264_qsv", "-global_quality", "23", "-preset", "fast"]))
    crf_val = str(getattr(config, "OUT_CRF", 21))
    preset_val = str(getattr(config, "OUT_PRESET", "veryfast"))
    encoders.append(("x264", ["yuv420p", "-c:v", "libx264", "-crf", crf_val, "-preset", preset_val]))
    err = None
    for label, enc in encoders:
        p = subprocess.run(base + enc + [f"{name}.mp4"], cwd=work, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", creationflags=NO_WINDOW, timeout=RENDER_SECONDS)
        if p.returncode == 0:
            if label == "qsv":
                _qsv_ok = True
            break
        err = p.stderr[-1200:]
        if label == "qsv":
            _qsv_ok = False
    else:
        raise RuntimeError(f"Render failed: {err}")
    cover_t = min(1.2, (end - start) / 2)
    run(["ffmpeg", "-y", "-v", "error", "-ss", f"{cover_t:.2f}", "-i", f"{name}.mp4", "-frames:v", "1",
         "-q:v", "3", f"{name}.jpg"], cwd=work)
    for ext in ("mp4", "jpg", "ass"):
        shutil.move(str(work / f"{name}.{ext}"), str(out_dir / f"{name}.{ext}"))
    return {"file": f"{name}.mp4", "cover": f"{name}.jpg", "layout": layout}
