#!/usr/bin/env bash
# Reproducible Android ARM64 (arm64-v8a) FFmpeg + libass build script.
# Cross-compiles static LGPL/BSD/MIT/ISC subtitle dependencies and FFmpeg 8.1.1
# targeting Android NDK API 24+ while preserving existing codecs and MediaCodec.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Pinned upstream sources and verified SHA-256 digests / git commits
EXPAT_VERSION="2.7.1"
EXPAT_ARCHIVE="expat-${EXPAT_VERSION}.tar.xz"
EXPAT_URL="https://github.com/libexpat/libexpat/releases/download/R_2_7_1/${EXPAT_ARCHIVE}"
EXPAT_SHA256="354552544b8f99012e5062f7d570ec77f14b412a3ff5c7d8d0dae62c0d217c30"

LIBPNG_VERSION="1.6.47"
LIBPNG_ARCHIVE="libpng-${LIBPNG_VERSION}.tar.xz"
LIBPNG_URL="https://downloads.sourceforge.net/project/libpng/libpng16/${LIBPNG_VERSION}/${LIBPNG_ARCHIVE}"
LIBPNG_SHA256="b213cb381fbb1175327bd708a77aab708a05adde7b471bc267bd15ac99893631"

FREETYPE_VERSION="2.13.3"
FREETYPE_ARCHIVE="freetype-${FREETYPE_VERSION}.tar.xz"
FREETYPE_URL="https://download.savannah.gnu.org/releases/freetype/${FREETYPE_ARCHIVE}"
FREETYPE_SHA256="0550350666d427c74daeb85d5ac7bb353acba5f76956395995311a9c6f063289"

FRIBIDI_VERSION="1.0.16"
FRIBIDI_ARCHIVE="fribidi-${FRIBIDI_VERSION}.tar.xz"
FRIBIDI_URL="https://github.com/fribidi/fribidi/releases/download/v${FRIBIDI_VERSION}/${FRIBIDI_ARCHIVE}"
FRIBIDI_SHA256="1b1cde5b235d40479e91be2f0e88a309e3214c8ab470ec8a2744d82a5a9ea05c"

HARFBUZZ_VERSION="10.2.0"
HARFBUZZ_ARCHIVE="harfbuzz-${HARFBUZZ_VERSION}.tar.xz"
HARFBUZZ_URL="https://github.com/harfbuzz/harfbuzz/releases/download/${HARFBUZZ_VERSION}/${HARFBUZZ_ARCHIVE}"
HARFBUZZ_SHA256="620e3468faec2ea8685d32c46a58469b850ef63040b3565cde05959825b48227"

FONTCONFIG_VERSION="2.16.0"
FONTCONFIG_ARCHIVE="fontconfig-${FONTCONFIG_VERSION}.tar.xz"
FONTCONFIG_URL="https://www.freedesktop.org/software/fontconfig/release/${FONTCONFIG_ARCHIVE}"
FONTCONFIG_SHA256="6a33dc555cc9ba8b10caf7695878ef134eeb36d0af366041f639b1da9b6ed220"

LIBUNIBREAK_VERSION="6.1"
LIBUNIBREAK_ARCHIVE="libunibreak-${LIBUNIBREAK_VERSION}.tar.gz"
LIBUNIBREAK_URL="https://github.com/adah1972/libunibreak/releases/download/libunibreak_6_1/${LIBUNIBREAK_ARCHIVE}"
LIBUNIBREAK_SHA256="cc4de0099cf7ff05005ceabff4afed4c582a736abc38033e70fdac86335ce93f"

LIBASS_VERSION="0.17.4"
LIBASS_ARCHIVE="libass-${LIBASS_VERSION}.tar.xz"
LIBASS_URL="https://github.com/libass/libass/releases/download/${LIBASS_VERSION}/${LIBASS_ARCHIVE}"
LIBASS_SHA256="78f1179b838d025e9c26e8fef33f8092f65611444ffa1bfc0cfac6a33511a05a"

FFMPEG_VERSION="8.1.1"
FFMPEG_GIT_URL="https://github.com/FFmpeg/FFmpeg.git"
FFMPEG_TAG="n8.1.1"
FFMPEG_COMMIT="239f2c733de417201d7ad3b3b8b0d9b63285b2b1"

API_LEVEL="${ANDROID_API_LEVEL:-24}"
JOBS="${JOBS:-$(nproc 2>/dev/null || echo 4)}"
OUTPUT_DIR="${OUTPUT_DIR:-$REPO_ROOT/build/android-ffmpeg-arm64/out}"
WORK_DIR="${WORK_DIR:-$REPO_ROOT/build/android-ffmpeg-arm64/work}"
DOWNLOAD_DIR="${DOWNLOAD_DIR:-$REPO_ROOT/build/android-ffmpeg-arm64/downloads}"
MANIFEST_PATH=""
VERIFY_ONLY=0
KEEP_WORK=0

usage() {
  cat <<EOF
Usage: $(basename "$0") [options]

Options:
  --output-dir DIR     Directory to write libffmpeg.so and libffprobe.so
                       (default: build/android-ffmpeg-arm64/out)
  --work-dir DIR       Scratch build directory
  --download-dir DIR   Cached source tarball directory
  --manifest PATH      Optional path to write build verification manifest
  --api-level N        Android NDK API level (default: 24)
  --jobs N             Parallel build jobs (default: nproc)
  --verify-only        Verify existing libffmpeg.so and libffprobe.so in --output-dir
  --keep-work          Preserve build tree after completion
  -h, --help           Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
    --work-dir) WORK_DIR="$2"; shift 2 ;;
    --download-dir) DOWNLOAD_DIR="$2"; shift 2 ;;
    --manifest) MANIFEST_PATH="$2"; shift 2 ;;
    --api-level) API_LEVEL="$2"; shift 2 ;;
    --jobs) JOBS="$2"; shift 2 ;;
    --verify-only) VERIFY_ONLY=1; shift 1 ;;
    --keep-work) KEEP_WORK=1; shift 1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "ERROR: Unknown argument: $1" >&2; usage >&2; exit 1 ;;
  esac
