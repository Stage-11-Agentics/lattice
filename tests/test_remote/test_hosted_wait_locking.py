"""Hosted ``wait`` reads one whole cache state (SPEC §9.4; round-3 review, blocking 1).

H-10b's real apply is paused through its ``_seam`` hook right after it writes
the first of two task snapshots, so the cache holds a mixed state: task X
already at the target, task Y still at its old status, which was also the
target. Read unlocked, that mix says "both reached the target" although the
server's state is "X did, Y moved away". Every hosted status check in ``wait``
must hold the cache's shared read lock, which the paused apply holds
exclusively, so it can only see the state before or after the apply.

Synchronization is by events only: the apply pauses on an ``Event`` and the
test observes the check entering ``read_lock`` through a wrapper; nothing sleeps.
"""

from __future__ import annotations

import contextlib
import json
import threading
from pathlib import Path

import pytest

from lattice.cli import wait_cmd
from lattice.remote import cache
from tests.test_remote.hosted_board import RealHostedBoard, real_hosted_board

TARGET = "in_planning"


def _cache_status(board: RealHostedBoard, task_id: str) -> str:
    path = board.b / ".lattice" / "tasks" / f"{task_id}.json"
    return json.loads(path.read_text("utf-8"))["status"]


@pytest.fixture
def mixed(tmp_path, monkeypatch):
    """A board whose next catch-up pauses in the mixed state; yields
    ``(board, x, y, paused, release, entered, start_apply)``."""
    with real_hosted_board(tmp_path, monkeypatch) as board:
        first = board.create("X")["id"]
        second = board.create("Y")["id"]
        # The apply writes snapshots in path order: X is the one written first.
        x, y = sorted([first, second])
        board.status(y, TARGET)
        cache.catch_up(board.b, bulk=True)  # cache: X backlog, Y at the target
        board.status(x, TARGET)  # server: X reaches the target ...
        board.status(y, "planned")  # ... and Y moves away. Not all complete.

        paused, release = threading.Event(), threading.Event()

        def seam(step: str) -> None:
            if (
                step == "file_written"
                and not paused.is_set()
                and _cache_status(board, x) == TARGET
                and _cache_status(board, y) == TARGET
            ):
                paused.set()
                assert release.wait(10), "the test never released the apply"

        monkeypatch.setattr(cache, "_seam", seam)

        entered = threading.Event()
        real_read_lock = cache.read_lock

        @contextlib.contextmanager
        def observed_read_lock(root: Path):
            entered.set()
            with real_read_lock(root) as lattice_dir:
                yield lattice_dir

        monkeypatch.setattr(cache, "read_lock", observed_read_lock)

        applied: list[object] = []

        def start_apply() -> threading.Thread:
            thread = threading.Thread(
                target=lambda: applied.append(cache.catch_up(board.b, bulk=True)), daemon=True
            )
            thread.start()
            assert paused.wait(5), "the apply never reached the mixed state"
            return thread

        yield board, x, y, paused, release, entered, start_apply, applied
        release.set()


def test_the_status_check_cannot_see_a_half_applied_sync(mixed) -> None:
    board, x, y, paused, release, entered, start_apply, applied = mixed
    lattice_dir = board.b / ".lattice"
    apply_thread = start_apply()

    # The hazard is real: read without the lock, the mixed state says "all done".
    assert _cache_status(board, x) == _cache_status(board, y) == TARGET

    results: list[tuple[list[str], list[str]]] = []
    checker = threading.Thread(
        target=lambda: results.append(wait_cmd._check_tasks_status(lattice_dir, [x, y], TARGET)),
        daemon=True,
    )
    checker.start()
    assert entered.wait(5), "the hosted status check did not take the cache's read lock"
    # The apply holds the lock exclusively while paused, so no answer can exist yet.
    assert results == []

    release.set()
    apply_thread.join(5)
    checker.join(5)
    assert applied and applied[0].kind == "applied"
    # It saw the whole applied state: X at the target, Y not.
    assert results == [([x], [y])]

    # Once the server really completes both, the same check reports success.
    board.op(
        "task.status", {"task": y, "new_status": TARGET, "force": True, "reason": "test: back"}
    )
    cache.catch_up(board.b, bulk=True)
    assert wait_cmd._check_tasks_status(lattice_dir, [x, y], TARGET) == ([x, y], [])


def test_lattice_wait_never_reports_success_on_the_mixed_state(mixed) -> None:
    board, x, y, paused, release, entered, start_apply, applied = mixed
    apply_thread = start_apply()
    outcome: list[object] = []
    runner = threading.Thread(
        target=lambda: outcome.append(
            board.cli("wait", f"{x},{y}", "--status", TARGET, "--timeout", "2", "--json")
        ),
        daemon=True,
    )
    runner.start()
    assert entered.wait(5), "lattice wait did not take the cache's read lock"
    assert outcome == []  # blocked behind the paused apply, not answering from the mix
    release.set()
    apply_thread.join(5)
    runner.join(10)
    result = outcome[0]
    assert result.exit_code == 1, result.output
    body = json.loads(result.stdout)
    assert body["ok"] is False and body["data"]["all_complete"] is False
    assert body["data"]["pending"] == [y]
