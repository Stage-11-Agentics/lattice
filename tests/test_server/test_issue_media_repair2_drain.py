"""LAT-368 repair 2: a refused upload's discarded body stays inside the token's
in-flight limit and has a total time and byte bound."""

from __future__ import annotations

import socket
import time
from pathlib import Path

import pytest

from lattice.server import admin
from lattice.server import app as app_module
from tests.test_server.conftest import mint
from tests.test_server.test_issue_media_repair1_upload import _head, _read_reply
from tests.test_server.test_issue_media_routes import (
    SLUG,
    blob,
    error_code,
    media_server,
    put_stage,
    sha,
)

KIB = 1024


@pytest.fixture()
def root(root: Path) -> Path:
    admin.set_project_config(root, SLUG, {"issues.enabled": True})
    return root


@pytest.fixture()
def token(root: Path) -> str:
    return mint(root, projects=[SLUG])


def _drip(server, token: str, declared: int, *, pieces: int, pause: float):
    """Open a too-large upload and keep it alive by dripping bytes."""
    conn = socket.create_connection(("127.0.0.1", server.port), timeout=10)
    data = blob(declared, b"drip")
    conn.sendall(_head(token, sha(data), declared))
    return conn


def test_a_refused_upload_being_discarded_still_counts_against_the_inflight_limit(
    root: Path, token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "REFUSED_UPLOAD_DRAIN_IDLE", 1.5)
    with media_server(
        root, max_issue_media_file_bytes=4 * KIB, max_inflight_per_token=1
    ) as server:
        conn = _drip(server, token, 6 * KIB, pieces=0, pause=0)  # over the limit, under 2x
        try:
            conn.sendall(b"x" * 100)  # the server is now discarding the rest
            time.sleep(0.3)
            status, _, body = put_stage(server, token, blob(300, b"second"))
            assert status == 429 and error_code(body) == "RATE_LIMITED", (status, body)
        finally:
            conn.close()
        # Once the discard is over the slot is free again.
        time.sleep(0.3)
        assert put_stage(server, token, blob(300, b"third"))[0] == 201


def test_discarding_stops_after_its_total_time_however_the_client_keeps_sending(
    root: Path, token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    monkeypatch.setattr(app_module, "REFUSED_UPLOAD_DRAIN_IDLE", 5.0)
    monkeypatch.setattr(app_module, "REFUSED_UPLOAD_DRAIN_TOTAL", 1.0)
    with media_server(root, max_issue_media_file_bytes=4 * KIB) as server:
        conn = _drip(server, token, 6 * KIB, pieces=0, pause=0)
        stop = threading.Event()

        def trickle() -> None:
            # A trickle that would keep an idle-only discard alive for ever.
            for _ in range(6 * KIB - 1):
                if stop.is_set():
                    return
                try:
                    conn.sendall(b"x")
                except OSError:
                    return
                time.sleep(0.1)

        sender = threading.Thread(target=trickle, daemon=True)
        started = time.monotonic()
        sender.start()
        try:
            conn.settimeout(8.0)
            status, body = _read_reply(conn)
            elapsed = time.monotonic() - started
        finally:
            stop.set()
            conn.close()
        assert status == 413 and body["error"]["code"] == "PAYLOAD_TOO_LARGE"
        assert elapsed < 2.5, f"the refusal waited {elapsed:.1f}s for a trickling client"