done

resolve_ndk() {
  local candidate=""
  for candidate in \
    "${ANDROID_NDK_ROOT:-}" \
    "${ANDROID_NDK_HOME:-}" \
    "${ANDROID_NDK_LATEST_HOME:-}" \
    "${NDK_DIR:-}"
  do
    if [[ -n "$candidate" && -d "$candidate/toolchains/llvm/prebuilt" ]]; then
      echo "$candidate"
      return 0
    fi
  done

  local sdk_root="${ANDROID_SDK_ROOT:-${ANDROID_HOME:-}}"
  if [[ -n "$sdk_root" && -d "$sdk_root/ndk" ]]; then
    candidate="$(find "$sdk_root/ndk" -mindepth 1 -maxdepth 1 -type d | sort -V | tail -n 1)"
    if [[ -n "$candidate" && -d "$candidate/toolchains/llvm/prebuilt" ]]; then
      echo "$candidate"
      return 0
    fi
  fi

  return 1
}

find_readelf() {
  local ndk_toolchain="${1:-}"
  if [[ -n "$ndk_toolchain" && -x "$ndk_toolchain/bin/llvm-readelf" ]]; then
    echo "$ndk_toolchain/bin/llvm-readelf"
    return 0
  fi
  for tool in llvm-readelf readelf; do
    if command -v "$tool" >/dev/null 2>&1; then
      command -v "$tool"
      return 0
    fi
  done
  return 1
}

verify_binary_artifacts() {
  local out_dir="$1"
  local readelf_bin="$2"
  local ffmpeg_bin="$out_dir/libffmpeg.so"
  local ffprobe_bin="$out_dir/libffprobe.so"

  for bin in "$ffmpeg_bin" "$ffprobe_bin"; do
    if [[ ! -f "$bin" ]]; then
      echo "ERROR: Missing output binary: $bin" >&2
      return 1
    fi
    local size_bytes
    size_bytes="$(wc -c < "$bin")"
    if [[ "$size_bytes" -lt 4000000 ]]; then
      echo "ERROR: Binary unexpectedly small ($size_bytes bytes): $bin" >&2
      return 1
    fi

    local elf_class elf_machine elf_type interp phdrs_out
    elf_class="$("$readelf_bin" -h "$bin" | awk '/Class:/ {print $2}')"
    elf_machine="$("$readelf_bin" -h "$bin" | awk '/Machine:/ {print $2}')"
    elf_type="$("$readelf_bin" -h "$bin" | awk '/Type:/ {print $2}')"
    phdrs_out="$("$readelf_bin" -lW "$bin")"
    interp="$(grep -o "/system/bin/linker64" <<< "$phdrs_out" | head -n 1 || true)"

    echo "Verified ELF ($(basename "$bin")): Class=$elf_class, Machine=$elf_machine, Type=$elf_type, Interp=$interp, Size=${size_bytes}B"
    if [[ "$elf_class" != "ELF64" || "$elf_machine" != "AArch64" || "$elf_type" != "DYN" || "$interp" != "/system/bin/linker64" ]]; then
      echo "ERROR: $bin failed Android ARM64 PIE executable verification!" >&2
      return 1
    fi

    python3 - "$bin" "$phdrs_out" <<'PY'
import sys

data = sys.argv[2].splitlines()
loads = []
for line in data:
    stripped = line.strip()
    if stripped.startswith("LOAD "):
        parts = stripped.split()
        if not parts:
            continue
        align = int(parts[-1], 0)
        loads.append(align)

if not loads:
    raise SystemExit(f"ERROR: No PT_LOAD segments found in {sys.argv[1]}")
min_align = min(loads)
if min_align < 16384:
    raise SystemExit(f"ERROR: PT_LOAD alignment {min_align} < 16384 bytes in {sys.argv[1]} (16 KB page alignment required)")
print(f"Verified PT_LOAD alignment ({sys.argv[1]}): min={min_align}B, count={len(loads)}")
PY

    local needed
    needed="$("$readelf_bin" -d "$bin" | sed -n 's/.*Shared library: \[\(.*\)\]/\1/p')"
    if [[ -z "$needed" ]]; then
      echo "ERROR: Could not parse NEEDED dynamic libraries from $bin" >&2
      return 1
    fi
    while IFS= read -r lib; do
      [[ -z "$lib" ]] && continue
      case "$lib" in
        libc.so|libm.so|libdl.so|libz.so|libandroid.so|libmediandk.so|libcamera2ndk.so)
          ;;
        *)
          echo "ERROR: Unexpected non-system dynamic dependency in $(basename "$bin"): $lib" >&2
          echo "All third-party libraries (libass, freetype, libpng, fribidi, harfbuzz, fontconfig, expat, unibreak, C++ runtime) must be statically linked." >&2
          return 1
          ;;
      esac
    done <<< "$needed"
  done

  python3 - "$ffmpeg_bin" "$ffprobe_bin" <<'PY'
import sys
from pathlib import Path

ff_bytes = Path(sys.argv[1]).read_bytes()
fp_bytes = Path(sys.argv[2]).read_bytes()

required_ff_cfg = [
    b"--disable-gpl",
    b"--disable-nonfree",
    b"--disable-iconv",
    b"--enable-mediacodec",
    b"--enable-libass",
    b"--enable-libfreetype",
    b"--enable-libfribidi",
    b"--enable-libharfbuzz",
    b"--enable-fontconfig",
    b"libavfilter license: LGPL",
]
for needle in required_ff_cfg:
    if needle not in ff_bytes:
        raise SystemExit(f"ERROR: Missing expected build/license string {needle!r} in {sys.argv[1]}")

for needle in (b"--disable-gpl", b"--enable-mediacodec", b"--enable-libass"):
    if needle not in fp_bytes:
        raise SystemExit(f"ERROR: Missing expected build string {needle!r} in {sys.argv[2]}")

