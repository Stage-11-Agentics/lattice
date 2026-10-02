"""Issue media (LAT-366): photos and video stored with an issue. Pure logic.

A photo or video passed to ``lattice issue file --evidence`` or ``lattice
issue attach`` is copied into ``.lattice/issues/media/``. Its type is decided
from its first bytes, never its name, by :func:`sniff_media`; only the types in
:data:`MEDIA_TYPES` are media. A video can carry still frames, derived files
named by the time they show (:func:`frame_name`), so an agent that cannot
watch video can still read it.

Nothing here touches the filesystem; ``lattice.storage.issue_media`` does.
"""

from __future__ import annotations

import math
import re
import struct
from collections.abc import Iterable, Mapping

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

#: Accepted media: content type -> (kind, stored extension).
MEDIA_TYPES: Mapping[str, tuple[str, str]] = {
    "image/png": ("photo", ".png"),
    "image/jpeg": ("photo", ".jpg"),
    "image/gif": ("photo", ".gif"),
    "image/webp": ("photo", ".webp"),
    "video/mp4": ("video", ".mp4"),
    "video/quicktime": ("video", ".mov"),
    "video/webm": ("video", ".webm"),
}

MEDIA_KINDS: tuple[str, ...] = ("photo", "video")

ACCEPTED_FORMATS_TEXT = "PNG, JPEG, GIF, WebP; MP4, MOV, WebM"

#: The macOS conversion an agent can run for a HEIC photo when Lattice cannot.
HEIC_HINT = "sips -s format jpeg in.heic --out out.jpg"

#: How many leading bytes the sniffers need.
SNIFF_BYTES = 64

_MP4_BRANDS = frozenset(
    {
        b"isom",
        b"iso2",
        b"iso3",
        b"iso4",
        b"iso5",
        b"iso6",
        b"mp41",
        b"mp42",
        b"avc1",
        b"M4V ",
        b"M4VH",
        b"M4VP",
        b"dash",
        b"mmp4",
    }
)
#: HEIF photo brands (iPhone photos). AVIF (``avif``, ``avis``) is not among them.
_HEIC_BRANDS = frozenset(
    {b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"hevm", b"hevs", b"mif1", b"msf1"}
)


def _ftyp_brands(head: bytes) -> tuple[bytes, bytes] | None:
    """``(major brand, the rest of the ftyp box)`` of an ISO BMFF file, else ``None``."""
    if len(head) < 12 or head[4:8] != b"ftyp":
        return None
    return head[8:12], head[12:SNIFF_BYTES]


def sniff_media(head: bytes) -> str | None:
    """The accepted content type of a file starting with *head*, else ``None``.

    Decided from magic bytes only. SVG and HTML (they can carry script), HEIC
    and AVIF, TIFF, BMP, PDF, AVI, plain Matroska and everything else are not
    media.
    """
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    brands = _ftyp_brands(head)
    if brands is not None:
        major, _rest = brands
        if major == b"qt  ":
            return "video/quicktime"
        if major in _MP4_BRANDS:
            return "video/mp4"
        return None
    if head.startswith(b"\x1a\x45\xdf\xa3") and b"webm" in head[:SNIFF_BYTES]:
        return "video/webm"
    return None


def sniff_heic(head: bytes) -> bool:
    """Whether *head* starts a HEIC/HEIF photo (which Lattice converts to JPEG)."""
    brands = _ftyp_brands(head)
    if brands is None:
        return False
    major, rest = brands
    if major in (b"mif1", b"msf1") and (b"avif" in rest or b"avis" in rest):
        return False
    return major in _HEIC_BRANDS


def media_kind(content_type: str) -> str | None:
    entry = MEDIA_TYPES.get(content_type)
    return entry[0] if entry else None


def media_ext(content_type: str) -> str | None:
    entry = MEDIA_TYPES.get(content_type)
    return entry[1] if entry else None


# ---------------------------------------------------------------------------
# Photo dimensions (bounded header parsing; failure is None, never an error)
# ---------------------------------------------------------------------------

