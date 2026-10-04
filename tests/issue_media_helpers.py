"""Issue media test helpers (LAT-366): media bytes built in code, and fake tools.

No binary is committed. The fake ``ffmpeg``, ``ffprobe`` and ``sips`` are
``/bin/sh`` scripts (a few milliseconds each) written into a temp dir and
selected with ``LATTICE_FFMPEG`` / ``LATTICE_SIPS``; environment variables
steer them:

- ``FAKE_PROBE``: the JSON ffprobe prints (``FAKE_PROBE_FAIL`` makes it fail);
  ``FAKE_PACKETS``: packet times it prints for a packet query.
- ``FAKE_TRANSCODE_FAIL``: ffmpeg fails to transcode;
  ``FAKE_EMPTY_AFTER``: ffmpeg returns no frame for ``-ss`` past this many seconds;
  ``FAKE_FRAME_FAIL``: ffmpeg fails every frame.
"""

from __future__ import annotations

import base64
import json
import struct
import sys
import zlib
from pathlib import Path


def png(width: int = 3, height: int = 2) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def jpeg(width: int = 64, height: int = 48) -> bytes:
    """SOI, SOF0, EOI: sniffable and sized, not decodable."""
    return (
        b"\xff\xd8\xff\xc0"
        + struct.pack(">HBHHB", 11, 8, height, width, 1)
        + b"\x01\x11\x00\xff\xd9"
    )


def gif(width: int = 5, height: int = 4) -> bytes:
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00\x00\x00;"


