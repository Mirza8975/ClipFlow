"""Turns a time range into a vertical clip: 9:16 layout, bouncy word captions, hook title, cover."""
import math
import os
import re
import shutil
import struct
import subprocess
import tempfile
import threading
import unicodedata
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape

from . import config, roman, tools as _tools
from .media import NO_WINDOW, run

W, H = config.OUT_W, config.OUT_H
BUNDLED_FONTS_DIR = Path(__file__).resolve().parent / "fonts"
_fonts_lock = threading.Lock()

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


def _parse_ttf_cmap_ranges(font_bytes: bytes, tables: dict[bytes, int]) -> tuple[tuple[int, int], ...]:
    """Extract sorted inclusive Unicode codepoint ranges from a TTF/OTF cmap table (formats 4 and 12)."""
    cmap_off = tables.get(b"cmap")
    if cmap_off is None or cmap_off + 4 > len(font_bytes):
        return ()
    num_tables = struct.unpack_from(">H", font_bytes, cmap_off + 2)[0]
    best_off = None
    best_fmt = None
    best_prio = -1
    for i in range(num_tables):
        rec_off = cmap_off + 4 + i * 8
        if rec_off + 8 > len(font_bytes):
            break
        pid, eid, sub_rel = struct.unpack_from(">HHI", font_bytes, rec_off)
        sub_off = cmap_off + sub_rel
        if sub_off + 4 > len(font_bytes):
            continue
        fmt = struct.unpack_from(">H", font_bytes, sub_off)[0]
        if fmt == 12 and pid in (0, 3):
            prio = 4 if (pid == 3 and eid == 10) else 3
        elif fmt == 4 and ((pid == 3 and eid in (0, 1)) or pid == 0):
            prio = 2 if (pid == 3 and eid == 1) else 1
        else:
            continue
        if prio > best_prio:
            best_prio = prio
            best_off = sub_off
            best_fmt = fmt
    if best_off is None:
        return ()

    ranges: list[tuple[int, int]] = []
    if best_fmt == 12 and best_off + 16 <= len(font_bytes):
        n_groups = struct.unpack_from(">I", font_bytes, best_off + 12)[0]
        for g in range(n_groups):
            g_off = best_off + 16 + g * 12
            if g_off + 12 > len(font_bytes):
                break
            start_ch, end_ch, start_gid = struct.unpack_from(">III", font_bytes, g_off)
            if start_gid == 0 and start_ch == end_ch:
                continue
            ranges.append((start_ch, end_ch))
    elif best_fmt == 4 and best_off + 14 <= len(font_bytes):
        seg_count_x2 = struct.unpack_from(">H", font_bytes, best_off + 6)[0]
        seg_count = seg_count_x2 // 2
        end_off = best_off + 14
        start_off = end_off + seg_count_x2 + 2
        delta_off = start_off + seg_count_x2
        range_off = delta_off + seg_count_x2
        if range_off + seg_count_x2 <= len(font_bytes):
            for s in range(seg_count):
                end_ch = struct.unpack_from(">H", font_bytes, end_off + s * 2)[0]
                start_ch = struct.unpack_from(">H", font_bytes, start_off + s * 2)[0]
                if start_ch == 0xFFFF and end_ch == 0xFFFF:
                    continue
                ranges.append((start_ch, end_ch))
    ranges.sort()
    return tuple(ranges)


def _is_emoji_codepoint(cp: int) -> bool:
    return (
        0x1F000 <= cp <= 0x1FAFF
        or 0x2600 <= cp <= 0x27BF
        or 0x2300 <= cp <= 0x23FF
        or 0x2B00 <= cp <= 0x2BFF
        or 0xFE00 <= cp <= 0xFE0F
        or cp in (0x200D, 0x20E3)
    )


def _char_script(ch: str) -> str:
    cp = ord(ch)
    if _is_emoji_codepoint(cp):
        return "emoji"
    if 0x0900 <= cp <= 0x097F or 0xA8E0 <= cp <= 0xA8FF:
        return "devanagari"
    if (
        0x0600 <= cp <= 0x06FF
        or 0x0750 <= cp <= 0x077F
        or 0x08A0 <= cp <= 0x08FF
        or 0xFB50 <= cp <= 0xFDFF
        or 0xFE70 <= cp <= 0xFEFF
    ):
        return "arabic"
    if 0xAC00 <= cp <= 0xD7AF or 0x1100 <= cp <= 0x11FF or 0x3130 <= cp <= 0x318F:
        return "korean"
    if 0x3000 <= cp <= 0x9FFF or 0xF900 <= cp <= 0xFAFF or 0xFF00 <= cp <= 0xFFEF:
        return "cjk"
    return "latin"


def _grapheme_cluster_count(text: str) -> int:
    """Count visual clusters (combining marks and ZWJ-joined emoji sequences do not multiply count)."""
    if not text:
        return 0
    count = 0
    after_zwj = False
    for ch in text:
        cp = ord(ch)
        cat = unicodedata.category(ch)
        if cp == 0x200D:
            after_zwj = True
            continue
        if cat in ("Mn", "Me") or 0xFE00 <= cp <= 0xFE0F:
            continue
        if after_zwj:
            after_zwj = False
            continue
        if cat == "Mc":
            continue
        count += 1
        after_zwj = False
    return max(count, 1 if text.strip() else 0)


_FALLBACK_FONT_PATTERNS = {
    "devanagari": ("NotoSansDevanagari-Bold.ttf", "NotoSansDevanagari-Regular.ttf", "NotoSansDevanagari-VF.ttf", "NotoSerifDevanagari-VF.ttf"),
    "arabic": ("NotoNaskhArabic-Bold.ttf", "NotoNaskhArabic-Regular.ttf", "NotoNaskhArabicUI-Bold.ttf", "NotoSansArabic-Regular.ttf"),
    "korean": ("NotoSansCJK-Regular.ttc", "NotoSansKR-Regular.otf"),
    "cjk": ("NotoSansCJK-Regular.ttc", "NotoSerifCJK-Regular.ttc"),
    "latin": ("NotoSans-Bold.ttf", "Roboto-Bold.ttf", "DejaVuSans-Bold.ttf"),
}


