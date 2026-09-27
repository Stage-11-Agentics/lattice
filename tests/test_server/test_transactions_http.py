"""AC-4 (H-22a part) over HTTP: what a client sees of transaction recovery (SPEC §8.6).

The boundary matrix lives in ``test_transactions.py``; these cases run a real
server on ``127.0.0.1:0``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lattice.core.ids import generate_op_id
from lattice.server import control
from lattice.server.testing import running_server
from tests.test_server.conftest import board_hash, mint
from tests.test_server.faults import Injector, install

SLUG = "alpha"


def test_a_quarantined_project_answers_503_everywhere_and_changes_nothing(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = mint(root)
    with running_server(root) as server:
        status, _, body = server.op(SLUG, "task.create", {"title": "a"}, token=token)
        task = body["data"]["result"]["task"]["id"]
        op_id = body["data"]["op_id"]
        injector = install(monkeypatch, Injector("journal.fsync"))
        status, _, body = server.op(SLUG, "task.create", {"title": "b"}, token=token)
        assert status == 503 and body["error"]["code"] == "BOARD_UNAVAILABLE"
        injector.disarm()
        board = root / "projects" / SLUG / ".lattice"
        frozen = board_hash(root, SLUG), (board / "hosted" / "journal.jsonl").read_bytes()
        base = f"/v1/projects/{SLUG}"
        for method, path, body in (
            ("POST", f"{base}/ops/task.create", {"params": {"title": "c"}}),
            ("GET", f"{base}/tasks", None),
            ("GET", f"{base}/tasks/{task}", None),
            ("GET", f"{base}/ops/{op_id}", None),
        ):
            status, _, answer = server.request(method, path, token=token, body=body)
            assert status == 503, (method, path, answer)
            assert answer["error"]["code"] == "BOARD_UNAVAILABLE"
        assert (board_hash(root, SLUG), (board / "hosted" / "journal.jsonl").read_bytes()) == (
            frozen
        )
        status, _, _ = server.op("beta", "task.create", {"title": "d"}, token=token)
        assert status == 200


def test_a_rolled_back_write_answers_its_error_and_the_retry_applies_once(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = mint(root)
    op_id = generate_op_id()
    with running_server(root) as server:
        before = board_hash(root, SLUG)
        with monkeypatch.context() as m:
            install(m, Injector("receipt.fsync"))
            status, _, body = server.op(
                SLUG, "task.create", {"title": "x"}, token=token, op_id=op_id
            )
        assert status == 500 and body["error"]["code"] == "INTERNAL_ERROR"
        assert board_hash(root, SLUG) == before
        status, _, body = server.request("GET", f"/v1/projects/{SLUG}/ops/{op_id}", token=token)
        assert body["data"] == {"state": "not_found"}
        status, _, body = server.op(SLUG, "task.create", {"title": "x"}, token=token, op_id=op_id)
        assert status == 200 and body["data"]["result"]["replayed"] is False
        assert body["data"]["seq"] == 1


def test_control_requests_answer_through_strict_writes(root: Path) -> None:
    """The set-config control request runs as a transaction on a running server."""
    with running_server(root):
        board = root / "projects" / SLUG / ".lattice"
        answer = control.send_request(board, "set-config", {"set": {"review_mode": "inline"}})
        assert answer["ok"] is True, answer
        assert answer["result"]["seq"] == 1
        assert not list((board / "hosted" / "undo").iterdir())


# -- the admin side is strictly durable too (SPEC §8.6; LAT-306 review) ------


def _spy_dir_fsyncs(monkeypatch: pytest.MonkeyPatch, fail_at: int | None = None) -> list[Path]:
    """Record every directory ``control`` fsyncs; raise EIO at call *fail_at* (1-based)."""
    import errno

    calls: list[Path] = []
    real = control._fsync_dir

    def spy(directory: Path) -> None:
        calls.append(Path(directory))
        if fail_at is not None and len(calls) == fail_at:
            raise OSError(errno.EIO, "injected directory fsync failure")
        real(directory)

    monkeypatch.setattr(control, "_fsync_dir", spy)
    return calls


def test_a_control_request_fsyncs_every_directory_entry_it_creates_or_removes(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    board = root / "projects" / "alpha" / ".lattice"
    with running_server(root):
        shutil.rmtree(board / "hosted" / "control")
        calls = _spy_dir_fsyncs(monkeypatch)
        answer = control.send_request(board, "set-config", {"set": {"review_mode": "inline"}})
        assert answer["ok"] is True, answer
    hosted, directory = board / "hosted", board / "hosted" / "control"
    # mkdir of control/ (its parent), the request's rename, the answer's removal.
    assert calls == [hosted, directory, directory]
    assert list(directory.iterdir()) == []


@pytest.mark.parametrize("fail_at", [1, 2, 3], ids=["mkdir", "request", "cleanup"])
def test_a_failed_control_directory_fsync_propagates(
    root: Path, monkeypatch: pytest.MonkeyPatch, fail_at: int
) -> None:
    import shutil

    board = root / "projects" / "alpha" / ".lattice"
    with running_server(root):
        shutil.rmtree(board / "hosted" / "control")
        _spy_dir_fsyncs(monkeypatch, fail_at=fail_at)
        with pytest.raises(OSError, match="injected directory fsync failure"):
            control.send_request(board, "set-config", {"set": {"review_mode": "inline"}})


def test_a_retry_queued_behind_its_own_first_attempt_applies_once(root: Path) -> None:
    """AC-46: the idempotency check runs after admission, so a retry that waited
    behind its first attempt sees the committed result and replays it."""
    from concurrent.futures import ThreadPoolExecutor

    from lattice.server.testing import wait_for

    token = mint(root)
    op_id = generate_op_id()
    with running_server(root) as server, ThreadPoolExecutor(2) as pool:
        first = pool.submit(
            server.op, "alpha", "xtest.sleep", {"ms": 400}, token=token, op_id=op_id
        )
        assert wait_for(lambda: server.project("alpha").work.locked())
        retry = pool.submit(
            server.op, "alpha", "xtest.sleep", {"ms": 400}, token=token, op_id=op_id
        )
        (s1, _, b1), (s2, _, b2) = first.result(), retry.result()
    assert s1 == s2 == 200
    assert b1["data"]["seq"] == b2["data"]["seq"]
    assert b2["data"]["result"]["replayed"] is True and not b1["data"]["result"]["replayed"]
    journal = root / "projects" / "alpha" / ".lattice" / "hosted" / "journal.jsonl"
    assert [json.loads(x)["op_id"] for x in journal.read_text().splitlines()].count(op_id) == 1
