"""Issue media over the dashboard (LAT-366, AC-11): bytes, ranges, refusals, and a
held video connection that does not stall the board (M5)."""

from __future__ import annotations

import http.client
import json
import os
import socket
import threading
import time
from pathlib import Path

import pytest

from lattice.boards import resolve_board
from lattice.core.config import default_config, serialize_config
from lattice.dashboard.media import RANGE_CAP, UNSATISFIABLE, parse_range
from lattice.dashboard.server import DashboardBoard, create_server
from lattice.ops import Caller
from lattice.ops.task_attach import encode_payload
from lattice.storage.fs import atomic_write, ensure_lattice_dirs
from tests.issue_media_helpers import jpeg, mp4, png

VIDEO = mp4(bytes(range(256)) * 9000)  # a little over 2 MiB, past RANGE_CAP


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, None),
        ("bytes=0-9", (0, 9)),
        ("bytes=5-", (5, 99)),
        ("bytes=-5", (95, 99)),
        ("bytes=-500", (0, 99)),
        ("bytes=90-500", (90, 99)),
        ("bytes=100-", UNSATISFIABLE),
        ("bytes=-0", UNSATISFIABLE),
        ("bytes=0-1,5-6", None),
        ("bytes=9-3", None),
        ("items=0-1", None),
        ("bytes=-", None),
        ("bytes=١-٢", None),
    ],
)
def test_parse_range(header: str | None, expected: object) -> None:
    assert parse_range(header, 100, cap=1000) == expected


def test_parse_range_caps_long_ranges() -> None:
    assert parse_range("bytes=0-", 10_000, cap=4096) == (0, 4095)
    assert parse_range("bytes=10-9999", 10_000, cap=4096) == (10, 4105)


def test_parse_range_bounds_oversized_decimal_groups() -> None:
    oversized = "9" * 5000
    zero_padded = "0" * 5000 + "5"

    assert parse_range(f"bytes={oversized}-", 100) == UNSATISFIABLE
    assert parse_range(f"bytes=0-{oversized}", 100, cap=16) == (0, 15)
    assert parse_range(f"bytes=-{oversized}", 100, cap=16) == (0, 15)
    assert parse_range(f"bytes={oversized}-1", 100) is None
    assert parse_range(f"bytes={zero_padded}-9", 100) == (5, 9)