_JPEG_SCAN_LIMIT = 1024 * 1024
_JPEG_SOF = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)


def _png_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 24 or data[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", data[16:24])


def _gif_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 10:
        return None
    return struct.unpack("<HH", data[6:10])


def _webp_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 30:
        return None
    chunk = data[12:16]
    if chunk == b"VP8X":
        width = 1 + int.from_bytes(data[24:27], "little")
        height = 1 + int.from_bytes(data[27:30], "little")
        return width, height
    if chunk == b"VP8 ":
        if data[23:26] != b"\x9d\x01\x2a":
            return None
        width, height = struct.unpack("<HH", data[26:30])
        return width & 0x3FFF, height & 0x3FFF
    if chunk == b"VP8L":
        if data[20] != 0x2F:
            return None
        bits = int.from_bytes(data[21:25], "little")
        return 1 + (bits & 0x3FFF), 1 + ((bits >> 14) & 0x3FFF)
    return None


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    i, end = 2, min(len(data), _JPEG_SCAN_LIMIT)
    while i + 4 <= end:
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker == 0xFF:  # fill byte
            i += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:  # no length
            i += 2
            continue
        length = int.from_bytes(data[i + 2 : i + 4], "big")
        if length < 2:
            return None
        if marker in _JPEG_SOF:
            if i + 9 > end:
                return None
            height, width = struct.unpack(">HH", data[i + 5 : i + 9])
            return width, height
        i += 2 + length
    return None


_DIMENSION_PARSERS = {
    "image/png": _png_size,
    "image/gif": _gif_size,
    "image/webp": _webp_size,
    "image/jpeg": _jpeg_size,
}


def image_dimensions(content_type: str, data: bytes) -> tuple[int, int] | None:
    """A photo's ``(width, height)`` as stored (EXIF orientation not applied), or ``None``."""
    parser = _DIMENSION_PARSERS.get(content_type)
    if parser is None:
        return None
    try:
        size = parser(data)
    except (struct.error, IndexError, ValueError):
        return None
    if size is None or size[0] <= 0 or size[1] <= 0:
        return None
    return int(size[0]), int(size[1])


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

MB = 1024 * 1024
#: Per file, checked before a video is transcoded and again after.
DEFAULT_MAX_MEDIA_MB = 100
#: Per issue, the present media after transcoding. Frames do not count.
DEFAULT_MAX_ISSUE_MEDIA_MB = 250

MEDIA_FILE_TOO_LARGE = "MEDIA_FILE_TOO_LARGE"
ISSUE_MEDIA_TOO_LARGE = "ISSUE_MEDIA_TOO_LARGE"


def _positive_int(value: object, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return default
    return value


def media_limits(config: Mapping) -> tuple[int, int]:
    """``(per file, per issue)`` in bytes, from ``issues.max_media_mb`` and
    ``issues.max_issue_media_mb``. An absent, non-integer or non-positive
    value falls back to its default."""
    section = config.get("issues")
    section = section if isinstance(section, Mapping) else {}
    per_file = _positive_int(section.get("max_media_mb"), DEFAULT_MAX_MEDIA_MB)
    per_issue = _positive_int(section.get("max_issue_media_mb"), DEFAULT_MAX_ISSUE_MEDIA_MB)
    return per_file * MB, per_issue * MB


def file_too_large_message(name: str, size: int, limit: int, *, nothing: str) -> str:
    """The refusal for one file over ``issues.max_media_mb``; *nothing*: what was not done."""
    message = (
        f"{name} is {format_size(size)}; the limit is {format_size(limit)} per file "
        f"(issues.max_media_mb in .lattice/config.json). {nothing}"
    )
    return message


def issue_too_large_message(issue: str, total: int, limit: int) -> str:
    return (
        f"{issue} would hold {format_size(total)} of media; the limit is "
        f"{format_size(limit)} per issue (issues.max_issue_media_mb in .lattice/config.json)."
    )


# ---------------------------------------------------------------------------
# Frames
# ---------------------------------------------------------------------------

MAX_FRAMES = 8
FRAME_MAX_EDGE = 1568
MAX_FRAME_BYTES = 2 * MB
_FRAME_NAME_RE = re.compile(r"^t(\d{4,})\.(\d{3})s\.jpg$")


def frame_times(duration_ms: int) -> list[int]:
    """When to take a video's frames: evenly spaced, both ends included.

    ``N = min(8, max(2, 1 + ceil(d / 2)))`` for a duration of *d* seconds, at
    ``i * (d - 0.1) / (N - 1)``; the last one sits 0.1 s before the end, where a
    frame still exists. A video under 1 s gets one frame, at 0.
    """
    if duration_ms < 1000:
        return [0]
    count = min(MAX_FRAMES, max(2, 1 + math.ceil(duration_ms / 2000)))
    span = duration_ms - 100
    return [round(i * span / (count - 1)) for i in range(count)]


def frame_name(t_ms: int) -> str:
    """``t0012.500s.jpg`` for 12.5 s: the time is evident and names sort by time."""
    return f"t{t_ms // 1000:04d}.{t_ms % 1000:03d}s.jpg"


def parse_frame_name(name: str) -> int | None:
    """The time in milliseconds a frame file's name shows, or ``None``."""
    match = _FRAME_NAME_RE.fullmatch(name)
    if match is None:
        return None
    return int(match.group(1)) * 1000 + int(match.group(2))


# ---------------------------------------------------------------------------
# Names, entries, summaries
# ---------------------------------------------------------------------------

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
NAME_LIMIT = 255


def clean_original_name(filename: str) -> str:
    """The basename of *filename*, control characters removed, cut to 255 characters."""
    base = re.split(r"[/\\]", filename)[-1]
    return _CONTROL_RE.sub("", base)[:NAME_LIMIT]


def present_media(snapshot: Mapping) -> list[dict]:
    """The issue's media entries that are not removed, in order."""
    return [m for m in snapshot.get("media", []) if not m.get("removed")]


def present_media_bytes(snapshot: Mapping) -> int:
    return sum(int(m.get("size_bytes") or 0) for m in present_media(snapshot))


def next_media_n(snapshot: Mapping) -> int:
    """1 plus the highest ordinal ever added to the issue (removed ones included)."""
    return 1 + max((int(m.get("n") or 0) for m in snapshot.get("media", [])), default=0)


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def media_counts(entries: Iterable[Mapping]) -> tuple[int, int, int]:
    """``(photos, videos, frames)`` among present *entries* (views or snapshot entries)."""
    photos = videos = frames = 0
    for entry in entries:
        if entry.get("removed"):
            continue
        if entry.get("kind") == "photo":
            photos += 1
        elif entry.get("kind") == "video":
            videos += 1
        frames += len(entry.get("frames") or [])
    return photos, videos, frames


def media_summary(entries: Iterable[Mapping]) -> str:
    """``1 photo, 1 video, 4 frames``; parts that are zero are left out."""
    photos, videos, frames = media_counts(entries)
    parts = []
    if photos:
        parts.append(_plural(photos, "photo"))
    if videos:
        parts.append(_plural(videos, "video"))
    if frames:
        parts.append(_plural(frames, "frame"))
    return ", ".join(parts)


def format_size(n: int) -> str:
    """``512 B``, ``212 KB``, ``8.1 MB`` (1 KB = 1024 bytes)."""
    if n < 1024:
        return f"{n} B"
    if n < MB:
        return f"{round(n / 1024)} KB"
    if n % MB == 0:
        return f"{n // MB} MB"
    return f"{n / MB:.1f} MB"


def format_duration(ms: int) -> str:
    """``0:14``, ``12:03``, ``1:02:03``."""
    seconds = max(0, round(ms / 1000))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


#: What ``issue media`` and ``issue show`` say about a video with no frames.
NO_FRAMES_TEXT = "no frames (ffmpeg was not available, or could not read it, when it was attached)"


def _entry_facts(entry: Mapping) -> list[str]:
    facts = []
    if entry.get("width") and entry.get("height"):
        facts.append(f"{entry['width']}x{entry['height']}")
    if entry.get("duration_ms") is not None:
        facts.append(format_duration(int(entry["duration_ms"])))
    facts.append(format_size(int(entry.get("size_bytes") or 0)))
    if entry.get("kind") == "video" and entry.get("frames"):
        facts.append(_plural(len(entry["frames"]), "frame"))
    return facts


def format_media_lines(entries: Iterable[Mapping], actor_display=None) -> list[str]:  # noqa: ANN001
    """The lines ``issue media`` and ``issue show`` print for view entries, unindented.

    Each present entry prints its ordinal, kind, original name and facts, then
    its path (or ``(missing: <path>)``), then its frames' full paths or the
    no-frames sentence. A removed entry is one line.
    """
    show_actor = actor_display or (lambda actor: str(actor))
    entries = list(entries)
    n_width = max((len(str(e.get("n", ""))) for e in entries), default=1)
    present = [e for e in entries if not e.get("removed")]
    name_width = min(40, max((len(e.get("original_name") or "") for e in present), default=0))
    lines: list[str] = []
    pad = " " * (n_width + 2)
    for entry in entries:
        n = f"{entry.get('n', '?'):>{n_width}}"
        removed = entry.get("removed")
        if removed:
            by = show_actor(removed.get("by") or "?")
            lines.append(f"{n}  removed {removed.get('at')} by {by}: {removed.get('reason')}")
            continue
        name = entry.get("original_name") or entry.get("id", "")
        head = f"{n}  {entry.get('kind', '?'):<5}  {name:<{name_width}}  "
        lines.append((head + "  ".join(_entry_facts(entry))).rstrip())
        path = entry.get("path")
        if entry.get("missing"):
            lines.append(f"{pad}(missing: {path or 'not on the server'})")
        elif path:
            lines.append(f"{pad}{path}")
        elif entry.get("available") == "remote":
            lines.append(f"{pad}{REMOTE_MEDIA_TEXT}")
        if entry.get("kind") == "video":
            frames = entry.get("frames") or []
            if frames and all(f.get("path") for f in frames):
                lines.append(f"{pad}frames: {frames[0]['path']}")
                lines.extend(f"{pad}        {f['path']}" for f in frames[1:])
            elif frames:
                at = ", ".join(f"{f.get('t_ms', 0) / 1000:.1f}s" for f in frames)
                lines.append(f"{pad}frames: {len(frames)} (at {at}; fetched on request)")
            elif not entry.get("missing"):
                lines.append(f"{pad}{NO_FRAMES_TEXT}")
    return lines


REMOTE_MEDIA_TEXT = "(on the server; 'lattice issue media <issue> --paths' fetches it)"

__all__ = [
    "ACCEPTED_FORMATS_TEXT",
    "DEFAULT_MAX_ISSUE_MEDIA_MB",
    "DEFAULT_MAX_MEDIA_MB",
    "FRAME_MAX_EDGE",
    "HEIC_HINT",
    "ISSUE_MEDIA_TOO_LARGE",
    "MAX_FRAMES",
    "MAX_FRAME_BYTES",
    "MEDIA_FILE_TOO_LARGE",
    "MEDIA_KINDS",
    "MEDIA_TYPES",
    "NO_FRAMES_TEXT",
    "SNIFF_BYTES",
    "clean_original_name",
    "file_too_large_message",
    "format_duration",
    "format_media_lines",
    "format_size",
    "frame_name",
    "frame_times",
    "image_dimensions",
    "issue_too_large_message",
    "media_counts",
    "media_ext",
    "media_kind",
    "media_limits",
    "media_summary",
    "next_media_n",
    "parse_frame_name",
    "present_media",
    "present_media_bytes",
    "sniff_heic",
    "sniff_media",
]
