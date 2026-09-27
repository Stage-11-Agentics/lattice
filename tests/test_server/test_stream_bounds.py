"""AC-15 (stream bounds, SPEC §8.9): slow readers are disconnected without slowing
writes, the per-project subscriber cap, slots freed on disconnect, the response
headers, and the replay reset threshold."""

from __future__ import annotations

import json
import socket
import time
import urllib.parse
from collections.abc import Iterator
from pathlib import Path

import pytest

from lattice.server.testing import BoardServer, open_stream, serve_board, wait_for

HEARTBEAT = 0.3


@pytest.fixture()
def board(tmp_path: Path) -> Iterator[BoardServer]:
    config = {
        "limits": {
            "stream_queue_entries": 2,
            "max_stream_subscribers_per_project": 2,
            "replay_reset_entries": 5,
        }
    }
    with serve_board(tmp_path, audit=False, config=config, heartbeat_seconds=HEARTBEAT) as served:
        yield served


def stalled_stream(board: BoardServer) -> socket.socket:
    """A stream whose client reads the response head and then never reads again."""
    parts = urllib.parse.urlsplit(board.url)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    sock.connect((parts.hostname, parts.port))
    sock.sendall(
        (
            f"GET /v1/projects/{board.slug}/stream HTTP/1.1\r\nHost: x\r\n"
            f"Authorization: Bearer {board.token}\r\n\r\n"
        ).encode()
    )
    head = b""
    while b"\r\n\r\n" not in head:
        head += sock.recv(1)
    assert head.startswith(b"HTTP/1.1 200"), head
    return sock


def test_a_reader_that_never_reads_is_disconnected_and_writes_stay_fast(
    board: BoardServer,
) -> None:
    task = board.op("task.create", {"title": "t"})["task"]["id"]
    stalled = stalled_stream(board)
    project = board.project
    assert wait_for(lambda: project.broadcaster.count() == 1)
    payload = json.dumps({"blob": "x" * 60_000})
    with board.stream() as healthy:
        assert healthy.next().event == "heartbeat"
        assert project.broadcaster.count() == 2
        slowest = 0.0
        writes = 0
        while project.broadcaster.overflows == 0:
            started = time.monotonic()
            board.op("task.event", {"task": task, "event_type": "x_blob", "data": payload})
            slowest = max(slowest, time.monotonic() - started)
            writes += 1
            assert writes < 400, "the stalled stream was never disconnected"
            # The healthy reader keeps up, so its queue never fills.
            message = healthy.next_of("journal")
            assert message.data["seq"] == writes + 1
        assert slowest < 1.0
        assert project.broadcaster.count() == 1  # only the stalled stream is gone
        board.op("task.comment", {"task": task, "text": "after"})
        assert healthy.next_of("journal").data["op"] == "task.comment"
    stalled.close()
    assert any(x.get("event") == "stream_overflow" for x in board.handle.log_lines)


def test_the_subscriber_cap_and_a_slot_freed_by_a_disconnect(board: BoardServer) -> None:
    first = open_stream(board.url, board.slug, board.token)
    with board.stream() as second:
        assert first.next().event == "heartbeat" and second.next().event == "heartbeat"
        with board.stream() as third:
            assert third.status == 429
        first.close()  # the client goes away
        freed_at = time.monotonic()
        assert wait_for(lambda: board.project.broadcaster.count() == 1, timeout=HEARTBEAT)
        with board.stream() as fourth:
            assert fourth.status == 200
            assert fourth.next().event == "heartbeat"
        assert time.monotonic() - freed_at < HEARTBEAT + 1.0


def test_over_the_cap_is_rate_limited_with_an_envelope(board: BoardServer) -> None:
    with board.stream() as a, board.stream() as b:
        a.next()
        b.next()
        status, headers, body = board.handle.request(
            "GET", f"/v1/projects/{board.slug}/stream", token=board.token
        )
        assert status == 429 and body["error"]["code"] == "RATE_LIMITED"
        assert headers["retry-after"] == "1"


def test_stream_headers(board: BoardServer) -> None:
    with board.stream() as reader:
        assert reader.status == 200
        assert reader.headers["content-type"] == "text/event-stream"
        assert reader.headers["cache-control"] == "no-store"
        assert reader.headers["x-accel-buffering"] == "no"
        assert reader.headers["lattice-protocol"] == "1"


def test_a_resume_point_past_the_replay_threshold_gets_reset(board: BoardServer) -> None:
    task = board.op("task.create", {"title": "t"})["task"]["id"]
    for n in range(6):
        board.op("task.comment", {"task": task, "text": str(n)})
    journal = board.project.journal
    head = journal.head_seq
    old = f"{journal.epoch}:1:{journal.hash_at(1)}"  # head - 1 = 6 > 5 entries behind
    with board.stream(last_event_id=old) as reader:
        first = reader.next()
        assert first.event == "reset" and first.data == {"epoch": journal.epoch}
        assert reader.next().event == "heartbeat"
    near = f"{journal.epoch}:{head - 5}:{journal.hash_at(head - 5)}"  # exactly 5 behind
    with board.stream(last_event_id=near) as reader:
        seqs = [reader.next_of("journal").data["seq"] for _ in range(5)]
        assert seqs == list(range(head - 4, head + 1))


def test_an_abort_ends_a_stream_blocked_in_send_and_releases_its_connection(
    tmp_path: Path,
) -> None:
    """Review round 1, finding 2: shutdown (and a failed publication or quarantine)
    ends a stream at once, even while its pump waits on a send a stalled client
    never drains, and aborts the connection so uvicorn's graceful shutdown has
    nothing to wait for."""
    with serve_board(tmp_path, audit=False, heartbeat_seconds=HEARTBEAT) as board:
        task = board.op("task.create", {"title": "t"})["task"]["id"]
        stalled = stalled_stream(board)
        project = board.project
        assert wait_for(lambda: project.broadcaster.count() == 1)
        subscriber = project.broadcaster._subscribers[0]
        payload = json.dumps({"blob": "x" * 60_000})
        for _ in range(120):
            board.op("task.event", {"task": task, "event_type": "x_blob", "data": payload})
            if len(subscriber._items) >= 3:
                break
        # Entries stay queued: the pump is blocked sending to the stalled client.
        assert wait_for(lambda: len(subscriber._items) >= 3, timeout=2)
        connections = board.handle._server.server_state.connections
        before = len(connections)
        started = time.monotonic()
        board.handle.state.registry.close_all_streams()
        assert wait_for(lambda: len(connections) < before, timeout=2)
        assert time.monotonic() - started < 1.0
        assert project.broadcaster.count() == 0
        # The client sees its connection end once it reads what was already sent.
        stalled.settimeout(5)
        try:
            while stalled.recv(1 << 20):
                pass
        except ConnectionResetError:
            pass
        stalled.close()