class _Font:
    """Measures text using the bundled TTF for Latin and script-aware Fontconfig fallbacks for Unicode/emoji."""
    _cache: dict = {}
    _fallback_cache: dict[str, Path | None] = {}

    def __init__(self, name):
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
        self.cmap_ranges = _parse_ttf_cmap_ranges(d, tables)
        self._pil = {}

    @classmethod
    def get(cls, name):
        if name not in cls._cache:
            cls._cache[name] = cls(name)
        return cls._cache[name]

    def has_codepoint(self, cp: int) -> bool:
        if not self.cmap_ranges:
            return cp < 0x250 or 0x1E00 <= cp <= 0x1EFF
        for start_cp, end_cp in self.cmap_ranges:
            if cp < start_cp:
                return False
            if cp <= end_cp:
                return True
        return False

    def supports_text(self, text: str) -> bool:
        """Return True iff every printable glyph in text exists natively in this bundled TTF."""
        for ch in text or "":
            if ch.isspace() or unicodedata.category(ch) in ("Cc", "Cf", "Mn", "Me"):
                continue
            if _is_emoji_codepoint(ord(ch)) or not self.has_codepoint(ord(ch)):
                return False
        return True

    def uses_simple_latin_metrics(self, text: str) -> bool:
        """Return True only when text can be accurately measured as simple unshaped Latin in this TTF."""
        for ch in text or "":
            if ch.isspace() or unicodedata.category(ch) in ("Cc", "Cf", "Mn", "Me"):
                continue
            cp = ord(ch)
            if _is_emoji_codepoint(cp) or _char_script(ch) != "latin" or not self.has_codepoint(cp):
                return False
        return True

    @classmethod
    def find_fallback_font(cls, script: str) -> Path | None:
        if script in cls._fallback_cache:
            return cls._fallback_cache[script]
        patterns = _FALLBACK_FONT_PATTERNS.get(script, ())
        search_dirs = (
            Path("/system/fonts"),
            config.FONTS_DIR,
            BUNDLED_FONTS_DIR,
            Path("/usr/share/fonts"),
        )
        found: Path | None = None
        for d in search_dirs:
            if not d.is_dir():
                continue
            for pat in patterns:
                cand = d / pat
                if cand.is_file() and cand.stat().st_size > 0:
                    found = cand
                    break
            if found:
                break
        cls._fallback_cache[script] = found
        return found

    def em(self, size):
        return size * self.upm / (self.asc + self.desc)

    def _measure_bundled(self, text: str, size: float) -> float:
        try:
            from PIL import ImageFont
            if "f" not in self._pil:
                self._pil["f"] = ImageFont.truetype(self.path, 200)
            return self._pil["f"].getlength(text) * self.em(size) / 200
        except Exception:  # noqa: BLE001 - no measuring library: a fair estimate keeps clips rendering
            return len(text) * self.em(size) * 0.62

    def _measure_fallback_segment(self, seg: str, script: str, size: float) -> float:
        if not seg:
            return 0.0
        # Pure emoji sequence (including ZWJ families, variation selectors, regional indicators)
        if script == "emoji":
            clusters = 0
            after_zwj = False
            reg_ind = 0
            for ch in seg:
                cp = ord(ch)
                if cp == 0x200D:
                    after_zwj = True
                    continue
                if 0xFE00 <= cp <= 0xFE0F or cp == 0x20E3:
                    continue
                if 0x1F1E6 <= cp <= 0x1F1FF:
                    reg_ind += 1
                    if reg_ind % 2 == 1 and not after_zwj:
                        clusters += 1
                    after_zwj = False
                    continue
                reg_ind = 0
                if after_zwj:
                    after_zwj = False
                    continue
                clusters += 1
            return max(1, clusters) * size * 1.15

        pil_w = 0.0
        fb_font = self.find_fallback_font(script)
        if fb_font is not None:
            try:
                from PIL import ImageFont
                key = str(fb_font)
                if key not in self._pil:
                    self._pil[key] = ImageFont.truetype(key, 200)
                pil_w = float(self._pil[key].getlength(seg)) * size / 200.0
            except Exception:
                pil_w = 0.0

        est = 0.0
        prev_virama = False
        for ch in seg:
            cp = ord(ch)
            cat = unicodedata.category(ch)
            if ch.isspace():
                est += size * 0.28
                prev_virama = False
                continue
            if cp == 0x094D:  # Devanagari virama triggers conjunct contraction
                prev_virama = True
                continue
            if cat in ("Mn", "Me") or cp in (0x200C, 0x200D) or 0xFE00 <= cp <= 0xFE0F:
                continue
            if cat == "Mc":
                est += size * 0.28
                prev_virama = False
                continue
            if script == "devanagari":
                est += size * (0.48 if prev_virama else 0.68)
            elif script == "arabic":
                est += size * 0.56
            elif script in ("cjk", "korean"):
                est += size * 1.02
            else:
                est += self.em(size) * 0.62
            prev_virama = False
        return max(pil_w, est) if (pil_w > 0 and script == "arabic") else (pil_w or est)

    def width(self, text, size):
        """Pixels wide at this ASS size, switching cleanly to script-aware metrics for non-Latin/emoji text."""
        if not text:
            return 0.0
        if self.uses_simple_latin_metrics(text):
            return self._measure_bundled(text, size)

        total = 0.0
        run_chars: list[str] = []
        run_mode: str | None = None  # "bundled" or script name

        def _flush():
            nonlocal total
            if not run_chars or run_mode is None:
                return
            seg = "".join(run_chars)
            if run_mode == "bundled":
                total += self._measure_bundled(seg, size)
            else:
                total += self._measure_fallback_segment(seg, run_mode, size)
            run_chars.clear()

        for ch in text:
            cp = ord(ch)
            if ch.isspace() and run_mode is not None:
                run_chars.append(ch)
                continue
            is_emoji = _is_emoji_codepoint(cp)
            script = _char_script(ch)
            if is_emoji:
                mode = "emoji"
            elif script != "latin":
                mode = script
            elif self.has_codepoint(cp):
                mode = "bundled"
            else:
                mode = "fallback_latin"
            if run_mode != mode:
                _flush()
                run_mode = mode
            run_chars.append(ch)
        _flush()
        return total

    def line_metrics(self, text: str, size: float) -> tuple[float, float, int]:
        """Return (line_height, cap_shift, extra_horizontal_pad) accounting for fallback script ascent/descent."""
        if self.uses_simple_latin_metrics(text):
            return self.em(size) * 1.16, self.cap_shift(size), 0
        # Non-Latin scripts (Devanagari matras above shirorekha, Arabic diacritics) need taller box metrics.
        return max(self.em(size) * 1.16, size * 1.32), size * 0.04, 8

    def cap_shift(self, size):
        """How far below the anchor the middle of a capital letter sits when text is centred on a point."""
        return ((self.asc - self.desc) / 2 - self.cap / 2) * size / (self.asc + self.desc)