@pytest.fixture()
def served(tmp_path: Path):  # noqa: ANN201
    ensure_lattice_dirs(tmp_path)
    ld = tmp_path / ".lattice"
    config = default_config()
    config["issues"] = {"enabled": True}
    atomic_write(ld / "config.json", serialize_config(config))
    board = resolve_board(tmp_path)
    issue = board.execute(
        "issue.file",
        {
            "text": "t",
            "media": [
                {"payload": encode_payload("shot.png", png())},
                {
                    "payload": encode_payload("r.mov", VIDEO),
                    "frames": [{"t_ms": 0, "payload": encode_payload("f.jpg", jpeg())}],
                },
            ],
        },
        Caller(actor="agent:qa"),
    ).value
    server = create_server(ld, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.daemon = True
    thread.start()
    yield server, issue, ld, config
    server.shutdown()
    server.server_close()


def get(server, path: str, **headers: str) -> tuple[int, dict, bytes]:  # noqa: ANN001
    conn = http.client.HTTPConnection(*server.server_address, timeout=5)
    conn.request("GET", path, headers=headers)
    response = conn.getresponse()
    body = response.read()
    conn.close()
    return response.status, {k.lower(): v for k, v in response.getheaders()}, body


def url(issue: dict, n: int, frame: str | None = None) -> str:
    entry = issue["media"][n]
    path = f"/api/issues/{issue['id']}/media/{entry['id']}"
    return path + (f"/frames/{frame}" if frame else "")


def test_media_bytes_headers_and_etag(served) -> None:  # noqa: ANN001
    server, issue, _ld, _config = served
    status, headers, body = get(server, url(issue, 0))
    assert (status, body) == (200, png())
    assert headers["content-type"] == "image/png"
    assert headers["accept-ranges"] == "bytes"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["content-security-policy"] == "default-src 'none'; sandbox"
    assert headers["etag"] == f'"{issue["media"][0]["sha256"]}"'
    assert headers["content-disposition"].startswith('inline; filename="med_')
    assert get(server, url(issue, 0), **{"If-None-Match": headers["etag"]})[0] == 304
    status, headers, body = get(server, url(issue, 1, "t0000.000s.jpg"))
    assert (status, headers["content-type"], body) == (200, "image/jpeg", jpeg())


def test_ranges(served) -> None:  # noqa: ANN001
    server, issue, _ld, _config = served
    size = len(VIDEO)
    status, headers, body = get(server, url(issue, 1), Range="bytes=0-9")
    assert (status, headers["content-range"], body) == (206, f"bytes 0-9/{size}", VIDEO[:10])
    assert headers["content-type"] == "video/mp4"
    assert get(server, url(issue, 1), Range="bytes=-5")[2] == VIDEO[-5:]
    status, headers, _ = get(server, url(issue, 1), Range=f"bytes={size}-")
    assert (status, headers["content-range"]) == (416, f"bytes */{size}")
    assert get(server, url(issue, 1), Range="bytes=0-1,4-5")[2] == VIDEO
    status, headers, body = get(server, url(issue, 1), Range="bytes=0-")
    assert (status, len(body)) == (206, RANGE_CAP)
    assert headers["content-range"] == f"bytes 0-{RANGE_CAP - 1}/{size}"


def test_ranges_work_without_pread(served, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    server, issue, _ld, _config = served
    monkeypatch.delattr(os, "pread", raising=False)

    status, headers, body = get(server, url(issue, 1), Range="bytes=17-31")

    assert (status, headers["content-range"], body) == (
        206,
        f"bytes 17-31/{len(VIDEO)}",
        VIDEO[17:32],
    )


def test_oversized_range_headers_keep_http_range_behavior(served) -> None:  # noqa: ANN001
    server, issue, _ld, _config = served
    oversized = "9" * 5000

    status, headers, body = get(server, url(issue, 1), Range=f"bytes=0-{oversized}")
    assert (status, len(body)) == (206, RANGE_CAP)
    assert headers["content-range"] == f"bytes 0-{RANGE_CAP - 1}/{len(VIDEO)}"

    status, headers, body = get(server, url(issue, 1), Range=f"bytes={oversized}-")
    assert (status, headers["content-range"], body) == (416, f"bytes */{len(VIDEO)}", b"")

    status, _headers, body = get(server, url(issue, 1), Range=f"bytes={oversized}-1")
    assert (status, body) == (200, VIDEO)


def test_refusals_serve_no_bytes(served, tmp_path: Path) -> None:  # noqa: ANN001
    server, issue, ld, config = served
    base = f"/api/issues/{issue['id']}/media/"
    assert get(server, "/api/issues/iss_bad/media/" + issue["media"][0]["id"])[0] == 400
    assert get(server, base + "..%2F..%2Fconfig.json")[0] == 400
    assert get(server, base + "med_01K00000000000000000000000")[0] == 404
    assert get(server, url(issue, 1, "..%2Fx.jpg"))[0] == 404
    assert get(server, url(issue, 1, "t0009.000s.jpg"))[0] == 404

    # a symlink planted at the media path is never followed
    photo = Path(issue["media"][0]["path"])
    secret = tmp_path / "secret.png"
    secret.write_bytes(b"secret")
    photo.unlink()
    photo.symlink_to(secret)
    status, _headers, body = get(server, url(issue, 0))
    assert status == 404 and b"secret" not in body

    # removed media is never served, even if its bytes linger
    board = resolve_board(ld.parent)
    board.execute(
        "issue.detach",
        {"issue": issue["id"], "media": "2", "reason": "r"},
        Caller(actor="agent:qa"),
    )
    Path(issue["media"][1]["path"]).write_bytes(VIDEO)
    assert get(server, url(issue, 1))[0] == 404

    config["issues"] = {"enabled": False}
    atomic_write(ld / "config.json", serialize_config(config))
    status, _headers, body = get(server, url(issue, 0))
    assert status == 409 and json.loads(body)["error"]["code"] == "ISSUES_DISABLED"


def test_issue_directory_symlink_cannot_read_sibling_media(served, tmp_path: Path) -> None:  # noqa: ANN001
    server, issue, ld, _config = served
    entry = issue["media"][0]
    media_root = ld / "issues" / "media"
    issue_dir = media_root / issue["id"]
    issue_dir.rename(tmp_path / "saved-issue-media")
    sibling = media_root / "sibling"
    sibling.mkdir()
    (sibling / Path(entry["path"]).name).write_bytes(b"sibling secret")
    issue_dir.symlink_to(sibling, target_is_directory=True)

    status, _headers, body = get(server, url(issue, 0))
    assert status == 404
    assert b"sibling secret" not in body


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO files are unavailable")
def test_non_regular_media_is_refused_without_blocking(served) -> None:  # noqa: ANN001
    server, issue, _ld, _config = served
    photo = Path(issue["media"][0]["path"])
    photo.unlink()
    os.mkfifo(photo)

    status, _headers, body = get(server, url(issue, 0))

    assert status == 404
    assert b"No such media" in body


def test_path_fallback_serves_media_and_rejects_detected_symlinks(
    served, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ANN001
    server, issue, ld, _config = served
    monkeypatch.setattr(os, "supports_dir_fd", set(), raising=False)

    status, _headers, body = get(server, url(issue, 0))
    assert (status, body) == (200, png())

    entry = issue["media"][0]
    media_root = ld / "issues" / "media"
    issue_dir = media_root / issue["id"]
    issue_dir.rename(tmp_path / "saved-issue-media")
    sibling = media_root / "sibling"
    sibling.mkdir()
    (sibling / Path(entry["path"]).name).write_bytes(b"sibling secret")
    issue_dir.symlink_to(sibling, target_is_directory=True)

    status, _headers, body = get(server, url(issue, 0))
    assert status == 404
    assert b"sibling secret" not in body


def test_bound_checkout_is_local_only(served) -> None:  # noqa: ANN001
    _server, issue, ld, _config = served
    target = DashboardBoard(resolve_board(ld.parent), hosted=True)
    server = create_server(ld, "127.0.0.1", 0, board=target)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.daemon = True
    thread.start()
    try:
        status, _headers, body = get(server, url(issue, 0))
        assert status == 400 and json.loads(body)["error"]["code"] == "LOCAL_ONLY"
    finally:
        server.shutdown()
        server.server_close()


def test_a_held_video_does_not_stall_the_board(served) -> None:  # noqa: ANN001
    """M5: a media response a client stops reading runs beside the board's requests."""
    server, issue, _ld, _config = served
    held = socket.create_connection(server.server_address, timeout=5)
    held.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    held.sendall(f"GET {url(issue, 1)} HTTP/1.0\r\nRange: bytes=0-\r\n\r\n".encode())
    time.sleep(0.05)  # the media response is now blocked on a full socket
    try:
        started = time.monotonic()
        status, _headers, _body = get(server, "/api/tasks")
        assert status == 200
        assert time.monotonic() - started < 2
    finally:
        held.close()
