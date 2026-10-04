"""Issue media's pure logic (LAT-366): types, dimensions, frames, limits, text."""

from __future__ import annotations

import pytest

from lattice.core.issue_media import (
    MB,
    PhotoMetadataError,
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
    strip_photo_metadata,
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
        (heic(), "image/heic"),
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


def _jpeg_segment(marker: int, payload: bytes) -> bytes:
    return b"\xff" + bytes([marker]) + (len(payload) + 2).to_bytes(2, "big") + payload


def _exif_with_gps(orientation: int = 6) -> bytes:
    """Tiny little-endian TIFF with Orientation and a GPS IFD pointer."""
    orientation_entry = (0x0112).to_bytes(2, "little") + (3).to_bytes(2, "little")
    orientation_entry += (1).to_bytes(4, "little") + orientation.to_bytes(2, "little") + b"\0\0"
    gps_entry = (0x8825).to_bytes(2, "little") + (4).to_bytes(2, "little")
    gps_entry += (1).to_bytes(4, "little") + (38).to_bytes(4, "little")
    ifd = (2).to_bytes(2, "little") + orientation_entry + gps_entry + b"\0\0\0\0"
    gps = (1).to_bytes(2, "little") + b"\x00\x00\x01\x02" + b"\x02\x00\0\0\0\x00\0\0\0\x00"
    return b"Exif\0\0II*\0\x08\0\0\0" + ifd + gps


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    import struct
    import zlib

    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload))
    )


def _png_chunks(data: bytes) -> list[tuple[bytes, bytes]]:
    """Independent test parser for the framing of a complete PNG."""
    import struct
    import zlib

    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    rows = []
    pos = 8
    while True:
        size = struct.unpack(">I", data[pos : pos + 4])[0]
        kind = data[pos + 4 : pos + 8]
        payload = data[pos + 8 : pos + 8 + size]
        crc = struct.unpack(">I", data[pos + 8 + size : pos + 12 + size])[0]
        assert zlib.crc32(kind + payload) & 0xFFFFFFFF == crc
        rows.append((kind, payload))
        pos += 12 + size
        if kind == b"IEND":
            assert pos == len(data)
            return rows


def _jpeg_app1_payloads(data: bytes) -> list[bytes]:
    """Independent marker walk for metadata APP1 segments before scan data."""
    rows = []
    pos = 2
    while pos < len(data):
        assert data[pos] == 0xFF
        while data[pos] == 0xFF:
            pos += 1
        marker = data[pos]
        pos += 1
        if marker == 0xD9:
            return rows
        if marker == 0xDA:
            return rows
        length = int.from_bytes(data[pos : pos + 2], "big")
        payload = data[pos + 2 : pos + length]
        if marker == 0xE1:
            rows.append(payload)
        pos += length
    return rows


def test_strip_jpeg_metadata_preserves_only_orientation_and_is_idempotent() -> None:
    raw = (
        b"\xff\xd8"
        + _jpeg_segment(0xE0, b"JFIF\0\x01\x02")
        + _jpeg_segment(0xE1, _exif_with_gps())
        + _jpeg_segment(0xE1, b"http://ns.adobe.com/xap/1.0/\0<x:xmpmeta>secret</x:xmpmeta>")
        + _jpeg_segment(0xED, b"Photoshop 3.0\0IPTC secret")
        + _jpeg_segment(0xFE, b"comment secret")
        + jpeg()
    )
    # Remove the duplicate SOI from the legacy tail while retaining its SOF/EOI.
    raw = raw.replace(b"\xff\xd8\xff\xc0", b"\xff\xc0", 1)
    clean = strip_photo_metadata(raw, "image/jpeg")
    assert clean.startswith(b"\xff\xd8") and clean.endswith(b"\xff\xd9")
    assert b"XMP" not in clean and b"Photoshop" not in clean and b"comment secret" not in clean
    app1 = _jpeg_app1_payloads(clean)
    assert len(app1) == 1 and app1[0].startswith(b"Exif\0\0")
    tiff = app1[0][6:]
    assert tiff[:8] == b"II*\0\x08\0\0\0"
    count = int.from_bytes(tiff[8:10], "little")
    assert count == 1
    assert int.from_bytes(tiff[10:12], "little") == 0x0112
    assert int.from_bytes(tiff[18:20], "little") == 6
    assert strip_photo_metadata(clean, "image/jpeg") == clean


def test_orientation_exif_follows_the_retained_app0_wherever_it_appears() -> None:
    app0 = _jpeg_segment(0xE0, b"JFIF\0\x01\x02")
    raw = (
        b"\xff\xd8"
        + _jpeg_segment(0xDB, b"\0")
        + app0
        + _jpeg_segment(0xE1, _exif_with_gps())
        + jpeg()[2:]
    )
    clean = strip_photo_metadata(raw, "image/jpeg")
    app1 = _jpeg_segment(0xE1, _jpeg_app1_payloads(clean)[0])
    assert clean.index(app1) == clean.index(app0) + len(app0)