if b"--enable-gpl" in ff_bytes or b"--enable-gpl" in fp_bytes:
    raise SystemExit("ERROR: --enable-gpl detected; LGPL-compatible build required.")

required_filters = [
    b"\x00ass\x00",
    b"\x00subtitles\x00",
    b"\x00drawtext\x00",
    b"\x00avgblur\x00",
    b"\x00gblur\x00",
    b"\x00colorlevels\x00",
    b"\x00huesaturation\x00",
    b"\x00hue\x00",
    b"\x00colorbalance\x00",
    b"\x00colorchannelmixer\x00",
    b"\x00scale\x00",
    b"\x00crop\x00",
    b"\x00pad\x00",
    b"\x00overlay\x00",
    b"\x00vstack\x00",
    b"\x00loudnorm\x00",
    b"\x00volume\x00",
    b"\x00volumedetect\x00",
]
for flt in required_filters:
    if flt not in ff_bytes:
        raise SystemExit(f"ERROR: Missing registered filter string {flt!r} inside {sys.argv[1]}")
PY

  echo "Static-link, 16KB PT_LOAD alignment, LGPL, PIE, and libass/filter verification passed for libffmpeg.so and libffprobe.so."
}

if [[ "$VERIFY_ONLY" -eq 1 ]]; then
  NDK_MAYBE="$(resolve_ndk || true)"
  TOOLCHAIN_MAYBE=""
  if [[ -n "$NDK_MAYBE" ]]; then
    TOOLCHAIN_MAYBE="$(find "$NDK_MAYBE/toolchains/llvm/prebuilt" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
  fi
  READELF_BIN="$(find_readelf "$TOOLCHAIN_MAYBE")" || {
    echo "ERROR: Could not locate llvm-readelf or readelf for verification." >&2
    exit 1
  }
  verify_binary_artifacts "$OUTPUT_DIR" "$READELF_BIN"
  exit 0
fi

MIN_MESON_VERSION="1.6.0"

verify_meson_version() {
  local meson_bin="${1:-meson}"
  local min_ver="${2:-$MIN_MESON_VERSION}"
  local actual_ver
  actual_ver="$("$meson_bin" --version | head -n 1 | tr -d '[:space:]')"
  python3 - "$actual_ver" "$min_ver" <<'PY'
import re, sys

def parse_ver(raw: str) -> tuple[int, ...]:
    nums = [int(x) for x in re.findall(r"\d+", raw)[:3]]
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums)

actual, minimum = sys.argv[1], sys.argv[2]
if not actual or parse_ver(actual) < parse_ver(minimum):
    raise SystemExit(
        f"ERROR: Meson >= {minimum} is required (Fontconfig 2.16.0 requires >= {minimum}), "
        f"found '{actual or 'unknown'}'. Install a newer Meson (e.g. python3 -m pip install meson==1.7.2)."
    )
print(f"Verified Meson version: {actual} (>= {minimum})")
PY
}

for req in curl tar git meson ninja pkg-config gperf make sha256sum python3; do
  if ! command -v "$req" >/dev/null 2>&1; then
    echo "ERROR: Missing required build tool: $req" >&2
    exit 1
  fi
done
verify_meson_version meson "$MIN_MESON_VERSION"

NDK_DIR="$(resolve_ndk)" || {
  echo "ERROR: Android NDK not found. Set ANDROID_NDK_ROOT, ANDROID_NDK_HOME, or ANDROID_NDK_LATEST_HOME." >&2
  exit 1
}

HOST_TAG=""
case "$(uname -s)-$(uname -m)" in
  Linux-x86_64) HOST_TAG="linux-x86_64" ;;
  Darwin-arm64|Darwin-x86_64) HOST_TAG="darwin-x86_64" ;;
  *) HOST_TAG="$(basename "$(find "$NDK_DIR/toolchains/llvm/prebuilt" -mindepth 1 -maxdepth 1 -type d | head -n 1)")" ;;
esac

TOOLCHAIN="$NDK_DIR/toolchains/llvm/prebuilt/$HOST_TAG"
if [[ ! -d "$TOOLCHAIN/bin" ]]; then
  echo "ERROR: NDK LLVM toolchain not found under: $TOOLCHAIN" >&2
  exit 1
fi

CC="$TOOLCHAIN/bin/aarch64-linux-android${API_LEVEL}-clang"
CXX="$TOOLCHAIN/bin/aarch64-linux-android${API_LEVEL}-clang++"
AR="$TOOLCHAIN/bin/llvm-ar"
NM="$TOOLCHAIN/bin/llvm-nm"
RANLIB="$TOOLCHAIN/bin/llvm-ranlib"
STRIP="$TOOLCHAIN/bin/llvm-strip"
READELF="$TOOLCHAIN/bin/llvm-readelf"
SYSROOT="$TOOLCHAIN/sysroot"
HOST_CC="${HOST_CC:-cc}"

for tool in "$CC" "$CXX" "$AR" "$NM" "$RANLIB" "$STRIP" "$READELF"; do
  if [[ ! -x "$tool" ]]; then
    echo "ERROR: Required NDK binary missing or not executable: $tool" >&2
    exit 1
  fi
done

mkdir -p "$OUTPUT_DIR" "$WORK_DIR" "$DOWNLOAD_DIR"
OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd)"
WORK_DIR="$(cd "$WORK_DIR" && pwd)"
DOWNLOAD_DIR="$(cd "$DOWNLOAD_DIR" && pwd)"

if [[ "$KEEP_WORK" -eq 0 ]]; then
  cleanup() {
    rm -rf "$WORK_DIR/src" "$WORK_DIR/prefix" "$WORK_DIR/cross" "$WORK_DIR/fontconfig-stage"
  }
  trap cleanup EXIT
fi

