"""LAT-368 repair 1: the hosted media staging route and project quota.

A stalled upload is dropped after a receive deadline and frees its object and
quota; a client that goes away is logged as an aborted upload, not a crash; a
refusal sent before the body is read still reaches a client that keeps sending;
uploads need the issue log on; a damaged stage is repaired by a verified
re-upload; a detach frees quota at once.
"""

from __future__ import annotations

import json
import socket
import threading
from pathlib import Path

import pytest

from lattice.server import admin
from lattice.server import app as app_module
from lattice.server.testing import ServerHandle, wait_for
from tests.test_server.conftest import mint
from tests.test_server.test_issue_media_routes import (
    SLUG,
    blob,
    error_code,
    file_issue,
    filed,
    item,
    media_server,
    names,
    put_stage,
    sha,
    stage_dir,
    stage_ok,
)

MIB = 1024 * 1024


@pytest.fixture()
def root(root: Path) -> Path:
    """The issue log is on in ``alpha`` only (``beta`` keeps it off)."""
    admin.set_project_config(root, SLUG, {"issues.enabled": True})
    return root


@pytest.fixture()
def token(root: Path) -> str:
    return mint(root, projects=[SLUG, "beta"])


def _head(token: str, digest: str, length: int, slug: str = SLUG, extra: str = "") -> bytes:
    return (
        f"PUT /v1/projects/{slug}/issues/media/staging/{digest} HTTP/1.1\r\nHost: x\r\n"
        f"Authorization: Bearer {token}\r\nContent-Type: application/octet-stream\r\n"
        f"Content-Length: {length}\r\n{extra}\r\n"
    ).encode()


def _read_reply(conn: socket.socket) -> tuple[int, dict]:
    """Read one whole HTTP reply (headers and a Content-Length body)."""
    reply = b""
    while b"\r\n\r\n" not in reply:
        part = conn.recv(65536)
        assert part, f"connection closed before a reply: {reply!r}"
        reply += part
    head, _, body = reply.partition(b"\r\n\r\n")
    length = next(
        int(line.split(b":", 1)[1])
        for line in head.split(b"\r\n")[1:]
        if line.lower().startswith(b"content-length:")
    )
    while len(body) < length:
        part = conn.recv(65536)
        assert part, "connection closed mid-reply"
        body += part
    return int(head.split()[1]), json.loads(body[:length])


def _no_crash(server: ServerHandle) -> None:
    events = {line["event"] for line in server.log_lines}
    assert not events & {"op_crashed", "request_crashed"}, events


# ---------------------------------------------------------------------------
# A stalled or vanished uploader
# ---------------------------------------------------------------------------


def test_a_stalled_upload_is_dropped_and_frees_its_object_and_quota(
    root: Path, token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "UPLOAD_CHUNK_TIMEOUT", 0.3)
    data = blob(4000, b"stall")
    with media_server(root, max_issue_media_project_bytes=5000) as server:
        media = server.project(SLUG).issue_media
        conn = socket.create_connection(("127.0.0.1", server.port), timeout=10)
        try:
            conn.sendall(_head(token, sha(data), len(data)) + data[:10])  # then nothing
            status, body = _read_reply(conn)
        finally:
            conn.close()
        assert status == 408 and body["error"]["code"] == "UPLOAD_TIMEOUT", body
        assert wait_for(lambda: media._inflight == set() and media._reserved == {})
        assert names(stage_dir(root)) == []
        # The same object, at the full quota, uploads at once.
        assert put_stage(server, token, data)[0] == 201
        assert "issue_media_upload_timeout" in {line["event"] for line in server.log_lines}
        _no_crash(server)


def test_a_client_that_goes_away_mid_upload_is_logged_as_an_aborted_upload(
    root: Path, token: str
) -> None:
    data = blob(4000, b"gone")
    with media_server(root) as server:
        conn = socket.create_connection(("127.0.0.1", server.port), timeout=10)
        try:
            conn.sendall(_head(token, sha(data), len(data)) + data[:100])
            conn.shutdown(socket.SHUT_WR)
            conn.recv(65536)
        finally:
            conn.close()
        assert wait_for(
            lambda: "issue_media_upload_aborted" in {line["event"] for line in server.log_lines}
        )
        assert wait_for(lambda: names(stage_dir(root)) == [])
        _no_crash(server)