@pytest.mark.parametrize("unknown_type", [13, 129])
def test_exif_orientation_skips_unknown_ifd0_types(unknown_type: int) -> None:
    unknown_entry = (0x0100).to_bytes(2, "little") + unknown_type.to_bytes(2, "little")
    unknown_entry += (1).to_bytes(4, "little") + b"\x01\x00\x00\x00"
    orientation_entry = (0x0112).to_bytes(2, "little") + (3).to_bytes(2, "little")
    orientation_entry += (1).to_bytes(4, "little") + b"\x06\x00\x00\x00"
    tiff = b"II*\0\x08\0\0\0" + (2).to_bytes(2, "little")
    tiff += unknown_entry + orientation_entry + b"\0\0\0\0"
    raw = b"\xff\xd8" + _jpeg_segment(0xE1, b"Exif\0\0" + tiff) + jpeg()[2:]

    clean = strip_photo_metadata(raw, "image/jpeg")

    retained = _jpeg_app1_payloads(clean)
    assert len(retained) == 1
    assert retained[0] == b"Exif\0\0" + (
        b"II*\0\x08\0\0\0\x01\0\x12\x01\x03\0\x01\0\0\0\x06\0\0\0\0\0\0\0"
    )


def test_strip_jpeg_skips_stray_bytes_between_segments() -> None:
    base = jpeg()
    raw = base[:-2] + b"camera-padding" + base[-2:]

    assert strip_photo_metadata(raw, "image/jpeg") == base


def test_strip_jpeg_drops_jfxx_but_keeps_jfif() -> None:
    jfif = _jpeg_segment(0xE0, b"JFIF\0\x01\x02")
    jfxx = _jpeg_segment(0xE0, b"JFXX\0\x10\x00\x01thumbnail")
    raw = b"\xff\xd8" + jfif + jfxx + jpeg()[2:]

    clean = strip_photo_metadata(raw, "image/jpeg")

    assert jfif in clean
    assert jfxx not in clean and b"thumbnail" not in clean


def test_strip_progressive_jpeg_metadata_between_scans_and_trailing_payload() -> None:
    # Four components plus Adobe APP14 exercises the CMYK/YCCK color marker path.
    sof = _jpeg_segment(0xC2, b"\x08\0\x10\0\x10\x04\x01\x11\0\x02\x11\0\x03\x11\0\x04\x11\0")
    sos = _jpeg_segment(0xDA, b"\x01\x01\x00\x00\x3f\x00")
    entropy = b"\x12\xff\x00\x34\xff\xff\xd0\x56"
    raw = (
        b"\xff\xd8"
        + sof
        + sos
        + entropy
        + _jpeg_segment(0xE1, _exif_with_gps())
        + sos
        + entropy
        + _jpeg_segment(0xE1, b"http://ns.adobe.com/xap/1.0/\0later scan private")
        + _jpeg_segment(0xED, b"Photoshop 3.0\0later scan IPTC")
        + _jpeg_segment(0xFE, b"later scan comment")
        + _jpeg_segment(0xE2, b"ICC_PROFILE\0\x01\x01profile")
        + _jpeg_segment(0xE2, b"MPF\0synthetic motion-photo directory")
        + _jpeg_segment(0xEE, b"Adobe\0\x64\0\0\0\0")
        + b"\xff\xd9motion-photo-secret"
    )
    clean = strip_photo_metadata(raw, "image/jpeg")
    assert b"motion-photo-secret" not in clean
    app1 = _jpeg_app1_payloads(clean)
    assert len(app1) == 1 and b"Exif\0\0" in app1[0]
    assert b"ICC_PROFILE" in clean and b"Adobe" in clean
    assert b"MPF" not in clean
    assert clean.count(b"\xff\xe1") == 1
    assert b"\xff\xed" not in clean and b"\xff\xfe" not in clean
    assert b"later scan" not in clean and b"\x25\x88" not in app1[0]
    assert clean.count(b"\xff\xda") == 2
    assert clean.count(entropy) == 2
    assert b"\xff\x00" in clean and b"\xff\xff\xd0" in clean
    assert strip_photo_metadata(clean, "image/jpeg") == clean


def test_strip_jpeg_refuses_unknown_non_app_marker() -> None:
    raw = b"\xff\xd8" + _jpeg_segment(0x02, b"unknown required coding data") + jpeg()[2:]
    with pytest.raises(PhotoMetadataError, match="unsupported marker 0x02"):
        strip_photo_metadata(raw, "image/jpeg")


