"""AC-46 check before retry (SPEC §8.6 "Op status"): an operation still pending,
including one queued behind another for the project's locks, reports
``in_flight``, never ``not_found``; once committed it reports ``committed``;
a rolled-back one reports ``not_found``."""

from __future__ import annotations

import contextlib
import json
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from lattice.core.ids import generate_op_id
from lattice.server import tokens, transactions
from lattice.server.testing import ServerHandle, make_root, running_server

SLUG = "demo"


@pytest.fixture()
def served(tmp_path: Path) -> Iterator[tuple[ServerHandle, str]]:
    root = make_root(
        tmp_path, projects={SLUG: {"code": "DEM"}}, config={"audit": {"enabled": False}}
    )
    token = tokens.create_token(root, user="human:alice", machine="laptop", projects=[SLUG])
    with running_server(root) as handle:
        yield handle, token["token"]


def _state(handle: ServerHandle, token: str, op_id: str) -> str:
    status, _, body = handle.request("GET", f"/v1/projects/{SLUG}/ops/{op_id}", token=token)
    assert status == 200, body
    return body["data"]["state"]


def _post(handle: ServerHandle, token: str, op_id: str, title: str, out: list) -> threading.Thread:
    def send() -> None:
        out.append(handle.op(SLUG, "task.create", {"title": title}, token=token, op_id=op_id))

    thread = threading.Thread(target=send, daemon=True)
    thread.start()
    return thread