CAPTION_LEAD = 0.0  # measured: word stamps are within 0.15 s of the voice, so no shift is needed


def install_fonts():
    """Copy the bundled caption fonts next to the data directory atomically and thread-safely."""
    try:
        if not BUNDLED_FONTS_DIR.is_dir():
            return
        with _fonts_lock:
            config.FONTS_DIR.mkdir(parents=True, exist_ok=True)
            for f in BUNDLED_FONTS_DIR.glob("*.ttf"):
                target = config.FONTS_DIR / f.name
                if not target.exists() or target.stat().st_size == 0:
                    try:
                        tmp = config.FONTS_DIR / f".{f.name}.{threading.get_ident()}.tmp"
                        shutil.copyfile(f, tmp)
                        tmp.replace(target)
                    except OSError:
                        pass
    except Exception as e:
        print(f"[ClipFlow] Font installation notice: {e}", flush=True)


def escape_ffmpeg_filter_value(val: str) -> str:
    """Escape a path or value for FFmpeg filtergraph options inside -filter_complex.

    FFmpeg parses filter options in two passes (avfilter_graph_parse2 -> av_opt_set_from_string),
    so pass-2 characters ('\\', ':', '=', '\\'') require two escaping levels while pass-1
    delimiters (',', ';', '[', ']') require one level.
    """
    s = str(val or "")
    s = s.replace("\\", "\\\\\\\\")
    s = s.replace(":", "\\\\:")
    s = s.replace("=", "\\\\=")
    s = s.replace("'", "\\\\\\'")
    s = s.replace(",", "\\,")
    s = s.replace(";", "\\;")
    s = s.replace("[", "\\[")
    s = s.replace("]", "\\]")
    if s.startswith(" "):
        s = "\\\\\\ " + s[1:]
    if s.endswith(" "):
        s = s[:-1] + "\\\\\\ "
    return s


def _safe_work_stem(name: str) -> str:
    """Sanitize clip stem for temp/output filenames while preserving spaces and Unicode."""
    cleaned = re.sub(r"[\\/\x00]+", "_", str(name or "")).strip()
    if not cleaned or cleaned in (".", ".."):
        return "clip"
    return cleaned


def _required_fonts_for_ass(ass_content: str) -> list[str]:
    """Identify bundled font filenames needed by the Styles section of an ASS script."""
    required: list[str] = []
    for line in (ass_content or "").splitlines():
        if line.startswith("Style:"):
            parts = line.split(",", 3)
            if len(parts) >= 2:
                font_name = parts[1].strip()
                ttf = FONT_FILES.get(font_name)
                if ttf and ttf not in required:
                    required.append(ttf)
    if not required:
        required.extend(FONT_FILES.values())
    return required


def _ensure_render_fonts(dest_fonts_dir: Path, required_fonts: list[str] | None = None) -> list[Path]:
    """Populate per-job fonts directory from config.FONTS_DIR and bundled fonts, verifying required files exist."""
    dest_fonts_dir.mkdir(parents=True, exist_ok=True)
    install_fonts()
    copied: dict[str, Path] = {}
    with _fonts_lock:
        for src_dir in (config.FONTS_DIR, BUNDLED_FONTS_DIR):
            if not src_dir.is_dir():
                continue
            for ext in ("*.ttf", "*.otf"):
                for font_file in src_dir.glob(ext):
                    if font_file.is_file() and font_file.stat().st_size > 0 and font_file.name not in copied:
                        dst = dest_fonts_dir / font_file.name
                        shutil.copyfile(font_file, dst)
                        copied[font_file.name] = dst

    if required_fonts:
        missing = [
            fname for fname in required_fonts
            if not (dest_fonts_dir / fname).is_file() or (dest_fonts_dir / fname).stat().st_size == 0
        ]
        if missing:
            raise RuntimeError(
                f"Render failed: Missing required subtitle font file(s): {', '.join(missing)}. "
                f"Checked directories: {config.FONTS_DIR}, {BUNDLED_FONTS_DIR}."
            )
    return list(copied.values())


def _write_android_fontconfig(
    work: Path,
    work_fonts_dir: Path,
    system_fonts_dir: Path | None = None,
    cache_dir: Path | None = None,
) -> Path | None:
    """Create a per-job Fontconfig file on Android pointing libass to job fonts, /system/fonts, and writable cache."""
    if os.name == "nt" or not _tools.is_android():
        return None
    dirs = [str(work_fonts_dir.resolve())]
    sys_fonts = system_fonts_dir if system_fonts_dir is not None else Path("/system/fonts")
    if sys_fonts.is_dir() and os.access(sys_fonts, os.R_OK):
        dirs.append(str(sys_fonts))
    fc_cache = cache_dir if cache_dir is not None else (work / "fc-cache")
    fc_cache.mkdir(parents=True, exist_ok=True)
    conf_path = work / "fonts.conf"
    dir_xml = "\n".join(f"  <dir>{_xml_escape(d)}</dir>" for d in dirs)
    cache_xml = f"  <cachedir>{_xml_escape(str(fc_cache.resolve()))}</cachedir>"
    conf_path.write_text(
        '<?xml version="1.0"?>\n<!DOCTYPE fontconfig SYSTEM "fonts.dtd">\n<fontconfig>\n'
        + dir_xml + "\n" + cache_xml + "\n</fontconfig>\n",
        encoding="utf-8",
    )
    return conf_path


