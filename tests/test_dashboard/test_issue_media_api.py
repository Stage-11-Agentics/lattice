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
            "title": "t",
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


def get_raw(
    server, path: str, *, connect_host: str | None = None, **headers: str
) -> tuple[int, list[tuple[str, str]], bytes]:  # noqa: ANN001
    conn = http.client.HTTPConnection(
        connect_host or server.server_address[0], server.server_address[1], timeout=5
    )
    conn.request("GET", path, headers=headers)
    response = conn.getresponse()
    body = response.read()
    raw_headers = response.getheaders()
    conn.close()
    return response.status, raw_headers, body


def get(server, path: str, **headers: str) -> tuple[int, dict, bytes]:  # noqa: ANN001
    status, raw_headers, body = get_raw(server, path, **headers)
    return status, {k.lower(): v for k, v in raw_headers}, body


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


@pytest.mark.parametrize(
    "bad_sha",
    ['x"\r\nSet-Cookie: injected=1\r\n\r\n<script>', "€", "a" * 63 + "G\r\nX-Injected: yes"],
)
def test_malformed_snapshot_hash_is_omitted_at_header_boundary(
    served, monkeypatch: pytest.MonkeyPatch, bad_sha: str
) -> None:  # noqa: ANN001
    server, issue, _ld, _config = served
    import lattice.dashboard.media as media

    snapshot = {**issue, "media": [dict(entry) for entry in issue["media"]]}
    snapshot["media"][0]["sha256"] = bad_sha
    monkeypatch.setattr(media, "read_issue_snapshot", lambda _ld, _issue: snapshot)

    status, raw_headers, body = get_raw(server, url(issue, 0))

    headers = [(name.lower(), value) for name, value in raw_headers]
    assert status == 200 and body == png()
    assert not any(name == "etag" for name, _value in headers)
    assert not any(name == "set-cookie" for name, _value in headers)
    assert not any(name == "content-type" and value == "text/html" for name, value in headers)


@pytest.mark.parametrize(
    ("source", "bad_sha"),
    [
        ("snapshot", 'x"\r\nContent-Type: text/html\r\n\r\n<script>'),
        ("snapshot", "€"),
        ("replay", 'x"\r\nContent-Type: text/html\r\n\r\n<script>'),
        ("replay", "€"),
    ],
)
def test_malformed_persisted_hash_never_reaches_response_headers(
    served, source: str, bad_sha: str
) -> None:  # noqa: ANN001
    server, issue, ld, _config = served
    if source == "snapshot":
        snapshot_path = ld / "issues" / f"{issue['id']}.json"
        snapshot = json.loads(snapshot_path.read_text())
        snapshot["media"][0]["sha256"] = bad_sha
        snapshot_path.write_text(json.dumps(snapshot))
    else:
        event_path = ld / "issues" / "events" / f"{issue['id']}.jsonl"
        events = [json.loads(line) for line in event_path.read_text().splitlines()]
        for event in events:
            if (
                event["type"] == "issue_media_added"
                and event["data"]["media_id"] == issue["media"][0]["id"]
            ):
                event["data"]["sha256"] = bad_sha
        event_path.write_text(
            "".join(
                json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n" for event in events
            )
        )
        (ld / "issues" / f"{issue['id']}.json").unlink()

    status, raw_headers, body = get_raw(server, url(issue, 0))
    headers = [(name.lower(), value) for name, value in raw_headers]

    if source == "snapshot":
        assert status == 200 and body == png()
        assert dict(headers)["etag"] == f'"{issue["media"][0]["sha256"]}"'
    else:
        assert status == 500 and json.loads(body)["error"]["code"] == "INTEGRITY_ERROR"
    assert not any(name == "set-cookie" for name, _value in headers)
    assert not any(name == "content-type" and value == "text/html" for name, value in headers)


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


def test_get_rejects_non_loopback_host_for_media_and_task_routes(served) -> None:  # noqa: ANN001
    server, issue, _ld, _config = served
    hostile_host = f"attacker.example:{server.server_address[1]}"

    assert get(server, url(issue, 0), Host=hostile_host)[0] == 403
    assert get(server, "/api/tasks", Host=hostile_host)[0] == 403


