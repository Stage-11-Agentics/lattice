"""``lattice.integrations.ffmpeg`` (LAT-366) with fake tools; one test runs the
real ffmpeg and carries the non-default ``ffmpeg`` marker (``pytest -m ffmpeg``)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from lattice.integrations import ffmpeg
from tests.issue_media_helpers import (
    fake_sips,
    fake_tools,
    heic,
    mov,
    mp4,
    probe_json,
    use_fake_ffmpeg,
)


def test_tool_lookup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert ffmpeg.find_tools() is None  # the hermetic default is off
    assert ffmpeg.ffmpeg_state() == "off"
    binary = fake_tools(tmp_path / "bin")
    monkeypatch.setenv("LATTICE_FFMPEG", str(binary))
    assert ffmpeg.find_tools() == ffmpeg.Tools(str(binary), str(tmp_path / "bin" / "ffprobe"))
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.setenv("LATTICE_FFMPEG", "ffmpeg")
    assert ffmpeg.find_tools() == ffmpeg.Tools(str(binary), str(tmp_path / "bin" / "ffprobe"))
    (tmp_path / "bin" / "ffprobe").unlink()
    assert ffmpeg.find_tools() is None
    assert ffmpeg.ffmpeg_state() == "missing"
    monkeypatch.delenv("LATTICE_FFMPEG")
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert ffmpeg.find_tools() is None


def test_explicit_relative_ffmpeg_path_resolves_from_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_tools(tmp_path / "bin")
    monkeypatch.chdir(tmp_path / "bin")
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setenv("LATTICE_FFMPEG", "./ffmpeg")

    real_which = shutil.which
    lookups: list[str] = []

    def record_lookup(command: str) -> str | None:
        lookups.append(command)
        return real_which(command)

    monkeypatch.setattr(shutil, "which", record_lookup)
    tools = ffmpeg.find_tools()
    assert tools is not None
    assert tools == ffmpeg.Tools(
        str((tmp_path / "bin" / "ffmpeg").resolve()),
        str((tmp_path / "bin" / "ffprobe").resolve()),
    )
    assert Path(tools.ffmpeg).is_absolute() and Path(tools.ffprobe).is_absolute()
    assert lookups == []


def test_probe_parses_rotation_and_duration_fallbacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_fake_ffmpeg(monkeypatch, tmp_path / "bin", width=1280, height=720, rotation=-90)
    tools = ffmpeg.find_tools()
    assert tools is not None
    src = tmp_path / "v.mov"
    src.write_bytes(mov())
    assert ffmpeg.probe(tools, src) == {
        "width": 720,
        "height": 1280,
        "duration_ms": 14200,
        "codec": "hevc",
    }
    monkeypatch.setenv("FAKE_PROBE", probe_json(duration=None, format_duration=2.5))
    assert ffmpeg.probe(tools, src)["duration_ms"] == 2500
    # M3: a streamed WebM declares no duration; the last packet gives it
    monkeypatch.setenv("FAKE_PROBE", probe_json(duration=None))
    monkeypatch.setenv("FAKE_PACKETS", "0.000000 1.500000 N/A 2.900000")
    assert ffmpeg.probe(tools, src)["duration_ms"] == 2900
    monkeypatch.setenv("FAKE_PACKETS", "")
    assert "duration_ms" not in ffmpeg.probe(tools, src)
    monkeypatch.setenv("FAKE_PROBE_FAIL", "1")
    assert ffmpeg.probe(tools, src) is None


def test_last_frame_is_retried_earlier_and_named_by_its_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M2: past the last decodable frame, try 0.5 s and then 1.5 s earlier."""
    use_fake_ffmpeg(monkeypatch, tmp_path / "bin")
    monkeypatch.setenv("FAKE_EMPTY_AFTER", "12.9")
    tools = ffmpeg.find_tools()
    assert tools is not None
    frames = ffmpeg.extract_frames(tools, tmp_path / "v.mov", [0, 6000, 14100])
    assert [t for t, _ in frames] == [0, 6000, 12600]


def test_a_tool_that_hangs_is_killed(tmp_path: Path) -> None:
    slow = tmp_path / "ffmpeg"
    slow.write_text("#!/bin/sh\nexec sleep 5\n")
    slow.chmod(0o755)
    tools = ffmpeg.Tools(str(slow), str(slow))
    assert ffmpeg.extract_frames(tools, tmp_path / "v", [0], timeout=0.2) == []
    assert ffmpeg.probe(tools, tmp_path / "v", timeout=0.2) is None


def test_tool_inputs_are_absolute_for_a_dash_leading_media_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    src = Path("-report")
    src.write_bytes(heic())
    monkeypatch.setenv("LATTICE_SIPS", str(fake_sips(tmp_path / "bin")))

    calls: list[list[str]] = []

    def fake_run(argv: list[str], timeout: float) -> subprocess.CompletedProcess:
        calls.append(argv)
        stdout = (
            probe_json(duration=None).encode()
            if Path(argv[0]).name == "ffprobe" and "packet=pts_time" not in argv
            else b""
        )
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr=b"")

    monkeypatch.setattr(ffmpeg, "_run", fake_run)
    tools = ffmpeg.Tools("ffmpeg", "ffprobe")
    ffmpeg.probe(tools, src)
    ffmpeg.extract_frames(tools, src, [0])
    ffmpeg.transcode(tools, src, tmp_path / "out.mp4")
    ffmpeg._remux_without_metadata(tools, src, tmp_path / "stripped.mp4")
    ffmpeg.convert_heic(src, tools)

    expected = str(src.absolute())
    inputs = []
    for argv in calls:
        if Path(argv[0]).name == "ffprobe":
            inputs.append(argv[-1])
        elif Path(argv[0]).name == "sips":
            inputs.append(argv[4])
        elif "-i" in argv:
            inputs.append(argv[argv.index("-i") + 1])
    assert {Path(argv[0]).name for argv in calls} >= {"ffmpeg", "ffprobe", "sips"}
    assert inputs and inputs == [expected] * len(inputs)