def _build_ffmpeg_subtitle_env(work: Path, conf_path: Path | None) -> dict[str, str] | None:
    """Build isolated per-subprocess Fontconfig/cache environment without mutating global os.environ."""
    explicit_override = (os.environ.get("CLIPFLOW_FONTCONFIG_FILE") or "").strip()
    override_valid = bool(explicit_override and Path(explicit_override).is_file() and os.access(explicit_override, os.R_OK))
    conf_valid = bool(conf_path is not None and conf_path.is_file())
    if not conf_valid and not override_valid:
        stale_fc = (os.environ.get("FONTCONFIG_FILE") or "").strip()
        if not stale_fc or Path(stale_fc).is_file():
            return None
        sub = {k: v for k, v in os.environ.items() if k != "FONTCONFIG_FILE"}
        fc_home = work / "fc-home"
        xdg_cache = work / "xdg-cache"
        fc_home.mkdir(parents=True, exist_ok=True)
        xdg_cache.mkdir(parents=True, exist_ok=True)
        sub["HOME"] = str(fc_home.resolve())
        sub["XDG_CACHE_HOME"] = str(xdg_cache.resolve())
        return sub

    fc_home = work / "fc-home"
    xdg_cache = work / "xdg-cache"
    fc_home.mkdir(parents=True, exist_ok=True)
    xdg_cache.mkdir(parents=True, exist_ok=True)

    resolved_conf = str(Path(explicit_override).resolve()) if override_valid else str(conf_path.resolve())
    return {
        **os.environ,
        "FONTCONFIG_FILE": resolved_conf,
        "HOME": str(fc_home.resolve()),
        "XDG_CACHE_HOME": str(xdg_cache.resolve()),
    }


LAYOUTS = {
    "blur": "Full frame + blurred fill (9:16)",
    "crop": "Fill the screen (centre crop 9:16)",
    "landscape": "Horizontal full frame (16:9)",
    "square": "Square video (1:1)",
    "split": "Split screen: top & bottom (two people)",
    "pip": "Picture-in-picture with facecam",
    "gameplay": "Gameplay fills the screen, facecam in the corner",
    "fit": "Full frame on black",
    "zoom": "Zoomed in + blurred fill",
    "left": "Fill the screen, keep the left side",
    "right": "Fill the screen, keep the right side",
}


def get_dimensions(layout: str, quality: int | None = None) -> tuple[int, int]:
    """Return even (width, height) for the target video layout and optional short-edge quality."""
    base_w, base_h = getattr(config, "OUT_W", 1080), getattr(config, "OUT_H", 1920)
    if layout == "landscape":
        base_w, base_h = 1920, 1080
    elif layout == "square":
        base_w, base_h = 1080, 1080
    if quality is None:
        return base_w, base_h
    try:
        q = max(360, min(1080, int(float(quality))))
    except (ValueError, TypeError):
        return base_w, base_h
    short = min(base_w, base_h)
    if q == short:
        return base_w, base_h
    scale = q / short
    w = max(2, int(round(base_w * scale / 2)) * 2)
    h = max(2, int(round(base_h * scale / 2)) * 2)
    return w, h


def _text_scale(cur_w: int, cur_h: int) -> float:
    """Scale ASS hook/caption typography proportionally for non-1080x1920 resolutions and aspect ratios."""
    if cur_w == W and cur_h == H:
        return 1.0
    if cur_w < cur_h:
        return max(0.5, min(1.5, cur_w / W))
    return max(0.5, min(1.0, (cur_h / H) * 1.45))


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
_FILLS = ("crop", "left", "right", "gameplay", "pip", "landscape", "square")      # layouts where the picture covers the whole frame


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


def geometry(src_w, src_h, layout, safe_margin=None, quality=None):
    """Where the video sits in the frame, and where hook and captions go, respecting social safe zones."""
    if layout not in LAYOUTS:
        layout = "blur"

    cur_w, cur_h = get_dimensions(layout, quality=quality)
    if layout == "landscape":
        fg_h = cur_h
        return layout, fg_h, max(48, int(round(120 * cur_h / 1080))), cur_h - max(60, int(round(140 * cur_h / 1080)))
    if layout == "square":
        fg_h = cur_h
        return layout, fg_h, max(56, int(round(140 * cur_h / 1080))), cur_h - max(64, int(round(160 * cur_h / 1080)))

    h_scale = cur_h / 1920.0
    if layout in ("blur", "fit"):
        fg_h = min(cur_h, max(2, int(round(cur_w * src_h / src_w / 2)) * 2))
    elif layout == "zoom":
        fg_h = min(cur_h, max(2, int(round(1000 * h_scale / 2)) * 2))
    else:
        fg_h = cur_h
    top = (cur_h - fg_h) // 2

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

    if cur_h != 1920:
        safe_top = int(round(safe_top * h_scale))
        safe_bottom = int(round(safe_bottom * h_scale))

    if layout in _FILLS:
        return layout, fg_h, safe_top + int(round(30 * h_scale)), safe_bottom
    if layout == "split":
        return layout, fg_h, safe_top, cur_h // 2        # captions sit on the line between the two people
    return layout, fg_h, max(safe_top, top - int(round(190 * h_scale))), min(safe_bottom, top + fg_h + int(round(180 * h_scale)))


def _hook_events(hook, dur, y, out_w=None, out_h=None):
    """The heading: dark text in a white rounded box that fits it, on one line or two balanced ones."""
    cur_w = out_w or W
    cur_h = out_h or H
    s = _text_scale(cur_w, cur_h)
    max_w = max(240, int(round(HOOK["max_w"] * (cur_w / W))))
    f, size = _Font.get(HOOK["font"]), max(30, int(round(HOOK["size"] * s)))
    pad_x = max(12, int(round(HOOK["pad_x"] * s)))
    pad_y = max(8, int(round(HOOK["pad_y"] * s)))
    radius = max(10, int(round(HOOK["radius"] * s)))
    words = hook.split()
    if not words:
        return []
    lines = [" ".join(words)]
    if f.width(lines[0], size) > max_w and len(words) > 1:      # two lines, split where they come out most even
        cut = min(range(1, len(words)), key=lambda i: max(f.width(" ".join(words[:i]), size), f.width(" ".join(words[i:]), size)))
        lines = [" ".join(words[:cut]), " ".join(words[cut:])]
    widest = max(f.width(l, size) for l in lines)
    if widest > max_w:
        size = max(28, int(size * max_w / widest))
        widest = max(f.width(l, size) for l in lines)
    line_h, v_shift, pad_x_extra = f.line_metrics(hook, size)
    bw = widest + 2 * (pad_x + pad_x_extra)
    bh = line_h * len(lines) + 2 * pad_y
    x0, y0, r = (cur_w - bw) / 2, y - bh / 2, min(radius, bh / 2)
    x1, y1 = x0 + bw, y0 + bh
    n = lambda v: f"{v:.0f}"                                                         # noqa: E731
    box = (f"m {n(x0 + r)} {n(y0)} l {n(x1 - r)} {n(y0)} b {n(x1)} {n(y0)} {n(x1)} {n(y0)} {n(x1)} {n(y0 + r)} "
           f"l {n(x1)} {n(y1 - r)} b {n(x1)} {n(y1)} {n(x1)} {n(y1)} {n(x1 - r)} {n(y1)} "
           f"l {n(x0 + r)} {n(y1)} b {n(x0)} {n(y1)} {n(x0)} {n(y1)} {n(x0)} {n(y1 - r)} "
           f"l {n(x0)} {n(y0 + r)} b {n(x0)} {n(y0)} {n(x0)} {n(y0)} {n(x0 + r)} {n(y0)}")
    span = f"{_ts(0)},{_ts(dur)}"
    ev = [f"Dialogue: 3,{span},Hook,,0,0,0,,{{\\an7\\pos(0,0)\\fad(160,0)\\c&H{HOOK['box']}&\\p1}}{box}{{\\p0}}"]
    for i, line in enumerate(lines):
        cy = y - line_h * (len(lines) - 1) / 2 + i * line_h - v_shift
        ev.append(f"Dialogue: 4,{span},Hook,,0,0,0,,{{\\an5\\pos({cur_w // 2},{cy:.0f})\\fad(160,0)\\fs{size}}}{line}")
    return ev