@pytest.mark.parametrize("bad_snapshot", [[], {"media": [1]}, {"media": [{"content_type": []}]}])
def test_malformed_issue_snapshot_returns_structured_http_error(
    served, bad_snapshot: object
) -> None:  # noqa: ANN001
    server, issue, ld, _config = served
    atomic_write(ld / "issues" / f"{issue['id']}.json", json.dumps(bad_snapshot) + "\n")
    (ld / "issues" / "events" / f"{issue['id']}.jsonl").unlink()

    status, headers, body = get(server, url(issue, 0))

    assert status == 500
    assert headers["content-type"].startswith("application/json")
    envelope = json.loads(body)
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "INTEGRITY_ERROR"


def test_network_bind_allows_lan_host_for_api_and_media_gets(served) -> None:  # noqa: ANN001
    _local_server, issue, ld, _config = served
    server = create_server(ld, "0.0.0.0", 0)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.daemon = True
    thread.start()
    try:
        assert server.server_address[0] == "0.0.0.0"
        host = f"box.lan:{server.server_address[1]}"
        for path in ("/api/tasks", "/api/issues", url(issue, 0)):
            status, _headers, _body = get_raw(server, path, connect_host="127.0.0.1", Host=host)
            assert status == 200
    finally:
        server.shutdown()
        server.server_close()


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


def test_media_serves_through_a_symlinked_board_root(served, tmp_path: Path) -> None:  # noqa: ANN001
    _server, issue, ld, _config = served
    alias_root = tmp_path / "board-alias"
    alias_root.symlink_to(ld.parent, target_is_directory=True)
    alias_server = create_server(alias_root / ".lattice", "127.0.0.1", 0)
    thread = threading.Thread(target=alias_server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.daemon = True
    thread.start()
    try:
        status, _headers, body = get(alias_server, url(issue, 0))
        assert (status, body) == (200, png())
    finally:
        alias_server.shutdown()
        alias_server.server_close()


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


def test_bound_checkout_reads_issues_without_media_or_writes(served) -> None:  # noqa: ANN001
    _server, issue, ld, _config = served
    target = DashboardBoard(resolve_board(ld.parent), hosted=True)
    server = create_server(ld, "127.0.0.1", 0, board=target)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.daemon = True
    thread.start()
    try:
        status, _headers, body = get(server, url(issue, 0))
        assert status == 400 and json.loads(body)["error"]["code"] == "LOCAL_ONLY"
        # The checkout mirror exposes issue metadata, but no local media URLs.
        for path in ("/api/issues", f"/api/issues/{issue['id']}", "/api/issues?by=agent:qa"):
            status, _headers, body = get(server, path)
            assert status == 200, path
            data = json.loads(body)["data"]
            if path == f"/api/issues/{issue['id']}":
                assert data["id"] == issue["id"]
                rows = [data]
            else:
                rows = data
                assert any(row["id"] == issue["id"] for row in rows), path
            assert all(media["url"] is None for row in rows for media in row["media"]), path
        media_path = url(issue, 0)
        status, _headers, body = get(server, media_path)
        assert status == 400, media_path
        assert json.loads(body)["error"]["code"] == "LOCAL_ONLY", media_path
        for path in (
            "/api/issues",
            f"/api/issues/{issue['id']}/comment",
            f"/api/issues/{issue['id']}/dismiss",
            f"/api/issues/{issue['id']}/reopen",
        ):
            conn = http.client.HTTPConnection(*server.server_address, timeout=5)
            host = f"127.0.0.1:{server.server_address[1]}"
            conn.request(
                "POST",
                path,
                json.dumps({"title": "x", "body": "x", "reason": "x"}),
                {"Content-Type": "application/json", "Origin": f"http://{host}", "Host": host},
            )
            response = conn.getresponse()
            assert response.status == 400, path
            error = json.loads(response.read())["error"]
            assert error["code"] == "LOCAL_ONLY", path
            assert "bound checkout is read-only" in error["message"], path
            conn.close()
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