rm -rf "$WORK_DIR/src" "$WORK_DIR/prefix" "$WORK_DIR/cross" "$WORK_DIR/fontconfig-stage"
SRC_DIR="$WORK_DIR/src"
PREFIX="$WORK_DIR/prefix"
CROSS_FILE="$WORK_DIR/cross/android-aarch64.ini"
mkdir -p "$SRC_DIR" "$PREFIX/lib/pkgconfig" "$WORK_DIR/cross"

cat > "$CROSS_FILE" <<EOF
[binaries]
c = '$CC'
cpp = '$CXX'
ar = '$AR'
strip = '$STRIP'
pkg-config = 'pkg-config'

[built-in options]
c_args = ['-fPIC', '-Os', '-I$PREFIX/include']
cpp_args = ['-fPIC', '-Os', '-I$PREFIX/include']
c_link_args = ['-L$PREFIX/lib']
cpp_link_args = ['-L$PREFIX/lib']

[host_machine]
system = 'android'
cpu_family = 'aarch64'
cpu = 'armv8-a'
endian = 'little'
EOF

# Isolate pkg-config so host distribution .pc files never leak into ARM64 Android builds.
export PKG_CONFIG_LIBDIR="$PREFIX/lib/pkgconfig:$PREFIX/share/pkgconfig"
export PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig:$PREFIX/share/pkgconfig"
export ACLOCAL_PATH="$PREFIX/share/aclocal"

fetch_and_verify() {
  local archive="$1"
  local url="$2"
  local expected_sha="$3"
  local dest="$DOWNLOAD_DIR/$archive"

  if [[ -f "$dest" ]]; then
    local cached_sha
    cached_sha="$(sha256sum "$dest" | awk '{print $1}')"
    if [[ "$cached_sha" != "$expected_sha" ]]; then
      echo "Cached $archive checksum mismatch ($cached_sha != $expected_sha); re-downloading..."
      rm -f "$dest"
    fi
  fi

  if [[ ! -f "$dest" ]]; then
    echo "Downloading $archive..."
    curl -fL --retry 4 --retry-delay 3 -o "$dest" "$url"
  fi

  local actual_sha
  actual_sha="$(sha256sum "$dest" | awk '{print $1}')"
  if [[ "$actual_sha" != "$expected_sha" ]]; then
    echo "ERROR: SHA-256 verification failed for $archive" >&2
    echo "Expected: $expected_sha" >&2
    echo "Actual:   $actual_sha" >&2
    rm -f "$dest"
    exit 1
  fi
  echo "Verified $archive ($actual_sha)"
}

extract_tarball() {
  local archive="$1"
  tar -xf "$DOWNLOAD_DIR/$archive" -C "$SRC_DIR"
}

echo "=== Building ClipFlow ARM64 FFmpeg ${FFMPEG_VERSION} + libass (${LIBASS_VERSION}) stack ==="
echo "NDK:        $NDK_DIR"
echo "API Level:  $API_LEVEL"
echo "Prefix:     $PREFIX"
echo "Output Dir: $OUTPUT_DIR"

# Synthesize pkg-config metadata for Android NDK sysroot zlib (required privately by libpng/freetype)
ZLIB_VER="$(awk '/#define ZLIB_VERSION/ {gsub(/"/, "", $3); print $3; exit}' "$SYSROOT/usr/include/zlib.h" 2>/dev/null || echo "1.2.13")"
cat > "$PREFIX/lib/pkgconfig/zlib.pc" <<EOF
Name: zlib
Description: Android NDK sysroot zlib
Version: ${ZLIB_VER:-1.2.13}
Libs: -lz
EOF

# 1. Expat (XML parser for Fontconfig)
fetch_and_verify "$EXPAT_ARCHIVE" "$EXPAT_URL" "$EXPAT_SHA256"
extract_tarball "$EXPAT_ARCHIVE"
(
  cd "$SRC_DIR/expat-${EXPAT_VERSION}"
  CC="$CC" AR="$AR" RANLIB="$RANLIB" STRIP="$STRIP" \
  CFLAGS="-fPIC -Os" CPPFLAGS="-fPIC -Os" \
  ./configure \
    --host=aarch64-linux-android \
    --prefix="$PREFIX" \
    --libdir="$PREFIX/lib" \
    --enable-static \
    --disable-shared \
    --without-xmlwf \
    --without-examples \
    --without-tests \
    --without-docbook
  make -j"$JOBS"
  make install
)

# 2. libpng (required by FreeType for Android NotoColorEmoji CBDT/CBLC bitmap PNG glyphs)
fetch_and_verify "$LIBPNG_ARCHIVE" "$LIBPNG_URL" "$LIBPNG_SHA256"
extract_tarball "$LIBPNG_ARCHIVE"
(
  cd "$SRC_DIR/libpng-${LIBPNG_VERSION}"
  CC="$CC" AR="$AR" RANLIB="$RANLIB" STRIP="$STRIP" \
  CFLAGS="-fPIC -Os" CPPFLAGS="-fPIC -Os" \
  ./configure \
    --host=aarch64-linux-android \
    --prefix="$PREFIX" \
    --libdir="$PREFIX/lib" \
    --enable-static \
    --disable-shared \
    --enable-arm-neon=on
  make -j"$JOBS"
  make install
)
echo "libpng.pc static flags: $(pkg-config --static --cflags --libs libpng)"
"$NM" --defined-only -g "$PREFIX/lib/libpng16.a" | grep -Eq "[[:space:]]png_create_read_struct$" || {
  echo "ERROR: libpng16.a missing png_create_read_struct!" >&2
  exit 1
}