def build_ass(words, start, end, hook, style_name, highlight, hook_y, cap_y, text_overlays=None, stickers=None, out_w=None, out_h=None) -> str:
    st = STYLES.get(style_name, STYLES["bounce"])
    cur_w = out_w or W
    cur_h = out_h or H
    s = _text_scale(cur_w, cur_h)
    ov_scale = min(cur_w, cur_h) / W
    cap_size = max(32, int(round(st["size"] * s)))
    cap_outline = max(2, int(round(st["outline"] * s)))
    hook_size = max(30, int(round(HOOK["size"] * s)))
    cap_max_w = int(cur_w * 0.82)
    safe_words = []
    for w in (words or []):
        if not isinstance(w, dict) or w.get("w") is None:
            continue
        try:
            ws = float(w.get("s", 0.0))
            we = max(ws, float(w.get("e", ws)))
        except (TypeError, ValueError):
            continue
        safe_words.append(dict(w, s=ws - CAPTION_LEAD, e=we - CAPTION_LEAD))
    words = safe_words
    strong = {_key(h) for h in highlight if _key(h)}
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {cur_w}
PlayResY: {cur_h}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{st['font']},{cap_size},&H00{st['base']},&H00{st['base']},&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,{cap_outline},0,5,0,0,0,1
Style: Hook,{HOOK['font']},{hook_size},&H00{HOOK['ink']},&H00{HOOK['ink']},&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,5,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    dur = max(0.05, float(end) - float(start))
    ev = _hook_events(_clean(hook), dur, hook_y, out_w=cur_w, out_h=cur_h)
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
            # Non-Latin scripts (Hindi, Arabic, CJK...): drawn whole-line so Fontconfig + HarfBuzz + FriBidi shape clusters/BiDi cleanly.
            line = " ".join(texts)
            clusters = max(1, _grapheme_cluster_count(line))
            fs = max(32, min(int(cap_size * 0.7), int(1500 * s / clusters * 1.6)))
            w_est = f.width(line, fs)
            if w_est > cap_max_w and w_est > 0:
                fs = max(28, int(fs * cap_max_w / w_est))
            ev.append(f"Dialogue: 1,{_ts(g_start)},{_ts(g_end)},Cap,,60,60,0,,{{\\an5\\pos({cur_w // 2},{cap_y:.0f})\\q0\\fs{fs}}}{line}")
            continue
        size = cap_size
        gap = f.width("n n", size) - 2 * f.width("n", size) + (2 * cap_outline + 6) * size / cap_size   # a space, plus room for both outlines
        total = sum(f.width(t, size) for t in texts) + gap * (len(texts) - 1)
        if total > cap_max_w:                                  # a long phrase: shrink it to fit, never run off the frame
            size = max(28, int(size * cap_max_w / total))
            gap = f.width("n n", size) - 2 * f.width("n", size) + (2 * cap_outline + 6) * size / cap_size   # a space, plus room for both outlines
            total = sum(f.width(t, size) for t in texts) + gap * (len(texts) - 1)
        cy = cap_y - f.cap_shift(size)
        x = (cur_w - total) / 2
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

    def _num(val, default: float) -> float:
        try:
            fv = float(val)
            return fv if math.isfinite(fv) else default
        except (ValueError, TypeError):
            return default

    # Custom text layers
    for item in (text_overlays or []):
        if not isinstance(item, dict):
            continue
        t_text = _clean(item.get("text", ""))
        if not t_text:
            continue
        t_s = max(0.0, _num(item.get("start"), 0.0))
        raw_te = item.get("end")
        t_e = min(dur, _num(raw_te, dur)) if raw_te not in (None, "", 0, 0.0) else dur
        if t_e <= t_s:
            t_e = dur
        t_x = _parse_coord(item.get("x"), cur_w)
        t_y = _parse_coord(item.get("y"), cur_h)
        raw_fs = item.get("font_size") if item.get("font_size") is not None else item.get("size", 72)
        fs_val = max(8.0, _num(raw_fs, 72.0))
        if fs_val <= 64:
            t_fs = max(20, int(round(fs_val * 2.5 * ov_scale)))
        else:
            t_fs = max(20, int(round(fs_val * ov_scale)))
        t_col = str(item.get("color", "FFFFFF")).lstrip("#")
        if len(t_col) == 3:
            t_col = "".join(c * 2 for c in t_col)
        ass_col = f"{t_col[4:6]}{t_col[2:4]}{t_col[0:2]}".upper() if len(t_col) == 6 else "FFFFFF"
        rot = int(round(_num(item.get("rotation"), 0.0)))
        rot_tag = f"\\frz{rot}" if rot else ""
        ev.append(f"Dialogue: 5,{_ts(t_s)},{_ts(t_e)},Cap,,0,0,0,,{{\\an5\\pos({t_x},{t_y})\\fs{t_fs}\\c&H{ass_col}&{rot_tag}\\bord3\\3c&H000000&}}{t_text}")

    # Stickers / Emojis
    for item in (stickers or []):
        if not isinstance(item, dict):
            continue
        s_emoji = _clean(str(item.get("emoji") or item.get("text") or ""))
        if not s_emoji:
            continue
        s_s = max(0.0, _num(item.get("start"), 0.0))
        raw_se = item.get("end")
        s_e = min(dur, _num(raw_se, dur)) if raw_se not in (None, "", 0, 0.0) else dur
        if s_e <= s_s:
            s_e = dur
        s_x = _parse_coord(item.get("x"), cur_w)
        s_y = _parse_coord(item.get("y"), cur_h)
        scale = max(0.2, min(5.0, _num(item.get("scale"), 1.0)))
        if item.get("size"):
            s_fs = max(24, int(round(max(8.0, _num(item.get("size"), 96.0)) * ov_scale)))
        else:
            s_fs = max(24, int(round(96 * scale * ov_scale)))
        rot = int(round(_num(item.get("rotation"), 0.0)))
        rot_tag = f"\\frz{rot}" if rot else ""
        ev.append(f"Dialogue: 6,{_ts(s_s)},{_ts(s_e)},Cap,,0,0,0,,{{\\an5\\pos({s_x},{s_y})\\fs{s_fs}{rot_tag}}}{s_emoji}")

    return head + "\n".join(ev) + "\n"


