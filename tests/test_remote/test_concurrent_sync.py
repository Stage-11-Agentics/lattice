"""AC-9 (H-10b part): deterministic interleavings of the syncer with the read
helpers. A reader holding ``read_lock`` never sees a sync half-applied, in
either order, and two syncs never interleave (the CLI-level cases are H-12's)."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from lattice.remote import cache
from lattice.storage.operations import read_task_authority, resolve_task_prose_path
from tests.test_remote.conftest import assert_mirror, create_task
from tests.test_remote.stub_sync_server import StubServer

WAIT = 5.0


def _task_ids(directory: Path) -> set[str]:
    return {p.stem for p in directory.glob("task_*.jsonl")} if directory.is_dir() else set()


class _Thread(threading.Thread):
    """A thread that keeps its target's result or exception."""

    def __init__(self, target) -> None:  # noqa: ANN001
        super().__init__(daemon=True)
        self._fn = target
        self.result = None
        self.error: BaseException | None = None

    def run(self) -> None:
        try:
            self.result = self._fn()
        except BaseException as exc:  # noqa: BLE001 - surfaced by the test
            self.error = exc


def _seam_gate(
    monkeypatch: pytest.MonkeyPatch, step: str
) -> tuple[threading.Event, threading.Event]:
    """Pause the syncer the first time it reaches *step*: returns (reached, release)."""
    reached, release = threading.Event(), threading.Event()

    def seam(name: str) -> None:
        if name == step and not reached.is_set():
            reached.set()
            release.wait(WAIT)

    monkeypatch.setattr(cache, "_seam", seam)
    return reached, release


def _applied_marks(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    marks: list[str] = []
    monkeypatch.setattr(cache, "_seam", marks.append)
    return marks


def _unarchive_pending(client: Path, stub: StubServer) -> str:
    """An archived task in the cache, with its unarchive waiting on the server."""
    task = create_task(stub)
    stub.op("task.archive", {"task": task})
    cache.catch_up(client)
    stub.op("task.unarchive", {"task": task})
    return task


def test_reader_between_enumerations_holds_off_an_unarchive(
    client_root: Path, stub: StubServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = _unarchive_pending(client_root, stub)
    marks = _applied_marks(monkeypatch)
    paused, resume = threading.Event(), threading.Event()

    def reader() -> list[str]:
        with cache.read_lock(client_root) as lattice:
            active = _task_ids(lattice / "events")
            paused.set()
            resume.wait(WAIT)
            archived = _task_ids(lattice / "archive" / "events")
            seen = [read_task_authority(lattice, t).location for t in sorted(active | archived)]
            return seen

    read = _Thread(reader)
    read.start()
    assert paused.wait(WAIT)
    sync = _Thread(lambda: cache.catch_up(client_root, bulk=True))
    sync.start()
    time.sleep(0.2)
    assert "applying_written" not in marks  # the apply waits for the reader
    resume.set()
    read.join(WAIT)
    sync.join(WAIT)
    assert read.error is None and sync.error is None
    assert read.result == ["archived"]  # exactly once, in one placement
    assert sync.result.kind == "applied"
    assert read_task_authority(client_root / ".lattice", task).location == "active"
    assert_mirror(client_root, stub)


def test_reader_between_plan_resolution_and_read_holds_off_an_unarchive(
    client_root: Path, stub: StubServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = _unarchive_pending(client_root, stub)
    marks = _applied_marks(monkeypatch)
    paused, resume = threading.Event(), threading.Event()

    def reader() -> bytes:
        with cache.read_lock(client_root) as lattice:
            path, _authority = resolve_task_prose_path(lattice, task, "plan")
            assert path is not None
            paused.set()
            resume.wait(WAIT)
            return path.read_bytes()

    read = _Thread(reader)
    read.start()
    assert paused.wait(WAIT)
    sync = _Thread(lambda: cache.catch_up(client_root, bulk=True))
    sync.start()
    time.sleep(0.2)
    assert "applying_written" not in marks
    resume.set()
    read.join(WAIT)
    sync.join(WAIT)
    assert read.error is None, read.error
    assert read.result is not None
    assert sync.result.kind == "applied"
    assert (client_root / ".lattice" / "plans" / f"{task}.md").exists()
    assert_mirror(client_root, stub)


def test_a_reader_arriving_mid_apply_waits_and_sees_the_whole_result(
    client_root: Path, stub: StubServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = _unarchive_pending(client_root, stub)
    reached, release = _seam_gate(monkeypatch, "file_written")
    sync = _Thread(lambda: cache.catch_up(client_root, bulk=True))
    sync.start()
    assert reached.wait(WAIT)  # the apply is half done
    entered = threading.Event()

    def reader() -> tuple[set[str], set[str], str]:
        with cache.read_lock(client_root) as lattice:
            entered.set()
            return (
                _task_ids(lattice / "events"),
                _task_ids(lattice / "archive" / "events"),
                read_task_authority(lattice, task).location,
            )

    read = _Thread(reader)
    read.start()
    time.sleep(0.2)
    assert not entered.is_set()  # the reader waits for the apply
    release.set()
    sync.join(WAIT)
    read.join(WAIT)
    assert sync.error is None and read.error is None, (sync.error, read.error)
    active, archived, location = read.result
    assert (task in active, task in archived, location) == (True, False, "active")
    assert_mirror(client_root, stub)


@pytest.mark.parametrize("reset_first", [False, True], ids=["delta", "epoch-reset"])
def test_a_second_sync_waits_for_the_first_to_apply(
    client_root: Path, stub: StubServer, reset_first: bool
) -> None:
    create_task(stub)
    cache.catch_up(client_root)
    create_task(stub, "second")
    if reset_first:
        stub.start_epoch()
    gate = threading.Event()
    stub.fault.sync_gate = gate
    before = len(stub.arrivals)
    first = _Thread(lambda: cache.catch_up(client_root, bulk=True))
    first.start()
    deadline = time.monotonic() + WAIT
    while len(stub.arrivals) == before and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(stub.arrivals) == before + 1  # the first is mid-fetch
    create_task(stub, "third")
    second = _Thread(lambda: cache.catch_up(client_root, bulk=True))
    second.start()
    time.sleep(0.2)
    assert len(stub.arrivals) == before + 1  # the second has not begun fetching
    gate.set()
    first.join(WAIT)
    second.join(WAIT)
    assert first.error is None and second.error is None, (first.error, second.error)
    assert first.result.kind == "applied" and second.result.kind == "applied"
    first_state_head = first.result.head_seq
    second_query = stub.arrivals[before + 1][1]
    assert second_query["since"] == str(first_state_head)  # it began from the first's result
    assert second.result.head_seq == stub.head
    assert_mirror(client_root, stub)
