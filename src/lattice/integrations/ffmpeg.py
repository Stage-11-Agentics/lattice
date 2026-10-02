"""Optional media tools for issue media (LAT-366): ffmpeg, ffprobe and sips.

Everything here runs in the client, on the filer's machine, before an
operation: operations never import ``lattice.integrations``, so a server never
runs a tool on uploaded content. Every tool is optional and found at run time;
without one the file is stored as it is and nothing fails.

- ``LATTICE_FFMPEG``: the ffmpeg binary (``ffprobe`` is looked up beside it),
  or ``off``. Default: both found on ``PATH``.
- ``LATTICE_SIPS``: the macOS ``sips`` binary, or ``off``. Default: ``PATH``.

With ffmpeg present, a video is re-encoded to H.264 (longest side 1280 px, CRF
28, ``+faststart``) and still frames are taken from what is stored. A HEIC
photo is converted to JPEG with sips, else ffmpeg. Tools write only into a
temporary directory outside the board, removed before returning.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from lattice.core.issue_media import (
    FRAME_MAX_EDGE,
    MAX_FRAME_BYTES,
    SNIFF_BYTES,
    frame_times,
    sniff_media,
)

FFMPEG_ENV = "LATTICE_FFMPEG"
SIPS_ENV = "LATTICE_SIPS"

PROBE_TIMEOUT = 10.0
PACKETS_TIMEOUT = 20.0
FRAME_TIMEOUT = 10.0
FRAMES_BUDGET = 30.0
TRANSCODE_TIMEOUT = 600.0
CONVERT_TIMEOUT = 60.0

#: Longest side of a transcoded video, and its quality.
TRANSCODE_MAX_EDGE = 1280
TRANSCODE_CRF = 28
#: When a frame comes back empty (past the last decodable frame), try earlier.
FRAME_RETRY_BACKOFF_MS = (500, 1500)


@dataclass(frozen=True)
class Tools:
    ffmpeg: str
    ffprobe: str


def _disabled(value: str | None) -> bool:
    return value is not None and value.strip().lower() == "off"


def find_tools() -> Tools | None:
    """ffmpeg and ffprobe, or ``None`` when either is missing or ``LATTICE_FFMPEG=off``."""
    configured = os.environ.get(FFMPEG_ENV)
    if _disabled(configured):
        return None
    if configured:
        ffmpeg = Path(configured).expanduser()
        if os.sep not in configured and (os.altsep is None or os.altsep not in configured):
            found = shutil.which(configured)
            if found is None:
                return None
            ffmpeg = Path(found)
        ffmpeg = Path(os.path.abspath(ffmpeg))
        ffprobe = ffmpeg.with_name("ffprobe")
        if ffmpeg.is_file() and os.access(ffmpeg, os.X_OK) and os.access(ffprobe, os.X_OK):
            return Tools(str(ffmpeg), str(ffprobe))
        return None
    ffmpeg_path, ffprobe_path = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if ffmpeg_path is None or ffprobe_path is None:
        return None
    return Tools(ffmpeg_path, ffprobe_path)


def ffmpeg_state() -> str:
    """``off`` (disabled), ``missing`` or ``ok``."""
    if _disabled(os.environ.get(FFMPEG_ENV)):
        return "off"
    return "ok" if find_tools() is not None else "missing"


def find_sips() -> str | None:
    configured = os.environ.get(SIPS_ENV)
    if _disabled(configured):
        return None
    if configured:
        path = Path(configured).expanduser()
        return str(path) if path.is_file() and os.access(path, os.X_OK) else None
    return shutil.which("sips")


def _run(argv: list[str], timeout: float) -> subprocess.CompletedProcess | None:
    """Run a tool; ``None`` when it cannot start, times out or fails."""
    try:
        proc = subprocess.run(
            argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=max(timeout, 0.1)
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc if proc.returncode == 0 else None


def _input_path(src: Path) -> str:
    """Make a user-supplied input an unambiguous local filename for tool CLIs."""
    return os.path.abspath(src)


# ---------------------------------------------------------------------------
# Probe
# ---------------------------------------------------------------------------


def _seconds_to_ms(value: object) -> int | None:
    try:
        seconds = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(seconds) or seconds <= 0:
        return None
    return round(seconds * 1000)


def _rotation(stream: dict) -> int:
    for side in stream.get("side_data_list") or []:
        if isinstance(side, dict) and "rotation" in side:
            try:
                return int(float(side["rotation"]))
            except (TypeError, ValueError):
                return 0
    try:
        return int(float((stream.get("tags") or {}).get("rotate", 0)))
    except (TypeError, ValueError):
        return 0


def _packet_duration_ms(tools: Tools, src: Path, timeout: float) -> int | None:
    """The largest packet time of the first video stream (reads packets, no decode):
    the duration of a streamed WebM that declares none."""
    proc = _run(
        [
            tools.ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "packet=pts_time",
            "-of",
            "csv=p=0",
            _input_path(src),
        ],
        timeout,
    )
    if proc is None:
        return None
    times = [_seconds_to_ms(line.strip().rstrip(",")) for line in proc.stdout.decode().split()]
    known = [t for t in times if t is not None]
    return max(known) if known else None


def probe(tools: Tools, src: Path, timeout: float = PROBE_TIMEOUT) -> dict | None:
    """``{width, height, duration_ms, codec}`` of a video's first video stream.

    Width and height are as displayed (swapped for a rotation of 90 or 270).
    Duration comes from the stream, else the container, else the last packet;
    a key is absent when unknown. ``None`` when ffprobe fails or finds no video.
    """
    proc = _run(
        [
            tools.ffprobe,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_entries",
            "format=duration:stream=codec_type,codec_name,width,height,duration"
            ":stream_side_data=rotation:stream_tags=rotate",
            _input_path(src),
        ],
        timeout,
    )
    if proc is None:
        return None
    try:
        data = json.loads(proc.stdout.decode("utf-8", "replace"))
    except ValueError:
        return None
    streams = [s for s in data.get("streams") or [] if isinstance(s, dict)]
    stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    if stream is None:
        return None
    info: dict = {}
    width, height = stream.get("width"), stream.get("height")
    if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
        if _rotation(stream) % 180 != 0:
            width, height = height, width
        info["width"], info["height"] = width, height
    if isinstance(stream.get("codec_name"), str):
        info["codec"] = stream["codec_name"]
    duration = _seconds_to_ms(stream.get("duration"))
    if duration is None:
        duration = _seconds_to_ms((data.get("format") or {}).get("duration"))
    if duration is None:
        duration = _packet_duration_ms(tools, src, PACKETS_TIMEOUT)
    if duration is not None:
        info["duration_ms"] = duration
    return info


# ---------------------------------------------------------------------------
# Frames
# ---------------------------------------------------------------------------

_FRAME_SCALE = (
    f"scale='if(gt(iw,ih),min({FRAME_MAX_EDGE},iw),-2)'"
    f":'if(gt(iw,ih),-2,min({FRAME_MAX_EDGE},ih))'"
)


def _frame_at(tools: Tools, src: Path, t_ms: int, timeout: float) -> bytes | None:
    proc = _run(
        [
            tools.ffmpeg,
            "-nostdin",
            "-v",
            "error",
            "-ss",
            f"{t_ms / 1000:.3f}",
            "-i",
            _input_path(src),
            "-frames:v",
            "1",
            "-vf",
            _FRAME_SCALE,
            "-f",
            "image2pipe",
            "-c:v",
            "mjpeg",
            "-q:v",
            "3",
            "-",
        ],
        timeout,
    )
    if proc is None or not proc.stdout:
        return None
    data = proc.stdout
    if sniff_media(data[:SNIFF_BYTES]) != "image/jpeg" or len(data) > MAX_FRAME_BYTES:
        return None
    return data


def extract_frames(
    tools: Tools,
    src: Path,
    times_ms: list[int],
    *,
    timeout: float = FRAME_TIMEOUT,
    budget: float = FRAMES_BUDGET,
) -> list[tuple[int, bytes]]:
    """One JPEG per time, to stdout (no temp file). A time that gives nothing is
    retried 0.5 s and then 1.5 s earlier, and named by the time actually used,
    so the last frame is not silently lost. Frames that fail are skipped."""
    deadline = time.monotonic() + budget
    frames: dict[int, bytes] = {}
    for t_ms in times_ms:
        for back in (0, *FRAME_RETRY_BACKOFF_MS):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return sorted(frames.items())
            at = max(0, t_ms - back)
            if at in frames:
                break
            data = _frame_at(tools, src, at, min(timeout, remaining))
            if data is not None:
                frames[at] = data
                break
            if at == 0:
                break
    return sorted(frames.items())


# ---------------------------------------------------------------------------
# Transcoding and conversion
# ---------------------------------------------------------------------------

_EVEN_EDGE = f"trunc(min({TRANSCODE_MAX_EDGE},{{side}})/2)*2"
_TRANSCODE_SCALE = (
    "scale='if(gte(iw,ih)," + _EVEN_EDGE.format(side="iw") + ",-2)'"
    ":'if(gte(iw,ih),-2," + _EVEN_EDGE.format(side="ih") + ")'"
)


def transcode(tools: Tools, src: Path, dst: Path, timeout: float = TRANSCODE_TIMEOUT) -> bool:
    """Re-encode *src* to H.264 MP4 at *dst* (longest side at most 1280 px, CRF 28,
    AAC audio when there is any, metadata such as location dropped)."""
    proc = _run(
        [
            tools.ffmpeg,
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-i",
            _input_path(src),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-map_metadata",
            "-1",
            "-vf",
            f"{_TRANSCODE_SCALE},format=yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            str(TRANSCODE_CRF),
            "-c:a",
            "aac",
            "-b:a",
            "96k",
            "-movflags",
            "+faststart",
            str(dst),
        ],
        timeout,
    )
    return proc is not None and dst.is_file()


def _remux_without_metadata(
    tools: Tools, src: Path, dst: Path, timeout: float = TRANSCODE_TIMEOUT
) -> bool:
    """Copy the streams into a fresh container without source metadata."""
    proc = _run(
        [
            tools.ffmpeg,
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-i",
            _input_path(src),
            "-map_metadata",
            "-1",
            "-c",
            "copy",
            str(dst),
        ],
        timeout,
    )
    return proc is not None and dst.is_file()


def convert_heic(src: Path, tools: Tools | None = None) -> bytes | None:
    """A HEIC photo as JPEG bytes: sips (macOS), else ffmpeg. ``None`` when neither can."""
    with tempfile.TemporaryDirectory(prefix="lattice-media-") as tmp:
        out = Path(tmp) / "photo.jpg"
        attempts: list[list[str]] = []
        sips = find_sips()
        if sips:
            attempts.append([sips, "-s", "format", "jpeg", _input_path(src), "--out", str(out)])
        tools = tools if tools is not None else find_tools()
        if tools is not None:
            attempts.append(
                [tools.ffmpeg, "-nostdin", "-v", "error", "-y", "-i", _input_path(src)]
                + ["-frames:v", "1", "-q:v", "2", str(out)]
            )
        for argv in attempts:
            if _run(argv, CONVERT_TIMEOUT) is None or not out.is_file():
                continue
            data = out.read_bytes()
            if sniff_media(data[:SNIFF_BYTES]) == "image/jpeg":
                return data
    return None


# ---------------------------------------------------------------------------
# A video, ready to attach
# ---------------------------------------------------------------------------


@dataclass
class PreparedVideo:
    """What the CLI sends for one video, and what to tell the filer.

    ``notes``: ``(reason, detail)`` pairs: ``transcoded`` / ``not_transcoded``
    (``ffmpeg_failed`` or ``kept_smaller_original``), ``no_frames``
    (``ffmpeg_not_found`` or ``ffmpeg_failed``), ``one_frame`` (``no_duration``).
    """

    content: bytes
    content_type: str
    video: dict = field(default_factory=dict)
    frames: list[tuple[int, bytes]] = field(default_factory=list)
    converted_from: dict | None = None
    notes: list[tuple[str, str]] = field(default_factory=list)


def prepare_video(src: Path, content: bytes, content_type: str, sha256: str) -> PreparedVideo:
    """Transcode, probe and take frames of the video at *src* (whose bytes are
    *content*), each step optional: a missing or failing tool leaves the video
    as it is, without frames, and says so in ``notes``."""
    tools = find_tools()
    if tools is None:
        return PreparedVideo(content, content_type, notes=[("no_frames", "ffmpeg_not_found")])
    prepared = PreparedVideo(content, content_type)
    with tempfile.TemporaryDirectory(prefix="lattice-media-") as tmp:
        source_info = probe(tools, src)
        stored = src
        out = Path(tmp) / "video.mp4"
        if transcode(tools, src, out):
            data = out.read_bytes()
            if sniff_media(data[:SNIFF_BYTES]) != "video/mp4":
                prepared.notes.append(("not_transcoded", "ffmpeg_failed"))
            elif len(data) > len(content) and (source_info or {}).get("codec") == "h264":
                suffix = ".mov" if content_type == "video/quicktime" else ".mp4"
                stripped = Path(tmp) / f"video-stripped{suffix}"
                if _remux_without_metadata(tools, src, stripped):
                    stripped_data = stripped.read_bytes()
                    stripped_type = sniff_media(stripped_data[:SNIFF_BYTES])
                else:
                    stripped_data, stripped_type = b"", None
                if stripped_type in ("video/mp4", "video/quicktime") and len(stripped_data) < len(
                    data
                ):
                    prepared.converted_from = {
                        "content_type": content_type,
                        "size_bytes": len(content),
                        "sha256": sha256,
                    }
                    prepared.content = stripped_data
                    prepared.content_type = stripped_type
                    stored = stripped
                    prepared.notes.append(("not_transcoded", "kept_smaller_original"))
                else:
                    prepared.converted_from = {
                        "content_type": content_type,
                        "size_bytes": len(content),
                        "sha256": sha256,
                    }
                    prepared.content, prepared.content_type, stored = data, "video/mp4", out
                    prepared.notes.append(("transcoded", ""))
            else:
                prepared.converted_from = {
                    "content_type": content_type,
                    "size_bytes": len(content),
                    "sha256": sha256,
                }
                prepared.content, prepared.content_type, stored = data, "video/mp4", out
                prepared.notes.append(("transcoded", ""))
        else:
            prepared.notes.append(("not_transcoded", "ffmpeg_failed"))
        info = source_info if stored == src else probe(tools, stored)
        if info is None:
            prepared.notes.append(("no_frames", "ffmpeg_failed"))
            return prepared
        prepared.video = {k: info[k] for k in ("width", "height", "duration_ms") if k in info}
        duration = info.get("duration_ms")
        if duration is None:
            times = [0]
            prepared.notes.append(("one_frame", "no_duration"))
        else:
            times = frame_times(duration)
        prepared.frames = extract_frames(tools, stored, times)
        if not prepared.frames:
            prepared.notes.append(("no_frames", "ffmpeg_failed"))
    return prepared


__all__ = [
    "FFMPEG_ENV",
    "SIPS_ENV",
    "PreparedVideo",
    "Tools",
    "convert_heic",
    "extract_frames",
    "ffmpeg_state",
    "find_sips",
    "find_tools",
    "prepare_video",
    "probe",
    "transcode",
]