PIP_W = 380            # how wide the facecam is in the 1080-wide frame
PIP_MARGIN = 26


def _normalize_facecam(facecam, src_w: int, src_h: int, out_w: int = None, out_h: int = None):
    """Converts percentage or pixel facecam coordinates to valid source-pixel crop parameters."""
    if not facecam or not isinstance(facecam, (list, tuple)) or len(facecam) < 4:
        return None
    try:
        raw_fx, raw_fy, raw_fw, raw_fh = (float(facecam[i]) for i in range(4))
    except (ValueError, TypeError):
        return None

    is_pct = (
        0 <= raw_fx <= 100 and 0 <= raw_fy <= 100
        and 0 < raw_fw <= 100 and 0 < raw_fh <= 100
        and (raw_fx + raw_fw) <= 105 and (raw_fy + raw_fh) <= 105
    )
    if is_pct:
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

    cw, ch = out_w or W, out_h or H
    ox = 0
    oy = 0
    if len(facecam) >= 6:
        try:
            raw_ox = float(facecam[4])
            raw_oy = float(facecam[5])
            if 0 < raw_ox <= 100:
                ox = int(round(cw * raw_ox / 100.0))
            else:
                ox = int(round(raw_ox))
            if 0 < raw_oy <= 100:
                oy = int(round(ch * raw_oy / 100.0))
            else:
                oy = int(round(raw_oy))
        except (ValueError, TypeError):
            ox, oy = 0, 0

    return [fx, fy, fw, fh, ox, oy]


def _resolve_video_filter(video_filter: str | None) -> str:
    """Resolve preset filter string with non-GPL fallbacks when FFmpeg lacks 'eq'."""
    name = str(video_filter or "").lower()
    raw_vf = FILTERS.get(name, "")
    if not raw_vf or "eq=" not in raw_vf or _tools.has_filter("eq"):
        return raw_vf
    has_cl = _tools.has_filter("colorlevels")
    has_hs = _tools.has_filter("huesaturation")
    has_hue = _tools.has_filter("hue")
    if name == "dramatic":
        parts = []
        if has_cl:
            parts.append("colorlevels=rimin=0.08:gimin=0.08:bimin=0.08:rimax=0.92:gimax=0.92:bimax=0.92")
        if has_hs:
            parts.append("huesaturation=saturation=-0.12")
        return ",".join(parts)
    if name == "cyber":
        parts = []
        if has_cl:
            parts.append("colorlevels=rimin=0.04:gimin=0.04:bimin=0.04:rimax=0.96:gimax=0.96:bimax=0.96")
        if has_hs:
            parts.append("huesaturation=saturation=0.45")
        if has_hue:
            parts.append("hue=h=15")
        return ",".join(parts)
    if name == "vivid":
        parts = []
        if has_cl:
            parts.append("colorlevels=rimin=0.05:gimin=0.05:bimin=0.05:rimax=0.95:gimax=0.95:bimax=0.95")
        if has_hs:
            parts.append("huesaturation=saturation=0.35")
        return ",".join(parts)
    return "huesaturation=saturation=0.25" if has_hs else ""