def test_prepare_video_without_duration_takes_one_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_fake_ffmpeg(monkeypatch, tmp_path / "bin", duration=None)
    monkeypatch.setenv("FAKE_TRANSCODE_FAIL", "1")
    src = tmp_path / "rec.webm"
    src.write_bytes(b"x")
    prepared = ffmpeg.prepare_video(src, b"x", "video/webm", "0" * 64)
    assert prepared.content == b"x" and prepared.converted_from is None
    assert [t for t, _ in prepared.frames] == [0]
    assert ("not_transcoded", "ffmpeg_failed") in prepared.notes
    assert ("one_frame", "no_duration") in prepared.notes


def test_heic_converts_with_sips_then_ffmpeg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "IMG.HEIC"
    src.write_bytes(heic())
    assert ffmpeg.convert_heic(src) is None  # both off
    monkeypatch.setenv("LATTICE_SIPS", str(fake_sips(tmp_path / "sips")))
    assert (ffmpeg.convert_heic(src) or b"").startswith(b"\xff\xd8\xff")
    monkeypatch.setenv("FAKE_SIPS_FAIL", "1")
    assert ffmpeg.convert_heic(src) is None
    use_fake_ffmpeg(monkeypatch, tmp_path / "bin")
    assert (ffmpeg.convert_heic(src) or b"").startswith(b"\xff\xd8\xff")


@pytest.mark.ffmpeg
@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="needs ffmpeg"
)
def test_real_ffmpeg_transcodes_and_takes_frames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LATTICE_FFMPEG", shutil.which("ffmpeg") or "")
    clip = tmp_path / "clip.mov"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=2:size=64x48:rate=25",
            "-c:v",
            "mjpeg",
            str(clip),
        ],
        check=True,
    )
    content = clip.read_bytes()
    prepared = ffmpeg.prepare_video(clip, content, "video/quicktime", "0" * 64)
    assert prepared.content_type == "video/mp4"
    assert prepared.converted_from == {
        "content_type": "video/quicktime",
        "size_bytes": len(content),
        "sha256": "0" * 64,
    }
    assert (prepared.video["width"], prepared.video["height"]) == (64, 48)
    assert abs(prepared.video["duration_ms"] - 2000) <= 100
    assert [t for t, _ in prepared.frames] == [0, 1900]
    assert all(data.startswith(b"\xff\xd8\xff") for _, data in prepared.frames)


@pytest.mark.ffmpeg
@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="needs ffmpeg"
)
def test_real_ffprobe_handles_a_regular_file_named_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ffmpeg_path = shutil.which("ffmpeg") or ""
    ffprobe_path = shutil.which("ffprobe") or ""
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "-report"
    subprocess.run(
        [
            ffmpeg_path,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:size=64x48:rate=1:duration=1",
            "-an",
            "-c:v",
            "mpeg4",
            "-f",
            "mp4",
            "-y",
            str(source),
        ],
        check=True,
    )

    info = ffmpeg.probe(ffmpeg.Tools(ffmpeg_path, ffprobe_path), Path("-report"))
    assert info is not None
    assert (info["width"], info["height"], info["codec"]) == (64, 48, "mpeg4")
    assert not list(tmp_path.glob("ffprobe-*.log"))


@pytest.mark.ffmpeg
@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="needs ffmpeg"
)
def test_smaller_h264_original_is_remuxed_without_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ffmpeg_path = shutil.which("ffmpeg") or ""
    ffprobe_path = shutil.which("ffprobe") or ""
    monkeypatch.setenv("LATTICE_FFMPEG", ffmpeg_path)
    source = tmp_path / "tagged.mp4"
    subprocess.run(
        [
            ffmpeg_path,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:size=64x48:rate=1:duration=1",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "35",
            "-pix_fmt",
            "yuv420p",
            "-metadata",
            "location=+37.7749-122.4194/",
            "-metadata",
            "title=secret title",
            "-movflags",
            "+faststart",
            "-y",
            str(source),
        ],
        check=True,
    )

    def format_tags(path: Path) -> dict[str, str]:
        result = subprocess.run(
            [
                ffprobe_path,
                "-v",
                "error",
                "-show_entries",
                "format_tags=location,title",
                "-of",
                "json",
                str(path.resolve()),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(result.stdout).get("format", {}).get("tags", {})

    source_tags = format_tags(source)
    assert source_tags.get("location") == "+37.7749-122.4194/"
    assert source_tags.get("title") == "secret title"

    def larger_transcode(_tools: ffmpeg.Tools, _src: Path, dst: Path) -> bool:
        # Force the already-H.264 smaller-original branch; the source is still
        # remuxed by the real ffmpeg binary below.
        dst.write_bytes(mp4() + b"x" * 4096)
        return True

    monkeypatch.setattr(ffmpeg, "transcode", larger_transcode)
    content = source.read_bytes()
    prepared = ffmpeg.prepare_video(source, content, "video/mp4", "a" * 64)
    assert ("not_transcoded", "kept_smaller_original") in prepared.notes
    assert prepared.converted_from == {
        "content_type": "video/mp4",
        "size_bytes": len(content),
        "sha256": "a" * 64,
    }
    stored = tmp_path / "stored.mp4"
    stored.write_bytes(prepared.content)
    stored_tags = format_tags(stored)
    assert "location" not in stored_tags
    assert "title" not in stored_tags