# ---------------------------------------------------------------------------
# A refusal before the body is read still reaches the client
# ---------------------------------------------------------------------------


def test_an_over_quota_refusal_reaches_a_client_that_sends_its_whole_body(
    root: Path, token: str
) -> None:
    data = blob(8 * MIB, b"over-quota")
    with media_server(
        root, max_issue_media_file_bytes=16 * MIB, max_issue_media_project_bytes=MIB
    ) as server:
        conn = socket.create_connection(("127.0.0.1", server.port), timeout=20)
        sent: dict = {}

        def send() -> None:
            try:
                # As the Lattice client (urllib) does: one request per connection.
                head = _head(token, sha(data), len(data), extra="Connection: close\r\n")
                conn.sendall(head + data)
                sent["ok"] = True
            except OSError as exc:  # a reset connection: the refusal was lost
                sent["error"] = exc

        sender = threading.Thread(target=send)
        sender.start()
        try:
            status, body = _read_reply(conn)
            sender.join(timeout=20)
        finally:
            conn.close()
        assert sent == {"ok": True}, sent
        assert status == 413 and body["error"]["code"] == "MEDIA_QUOTA_EXCEEDED", body
        assert names(stage_dir(root)) == []
        _no_crash(server)


def test_a_client_that_asked_for_100_continue_is_answered_without_sending_its_body(
    root: Path, token: str
) -> None:
    data = blob(4000, b"expect")
    with media_server(root, max_issue_media_project_bytes=1000) as server:
        conn = socket.create_connection(("127.0.0.1", server.port), timeout=1.5)
        try:
            conn.sendall(_head(token, sha(data), len(data), extra="Expect: 100-continue\r\n"))
            status, body = _read_reply(conn)  # well inside the drain's idle wait
        finally:
            conn.close()
        assert status == 413 and body["error"]["code"] == "MEDIA_QUOTA_EXCEEDED"


# ---------------------------------------------------------------------------
# Small server-side gaps
# ---------------------------------------------------------------------------


def test_uploads_are_refused_while_the_issue_log_is_off(root: Path, token: str) -> None:
    with media_server(root) as server:
        status, _, body = put_stage(server, token, blob(300, b"off"), slug="beta")
        assert status == 409 and error_code(body) == "ISSUES_DISABLED", body
        assert "issues.enabled=true" in json.loads(body)["error"]["message"]
        assert names(stage_dir(root, "beta")) == []
        assert server.project("beta").issue_media._reserved == {}


def test_a_verified_re_upload_repairs_a_damaged_stage(root: Path, token: str) -> None:
    data = blob(3000, b"repair")
    with media_server(root) as server:
        stage_ok(server, token, data)
        staged = stage_dir(root) / f"{sha(data)}.blob"
        damaged = bytearray(staged.read_bytes())
        damaged[-1] ^= 1
        staged.write_bytes(bytes(damaged))
        assert stage_ok(server, token, data)["sha256"] == sha(data)
        assert staged.read_bytes() == data
        assert (staged.stat().st_mode & 0o777) == 0o600
        status, _, body = file_issue(server, token, [item(data)])
        assert status == 200, body


def test_a_detach_frees_project_quota_at_once(root: Path, token: str) -> None:
    first, second = blob(2000, b"q1"), blob(2000, b"q2")
    with media_server(root, max_issue_media_project_bytes=3000) as server:
        stage_ok(server, token, first)
        issue = filed(server, token, [item(first)])
        status, _, body = server.op(
            SLUG,
            "issue.detach",
            {"issue": issue["id"], "media": "1", "reason": "room"},
            token=token,
        )
        assert status == 200, body
        assert server.project(SLUG).issue_media.published_bytes == 0
        stage_ok(server, token, second)
        status, _, body = file_issue(server, token, [item(second, "two.png")])
        assert status == 200, body