# 3. FreeType (font rasterizer with zlib + libpng bitmap emoji support)
fetch_and_verify "$FREETYPE_ARCHIVE" "$FREETYPE_URL" "$FREETYPE_SHA256"
extract_tarball "$FREETYPE_ARCHIVE"
(
  cd "$SRC_DIR/freetype-${FREETYPE_VERSION}"
  CC_BUILD="$HOST_CC" CC="$CC" AR="$AR" RANLIB="$RANLIB" STRIP="$STRIP" \
  CFLAGS="-fPIC -Os" \
  ./configure \
    --host=aarch64-linux-android \
    --prefix="$PREFIX" \
    --libdir="$PREFIX/lib" \
    --enable-static \
    --disable-shared \
    --with-zlib=yes \
    --with-bzip2=no \
    --with-png=yes \
    --with-harfbuzz=no \
    --with-brotli=no
  make -j"$JOBS"
  make install
)
FT_OPTION_H="$(find "$PREFIX/include" -name ftoption.h | head -n 1)"
if [[ -z "$FT_OPTION_H" ]] || ! grep -Eq "^#define[[:space:]]+FT_CONFIG_OPTION_USE_PNG" "$FT_OPTION_H"; then
  echo "ERROR: FreeType was built without FT_CONFIG_OPTION_USE_PNG enabled!" >&2
  exit 1
fi
echo "freetype2.pc static flags: $(pkg-config --static --cflags --libs freetype2)"

# 4. FriBidi (bidirectional script layout)
fetch_and_verify "$FRIBIDI_ARCHIVE" "$FRIBIDI_URL" "$FRIBIDI_SHA256"
extract_tarball "$FRIBIDI_ARCHIVE"
(
  cd "$SRC_DIR/fribidi-${FRIBIDI_VERSION}"
  meson setup build-android \
    --cross-file "$CROSS_FILE" \
    --prefix="$PREFIX" \
    --libdir=lib \
    --default-library=static \
    -Dprefer_static=true \
    -Dbuildtype=release \
    -Ddocs=false \
    -Dbin=false \
    -Dtests=false
  ninja -C build-android -j"$JOBS"
  ninja -C build-android install
)

# 5. HarfBuzz (complex text shaping; inspect pthread vs C++ ABI linkage empirically)
fetch_and_verify "$HARFBUZZ_ARCHIVE" "$HARFBUZZ_URL" "$HARFBUZZ_SHA256"
extract_tarball "$HARFBUZZ_ARCHIVE"
(
  cd "$SRC_DIR/harfbuzz-${HARFBUZZ_VERSION}"
  meson setup build-android \
    --cross-file "$CROSS_FILE" \
    --prefix="$PREFIX" \
    --libdir=lib \
    --default-library=static \
    -Dprefer_static=true \
    -Dbuildtype=release \
    -Dfreetype=enabled \
    -Dglib=disabled \
    -Dgobject=disabled \
    -Dcairo=disabled \
    -Dchafa=disabled \
    -Dicu=disabled \
    -Dgraphite2=disabled \
    -Dgdi=disabled \
    -Ddirectwrite=disabled \
    -Dcoretext=disabled \
    -Dwasm=disabled \
    -Dtests=disabled \
    -Ddocs=disabled \
    -Dintrospection=disabled \
    -Dutilities=disabled \
    -Dbenchmark=disabled
  ninja -C build-android -j"$JOBS"
  ninja -C build-android install
  grep -Eq "^#define[[:space:]]+HAVE_PTHREAD[[:space:]]+1" build-android/config.h || {
    echo "WARNING: HarfBuzz built without HAVE_PTHREAD; C++ std::mutex may require static libc++." >&2
  }
)
echo "harfbuzz.pc static flags: $(pkg-config --static --cflags --libs harfbuzz)"

detect_archive_external_cxx_symbols() {
  local archive="$1"
  local nm_bin="${NM:-nm}"
  # Resolve intra-archive references across all members before classifying any
  # remaining undefined C++ ABI/runtime symbols, so internal cross-object calls
  # (e.g. _Z12hb_parse_intPPKcS0_Pib) do not trigger a false C++ fallback.
  # Exclude __cxa_atexit, __cxa_finalize, and __cxa_thread_atexit_impl because
  # Android Bionic libc exports them natively on API 24 without libstdc++/libc++.
  LC_ALL=C awk '
    FILENAME == ARGV[1] {
      if (NF >= 3 && $2 ~ /^[A-Za-z]$/) {
        defined[$3] = 1
      }
      next
    }
    ($1 == "U" || $1 == "w") && NF >= 2 {
      sym = $2
      if (!(sym in defined)) {
        base = sym
        sub(/@.*/, "", base)
        if (base ~ /^(__cxa_atexit|__cxa_finalize|__cxa_thread_atexit_impl)$/) {
          next
        }
        if (base ~ /^(__cxa_|__gxx_|_ZNSt|_ZNKSt|_ZSt|_ZNS|_ZNKS|_Znw|_Zna|_Zdl|_Zda|_ZTV|_ZTI|_ZTS|_ZTC|_ZTT|_ZGV)/ || base ~ /(std::|__ndk1|__cxxabiv1)/) {
          if (!(sym in seen)) {
            seen[sym] = 1
            print sym
          }
        }
      }
    }
  ' <("$nm_bin" --defined-only -g "$archive") <("$nm_bin" -u "$archive")
}

HB_UNDEF_CXX="$(detect_archive_external_cxx_symbols "$PREFIX/lib/libharfbuzz.a" || true)"
cat > "$WORK_DIR/hb_c_probe.c" <<'C'
#include "hb.h"
int main(void) {
  hb_buffer_t *b = hb_buffer_create();
  hb_buffer_destroy(b);
  return 0;
}
C
HB_STATIC_FLAGS="$(pkg-config --static --cflags --libs harfbuzz)"
HARFBUZZ_CXX_RUNTIME="none"
FFMPEG_LD_ARGS=(--extra-ldflags="-L$PREFIX/lib -Wl,-z,max-page-size=16384")
# shellcheck disable=SC2086
if [[ -z "$HB_UNDEF_CXX" ]] && "$CC" -fPIE -pie "$WORK_DIR/hb_c_probe.c" $HB_STATIC_FLAGS -lm -o "$WORK_DIR/hb_c_probe"; then
  echo "Verified libharfbuzz.a resolves cleanly with C driver ($CC) and zero C++ ABI dependencies."
