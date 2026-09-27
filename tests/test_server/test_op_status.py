"""AC-46 check before retry (SPEC §8.6 "Op status"): an operation still pending,
including one queued behind another for the project's locks, reports
``in_flight``, never ``not_found``; once committed it reports ``committed``;
a rolled-back one reports ``not_found``."""

from __future__ import annotations

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
    root = make_root(tmp_path, projects={SLUG: {"code": "DEM"}})
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