def webp(width: int = 7, height: int = 9) -> bytes:
    body = b"WEBP" + b"VP8X" + struct.pack("<I", 10) + b"\x00\x00\x00\x00"
    body += (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    return b"RIFF" + struct.pack("<I", len(body)) + body


def mp4(tag: bytes = b"") -> bytes:
    return b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2" + b"\x00\x00\x00\x08free" + tag


def mov(tag: bytes = b"") -> bytes:
    return b"\x00\x00\x00\x14ftypqt  \x00\x00\x00\x00qt  " + b"\x00\x00\x00\x08wide" + tag


def webm(tag: bytes = b"") -> bytes:
    return b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81\x01\x42\x82\x84webm\x42\x87\x81\x02" + tag


def heic() -> bytes:
    return b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\x00" * 16


HTML_AS_PNG = b"<!doctype html><script>alert(1)</script>"
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'

# The fake tools' JPEG (64x48): octal escapes for POSIX printf.
_JPEG_PRINTF = (
    "\\377\\330\\377\\300\\000\\013\\010\\000\\060\\000\\100\\001\\001\\021\\000\\377\\331"
)
_MP4_PRINTF = "\\000\\000\\000\\030ftypisom\\000\\000\\002\\000isomiso2"

_FFMPEG = f"""#!/bin/sh
last=""; prev=""; t=0; in=""
for a in "$@"; do
  [ "$prev" = "-ss" ] && t="$a"
  [ "$prev" = "-i" ] && in="$a"
  prev="$a"; last="$a"
done
echo "$*" >> "${{FAKE_LOG:-/dev/null}}"
if [ "$last" != "-" ]; then
  [ -n "$FAKE_TRANSCODE_FAIL" ] && exit 1
  case "$last" in
    *.jpg) printf '{_JPEG_PRINTF}' > "$last" ;;
    *) printf '{_MP4_PRINTF}' > "$last"; head -c 64 "$in" >> "$last" ;;
  esac
  exit 0
fi
[ -n "$FAKE_FRAME_FAIL" ] && exit 1
if [ -n "$FAKE_EMPTY_AFTER" ] && awk "BEGIN {{ exit !($t > $FAKE_EMPTY_AFTER) }}"; then
  exit 0
fi
printf '{_JPEG_PRINTF}'
"""

_FFPROBE = """#!/bin/sh
case "$*" in
  *packet=pts_time*) for p in $FAKE_PACKETS; do echo "$p"; done; exit 0 ;;
esac
[ -n "$FAKE_PROBE_FAIL" ] && exit 1
printf '%s' "$FAKE_PROBE"
"""

_SIPS = f"""#!/bin/sh
prev=""
for a in "$@"; do
  [ "$prev" = "--out" ] && out="$a"
  prev="$a"
done
[ -n "$FAKE_SIPS_FAIL" ] && exit 1
printf '{_JPEG_PRINTF}' > "$out"
"""


def _script(path: Path, text: str) -> Path:
    path.write_text(text)
    path.chmod(0o755)
    return path


def fake_tools(directory: Path) -> Path:
    """Write fake ``ffmpeg`` and ``ffprobe`` into *directory*; return ffmpeg's path."""
    directory.mkdir(parents=True, exist_ok=True)
    _script(directory / "ffprobe", _FFPROBE)
    return _script(directory / "ffmpeg", _FFMPEG)


def fake_sips(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    return _script(directory / "sips", _SIPS)


def probe_json(
    width: int = 1280,
    height: int = 800,
    duration: float | None = 14.2,
    *,
    codec: str = "hevc",
    rotation: int | None = None,
    format_duration: float | None = None,
) -> str:
    """What the fake ffprobe prints."""
    stream: dict = {"codec_type": "video", "codec_name": codec, "width": width, "height": height}
    if duration is not None:
        stream["duration"] = f"{duration:.6f}"
    if rotation is not None:
        stream["side_data_list"] = [{"rotation": rotation}]
    fmt = {} if format_duration is None else {"duration": f"{format_duration:.6f}"}
    return json.dumps({"streams": [stream], "format": fmt})


def use_fake_ffmpeg(monkeypatch, directory: Path, **probe) -> Path:  # noqa: ANN001
    """Select fake tools for this test, the probe printing ``probe_json(**probe)``."""
    ffmpeg = fake_tools(directory)
    monkeypatch.setenv("LATTICE_FFMPEG", str(ffmpeg))
    monkeypatch.setenv("FAKE_PROBE", probe_json(**probe))
    return ffmpeg


def use_stdlib_fake_ffmpeg(
    monkeypatch, directory: Path, *, source: bytes, frame: bytes, fail_transcode: bool = False
) -> Path:  # noqa: ANN001
    """Install Python-stdlib ffmpeg/ffprobe shims and return the argv log path."""
    directory.mkdir(parents=True, exist_ok=True)
    calls = directory / "ffmpeg-argv.jsonl"
    ffmpeg = directory / "ffmpeg"
    ffmpeg.write_text(
        "#!" + sys.executable + "\n"
        "import base64, json, pathlib, sys\n"
        f"log = pathlib.Path({str(calls)!r})\n"
        "args = sys.argv[1:]\n"
        "with log.open('a') as handle: handle.write(json.dumps(args) + '\\n')\n"
        "if args[-1] == '-':\n"
        f"    sys.stdout.buffer.write(base64.b64decode({base64.b64encode(frame).decode()!r}))\n"
        "else:\n"
        f"    if {fail_transcode!r}: sys.exit(1)\n"
        f"    pathlib.Path(args[-1]).write_bytes(base64.b64decode({base64.b64encode(source).decode()!r}))\n",
        encoding="utf-8",
    )
    ffmpeg.chmod(0o755)
    ffprobe = directory / "ffprobe"
    ffprobe.write_text(
        "#!" + sys.executable + "\n"
        "import json\n"
        "print(json.dumps({'streams': [{'codec_type': 'video', 'codec_name': 'h264', "
        "'width': 64, 'height': 48, 'duration': '1'}], 'format': {'duration': '1'}}))\n",
        encoding="utf-8",
    )
    ffprobe.chmod(0o755)
    monkeypatch.setenv("LATTICE_FFMPEG", str(ffmpeg))
    return calls