def test_independent_jpeg_parser_checks_metadata_after_progressive_scans() -> None:
    from tests.photo_metadata_helpers import assert_no_identifying_metadata, jpeg_segments

    sof = _jpeg_segment(0xC0, b"\x08\0\x10\0\x10\x01\x01\x11\0")
    sos = _jpeg_segment(0xDA, b"\x01\x01\x00\x00\x3f\x00")
    late_xmp = _jpeg_segment(0xE1, b"http://ns.adobe.com/xap/1.0/\0private")
    raw = b"\xff\xd8" + sof + sos + b"\x12\xff\x00\x34" + sos + b"\x56" + late_xmp + b"\xff\xd9"
    rows = jpeg_segments(raw)
    assert any(
        marker == 0xE1 and payload.startswith(b"http://ns.adobe.com/") for marker, payload in rows
    )
    with pytest.raises(AssertionError):
        assert_no_identifying_metadata(raw, "image/jpeg")


def test_strip_png_identifying_chunks_and_bytes_after_iend() -> None:
    import struct

    original = png(2, 1)
    ihdr = original[8:33]
    idat_start = original.index(b"IDAT") - 4
    idat_end = original.index(b"IEND") - 4
    idat = original[idat_start:idat_end]
    chunks = (
        ihdr
        + _png_chunk(b"eXIf", _exif_with_gps())
        + _png_chunk(b"tEXt", b"Location\0GPS secret")
        + _png_chunk(b"iTXt", b"XML:com.adobe.xmp\0\0\0\0\0<x:xmpmeta/>")
        + _png_chunk(b"zTXt", b"Comment\0\0compressed-secret")
        + _png_chunk(b"iDOT", b"future metadata")
        + _png_chunk(b"pHYs", struct.pack(">IIB", 3780, 3780, 1))
        + idat
        + _png_chunk(b"IEND", b"")
    )
    raw = b"\x89PNG\r\n\x1a\n" + chunks + b"unparsed trailing bytes" + png(1, 1)
    clean = strip_photo_metadata(raw, "image/png")
    rows = _png_chunks(clean)
    kinds = [kind for kind, _data in rows]
    assert b"eXIf" not in kinds and b"tEXt" not in kinds and b"iTXt" not in kinds
    assert b"zTXt" not in kinds and b"iDOT" not in kinds
    assert b"pHYs" in kinds and rows[-1][0] == b"IEND"
    assert clean.endswith(_png_chunk(b"IEND", b""))
    assert strip_photo_metadata(clean, "image/png") == clean


def test_strip_png_refuses_unknown_critical_chunks() -> None:
    original = png(2, 1)
    ihdr_end = 8 + 25
    raw = original[:ihdr_end] + _png_chunk(b"ABCD", b"required decoder data") + original[ihdr_end:]
    with pytest.raises(PhotoMetadataError, match="unknown critical PNG chunk"):
        strip_photo_metadata(raw, "image/png")


def test_strip_png_refuses_a_chunk_with_an_invalid_crc() -> None:
    raw = bytearray(png(2, 1))
    idat = raw.index(b"IDAT")
    crc_at = idat + 4 + int.from_bytes(raw[idat - 4 : idat], "big")
    raw[crc_at] ^= 0x01

    with pytest.raises(PhotoMetadataError, match="checksum"):
        strip_photo_metadata(bytes(raw), "image/png")


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(jpeg()[:-2], id="truncated-before-sos"),
        pytest.param(
            jpeg()[:-2] + _jpeg_segment(0xDA, b"\x01\x01\x00\x00\x3f\x00") + b"\x12\x34",
            id="truncated-mid-scan",
        ),
        pytest.param(
            jpeg()[:-2]
            + _jpeg_segment(0xDA, b"\x01\x01\x00\x00\x3f\x00")
            + b"\x12\x34"
            + _jpeg_segment(0xFE, b"between scan and end"),
            id="without-eoi",
        ),
    ],
)
def test_strip_jpeg_refuses_missing_eoi(raw: bytes) -> None:
    with pytest.raises(PhotoMetadataError):
        strip_photo_metadata(raw, "image/jpeg")


@pytest.mark.parametrize(
    ("content_type", "data"),
    [
        ("image/jpeg", b"\xff\xd8\xff\xe1\x00\x20Exif"),
        ("image/png", b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDRshort"),
        (
            "image/png",
            b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", b"bad") + _png_chunk(b"IEND", b""),
        ),
    ],
)
def test_photo_metadata_stripper_refuses_malformed_framing(content_type: str, data: bytes) -> None:
    with pytest.raises(PhotoMetadataError):
        strip_photo_metadata(data, content_type)


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