def _filter(layout, fg_h, facecam=None, video_filter=None, out_w=None, out_h=None, blur_filter=None, fps=None, transition=None, duration=None):
    cur_w = out_w or (get_dimensions(layout)[0] if layout in LAYOUTS else W)
    cur_h = out_h or (get_dimensions(layout)[1] if layout in LAYOUTS else H)
    target_fps = max(15, min(60, int(fps))) if fps else config.OUT_FPS
    scale_ratio = min(cur_w, cur_h) / W
    pip_w = max(120, int(round(PIP_W * scale_ratio / 2)) * 2)
    pip_margin = max(12, int(round(PIP_MARGIN * scale_ratio)))
    vf_parts = []
    vf = _resolve_video_filter(video_filter)
    if vf:
        vf_parts.append(vf)
    if str(transition or "").lower() == "fade" and duration and float(duration) > 1.0:
        if not _tools.get_supported_filters() or _tools.has_filter("fade"):
            fade_out = max(0.0, float(duration) - 0.35)
            vf_parts.append(f"fade=t=in:st=0:d=0.35,fade=t=out:st={fade_out:.2f}:d=0.35")
    post_vf = ",".join(vf_parts)
    if layout in ("gameplay", "pip") and facecam:
        fx, fy, fw, fh = (int(facecam[i]) for i in range(4))
        if fw > 8 and fh > 8:
            pip_h = max(2, int(round(pip_w * fh / fw / 2)) * 2)
            ox = int(facecam[4]) if len(facecam) >= 6 and int(facecam[4]) > 0 else (cur_w - pip_w - pip_margin)
            oy = int(facecam[5]) if len(facecam) >= 6 and int(facecam[5]) > 0 else pip_margin
            base_chain = (f"[0:v]fps={target_fps},split=2[base][fc];"
                          f"[base]scale={cur_w}:{cur_h}:force_original_aspect_ratio=increase,crop={cur_w}:{cur_h},setsar=1[b];"
                          f"[fc]crop={fw}:{fh}:{fx}:{fy},scale={pip_w}:{pip_h},setsar=1[p];"
                          f"[b][p]overlay={ox}:{oy}[v_raw];")
            return base_chain + (f"[v_raw]{post_vf}[v0];" if post_vf else "[v_raw]copy[v0];")
    fill = f"[0:v]fps={target_fps},scale={cur_w}:{cur_h}:force_original_aspect_ratio=increase,crop={cur_w}:{cur_h}"
    if layout in ("crop", "landscape", "square") or (layout in ("gameplay", "pip") and not facecam):
        core = fill + ",setsar=1[v_raw];"
    elif layout == "left":
        core = fill + ":0:0,setsar=1[v_raw];"
    elif layout == "right":
        core = fill + ":in_w-out_w:0,setsar=1[v_raw];"
    elif layout == "fit":
        core = (f"[0:v]fps={target_fps},scale={cur_w}:{cur_h}:force_original_aspect_ratio=decrease,"
                f"pad={cur_w}:{cur_h}:(ow-iw)/2:(oh-ih)/2:black,setsar=1[v_raw];")
    elif layout == "split":
        half = f"scale={cur_w}:{cur_h // 2}:force_original_aspect_ratio=increase,crop={cur_w}:{cur_h // 2}"
        if facecam and len(facecam) >= 4 and int(facecam[2]) > 8 and int(facecam[3]) > 8:
            fx, fy, fw, fh = (int(facecam[i]) for i in range(4))
            core = (f"[0:v]fps={target_fps},split=2[fc][main];"
                    f"[fc]crop={fw}:{fh}:{fx}:{fy},{half}[t];"
                    f"[main]{half}[u];[t][u]vstack,setsar=1[v_raw];")
        else:
            core = (f"[0:v]fps={target_fps},split=2[l][r];[l]crop=iw/2:ih:0:0,{half}[t];"
                    f"[r]crop=iw/2:ih:iw/2:0,{half}[u];[t][u]vstack,setsar=1[v_raw];")
    else:
        blur_cmd = blur_filter if blur_filter is not None else _tools.get_best_blur_filter()
        blur_segment = f"{blur_cmd}," if blur_cmd else ""
        dim_cmd = _tools.get_bg_dim_filter()
        dim_segment = f",{dim_cmd}" if dim_cmd else ""
        core = (f"[0:v]fps={target_fps},split=2[a][b];"
                f"[a]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,{blur_segment}"
                f"scale={cur_w}:{cur_h}{dim_segment}[bg];"
                f"[b]scale={cur_w}:{fg_h}:force_original_aspect_ratio=increase,crop={cur_w}:{fg_h}[fg];"
                f"[bg][fg]overlay=0:({cur_h}-h)/2,setsar=1[v_raw];")
    return core + (f"[v_raw]{post_vf}[v0];" if post_vf else "[v_raw]copy[v0];")


def _hook_y(layout, hook_y, facecam=None, out_w=None, out_h=None):
    """Where the heading goes. In the gameplay layout the webcam sits in the top corner, so a heading
    across the top would run under it: it goes just below the picture instead."""
    if layout in ("gameplay", "pip") and facecam and len(facecam) >= 4 and facecam[2] > 8:
        cur_w = out_w or (get_dimensions(layout)[0] if layout in LAYOUTS else W)
        cur_h = out_h or (get_dimensions(layout)[1] if layout in LAYOUTS else H)
        scale_ratio = min(cur_w, cur_h) / W
        pip_w = max(120, int(round(PIP_W * scale_ratio / 2)) * 2)
        pip_margin = max(12, int(round(PIP_MARGIN * scale_ratio)))
        return pip_margin + int(round(pip_w * facecam[3] / facecam[2])) + max(18, int(round(32 * scale_ratio)))
    return hook_y


def has_ass_events(ass_content: str) -> bool:
    """Return True if the ASS script contains any Dialogue events."""
    if not ass_content:
        return False
    return any(line.strip().startswith("Dialogue:") for line in ass_content.splitlines())


def _ass_event_kinds(ass_content: str) -> list[str]:
    """Identify which overlay element categories exist in an ASS script."""
    if not ass_content:
        return []
    kinds = set()
    for line in ass_content.splitlines():
        s = line.strip()
        if not s.startswith("Dialogue:"):
            continue
        parts = s.split(",", 4)
        layer = parts[0].split(":", 1)[1].strip() if ":" in parts[0] else ""
        style = parts[3].strip() if len(parts) > 3 else ""
        if style == "Hook":
            kinds.add("title hook")
        elif layer == "5":
            kinds.add("text overlays")
        elif layer == "6":
            kinds.add("stickers")
        else:
            kinds.add("captions")
    order = ["captions", "title hook", "text overlays", "stickers"]
    return [k for k in order if k in kinds]


_qsv_ok = None
RENDER_SECONDS = 600      # one clip's video encode; far beyond normal, so a frozen encoder is stopped, not waited on