def test_pending_operations_are_in_flight_until_committed(
    served: tuple[ServerHandle, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    handle, token = served
    paused, release = threading.Event(), threading.Event()

    def fault(point: str, **ctx: object) -> None:
        if point == "journal.write" and not paused.is_set():
            paused.set()
            assert release.wait(10), "the test never released the commit"

    monkeypatch.setattr(transactions, "_fault", fault)
    first, second = generate_op_id(), generate_op_id()
    answers: list = []
    one = _post(handle, token, first, "Paused before its journal commit", answers)
    assert paused.wait(5)
    two = _post(handle, token, second, "Queued behind it", answers)
    # The second request is waiting for the project's locks: it counts too.
    deadline = time.monotonic() + 5
    while (handle.state.pending_ops.get((SLUG, _token_id(handle, token), second)) or 0) < 1:
        assert time.monotonic() < deadline, "the second request never reached admission"
        time.sleep(0.01)
    assert _state(handle, token, first) == "in_flight"
    assert _state(handle, token, second) == "in_flight"

    seen: list[str] = []
    stop = threading.Event()

    def watch() -> None:
        while not stop.is_set():
            seen.extend(_state(handle, token, op) for op in (first, second))

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    release.set()
    one.join(10)
    two.join(10)
    stop.set()
    watcher.join(10)

    assert [status for status, _, _ in answers] == [200, 200]
    assert _state(handle, token, first) == "committed"
    assert _state(handle, token, second) == "committed"
    assert "not_found" not in seen
    assert handle.state.pending_ops == {}


def test_a_rolled_back_operation_is_not_found(
    served: tuple[ServerHandle, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    handle, token = served

    def fault(point: str, **ctx: object) -> None:
        if point == "journal.write":
            raise OSError(5, "injected before the commit point")

    monkeypatch.setattr(transactions, "_fault", fault)
    op_id = generate_op_id()
    status, _, _ = handle.op(
        SLUG, "task.create", {"title": "Rolled back"}, token=token, op_id=op_id
    )
    assert status >= 400
    assert _state(handle, token, op_id) == "not_found"
    assert handle.state.pending_ops == {}


def test_another_tokens_pending_operation_is_not_found(
    served: tuple[ServerHandle, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    handle, token = served
    other = tokens.create_token(handle.root, user="human:bob", machine="box", projects=[SLUG])
    paused, release = threading.Event(), threading.Event()

    def fault(point: str, **ctx: object) -> None:
        if point == "journal.write" and not paused.is_set():
            paused.set()
            release.wait(10)

    monkeypatch.setattr(transactions, "_fault", fault)
    op_id = generate_op_id()
    answers: list = []
    thread = _post(handle, token, op_id, "Alice's", answers)
    assert paused.wait(5)
    try:
        assert _state(handle, other["token"], op_id) == "not_found"
        assert _state(handle, token, op_id) == "in_flight"
    finally:
        release.set()
        thread.join(10)


def _token_id(handle: ServerHandle, token: str) -> str:
    status, _, body = handle.request("GET", "/v1/info", token=token)
    assert status == 200, body
    return body["data"]["identity"]["token_id"]


# ---------------------------------------------------------------------------
# The pending set's lifecycle: every exit path leaves it empty
# ---------------------------------------------------------------------------


class RecordingPending(dict):
    """``ServerState.pending_ops`` that records the count after every change,
    ``0`` when the pair leaves."""

    def __init__(self) -> None:
        super().__init__()
        self.history: list[tuple[str, int]] = []

    def __setitem__(self, key, value) -> None:  # noqa: ANN001
        super().__setitem__(key, value)
        self.history.append((key[2], value))

    def __delitem__(self, key) -> None:  # noqa: ANN001
        super().__delitem__(key)
        self.history.append((key[2], 0))

    def counts(self, op_id: str) -> list[int]:
        return [value for key, value in self.history if key == op_id]


@contextlib.contextmanager
def _server(tmp_path: Path, **limits: int) -> Iterator[tuple[ServerHandle, str, RecordingPending]]:
    root = make_root(
        tmp_path, projects={SLUG: {"code": "DEM"}}, config={"audit": {"enabled": False}}
    )
    token = tokens.create_token(root, user="human:alice", machine="laptop", projects=[SLUG])
    config = {"limits": limits} if limits else None
    with running_server(root, config=config) as handle:
        pending = RecordingPending()
        handle.state.pending_ops = pending
        yield handle, token["token"], pending


@contextlib.contextmanager
def _paused_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[threading.Event, threading.Event]]:
    """Pause the first operation that reaches its journal commit."""
    paused, release = threading.Event(), threading.Event()

    def fault(point: str, **ctx: object) -> None:
        if point == "journal.write" and not paused.is_set():
            paused.set()
            release.wait(10)

    monkeypatch.setattr(transactions, "_fault", fault)
    try:
        yield paused, release
    finally:
        release.set()


def _wait_for(predicate, timeout: float = 5.0) -> None:  # noqa: ANN001
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


def test_a_retry_queued_behind_its_own_first_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same (token, op_id) twice: the count goes 1, 2, 1, 0; op status is
    in_flight until the commit; the retry replays the stored result."""
    with (
        _server(tmp_path) as (handle, token, pending),
        _paused_commit(monkeypatch) as (
            paused,
            release,
        ),
    ):
        op_id = generate_op_id()
        answers: list = []
        first = _post(handle, token, op_id, "Once", answers)
        assert paused.wait(5)
        retry = _post(handle, token, op_id, "Once", answers)
        _wait_for(lambda: pending.counts(op_id)[-1:] == [2])
        assert _state(handle, token, op_id) == "in_flight"
        release.set()
        first.join(10)
        retry.join(10)
        assert pending.counts(op_id) == [1, 2, 1, 0]
        assert pending == {}
        assert _state(handle, token, op_id) == "committed"
        results = sorted(body["data"]["result"]["replayed"] for _, _, body in answers)
        assert results == [False, True]
        assert len(list((handle.project(SLUG).board / "tasks").glob("*.json"))) == 1


def test_a_request_without_an_op_id_never_joins(tmp_path: Path) -> None:
    with _server(tmp_path) as (handle, token, pending):
        status, _, body = handle.op(SLUG, "task.create", {"title": "No op_id"}, token=token)
        assert status == 200, body
        assert pending.history == [] and pending == {}


def test_a_replay_joins_and_leaves(tmp_path: Path) -> None:
    with _server(tmp_path) as (handle, token, pending):
        op_id = generate_op_id()
        for _ in range(2):
            status, _, body = handle.op(
                SLUG, "task.create", {"title": "Twice"}, token=token, op_id=op_id
            )
            assert status == 200, body
        assert body["data"]["result"]["replayed"] is True
        assert pending.counts(op_id) == [1, 0, 1, 0]
        assert pending == {}


def test_a_rejection_and_a_rollback_leave(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _server(tmp_path) as (handle, token, pending):
        rejected = generate_op_id()
        status, _, _ = handle.op(
            SLUG,
            "task.status",
            {"task": "DEM-99", "new_status": "done"},
            token=token,
            op_id=rejected,
        )
        assert status == 404
        assert pending.counts(rejected) == [1, 0]

        def fault(point: str, **ctx: object) -> None:
            if point == "journal.write":
                raise OSError(5, "injected before the commit point")

        monkeypatch.setattr(transactions, "_fault", fault)
        rolled_back = generate_op_id()
        status, _, _ = handle.op(
            SLUG, "task.create", {"title": "x"}, token=token, op_id=rolled_back
        )
        assert status >= 500
        assert pending.counts(rolled_back) == [1, 0]
        assert pending == {}
        assert _state(handle, token, rejected) == "not_found"
        assert _state(handle, token, rolled_back) == "not_found"


def test_a_lock_timeout_leaves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with (
        _server(tmp_path, lock_timeout_seconds=1) as (handle, token, pending),
        _paused_commit(monkeypatch) as (paused, release),
    ):
        holder, waiter = generate_op_id(), generate_op_id()
        answers: list = []
        first = _post(handle, token, holder, "Holds the locks", answers)
        assert paused.wait(5)
        status, _, body = handle.op(
            SLUG, "task.create", {"title": "Waits"}, token=token, op_id=waiter
        )
        assert status == 503 and body["error"]["code"] == "BOARD_BUSY"
        assert pending.counts(waiter) == [1, 0]
        assert _state(handle, token, waiter) == "not_found"
        assert _state(handle, token, holder) == "in_flight"
        release.set()
        first.join(10)
        assert pending == {}


def test_the_per_token_limit_bounds_the_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with (
        _server(tmp_path, max_inflight_per_token=1) as (handle, token, pending),
        _paused_commit(monkeypatch) as (paused, release),
    ):
        holder, refused = generate_op_id(), generate_op_id()
        answers: list = []
        first = _post(handle, token, holder, "In flight", answers)
        assert paused.wait(5)
        status, _, body = handle.op(
            SLUG, "task.create", {"title": "Over"}, token=token, op_id=refused
        )
        assert status == 429 and body["error"]["code"] == "RATE_LIMITED"
        assert pending.counts(refused) == []  # refused before it could join
        assert len(pending) == 1
        release.set()
        first.join(10)
        assert pending == {}


def test_a_client_that_disconnects_while_queued_leaves_no_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The request stays pending while it waits for the locks (it can still
    commit); whatever happens to it, the pair leaves when it ends."""
    with (
        _server(tmp_path) as (handle, token, pending),
        _paused_commit(monkeypatch) as (
            paused,
            release,
        ),
    ):
        holder, gone = generate_op_id(), generate_op_id()
        answers: list = []
        first = _post(handle, token, holder, "Holds the locks", answers)
        assert paused.wait(5)
        body = json.dumps({"op_id": gone, "params": {"title": "Abandoned"}}).encode()
        sock = socket.create_connection(("127.0.0.1", handle.port))
        sock.sendall(
            f"POST /v1/projects/{SLUG}/ops/task.create HTTP/1.1\r\nHost: x\r\n"
            f"Authorization: Bearer {token}\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body
        )
        _wait_for(lambda: pending.counts(gone)[-1:] == [1])
        sock.close()
        assert _state(handle, token, gone) == "in_flight"
        release.set()
        first.join(10)
        _wait_for(lambda: pending == {})
        assert _state(handle, token, gone) in ("committed", "not_found")


def test_cancellation_while_awaiting_admission_leaves() -> None:
    """A request task cancelled at an ``await`` inside the block unwinds through
    ``pending_op`` (the path a cancelled admission wait takes)."""
    import asyncio
    from types import SimpleNamespace

    from lattice.server.app import pending_op

    state = SimpleNamespace(pending_ops={})
    key = (SLUG, "tok_x", generate_op_id())

    async def request() -> None:
        with pending_op(state, key):
            await asyncio.Event().wait()  # the admission wait that never ends

    async def main() -> None:
        task = asyncio.create_task(request())
        await asyncio.sleep(0)
        assert state.pending_ops == {key: 1}
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert state.pending_ops == {}

    asyncio.run(main())