else
  echo "HarfBuzz requires Android static C++ runtime; switching FFmpeg linker to $CXX -static-libstdc++."
  echo "Detected C++ ABI refs: ${HB_UNDEF_CXX:-probe-link-failed}"
  HARFBUZZ_CXX_RUNTIME="static-libstdc++"
  FFMPEG_LD_ARGS=(--ld="$CXX" --extra-ldflags="-L$PREFIX/lib -static-libstdc++ -Wl,-z,max-page-size=16384")
  # shellcheck disable=SC2086
  "$CXX" -fPIE -pie -static-libstdc++ "$WORK_DIR/hb_c_probe.c" $HB_STATIC_FLAGS -lm -o "$WORK_DIR/hb_c_probe"
  "$READELF" -d "$WORK_DIR/hb_c_probe" | grep -q "libc++_shared.so" && {
    echo "ERROR: Probe binary dynamically linked libc++_shared.so!" >&2
    exit 1
  }
fi
rm -f "$WORK_DIR/hb_c_probe.c" "$WORK_DIR/hb_c_probe"

stage_fontconfig_devel() {
  local build_dir="$1"
  local prefix_dir="$2"
  local stage_dir="$3"
  local meson_bin="${MESON:-meson}"

  rm -rf "$stage_dir"
  mkdir -p "$stage_dir"
  # Install ONLY 'devel' artifacts (libfontconfig.a, headers, fontconfig.pc) inside an
  # isolated DESTDIR staging tree so runtime rules (fonts.conf, conf.d symlinks, README)
  # never attempt writes into Android /system/etc/fonts or host /etc/fonts directories.
  DESTDIR="$stage_dir" "$meson_bin" install -C "$build_dir" --no-rebuild --tags devel

  if [[ -e "$stage_dir/system" || -e "$stage_dir/etc" ]]; then
    echo "ERROR: Fontconfig staged install wrote forbidden host system paths under $stage_dir!" >&2
    find "$stage_dir" >&2
    return 1
  fi
  local symlinks
  symlinks="$(find "$stage_dir" -type l)"
  if [[ -n "$symlinks" ]]; then
    echo "ERROR: Fontconfig staged install created unexpected symlink(s):" >&2
    echo "$symlinks" >&2
    return 1
  fi
  if [[ ! -d "$stage_dir$prefix_dir" ]]; then
    echo "ERROR: Staged Fontconfig prefix directory missing: $stage_dir$prefix_dir" >&2
    return 1
  fi

  local unexpected_paths
  unexpected_paths="$(find "$stage_dir" \( -type f -o -type l \) ! -path "$stage_dir$prefix_dir/*")"
  if [[ -n "$unexpected_paths" ]]; then
    echo "ERROR: Fontconfig staged install wrote files outside staged prefix ($prefix_dir):" >&2
    echo "$unexpected_paths" >&2
    return 1
  fi

  cp -a "$stage_dir$prefix_dir/." "$prefix_dir/"
  rm -rf "$stage_dir"

  for fc_req in \
    "$prefix_dir/lib/libfontconfig.a" \
    "$prefix_dir/lib/pkgconfig/fontconfig.pc" \
    "$prefix_dir/include/fontconfig/fontconfig.h"
  do
    if [[ ! -s "$fc_req" ]]; then
      echo "ERROR: Missing expected Fontconfig devel artifact after staging: $fc_req" >&2
      return 1
    fi
  done
}

# 6. Fontconfig (fallback resolution across bundled fonts and /system/fonts)
fetch_and_verify "$FONTCONFIG_ARCHIVE" "$FONTCONFIG_URL" "$FONTCONFIG_SHA256"
extract_tarball "$FONTCONFIG_ARCHIVE"
FC_STAGE="$WORK_DIR/fontconfig-stage"
(
  cd "$SRC_DIR/fontconfig-${FONTCONFIG_VERSION}"
  meson setup build-android \
    --cross-file "$CROSS_FILE" \
    --prefix="$PREFIX" \
    --libdir=lib \
    --default-library=static \
    -Dprefer_static=true \
    -Dbuildtype=release \
    -Ddoc=disabled \
    -Ddoc-txt=disabled \
    -Ddoc-man=disabled \
    -Ddoc-pdf=disabled \
    -Ddoc-html=disabled \
    -Dnls=disabled \
    -Dtests=disabled \
    -Dtools=disabled \
    -Dcache-build=disabled \
    -Diconv=disabled \
    -Dxml-backend=expat \
    -Dfontations=disabled \
    -Ddefault-fonts-dirs=/system/fonts \
    -Dadditional-fonts-dirs=no \
    -Dcache-dir=default \
    -Dbaseconfig-dir=/system/etc/fonts
  ninja -C build-android -j"$JOBS"
  stage_fontconfig_devel build-android "$PREFIX" "$FC_STAGE"
)
FC_STATIC_LIBS="$(pkg-config --static --libs fontconfig)"
echo "fontconfig.pc static flags: $(pkg-config --static --cflags --libs fontconfig)"
for needed_flag in -lfontconfig -lexpat -lfreetype -lpng16 -lz; do
  if ! grep -Fq -- "$needed_flag" <<< "$FC_STATIC_LIBS"; then
    echo "ERROR: Expected transitive static flag '$needed_flag' missing in fontconfig pkg-config output: $FC_STATIC_LIBS" >&2
    exit 1
  fi
done

# 7. libunibreak (Unicode line breaking for libass 0.17.4)
fetch_and_verify "$LIBUNIBREAK_ARCHIVE" "$LIBUNIBREAK_URL" "$LIBUNIBREAK_SHA256"
extract_tarball "$LIBUNIBREAK_ARCHIVE"
(
  cd "$SRC_DIR/libunibreak-${LIBUNIBREAK_VERSION}"
  CC="$CC" AR="$AR" RANLIB="$RANLIB" STRIP="$STRIP" \
  CFLAGS="-fPIC -Os" \
  ./configure \
    --host=aarch64-linux-android \
    --prefix="$PREFIX" \
    --libdir="$PREFIX/lib" \
    --enable-static \
    --disable-shared
  make -j"$JOBS"
  make install
)

# 8. libass (ASS/SSA subtitle renderer + Fontconfig + HarfBuzz + FriBidi + FreeType + libpng + libunibreak)
fetch_and_verify "$LIBASS_ARCHIVE" "$LIBASS_URL" "$LIBASS_SHA256"
extract_tarball "$LIBASS_ARCHIVE"
(
  cd "$SRC_DIR/libass-${LIBASS_VERSION}"
  # Disable legacy iconv detection cleanly on API 24 via libass configure's actual cache vars; ClipFlow subtitle tracks are UTF-8.
  CC="$CC" CCAS="$CC" AR="$AR" RANLIB="$RANLIB" STRIP="$STRIP" \
  ac_cv_header_iconv_h=no ac_cv_search_libiconv_open=no ac_cv_search_iconv_open=no \
  CFLAGS="-fPIC -Os -I$PREFIX/include" LDFLAGS="-L$PREFIX/lib" \
  ./configure \
    --host=aarch64-linux-android \
    --prefix="$PREFIX" \
    --libdir="$PREFIX/lib" \
    --enable-static \
    --disable-shared \
    --enable-fontconfig \
    --enable-libunibreak \
    --disable-test \
    --disable-compare
  make -j"$JOBS"
  make install
)

LIBASS_STATIC_LIBS="$(pkg-config --static --libs libass)"
echo "Resolved static libass flags: $(pkg-config --static --cflags --libs libass)"
for needed_flag in -lass -lfontconfig -lexpat -lunibreak -lharfbuzz -lfreetype -lpng16 -lfribidi -lz -lm; do
  if ! grep -Fq -- "$needed_flag" <<< "$LIBASS_STATIC_LIBS"; then
    echo "ERROR: Expected transitive static flag '$needed_flag' missing in libass pkg-config output: $LIBASS_STATIC_LIBS" >&2
    exit 1
  fi
done

# 8. FFmpeg (pinned tag n8.1.1 -> exact commit)
FFMPEG_SRC="$SRC_DIR/ffmpeg-${FFMPEG_VERSION}"
git clone --depth 1 --branch "$FFMPEG_TAG" "$FFMPEG_GIT_URL" "$FFMPEG_SRC"
(
  cd "$FFMPEG_SRC"
  actual_commit="$(git rev-parse HEAD)"
  if [[ "$actual_commit" != "$FFMPEG_COMMIT" ]]; then
    echo "ERROR: FFmpeg commit mismatch! Expected $FFMPEG_COMMIT, got $actual_commit" >&2
    exit 1
  fi
  echo "Verified FFmpeg $FFMPEG_TAG commit ($actual_commit)"
  echo "$FFMPEG_VERSION" > VERSION

  ./configure \
    --prefix="$PREFIX/ffmpeg-out" \
    --disable-gpl \
    --disable-nonfree \
    --enable-static \
    --disable-shared \
    --enable-small \
    --enable-pic \
    --disable-doc \
    --disable-ffplay \
    --enable-ffmpeg \
    --enable-ffprobe \
    --disable-vulkan \
    --enable-runtime-cpudetect \
    --enable-gray \
    --enable-swscale-alpha \
    --enable-avdevice \
    --enable-avcodec \
    --enable-avformat \
    --enable-avutil \
    --enable-swresample \
    --enable-swscale \
    --enable-avfilter \
    --enable-network \
    --disable-d3d11va \
    --disable-dxva2 \
    --disable-vaapi \
    --disable-vdpau \
    --disable-videotoolbox \
    --disable-audiotoolbox \
    --enable-encoders \
    --enable-decoders \
    --enable-hwaccels \
    --enable-muxers \
    --enable-demuxers \
    --enable-parsers \
    --enable-bsfs \
    --enable-protocols \
    --enable-filters \
    --disable-iconv \
    --disable-bzlib \
    --enable-linux-perf \
    --enable-jni \
    --enable-mediacodec \
    --enable-decoder=h264_mediacodec \
    --enable-decoder=hevc_mediacodec \
    --enable-decoder=mpeg4_mediacodec \
    --enable-fontconfig \
    --enable-libfreetype \
    --enable-libfribidi \
    --enable-libharfbuzz \
    --enable-libass \
    --target-os=android \
    --arch=aarch64 \
    --cpu=armv8-a \
    --enable-cross-compile \
    --sysroot="$SYSROOT" \
    --cc="$CC" \
    --cxx="$CXX" \
    --ar="$AR" \
    --nm="$NM" \
    --ranlib="$RANLIB" \
    --strip="$STRIP" \
    --pkg-config="pkg-config" \
    --pkg-config-flags="--static" \
    --extra-cflags="-Os -fPIE -fPIC -march=armv8-a -I$PREFIX/include" \
    "${FFMPEG_LD_ARGS[@]}" \
    --extra-libs="-lm -lz"

  # Assert compile-time configuration before compiling
  for cfg_flag in \
    "CONFIG_ASS_FILTER=yes" \
    "CONFIG_SUBTITLES_FILTER=yes" \
    "CONFIG_DRAWTEXT_FILTER=yes" \
    "CONFIG_LIBASS=yes" \
    "CONFIG_LIBFREETYPE=yes" \
    "CONFIG_LIBFRIBIDI=yes" \
    "CONFIG_LIBHARFBUZZ=yes" \
    "CONFIG_FONTCONFIG=yes" \
    "CONFIG_MEDIACODEC=yes" \
    "CONFIG_H264_MEDIACODEC_DECODER=yes" \
    "CONFIG_HEVC_MEDIACODEC_DECODER=yes" \
    "CONFIG_MPEG4_MEDIACODEC_DECODER=yes" \
    "CONFIG_H264_MEDIACODEC_ENCODER=yes" \
    "CONFIG_HEVC_MEDIACODEC_ENCODER=yes" \
    "CONFIG_MPEG4_ENCODER=yes" \
    "CONFIG_AAC_ENCODER=yes" \
    "CONFIG_MJPEG_ENCODER=yes" \
    "CONFIG_H264_DECODER=yes" \
    "CONFIG_HEVC_DECODER=yes" \
    "CONFIG_AAC_DECODER=yes" \
    "CONFIG_AVGBLUR_FILTER=yes" \
    "CONFIG_GBLUR_FILTER=yes" \
    "CONFIG_COLORLEVELS_FILTER=yes" \
    "CONFIG_HUESATURATION_FILTER=yes" \
    "CONFIG_HUE_FILTER=yes" \
    "CONFIG_COLORBALANCE_FILTER=yes" \
    "CONFIG_COLORCHANNELMIXER_FILTER=yes" \
    "CONFIG_SCALE_FILTER=yes" \
    "CONFIG_CROP_FILTER=yes" \
    "CONFIG_PAD_FILTER=yes" \
    "CONFIG_OVERLAY_FILTER=yes" \
    "CONFIG_VSTACK_FILTER=yes" \
    "CONFIG_LOUDNORM_FILTER=yes" \
    "CONFIG_VOLUME_FILTER=yes" \
    "CONFIG_VOLUMEDETECT_FILTER=yes"
  do
    if ! grep -Fq -- "$cfg_flag" ffbuild/config.mak; then
      echo "ERROR: Expected option $cfg_flag is not enabled in ffbuild/config.mak!" >&2
      exit 1
    fi
  done

  make -j"$JOBS"
  make install

  # Assert registered filters and codecs in generated registration lists
  for sym in \
    ff_vf_ass ff_vf_subtitles ff_vf_drawtext ff_vf_avgblur ff_vf_gblur \
    ff_vf_colorlevels ff_vf_huesaturation ff_vf_hue ff_vf_colorbalance \
    ff_vf_colorchannelmixer ff_vf_scale ff_vf_crop ff_vf_pad ff_vf_overlay \
    ff_vf_vstack ff_af_loudnorm ff_af_volume ff_af_volumedetect
  do
    if ! grep -Fq -- "$sym" libavfilter/filter_list.c; then
      echo "ERROR: Expected filter symbol $sym missing from libavfilter/filter_list.c!" >&2
      exit 1
    fi
  done
  for codec_sym in \
    ff_h264_mediacodec_decoder ff_hevc_mediacodec_decoder ff_mpeg4_mediacodec_decoder \
    ff_h264_mediacodec_encoder ff_hevc_mediacodec_encoder ff_mpeg4_encoder \
    ff_aac_encoder ff_mjpeg_encoder ff_h264_decoder ff_hevc_decoder ff_aac_decoder
  do
    if ! grep -Fq -- "$codec_sym" libavcodec/codec_list.c; then
      echo "ERROR: Expected codec symbol $codec_sym missing from libavcodec/codec_list.c!" >&2
      exit 1
    fi
  done

  # Assert static subtitle, font, and PNG emoji dependency symbols in unstripped ffmpeg_g
  ffmpeg_sym_dump="$("$NM" --defined-only -g ffmpeg_g)"
  for sym in \
    ass_library_init ass_process_chunk FcInit hb_buffer_create fribidi_get_bidi_types \
    FT_Init_FreeType png_create_read_struct init_linebreak set_linebreaks_utf32 XML_ParserCreate
  do
    if ! grep -Eq "[[:space:]]${sym}$" <<< "$ffmpeg_sym_dump"; then
      echo "ERROR: Expected statically linked symbol '$sym' missing in ffmpeg_g!" >&2
      exit 1
    fi
  done

  "$STRIP" --strip-unneeded -o "$OUTPUT_DIR/libffmpeg.so" ffmpeg_g
  "$STRIP" --strip-unneeded -o "$OUTPUT_DIR/libffprobe.so" ffprobe_g
  chmod 0755 "$OUTPUT_DIR/libffmpeg.so" "$OUTPUT_DIR/libffprobe.so"
)

verify_binary_artifacts "$OUTPUT_DIR" "$READELF"

if [[ -n "$MANIFEST_PATH" ]]; then
  mkdir -p "$(dirname "$MANIFEST_PATH")"
  cat > "$MANIFEST_PATH" <<EOF
ffmpeg_version=${FFMPEG_VERSION}
ffmpeg_tag=${FFMPEG_TAG}
ffmpeg_commit=${FFMPEG_COMMIT}
api_level=${API_LEVEL}
meson_version=$(meson --version | head -n 1)
harfbuzz_cxx_runtime=${HARFBUZZ_CXX_RUNTIME}
expat=${EXPAT_VERSION} sha256=${EXPAT_SHA256}
libpng=${LIBPNG_VERSION} sha256=${LIBPNG_SHA256}
freetype=${FREETYPE_VERSION} sha256=${FREETYPE_SHA256}
fribidi=${FRIBIDI_VERSION} sha256=${FRIBIDI_SHA256}
harfbuzz=${HARFBUZZ_VERSION} sha256=${HARFBUZZ_SHA256}
fontconfig=${FONTCONFIG_VERSION} sha256=${FONTCONFIG_SHA256}
libunibreak=${LIBUNIBREAK_VERSION} sha256=${LIBUNIBREAK_SHA256}
libass=${LIBASS_VERSION} sha256=${LIBASS_SHA256}
libass_static_libs=${LIBASS_STATIC_LIBS}
libffmpeg_sha256=$(sha256sum "$OUTPUT_DIR/libffmpeg.so" | awk '{print $1}')
libffprobe_sha256=$(sha256sum "$OUTPUT_DIR/libffprobe.so" | awk '{print $1}')
EOF
  echo "Wrote build manifest: $MANIFEST_PATH"
fi

ls -lh "$OUTPUT_DIR/libffmpeg.so" "$OUTPUT_DIR/libffprobe.so"
