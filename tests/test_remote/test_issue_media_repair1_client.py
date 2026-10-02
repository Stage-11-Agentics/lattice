"""LAT-368 repair 1, client side of hosted media uploads (PR #132 reviews B and C).

- An upload follows the operation rules (SPEC §8.6, EVALUATION G-8): a dropped
  answer is retried with the same staged object; a server that keeps answering
  busy ends in its own error, not in "unreachable".
- A refusal the server sends before reading the body (over quota, too large,
  rate limited) reaches the user as itself, not as a broken pipe (C1).
- A slow but moving upload is never cut off: the budget is inactivity, not the
  whole send (B5).
- Issue reads ask for media availability with the read probe's budget and
  never inside the offline window (B2).
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import http.client as http_client
import json
import socket
import struct
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from lattice.core.errors import OpError
from lattice.remote import client, http
from tests.test_remote.proxies import scripted_tcp

PROJECT = "demo"


def _remote(url: str, retry_seconds: float = 2.0) -> http.Remote:
    return http.Remote(alias="team", url=url, token="t", retry_seconds=retry_seconds)


def _params(content: bytes, name: str = "shot.png") -> dict:
    return {
        "media": [
            {
                "payload": {
                    "filename": name,
                    "content_b64": base64.b64encode(content).decode("ascii"),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            }
        ]
    }


def _staged(content: bytes) -> dict:
    return {
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
        "content_type": "image/png",
        "staged": True,
    }


def _envelope(status: int, payload: dict, headers: dict[str, str] | None = None) -> bytes:
    raw = json.dumps(payload).encode()
    head = [
        f"HTTP/1.1 {status} X",
        "Content-Type: application/json",
        "Lattice-Protocol: 1",
        f"Content-Length: {len(raw)}",
        "Connection: close",
        *(f"{k}: {v}" for k, v in (headers or {}).items()),
    ]
    return ("\r\n".join(head) + "\r\n\r\n").encode() + raw


def _read_request(conn: socket.socket) -> tuple[bytes, bytes]:
    """The request head and its whole body (by ``Content-Length``)."""
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(65536)
        if not chunk:
            return data, b""
        data += chunk
    head, body = data.split(b"\r\n\r\n", 1)
    length = 0
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            length = int(value.strip())
    while len(body) < length:
        chunk = conn.recv(65536)
        if not chunk:
            break
        body += chunk
    return head, body


@contextmanager
def upload_server(answers: list[Any]) -> Iterator[dict]:
    """Answer upload *n* with ``answers[n]`` (the last repeats): ``"drop"`` reads
    the whole request and closes without an answer; a ``(status, payload)``
    pair answers with that envelope after reading the body."""
    listen = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listen.bind(("127.0.0.1", 0))
    listen.listen(8)
    listen.settimeout(0.1)
    seen: dict[str, Any] = {"paths": [], "url": f"http://127.0.0.1:{listen.getsockname()[1]}"}
    stop = threading.Event()

    def serve(conn: socket.socket) -> None:
        try:
            head, _body = _read_request(conn)
            seen["paths"].append(head.split(b"\r\n", 1)[0].decode())
            answer = answers[min(len(seen["paths"]) - 1, len(answers) - 1)]
            if answer != "drop":
                conn.sendall(_envelope(*answer))
        except OSError:
            pass
        finally:
            conn.close()

    def accept() -> None:
        while not stop.is_set():
            try:
                conn, _ = listen.accept()
            except (TimeoutError, OSError):
                continue
            threading.Thread(target=serve, args=(conn,), daemon=True).start()

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    try:
        yield seen
    finally:
        stop.set()
        thread.join(timeout=5)
        listen.close()


# ---------------------------------------------------------------------------
# Item 2: uploads follow the operation rules
# ---------------------------------------------------------------------------


def test_a_dropped_upload_answer_is_retried_with_the_same_object(
    capsys: pytest.CaptureFixture,
) -> None:
    content = b"\x89PNG\r\n\x1a\n" + b"x" * 2000
    with upload_server(["drop", (201, {"ok": True, "data": _staged(content)})]) as server:
        staged = client.stage_issue_media(_remote(server["url"]), PROJECT, _params(content))
    sha = hashlib.sha256(content).hexdigest()
    assert server["paths"] == [f"PUT /v1/projects/demo/issues/media/staging/{sha} HTTP/1.1"] * 2
    assert staged["media"][0]["payload"] == {
        "filename": "shot.png",
        "sha256": sha,
        "size": len(content),
        "staged": True,
    }
    err = capsys.readouterr().err
    assert "lattice: server team (" in err and "is not available; retrying for up to 2 s" in err


def test_an_upload_that_never_connects_gives_up_in_plain_words() -> None:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    sock.close()  # nothing listens here
    started = time.monotonic()
    with pytest.raises(OpError) as raised:
        client.stage_issue_media(_remote(url, 1.0), PROJECT, _params(b"\x89PNG\r\n\x1a\nabc"))
    error = raised.value
    assert error.code == "SERVER_UNREACHABLE"
    assert error.message == (
        f"server team ({url}) is not available. Nothing was written; "
        "run the command again when it is back."
    )
    assert set(error.details) == {"remote", "url", "os_error", "waited_seconds"}
    assert "refused" in error.details["os_error"].lower()
    assert time.monotonic() - started >= 0.8  # it retried for the budget


def test_an_upload_in_the_offline_window_gives_up_at_once() -> None:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    sock.close()
    started = time.monotonic()
    with pytest.raises(OpError) as raised:
        client.stage_issue_media(
            _remote(url, 5.0), PROJECT, _params(b"\x89PNG\r\n\x1a\nabc"), offline=True
        )
    assert raised.value.code == "SERVER_UNREACHABLE"
    assert raised.value.details["waited_seconds"] == 0
    assert time.monotonic() - started < 2


def test_a_server_that_stays_busy_ends_in_its_own_error() -> None:
    busy = (429, {"ok": False, "error": {"code": "RATE_LIMITED", "message": "slow down"}})
    with upload_server([busy]) as server:
        with pytest.raises(OpError) as raised:
            client.stage_issue_media(
                _remote(server["url"], 0.6), PROJECT, _params(b"\x89PNG\r\n\x1a\nabc")
            )
    assert raised.value.code == "RATE_LIMITED"
    assert len(server["paths"]) >= 2  # retried like an operation


# ---------------------------------------------------------------------------
# Item 10 (client half): a refusal before the body is read is read and named
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (413, "MEDIA_QUOTA_EXCEEDED"),
        (413, "PAYLOAD_TOO_LARGE"),
    ],
)
def test_a_refusal_sent_before_the_body_is_read_is_surfaced_as_itself(
    status: int, code: str
) -> None:
    """The server answers on the headers and closes while the client is still
    sending (a broken pipe on the client): the client reads the answer."""
    answer = _envelope(status, {"ok": False, "error": {"code": code, "message": f"{code} here"}})
    content = b"\x89PNG\r\n\x1a\n" + b"\0" * (16 * 1024 * 1024)
    connections = []

    def refuse(conn: socket.socket) -> None:
        connections.append(1)
        conn.sendall(answer)

    with scripted_tcp(refuse) as url:
        with pytest.raises(OpError) as raised:
            client.stage_issue_media(_remote(url, 1.0), PROJECT, _params(content))
    assert raised.value.code == code
    assert raised.value.message == f"{code} here"
    assert len(connections) == 1  # a refusal is not retried


def test_a_send_cut_short_with_no_answer_is_still_unreachable() -> None:
    content = b"\x89PNG\r\n\x1a\n" + b"\0" * (16 * 1024 * 1024)

    def hang_up(conn: socket.socket) -> None:
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))  # RST

    with scripted_tcp(hang_up) as url:
        with pytest.raises(http.Unreachable):
            http.request(
                _remote(url),
                "PUT",
                "/x",
                raw_body=content,
                content_type="application/octet-stream",
                policy=client.MEDIA_UPLOAD_POLICY,
            )


# ---------------------------------------------------------------------------
# Item 9: an upload's budget is inactivity, not the whole send
# ---------------------------------------------------------------------------


def test_a_slow_but_moving_upload_is_not_cut_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """A link at 1 MiB/s sends a 3 MiB body in about 3 s; each piece moves
    within the 1 s budget, so the upload succeeds."""
    rate = 1024 * 1024
    send = http_client.HTTPConnection.send

    def throttled(self: http_client.HTTPConnection, data: Any) -> None:
        if isinstance(data, bytes | bytearray | memoryview):
            time.sleep(len(data) / rate)
        send(self, data)

    monkeypatch.setattr(http_client.HTTPConnection, "send", throttled)
    # The real upload policy, with a 1 s budget instead of 60 s.
    monkeypatch.setattr(
        client,
        "MEDIA_UPLOAD_POLICY",
        dataclasses.replace(client.MEDIA_UPLOAD_POLICY, response_seconds=1.0),
    )
    content = b"\x89PNG\r\n\x1a\n" + b"\0" * (3 * 1024 * 1024)
    with upload_server([(201, {"ok": True, "data": _staged(content)})]) as server:
        staged = client.stage_issue_media(_remote(server["url"], 0.1), PROJECT, _params(content))
    assert staged["media"][0]["payload"]["staged"] is True
    assert len(server["paths"]) == 1


def test_an_upload_with_no_progress_still_times_out() -> None:
    """Inactivity is still bounded: a server that takes the request and never
    answers ends the attempt after the budget."""
    policy = http.Policy(5.0, 0.5, idle=True)

    def silent(conn: socket.socket) -> None:
        time.sleep(3)

    started = time.monotonic()
    with scripted_tcp(silent) as url:
        with pytest.raises(http.Unreachable) as raised:
            http.request(
                _remote(url),
                "PUT",
                "/x",
                raw_body=b"abc",
                content_type="application/octet-stream",
                policy=policy,
            )
    assert time.monotonic() - started < 2.5
    # The socket timeout or the watchdog, whichever notices first.
    assert raised.value.reason in {"timed out", "no progress for 0.5 s"}


# ---------------------------------------------------------------------------
# Item 6: availability is a read: probe budget, offline window
# ---------------------------------------------------------------------------


def test_availability_uses_the_read_probe_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    from lattice.remote import issue_media

    policies = []

    def fake_get_json(remote: http.Remote, path: str, **kwargs: Any) -> dict:
        policies.append(kwargs.get("policy"))
        return {"issues": {"iss_01M3ZB7K0000000000000000AA": []}}

    monkeypatch.setattr(issue_media, "get_json", fake_get_json)
    issue_media.availability(
        _remote("http://127.0.0.1:9"), PROJECT, ["iss_01M3ZB7K0000000000000000AA"]
    )
    assert policies == [http.PROBE]


def _view(issue_id: str, media_id: str, content: bytes) -> dict:
    return {
        "id": issue_id,
        "media": [
            {
                "id": media_id,
                "n": 1,
                "kind": "photo",
                "original_name": "shot.png",
                "content_type": "image/png",
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
            }
        ],
    }


def test_inside_the_offline_window_availability_makes_no_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lattice.remote import issue_media

    window = tmp_path / ".lattice" / "cache" / "unreachable_until"
    window.parent.mkdir(parents=True)
    window.write_text(f"{time.time() + 15:.3f}\n")

    def no_request(*_args: Any, **_kwargs: Any) -> dict:
        raise AssertionError("no network attempt inside the offline window")

    monkeypatch.setattr(issue_media, "get_json", no_request)
    view = _view("iss_01M3ZB7K0000000000000000AA", "med_01M3ZB7K0000000000000000AB", b"png")
    entry = issue_media.annotate_views(tmp_path, _remote("http://127.0.0.1:9"), PROJECT, [view])[
        0
    ]["media"][0]
    assert (entry["available"], entry["missing"], entry["path"]) == ("unreachable", False, None)


def test_an_unreachable_availability_request_opens_the_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lattice.remote import issue_media

    def unreachable(remote: http.Remote, *_args: Any, **_kwargs: Any) -> dict:
        raise client.server_unreachable(remote, "timed out")

    monkeypatch.setattr(issue_media, "get_json", unreachable)
    view = _view("iss_01M3ZB7K0000000000000000AA", "med_01M3ZB7K0000000000000000AB", b"png")
    entry = issue_media.annotate_views(tmp_path, _remote("http://127.0.0.1:9"), PROJECT, [view])[
        0
    ]["media"][0]
    assert entry["available"] == "unreachable" and entry["missing"] is False
    until = float((tmp_path / ".lattice" / "cache" / "unreachable_until").read_text())
    assert until > time.time()


def test_media_lines_say_unreachable_on_hosted_and_keep_local_wording() -> None:
    from lattice.core.issue_media import UNREACHABLE_MEDIA_TEXT, format_media_lines

    base = {"n": 1, "kind": "photo", "original_name": "shot.png", "size_bytes": 10}
    hosted_offline = format_media_lines(
        [{**base, "path": None, "missing": False, "available": "unreachable"}]
    )
    assert hosted_offline[1].strip() == UNREACHABLE_MEDIA_TEXT
    assert "not on the server" not in "\n".join(hosted_offline)
    hosted_gone = format_media_lines(
        [{**base, "path": None, "missing": True, "available": "missing"}]
    )
    assert hosted_gone[1].strip() == "(missing: not on the server)"
    # A local entry (no ``available``) keeps the local wording (B minor 11).
    local = format_media_lines([{**base, "path": None, "missing": True}])
    assert local[1].strip() == "(missing: None)"
    local_path = format_media_lines([{**base, "path": "/b/m.png", "missing": True}])
    assert local_path[1].strip() == "(missing: /b/m.png)"


def test_an_unreachable_video_does_not_claim_ffmpeg_was_missing() -> None:
    from lattice.core.issue_media import NO_FRAMES_TEXT, format_media_lines

    entry = {
        "n": 1,
        "kind": "video",
        "original_name": "clip.mp4",
        "size_bytes": 10,
        "path": None,
        "missing": False,
        "available": "unreachable",
        "frames": [],
    }
    assert NO_FRAMES_TEXT not in "\n".join(format_media_lines([entry]))
