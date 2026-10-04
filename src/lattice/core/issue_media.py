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
import zlib
from collections.abc import Iterable, Mapping

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

#: Accepted media: content type -> (kind, stored extension).
MEDIA_TYPES: Mapping[str, tuple[str, str]] = {
    "image/png": ("photo", ".png"),
    "image/jpeg": ("photo", ".jpg"),
    "image/heic": ("photo", ".heic"),
    "image/gif": ("photo", ".gif"),
    "image/webp": ("photo", ".webp"),
    "video/mp4": ("video", ".mp4"),
    "video/quicktime": ("video", ".mov"),
    "video/webm": ("video", ".webm"),
}

MEDIA_KINDS: tuple[str, ...] = ("photo", "video")

ACCEPTED_FORMATS_TEXT = "PNG, JPEG, HEIC, GIF, WebP; MP4, MOV, WebM"

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

    Decided from magic bytes only. SVG and HTML (they can carry script), AVIF,
    TIFF, BMP, PDF, AVI, plain Matroska and everything else are not media.
    HEIC is recognized for the explicit keep-metadata fallback; normal filers
    convert it before calling the operation.
    """
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if sniff_heic(head):
        return "image/heic"
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


class PhotoMetadataError(ValueError):
    """A JPEG or PNG could not be walked safely enough to strip metadata."""


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_SAFE_CHUNKS = frozenset(
    {
        b"IHDR",
        b"PLTE",
        b"IDAT",
        b"IEND",
        b"tRNS",
        b"cHRM",
        b"gAMA",
        b"iCCP",
        b"sBIT",
        b"sRGB",
        b"cICP",
        b"mDCV",
        b"cLLI",
        b"bKGD",
        b"pHYs",
        b"acTL",
        b"fcTL",
        b"fdAT",
    }
)
_JPEG_SOF_MARKERS = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)
_JPEG_SAFE_SEGMENTS = frozenset({0xC4, 0xCC, 0xDB, 0xDC, 0xDD}) | _JPEG_SOF_MARKERS


def _exif_orientation(payload: bytes) -> int | None:
    """Read a structurally bounded Orientation from an Exif APP1 payload."""
    if not payload.startswith(b"Exif\0\0"):
        return None
    tiff = payload[6:]
    if len(tiff) < 8:
        return None
    order = tiff[:2]
    if order == b"II":
        endian = "little"
    elif order == b"MM":
        endian = "big"
    else:
        return None

    def u16(offset: int) -> int:
        return int.from_bytes(tiff[offset : offset + 2], endian)

    def u32(offset: int) -> int:
        return int.from_bytes(tiff[offset : offset + 4], endian)

    if u16(2) != 42:
        return None
    ifd = u32(4)
    if ifd < 8 or ifd + 2 > len(tiff):
        return None
    count = u16(ifd)
    end = ifd + 2 + count * 12 + 4
    if end > len(tiff):
        return None
    type_sizes = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8}
    orientation = None
    for index in range(count):
        entry = ifd + 2 + index * 12
        tag, value_type, value_count = u16(entry), u16(entry + 2), u32(entry + 4)
        unit = type_sizes.get(value_type)
        if unit is None:
            continue
        if value_count > (len(tiff) // unit):
            return None
        byte_count = unit * value_count
        if byte_count > 4:
            value_offset = u32(entry + 8)
            if value_offset > len(tiff) or byte_count > len(tiff) - value_offset:
                return None
        if tag == 0x0112:
            if value_type != 3 or value_count != 1:
                return None
            value = u16(entry + 8)
            orientation = value if 1 <= value <= 8 else None
    next_ifd = u32(ifd + 2 + count * 12)
    if next_ifd and next_ifd + 2 > len(tiff):
        return None
    return orientation


def _minimal_orientation_exif(orientation: int) -> bytes:
    """One little-endian IFD entry, with no identifying Exif tags."""
    tiff = (
        b"II*\0\x08\0\0\0"
        + b"\x01\0"
        + b"\x12\x01\x03\0\x01\0\0\0"
        + orientation.to_bytes(2, "little")
        + b"\0\0\0\0\0\0"
    )
    return b"Exif\0\0" + tiff


def _strip_jpeg(data: bytes) -> tuple[bytes, tuple[str, ...]]:
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        raise PhotoMetadataError("JPEG is missing its start marker.")
    kept: list[tuple[int, bytes]] = []
    orientation = None
    saw_sof = False
    pos = 2
    while pos < len(data):
        marker_start = pos
        if data[pos] != 0xFF:
            marker_start = data.find(b"\xff", pos)
            if marker_start < 0:
                raise PhotoMetadataError("JPEG is missing its end marker.")
            pos = marker_start
        while pos < len(data) and data[pos] == 0xFF:
            pos += 1
        if pos >= len(data):
            raise PhotoMetadataError("JPEG ends inside a marker.")
        marker = data[pos]
        pos += 1
        if marker == 0x00:
            raise PhotoMetadataError("JPEG has a stuffed byte outside image data.")
        if marker == 0xD8:
            raise PhotoMetadataError("JPEG contains a second start marker.")
        if marker == 0xD9:
            if not saw_sof:
                raise PhotoMetadataError("JPEG has no frame header.")
            kept.append((marker, data[marker_start:pos]))
            break
        if marker == 0x01:
            kept.append((marker, data[marker_start:pos]))
            continue
        if 0xD0 <= marker <= 0xD7:
            raise PhotoMetadataError("JPEG restart marker is outside a scan.")
        if pos + 2 > len(data):
            raise PhotoMetadataError("JPEG ends inside a segment length.")
        segment_length = int.from_bytes(data[pos : pos + 2], "big")
        if segment_length < 2 or segment_length > len(data) - pos:
            raise PhotoMetadataError("JPEG segment length is invalid or truncated.")
        segment_end = pos + segment_length
        payload = data[pos + 2 : segment_end]

        if marker == 0xDA:
            if not payload or not 1 <= payload[0] <= 4 or segment_length != 6 + 2 * payload[0]:
                raise PhotoMetadataError("JPEG scan header is invalid.")
            scan = segment_end
            while True:
                marker_at = data.find(b"\xff", scan)
                if marker_at < 0 or marker_at + 1 >= len(data):
                    raise PhotoMetadataError("JPEG scan has no following marker.")
                code_at = marker_at + 1
                while code_at < len(data) and data[code_at] == 0xFF:
                    code_at += 1
                if code_at >= len(data):
                    raise PhotoMetadataError("JPEG scan ends inside a marker.")
                code = data[code_at]
                if code == 0x00 or 0xD0 <= code <= 0xD7:
                    scan = code_at + 1
                    continue
                break
            # ``scan`` advances past stuffed bytes and restart markers while
            # looking for the next marker, but ordinary entropy bytes after the
            # last such marker are still part of this scan.
            kept.append((marker, data[marker_start:marker_at]))
            pos = marker_at
            continue

        segment = data[marker_start:segment_end]
        if marker == 0xE1:
            if orientation is None:
                orientation = _exif_orientation(payload)
        elif 0xE0 <= marker <= 0xEF:
            keep_segment = (
                (marker == 0xE0 and payload.startswith(b"JFIF\0"))
                or (marker == 0xE2 and payload.startswith(b"ICC_PROFILE\0"))
                or (marker == 0xEE and payload.startswith(b"Adobe"))
            )
            if keep_segment:
                kept.append((marker, segment))
        elif marker == 0xFE:
            pass
        elif marker in _JPEG_SAFE_SEGMENTS:
            if marker in _JPEG_SOF_MARKERS:
                if len(payload) < 6 or not payload[5] or len(payload) != 6 + 3 * payload[5]:
                    raise PhotoMetadataError("JPEG frame header is invalid.")
                saw_sof = True
            kept.append((marker, segment))
        else:
            raise PhotoMetadataError(f"JPEG contains unsupported marker 0x{marker:02x}.")
        pos = segment_end
    else:
        raise PhotoMetadataError("JPEG is missing its end marker.")

    eoi_end = pos
    out = bytearray(data[:2])
    orientation_segment = (
        _jpeg_segment(0xE1, _minimal_orientation_exif(orientation)) if orientation else None
    )
    app0_index = next(
        (index for index, (marker, _segment) in enumerate(kept) if marker == 0xE0), None
    )
    inserted = False
    for index, (marker, segment) in enumerate(kept):
        out.extend(segment)
        if orientation_segment and index == app0_index:
            out.extend(orientation_segment)
            inserted = True
    if orientation_segment and not inserted:
        out[2:2] = orientation_segment
    changes = []
    if bytes(out) != data[:eoi_end]:
        changes.append("jpeg_metadata_or_trailing_data")
    return bytes(out), tuple(changes)


def _jpeg_segment(marker: int, payload: bytes) -> bytes:
    length = len(payload) + 2
    if length > 0xFFFF:
        raise PhotoMetadataError("JPEG metadata segment is too large.")
    return b"\xff" + bytes([marker]) + length.to_bytes(2, "big") + payload


def _strip_png(data: bytes) -> tuple[bytes, tuple[str, ...]]:
    if not data.startswith(_PNG_SIGNATURE):
        raise PhotoMetadataError("PNG is missing its signature.")
    pos = len(_PNG_SIGNATURE)
    out = bytearray(_PNG_SIGNATURE)
    first = True
    saw_idat = False
    changes = []
    while pos + 12 <= len(data):
        length = int.from_bytes(data[pos : pos + 4], "big")
        kind = data[pos + 4 : pos + 8]
        if any(not (65 <= byte <= 90 or 97 <= byte <= 122) for byte in kind):
            raise PhotoMetadataError("PNG chunk type is invalid.")
        chunk_end = pos + 12 + length
        if chunk_end > len(data):
            raise PhotoMetadataError("PNG chunk is truncated.")
        payload = data[pos + 8 : pos + 8 + length]
        expected_crc = int.from_bytes(data[pos + 8 + length : chunk_end], "big")
        if zlib.crc32(kind + payload) & 0xFFFFFFFF != expected_crc:
            raise PhotoMetadataError("PNG chunk checksum is invalid.")
        if first:
            if kind != b"IHDR" or length != 13:
                raise PhotoMetadataError("PNG must begin with one 13-byte IHDR chunk.")
            width = int.from_bytes(payload[:4], "big")
            height = int.from_bytes(payload[4:8], "big")
            if not width or not height:
                raise PhotoMetadataError("PNG dimensions must be positive.")
            first = False
        elif kind == b"IHDR":
            raise PhotoMetadataError("PNG contains a second IHDR chunk.")
        if kind == b"IDAT":
            saw_idat = True
        if kind == b"IEND":
            if length != 0 or not saw_idat:
                raise PhotoMetadataError("PNG end chunk or image data is invalid.")
            out.extend(data[pos:chunk_end])
            if chunk_end < len(data):
                changes.append("png_trailing_data")
            return bytes(out), tuple(changes)
        if kind in _PNG_SAFE_CHUNKS:
            out.extend(data[pos:chunk_end])
        else:
            if 65 <= kind[0] <= 90:
                raise PhotoMetadataError(
                    f"PNG contains an unknown critical PNG chunk {kind.decode('ascii')!r}."
                )
            changes.append(kind.decode("ascii", errors="replace"))
        pos = chunk_end
    raise PhotoMetadataError("PNG is missing a complete IEND chunk.")


def photo_metadata_findings(data: bytes, content_type: str) -> tuple[str, ...]:
    """Return categories removed by the stdlib JPEG/PNG scrubber.

    Malformed input raises :class:`PhotoMetadataError`; callers that offer an
    explicit keep option must make that choice before publishing the original.
    """
    if content_type == "image/jpeg":
        return _strip_jpeg(data)[1]
    if content_type == "image/png":
        return _strip_png(data)[1]
    raise PhotoMetadataError(f"metadata stripping does not support {content_type}.")


def strip_photo_metadata(data: bytes, content_type: str) -> bytes:
    """Strip JPEG/PNG identifying metadata using only the Python standard library.

    JPEGs retain image coding, JFIF/JFXX, ICC and Adobe color markers, plus a
    minimal orientation-only Exif APP1. PNGs retain rendering and animation
    chunks. Malformed framing raises :class:`PhotoMetadataError`.
    """
    if content_type == "image/jpeg":
        return _strip_jpeg(data)[0]
    if content_type == "image/png":
        return _strip_png(data)[0]
    raise PhotoMetadataError(f"metadata stripping does not support {content_type}.")


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
        hosted = "available" in entry  # only a hosted view says where media is
        if entry.get("missing"):
            shown = "not on the server" if hosted and not path else path
            lines.append(f"{pad}(missing: {shown})")
        elif path:
            lines.append(f"{pad}{path}")
        elif entry.get("available") == "remote":
            lines.append(f"{pad}{REMOTE_MEDIA_TEXT}")
        elif entry.get("available") == "unreachable":
            lines.append(f"{pad}{UNREACHABLE_MEDIA_TEXT}")
        if entry.get("kind") == "video":
            frames = entry.get("frames") or []
            if frames and all(f.get("path") for f in frames):
                lines.append(f"{pad}frames: {frames[0]['path']}")
                lines.extend(f"{pad}        {f['path']}" for f in frames[1:])
            elif frames:
                at = ", ".join(f"{f.get('t_ms', 0) / 1000:.1f}s" for f in frames)
                lines.append(f"{pad}frames: {len(frames)} (at {at}; fetched on request)")
            elif not entry.get("missing") and entry.get("available") != "unreachable":
                lines.append(f"{pad}{NO_FRAMES_TEXT}")
    return lines


REMOTE_MEDIA_TEXT = "(on the server; 'lattice issue media <issue> --paths' fetches it)"
#: A hosted entry that is not cached while the server cannot be reached.
UNREACHABLE_MEDIA_TEXT = (
    "(not fetched: server unreachable; 'lattice issue media <issue> --paths' "
    "fetches it when it is back)"
)

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
    "PhotoMetadataError",
    "SNIFF_BYTES",
    "UNREACHABLE_MEDIA_TEXT",
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
    "photo_metadata_findings",
    "present_media",
    "present_media_bytes",
    "sniff_heic",
    "sniff_media",
    "strip_photo_metadata",
]