def render_clip(src: Path, out_dir: Path, name: str, clip: dict, words: list, src_w: int, src_h: int) -> dict:
    """Writes <name>.mp4, <name>.jpg and <name>.ass into out_dir. Returns file names."""
    out_dir.mkdir(parents=True, exist_ok=True)
    start, end = clip["start"], clip["end"]
    clip_quality = clip.get("quality")
    try:
        clip_fps = int(clip["fps"]) if clip.get("fps") else config.OUT_FPS
        clip_fps = max(15, min(60, clip_fps))
    except (TypeError, ValueError):
        clip_fps = config.OUT_FPS
    clip_transition = str(clip.get("transition") or "none").lower()
    layout, fg_h, hook_y, cap_y = geometry(src_w, src_h, clip.get("layout", "blur"), clip.get("safe_margin"), quality=clip_quality)
    cur_w, cur_h = get_dimensions(layout, quality=clip_quality)
    face = _normalize_facecam(clip.get("facecam"), src_w, src_h, out_w=cur_w, out_h=cur_h)
    if layout == "pip" and not (face and len(face) >= 4):
        face = _normalize_facecam([10, 10, 40, 40, 0, 0], src_w, src_h, out_w=cur_w, out_h=cur_h)
    hook_y = _hook_y(layout, hook_y, face, out_w=cur_w, out_h=cur_h)
    text_overlays = clip.get("text_overlays") or []
    stickers = clip.get("stickers") or []
    v_filter = clip.get("filter")
    vol = float(clip.get("volume", 1.0))
    muted = bool(clip.get("muted", False))
    ass = build_ass(words if clip.get("captions", True) else [], start, end, clip.get("hook", ""), clip.get("style", "bounce"),
                    clip.get("highlight_words", []), hook_y, cap_y, text_overlays=text_overlays, stickers=stickers,
                    out_w=cur_w, out_h=cur_h)
    captions_requested = has_ass_events(ass)
    # The clip is made in an isolated writable work folder and moved into place afterwards.
    tmp_base = None
    try:
        config.TMP_DIR.mkdir(parents=True, exist_ok=True)
        if os.access(config.TMP_DIR, os.W_OK):
            tmp_base = str(config.TMP_DIR)
    except OSError:
        tmp_base = None
    safe_prefix = re.sub(r"[^A-Za-z0-9_-]", "_", str(name or "clip"))[:16] or "clip"
    work = Path(tempfile.mkdtemp(prefix=f"cf_{safe_prefix}_", dir=tmp_base))
    try:
        meta = {"title": clip.get("title") or "", "description": clip.get("description") or "", "comment": clip.get("description") or ""}
        return _render_in(work, src, out_dir, name, start, end, layout, fg_h, ass, face=face, meta=meta,
                          video_filter=v_filter, volume=vol, muted=muted, out_w=cur_w, out_h=cur_h,
                          captions_requested=captions_requested, fps=clip_fps, transition=clip_transition)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _render_in(work: Path, src: Path, out_dir: Path, name: str, start, end, layout, fg_h, ass: str, face=None, meta=None,
               video_filter=None, volume=1.0, muted=False, out_w=None, out_h=None, captions_requested: bool = True,
               fps=None, transition=None) -> dict:
    global _qsv_ok
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_name = _safe_work_stem(name)
    ass_path = work / f"{safe_name}.ass"
    ass_path.write_text(ass, encoding="utf-8")
    fonts_dir = work / "f"
    has_events = has_ass_events(ass)
    ass_ok = _tools.has_ass_filter()

    if has_events and not ass_ok:
        kinds = _ass_event_kinds(ass)
        items_str = ", ".join(kinds) if kinds else "captions, title hook, text overlays, or stickers"
        diag = _tools.get_subtitle_diagnostics()
        missing_str = ", ".join(diag.get("missing_dependencies") or ["libass"])
        raise RuntimeError(
            f"Render failed: Subtitle burning requires FFmpeg with 'ass' filter (libass) support; "
            f"cannot render burned-in {items_str}. "
            f"FFmpeg path: {diag.get('ffmpeg_path')}; version: {diag.get('ffmpeg_version')}; "
            f"ass filter: {diag.get('ass_filter')}; subtitles filter: {diag.get('subtitles_filter')}; "
            f"missing: {missing_str}. {diag.get('remediation')}"
        )

    _ensure_render_fonts(fonts_dir, required_fonts=_required_fonts_for_ass(ass) if has_events else None)

    dur = max(0.0, float(end) - float(start))
    sub_env = None
    if has_events:
        if not ass_path.is_file() or ass_path.stat().st_size == 0:
            raise RuntimeError(f"Render failed: Temporary subtitle file missing or empty: {ass_path}")
        conf_path = _write_android_fontconfig(work, fonts_dir)
        sub_env = _build_ffmpeg_subtitle_env(work, conf_path)
        esc_ass = escape_ffmpeg_filter_value(f"{safe_name}.ass")
        esc_fonts = escape_ffmpeg_filter_value("f")
        fc = _filter(layout, fg_h, face, video_filter=video_filter, out_w=out_w, out_h=out_h,
                     fps=fps, transition=transition, duration=dur) + f"[v0]ass={esc_ass}:fontsdir={esc_fonts}[v]"
    else:
        fc = _filter(layout, fg_h, face, video_filter=video_filter, out_w=out_w, out_h=out_h,
                     fps=fps, transition=transition, duration=dur) + "[v0]copy[v]"

    if muted or volume <= 0.001:
        af_chain = "volume=0"
    elif abs(volume - 1.0) > 0.01:
        af_chain = f"loudnorm=I=-14:TP=-1.5:LRA=11,volume={volume:.2f}"
    else:
        af_chain = "loudnorm=I=-14:TP=-1.5:LRA=11"
    if str(transition or "").lower() == "fade" and dur > 1.0 and not muted and volume > 0.001:
        if not _tools.get_supported_filters() or _tools.has_filter("afade"):
            fade_out_a = max(0.0, dur - 0.30)
            af_chain += f",afade=t=in:st=0:d=0.30,afade=t=out:st={fade_out_a:.2f}:d=0.30"

    base = [_tools.get_ffmpeg(), "-y", "-v", "error", "-ss", f"{start:.2f}", "-t", f"{end - start:.2f}", "-i", str(src),
            "-filter_complex", fc, "-map", "[v]", "-map", "0:a:0", "-af", af_chain,
            "-c:a", "aac", "-b:a", "160k", "-ar", "48000"]
    for k, v in (meta or {}).items():
        if v:
            base += ["-metadata", f"{k}={v}"]
    base += ["-movflags", "+faststart", "-pix_fmt"]
    crf_val = str(getattr(config, "OUT_CRF", 21))
    preset_val = str(getattr(config, "OUT_PRESET", "veryfast"))
    candidates = _tools.get_render_encoder_candidates(crf_val, preset_val)
    if _qsv_ok is False:
        candidates = [c for c in candidates if c[0] != "qsv"]

    err = None
    out_mp4 = f"{safe_name}.mp4"
    out_jpg = f"{safe_name}.jpg"
    for label, enc in candidates:
        p = subprocess.run(base + enc + [out_mp4], cwd=work, env=sub_env, capture_output=True, text=True,
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
    cover_t = min(1.2, max(0.1, (end - start) / 2))
    run(["ffmpeg", "-y", "-v", "error", "-ss", f"{cover_t:.2f}", "-i", out_mp4, "-frames:v", "1",
         "-q:v", "3", out_jpg], cwd=work)
    for ext in ("mp4", "jpg", "ass"):
        shutil.move(str(work / f"{safe_name}.{ext}"), str(out_dir / f"{safe_name}.{ext}"))
    return {"file": out_mp4, "cover": out_jpg, "layout": layout}
