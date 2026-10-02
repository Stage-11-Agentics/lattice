"""Issue media's pure logic (LAT-366): types, dimensions, frames, limits, text."""

from __future__ import annotations

import pytest

from lattice.core.issue_media import (
    MB,
    clean_original_name,
    format_duration,
    format_media_lines,
    format_size,
    frame_name,
    frame_times,
    image_dimensions,
    media_limits,
    media_summary,
    next_media_n,
    parse_frame_name,
    present_media_bytes,
    sniff_heic,
    sniff_media,
)
from tests.issue_media_helpers import HTML_AS_PNG, SVG, gif, heic, jpeg, mov, mp4, png, webm, webp


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (png(), "image/png"),
        (jpeg(), "image/jpeg"),
        (gif(), "image/gif"),
        (webp(), "image/webp"),
        (mp4(), "video/mp4"),
        (mov(), "video/quicktime"),
        (webm(), "video/webm"),
        (HTML_AS_PNG, None),
        (SVG, None),
        (heic(), None),
        (b"\x00\x00\x00\x18ftypavif\x00\x00\x00\x00mif1avif", None),
        (b"%PDF-1.7\n", None),
        (b"II*\x00", None),  # TIFF
        (b"BM\x00\x00", None),
        (b"RIFF\x00\x00\x00\x00AVI LIST", None),
        (b"\x1a\x45\xdf\xa3\x42\x82\x88matroska", None),
        (b"", None),
    ],
)
def test_sniff_media_decides_by_content(data: bytes, expected: str | None) -> None:
    assert sniff_media(data[:64]) == expected


def test_sniff_heic_brands() -> None:
    assert sniff_heic(heic())
    assert sniff_heic(b"\x00\x00\x00\x18ftypmif1\x00\x00\x00\x00mif1heic")
    assert not sniff_heic(b"\x00\x00\x00\x18ftypmif1\x00\x00\x00\x00mif1avif")
    assert not sniff_heic(b"\x00\x00\x00\x18ftypavif\x00\x00\x00\x00mif1avif")
    assert not sniff_heic(mp4())


def test_image_dimensions_per_format_and_truncation() -> None:
    assert image_dimensions("image/png", png(1440, 900)) == (1440, 900)
    assert image_dimensions("image/jpeg", jpeg(640, 480)) == (640, 480)
    assert image_dimensions("image/gif", gif(5, 4)) == (5, 4)
    assert image_dimensions("image/webp", webp(7, 9)) == (7, 9)
    for kind, data in (("image/png", png()), ("image/jpeg", jpeg()), ("image/webp", webp())):
        assert image_dimensions(kind, data[:8]) is None
    assert image_dimensions("image/jpeg", b"\xff\xd8\x00\x00garbage") is None
    assert image_dimensions("video/mp4", mp4()) is None


@pytest.mark.parametrize(
    ("duration_ms", "expected"),
    [
        (500, [0]),
        (3000, [0, 1450, 2900]),
        (8000, [0, 1975, 3950, 5925, 7900]),
        (14200, [round(i * 14100 / 7) for i in range(8)]),
        (60000, [round(i * 59900 / 7) for i in range(8)]),
        (600000, [round(i * 599900 / 7) for i in range(8)]),
    ],
)
def test_frame_times(duration_ms: int, expected: list[int]) -> None:
    assert frame_times(duration_ms) == expected


def test_frame_names_round_trip_and_sort() -> None:
    assert frame_name(0) == "t0000.000s.jpg"
    assert frame_name(12500) == "t0012.500s.jpg"
    assert frame_name(14_100) == "t0014.100s.jpg"
    for t in (0, 1, 999, 1000, 14100, 12_345_678):
        assert parse_frame_name(frame_name(t)) == t
    assert parse_frame_name("t12.500s.jpg") is None
    assert parse_frame_name("../t0001.000s.jpg") is None
    assert parse_frame_name("t0001.000s.png") is None
    assert sorted(frame_name(t) for t in (9000, 100, 12000)) == [
        frame_name(t) for t in (100, 9000, 12000)
    ]


@pytest.mark.parametrize(
    ("section", "expected"),
    [
        (None, (100 * MB, 250 * MB)),
        ({"enabled": True}, (100 * MB, 250 * MB)),
        ({"max_media_mb": 1, "max_issue_media_mb": 3}, (1 * MB, 3 * MB)),
        ({"max_media_mb": 0, "max_issue_media_mb": -1}, (100 * MB, 250 * MB)),
        ({"max_media_mb": "5", "max_issue_media_mb": 2.5}, (100 * MB, 250 * MB)),
        ({"max_media_mb": True}, (100 * MB, 250 * MB)),
    ],
)
def test_media_limits_fall_back_to_defaults(section: object, expected: tuple) -> None:
    config = {} if section is None else {"issues": section}
    assert media_limits(config) == expected


def test_names_sizes_durations_and_summary() -> None:
    assert clean_original_name("/a/b/shot\x07.png") == "shot.png"
    assert clean_original_name("C:\\x\\y.png") == "y.png"
    assert len(clean_original_name("a" * 400)) == 255
    assert format_size(512) == "512 B"
    assert format_size(217004) == "212 KB"
    assert format_size(8493120) == "8.1 MB"
    assert format_size(100 * MB) == "100 MB"
    assert format_duration(14200) == "0:14"
    assert format_duration(3_723_000) == "1:02:03"
    entries = [
        {"kind": "photo"},
        {"kind": "video", "frames": [{}, {}, {}, {}]},
        {"kind": "photo", "removed": {"at": "x"}},
    ]
    assert media_summary(entries) == "1 photo, 1 video, 4 frames"
    assert media_summary([]) == ""
    snapshot = {
        "media": [
            {"n": 1, "size_bytes": 10},
            {"n": 4, "size_bytes": 5, "removed": {"at": "x"}},
        ]
    }
    assert present_media_bytes(snapshot) == 10
    assert next_media_n(snapshot) == 5
    assert next_media_n({}) == 1


def test_format_media_lines() -> None:
    lines = format_media_lines(
        [
            {
                "n": 1,
                "kind": "photo",
                "original_name": "footer.png",
                "width": 1440,
                "height": 900,
                "size_bytes": 217004,
                "path": "/b/m1.png",
            },
            {
                "n": 2,
                "kind": "video",
                "original_name": "repro.mov",
                "duration_ms": 14200,
                "size_bytes": 2 * MB,
                "path": "/b/m2.mp4",
                "frames": [{"t_ms": 0, "path": "/b/f0.jpg"}, {"t_ms": 1, "path": "/b/f1.jpg"}],
            },
            {"n": 3, "kind": "video", "size_bytes": 1, "path": "/b/m3.webm", "frames": []},
            {"n": 4, "kind": "photo", "size_bytes": 1, "path": "/b/m4.png", "missing": True},
            {"n": 5, "kind": "photo", "removed": {"at": "T", "by": "human:a", "reason": "key"}},
        ]
    )
    text = "\n".join(lines)
    assert "1  photo  footer.png  1440x900  212 KB" in text
    assert "   /b/m1.png" in text
    assert "0:14  2 MB  2 frames" in text
    assert "frames: /b/f0.jpg" in text and "        /b/f1.jpg" in text
    assert "no frames (ffmpeg was not available" in text
    assert "(missing: /b/m4.png)" in text
    assert lines[-1] == "5  removed T by human:a: key"
