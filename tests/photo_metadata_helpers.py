"""Tiny stdlib-built photo fixtures and independent metadata assertions."""

from __future__ import annotations

import struct
import zlib

from tests.issue_media_helpers import jpeg, png


def jpeg_segment(marker: int, payload: bytes) -> bytes:
    return b"\xff" + bytes([marker]) + (len(payload) + 2).to_bytes(2, "big") + payload


def exif_gps(orientation: int = 6, gps_value: int = 1) -> bytes:
    """A valid TIFF IFD0 with orientation and a GPS sub-IFD version entry."""
    orient = b"\x12\x01\x03\0\x01\0\0\0" + orientation.to_bytes(2, "little") + b"\0\0"
    gps_ptr = b"\x25\x88\x04\0\x01\0\0\0" + (38).to_bytes(4, "little")
    ifd0 = b"\x02\0" + orient + gps_ptr + b"\0\0\0\0"
    gps_entry = b"\0\0\x01\0\x04\0\0\0" + bytes([gps_value, 2, 0, 0])
    gps_ifd = b"\x01\0" + gps_entry + b"\0\0\0\0"
    return b"Exif\0\0II*\0\x08\0\0\0" + ifd0 + gps_ifd


def jpeg_with_gps(gps_value: int = 1, orientation: int = 6) -> bytes:
    return (
        b"\xff\xd8"
        + jpeg_segment(0xE0, b"JFIF\0\x01\x02")
        + jpeg_segment(0xE1, exif_gps(orientation, gps_value))
        + jpeg_segment(0xE1, b"http://ns.adobe.com/xap/1.0/\0<x:xmpmeta>private</x:xmpmeta>")
        + jpeg_segment(0xED, b"Photoshop 3.0\0IPTC private")
        + jpeg_segment(0xFE, b"private comment")
        + jpeg()[2:]
    )


def png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload))
    )


def png_with_gps(gps_value: int = 1) -> bytes:
    data = png(2, 1)
    ihdr_end = 8 + 25
    return (
        data[:ihdr_end]
        + png_chunk(b"eXIf", exif_gps(6, gps_value)[6:])
        + png_chunk(b"tEXt", b"Location\0private gps")
        + png_chunk(b"iTXt", b"XML:com.adobe.xmp\0\0\0\0\0<x:xmpmeta/>")
        + data[ihdr_end:]
    )


def jpeg_segments(data: bytes) -> list[tuple[int, bytes]]:
    """Independently parse JPEG markers, including entropy-coded scan data."""
    assert data.startswith(b"\xff\xd8")
    rows = []
    pos = 2
    while pos < len(data):
        assert data[pos] == 0xFF
        while data[pos] == 0xFF:
            pos += 1
        marker = data[pos]
        pos += 1
        if marker == 0xD9:
            assert pos == len(data)
            return rows
        if marker == 0xD8:
            raise AssertionError("unexpected JPEG start marker")
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            rows.append((marker, b""))
            continue
        length = int.from_bytes(data[pos : pos + 2], "big")
        assert 2 <= length <= len(data) - pos
        rows.append((marker, data[pos + 2 : pos + length]))
        pos += length
        if marker == 0xDA:
            scan = pos
            while True:
                marker_at = data.find(b"\xff", scan)
                assert marker_at >= 0, "JPEG scan lacks a following marker"
                code_at = marker_at + 1
                while code_at < len(data) and data[code_at] == 0xFF:
                    code_at += 1
                assert code_at < len(data), "JPEG scan ends inside a marker"
                code = data[code_at]
                if code == 0x00 or 0xD0 <= code <= 0xD7:
                    scan = code_at + 1
                    continue
                pos = marker_at
                break
    raise AssertionError("JPEG lacks EOI")


def png_chunks(data: bytes) -> list[tuple[bytes, bytes]]:
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    rows = []
    pos = 8
    while True:
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        kind = data[pos + 4 : pos + 8]
        payload = data[pos + 8 : pos + 8 + length]
        crc = struct.unpack(">I", data[pos + 8 + length : pos + 12 + length])[0]
        assert zlib.crc32(kind + payload) & 0xFFFFFFFF == crc
        rows.append((kind, payload))
        pos += 12 + length
        if kind == b"IEND":
            assert pos == len(data)
            return rows


def assert_no_identifying_metadata(data: bytes, content_type: str) -> None:
    """Inspect markers/chunks directly; do not call the implementation predicate."""
    if content_type == "image/jpeg":
        rows = jpeg_segments(data)
        assert all(marker not in {0xED, 0xFE} for marker, _payload in rows)
        app1 = [payload for marker, payload in rows if marker == 0xE1]
        assert all(payload.startswith(b"Exif\0\0") for payload in app1)
        for payload in app1:
            tiff = payload[6:]
            assert tiff[:2] == b"II"
            offset = int.from_bytes(tiff[4:8], "little")
            count = int.from_bytes(tiff[offset : offset + 2], "little")
            tags = [
                int.from_bytes(tiff[offset + 2 + 12 * index : offset + 4 + 12 * index], "little")
                for index in range(count)
            ]
            assert tags == [0x0112]
            assert (
                int.from_bytes(tiff[offset + 2 + 12 * count : offset + 6 + 12 * count], "little")
                == 0
            )
    elif content_type == "image/png":
        kinds = {kind for kind, _payload in png_chunks(data)}
        assert not kinds & {b"eXIf", b"tEXt", b"iTXt", b"zTXt", b"tIME", b"iDOT"}
    else:
        raise AssertionError(f"unsupported fixture content type: {content_type}")
