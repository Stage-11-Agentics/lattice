"""LAT-330: one sync serves every caller that waited for it (SPEC §9.4, §9.5).

A caller is served by any sync whose request was sent after it read the ticket
record (``locks/cache_sync.json``), whoever ran it, and by no other. The
cross-process cases run the waiters and the syncer as real subprocesses, so
``flock`` and the ticket file are exercised between processes, not threads.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from lattice.remote import cache, http
from tests.test_remote.conftest import assert_mirror, create_task, record_requests
from tests.test_remote.stub_sync_server import StubServer

WAIT = 10.0

#: A subprocess that calls ``catch_up`` on argv[1] as a read would (adopting any
#: outcome), touching argv[2] once it has read the ticket record and is polling
#: for another process's sync, and printing ``[kind, head_seq]``.
WAITER = """
import json, sys
from pathlib import Path
from lattice.remote import cache
root, ready = Path(sys.argv[1]), Path(sys.argv[2])
real = cache._adoptable
def adoptable(*args):
    ready.touch()
    return real(*args)
cache._adoptable = adoptable
outcome = cache.catch_up(root, adopt=cache.ANY_KIND)
print(json.dumps([outcome.kind, outcome.head_seq]))
"""

#: A subprocess syncer that stops forever at the sync seam argv[2] (``None``:
#: never), touching argv[3] when it gets there.
STUCK_SYNCER = """
import sys, time
from pathlib import Path
from lattice.remote import cache
root, step, reached = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
def seam(name):
    if name == step:
        reached.touch()
        time.sleep(3600)
cache._seam = seam
cache.catch_up(root, bulk=True)
"""


def _spawn(code: str, *args: object) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", code, *map(str, args)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _wait_for(predicate, what: str, timeout: float = WAIT) -> None:  # noqa: ANN001
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.01)


def _syncs(stub: StubServer) -> int:
    return sum(1 for kind, _query in stub.arrivals if kind == "sync")


class _Thread(threading.Thread):
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


def _gate_at(
    monkeypatch: pytest.MonkeyPatch, step: str
) -> tuple[threading.Event, threading.Event]:
    reached, release = threading.Event(), threading.Event()

    def seam(name: str) -> None:
        if name == step and not reached.is_set():
            reached.set()
            release.wait(WAIT)

    monkeypatch.setattr(cache, "_seam", seam)
    return reached, release


def _polling(monkeypatch: pytest.MonkeyPatch) -> threading.Event:
    """Set once a caller in this process has read the record and is polling."""
    polling = threading.Event()
    real = cache._adoptable

    def adoptable(*args):  # noqa: ANN002, ANN202
        polling.set()
        return real(*args)

    monkeypatch.setattr(cache, "_adoptable", adoptable)
    return polling


# ---------------------------------------------------------------------------
# Cross-process
# ---------------------------------------------------------------------------


@pytest.mark.timeout(90)
def test_five_waiting_processes_are_served_by_one_more_sync(
    client_root: Path, stub: StubServer, tmp_path: Path
) -> None:
    """Sync A is in flight (its request answered from the head before a write);
    five reader processes arrive. Not one adopts A (it predates them), and all
    five are served by exactly one more sync, which carries the write."""
    create_task(stub)
    cache.catch_up(client_root)
    gate = threading.Event()
    stub.fault.sync_gate = gate
    before = _syncs(stub)
    first = _Thread(lambda: cache.catch_up(client_root, bulk=True))
    first.start()
    _wait_for(lambda: _syncs(stub) == before + 1, "sync A's request")
    late = create_task(stub, "written while A is in flight")
    stub.fault.sync_gate = None
    ready = [tmp_path / f"ready-{n}" for n in range(5)]
    waiters = [_spawn(WAITER, client_root, flag) for flag in ready]
    try:
        _wait_for(lambda: all(f.exists() for f in ready), "every waiter polling", timeout=60)
        time.sleep(0.2)
        assert _syncs(stub) == before + 1  # nobody queued a sync of its own
        gate.set()
        first.join(WAIT)
        assert first.error is None, first.error
        results = [json.loads(p.communicate(timeout=60)[0]) for p in waiters]
    finally:
        for proc in waiters:
            proc.kill()
    assert all(p.returncode == 0 for p in waiters)
    assert _syncs(stub) == before + 2  # A, then one sync for all five
    assert {kind for kind, _head in results} <= {"applied", "unchanged"}
    assert {head for _kind, head in results} == {stub.head}
    assert any(late in p.name for p in (client_root / ".lattice" / "tasks").iterdir())
    assert_mirror(client_root, stub)


@pytest.mark.timeout(90)
@pytest.mark.parametrize("step", ["sync_ticket_taken", "file_written"])
def test_a_syncer_killed_mid_sync_never_strands_a_waiting_read(
    client_root: Path, stub: StubServer, tmp_path: Path, step: str
) -> None:
    """SIGKILL the syncer after it published its ticket (``sync_ticket_taken``,
    its request not yet sent) or mid-apply: its ``finished`` never lands, its
    ``flock`` goes with it, and a read that was polling runs its own sync
    within the probe budget (after a torn apply, a reset that matches)."""
    create_task(stub)
    cache.catch_up(client_root)
    create_task(stub, "pending")
    reached = tmp_path / "reached"
    syncer = _spawn(STUCK_SYNCER, client_root, step, reached)
    try:
        _wait_for(reached.exists, f"the syncer at {step}", timeout=60)
        ready = tmp_path / "ready"
        waiter = _spawn(WAITER, client_root, ready)
        _wait_for(ready.exists, "the waiter polling", timeout=60)
        started = time.monotonic()
        syncer.send_signal(signal.SIGKILL)
        syncer.wait(WAIT)
        out, err = waiter.communicate(timeout=60)
        elapsed = time.monotonic() - started
    finally:
        syncer.kill()
    assert waiter.returncode == 0, err
    kind, head = json.loads(out)
    assert kind == "applied" and head == stub.head
    assert elapsed < cache.PROBE_SECONDS + 1
    assert not (client_root / ".lattice" / "cache" / "applying").exists()
    assert_mirror(client_root, stub)


# ---------------------------------------------------------------------------
# Who may adopt what
# ---------------------------------------------------------------------------


def test_a_caller_that_arrived_before_the_request_adopts_it(
    client_root: Path, stub: StubServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_task(stub)
    cache.catch_up(client_root)
    create_task(stub, "second")
    reached, release = _gate_at(monkeypatch, "sync_ticket")
    before = _syncs(stub)
    first = _Thread(lambda: cache.catch_up(client_root, bulk=True))
    first.start()
    assert reached.wait(WAIT)  # A holds the lock; its ticket is not yet taken
    polling = _polling(monkeypatch)  # from here on, only a waiter polls
    waiter = _Thread(lambda: cache.catch_up(client_root, adopt=cache.ANY_KIND))
    waiter.start()
    assert polling.wait(WAIT)
    release.set()
    first.join(WAIT)
    waiter.join(WAIT)
    assert first.error is None and waiter.error is None, (first.error, waiter.error)
    assert _syncs(stub) == before + 1  # the waiter sent nothing
    assert waiter.result.kind == first.result.kind == "applied"
    assert waiter.result.head_seq == stub.head
    assert_mirror(client_root, stub)


@pytest.mark.parametrize("damage", ["deleted", "rotated", "corrupt"])
def test_a_damaged_or_replaced_record_is_never_adopted(
    client_root: Path, stub: StubServer, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    """The waiter read the record before A's ticket, so A would qualify; but
    the record is deleted, rotated to a new generation, or corrupted while A is
    in flight. The waiter cannot tell A's number from a restarted counter's, so
    it syncs itself."""
    create_task(stub)
    cache.catch_up(client_root)
    create_task(stub, "second")
    reached, release = _gate_at(monkeypatch, "sync_ticket")
    gate = threading.Event()
    stub.fault.sync_gate = gate
    before = _syncs(stub)
    first = _Thread(lambda: cache.catch_up(client_root, bulk=True))
    first.start()
    assert reached.wait(WAIT)
    polling = _polling(monkeypatch)  # from here on, only a waiter polls
    waiter = _Thread(lambda: cache.catch_up(client_root, adopt=cache.ANY_KIND))
    waiter.start()
    assert polling.wait(WAIT)
    release.set()
    _wait_for(lambda: _syncs(stub) == before + 1, "A's request")
    record = client_root / ".lattice" / "locks" / cache.TICKET_FILE
    if damage == "deleted":
        record.unlink()
    elif damage == "rotated":
        cache._rotate_ticket(client_root / ".lattice", "team", "demo")
    else:
        record.write_text("{not json")
    stub.fault.sync_gate = None
    gate.set()
    first.join(WAIT)
    waiter.join(WAIT)
    assert first.error is None and waiter.error is None, (first.error, waiter.error)
    assert _syncs(stub) == before + 2  # the waiter ran its own
    assert waiter.result.kind == "unchanged"
    assert_mirror(client_root, stub)


def test_a_read_adopts_a_failure_but_a_post_write_or_bulk_caller_does_not(
    client_root: Path, stub: StubServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_task(stub)
    cache.catch_up(client_root)
    busy = (
        503,
        {"Content-Type": "application/json", "Lattice-Protocol": "1"},
        json.dumps({"ok": False, "error": {"code": "BOARD_BUSY", "message": "busy"}}).encode(),
    )
    stub.fault.raw = busy
    reached, release = _gate_at(monkeypatch, "sync_ticket")
    calls = record_requests(monkeypatch)
    first = _Thread(lambda: cache.catch_up(client_root))
    first.start()
    assert reached.wait(WAIT)
    polling = _polling(monkeypatch)  # from here on, only a waiter polls
    read = _Thread(lambda: cache.catch_up(client_root, adopt=cache.ANY_KIND))
    strict = _Thread(lambda: cache.catch_up(client_root))  # a post-write sync's set
    read.start()
    strict.start()
    assert polling.wait(WAIT)
    time.sleep(0.1)  # both are polling
    release.set()
    first.join(WAIT)
    read.join(WAIT)
    strict.join(WAIT)
    assert (first.error, read.error, strict.error) == (None, None, None)
    assert first.result.kind == read.result.kind == strict.result.kind == "busy"
    assert len(calls) == 2  # A, and the strict caller's own


def test_a_read_that_adopts_unreachable_never_opens_the_offline_window(
    client_root: Path, stub: StubServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the syncer opens the window, under ``cache_sync.lock`` (ticket
    order): an adopter returning later cannot reopen it over a newer success."""
    create_task(stub)
    cache.catch_up(client_root)

    def down(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise http.Unreachable("connection refused")

    monkeypatch.setattr(http, "request", down)
    reached, release = _gate_at(monkeypatch, "sync_ticket")
    opened: list[tuple[str, bool]] = []
    sync_lock = client_root / ".lattice" / "locks" / "cache_sync.lock"

    def on_unreachable(who: str):  # noqa: ANN202
        def record() -> None:
            locked = cache._lock(sync_lock, True, deadline=time.monotonic())
            if locked is not None:
                os.close(locked)
            opened.append((who, locked is None))

        return record

    first = _Thread(
        lambda: cache.catch_up(
            client_root, adopt=cache.ANY_KIND, on_unreachable=on_unreachable("syncer")
        )
    )
    first.start()
    assert reached.wait(WAIT)
    polling = _polling(monkeypatch)  # from here on, only a waiter polls
    waiter = _Thread(
        lambda: cache.catch_up(
            client_root, adopt=cache.ANY_KIND, on_unreachable=on_unreachable("adopter")
        )
    )
    waiter.start()
    assert polling.wait(WAIT)
    release.set()
    first.join(WAIT)
    waiter.join(WAIT)
    assert first.error is None and waiter.error is None, (first.error, waiter.error)
    assert first.result.kind == waiter.result.kind == "unreachable"
    assert opened == [("syncer", True)]  # once, by the syncer, holding the lock


def test_clear_rotates_the_generation(client_root: Path, stub: StubServer) -> None:
    create_task(stub)
    cache.catch_up(client_root)
    before = cache._read_ticket(client_root / ".lattice")
    cache.clear_cache(client_root)
    after = cache._read_ticket(client_root / ".lattice")
    assert before is not None and after is not None
    assert after.generation != before.generation and after.started == 0
    assert cache.catch_up(client_root).kind == "applied"
    assert_mirror(client_root, stub)


# ---------------------------------------------------------------------------
# A refused write's offline window, in ticket order (LAT-343's write path)
# ---------------------------------------------------------------------------


def _window(client: Path) -> Path:
    return client / ".lattice" / "cache" / "unreachable_until"


def _open(client: Path):  # noqa: ANN202
    def write() -> None:
        _window(client).write_text(f"{time.time() + 15:.3f}\n")

    return write


def test_a_refused_write_never_reopens_the_window_over_a_newer_success(
    client_root: Path, stub: StubServer
) -> None:
    create_task(stub)
    cache.catch_up(client_root)
    since = cache.sample_ticket(client_root)  # the write begins
    assert cache.catch_up(client_root).kind == "unchanged"  # a newer sync succeeds
    assert cache.open_window_in_order(client_root, since, _open(client_root)) is False
    assert not _window(client_root).exists()
    # With no newer success since it began, the window opens.
    since = cache.sample_ticket(client_root)
    assert cache.open_window_in_order(client_root, since, _open(client_root)) is True
    assert _window(client_root).exists()


def test_a_newer_failure_does_not_block_the_window(
    client_root: Path, stub: StubServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_task(stub)
    cache.catch_up(client_root)
    since = cache.sample_ticket(client_root)

    def down(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise http.Unreachable("connection refused")

    monkeypatch.setattr(http, "request", down)
    assert cache.catch_up(client_root).kind == "unreachable"
    assert cache.open_window_in_order(client_root, since, _open(client_root)) is True


def test_a_refused_write_leaves_the_window_to_a_sync_in_flight(
    client_root: Path, stub: StubServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_task(stub)
    cache.catch_up(client_root)
    since = cache.sample_ticket(client_root)
    reached, release = _gate_at(monkeypatch, "sync_ticket")
    sync = _Thread(lambda: cache.catch_up(client_root))
    sync.start()
    assert reached.wait(WAIT)
    try:
        assert cache.open_window_in_order(client_root, since, _open(client_root)) is False
    finally:
        release.set()
        sync.join(WAIT)
    assert not _window(client_root).exists()


def test_a_fresh_checkout_opens_the_window(tmp_path: Path) -> None:
    """No sync ever ran (no lock file), so no newer outcome can exist."""
    root = tmp_path / "fresh"
    (root / ".lattice" / "cache").mkdir(parents=True)
    assert cache.open_window_in_order(root, None, _open(root)) is True
    assert _window(root).exists()
    assert not (root / ".lattice" / "locks").exists()  # nothing else created


# ---------------------------------------------------------------------------
# The record itself
# ---------------------------------------------------------------------------

_VALID = {
    "generation": "g1",
    "remote": "team",
    "project": "demo",
    "started": 3,
    "finished": 2,
    "kind": "applied",
    "detail": None,
}


def _record(tmp_path: Path, data: object) -> Path:
    locks = tmp_path / ".lattice" / "locks"
    locks.mkdir(parents=True, exist_ok=True)
    (locks / cache.TICKET_FILE).write_text(json.dumps(data))
    return tmp_path / ".lattice"


def test_a_valid_record_reads_back_field_for_field(tmp_path: Path) -> None:
    lattice_dir = _record(tmp_path, _VALID)
    ticket = cache._read_ticket(lattice_dir)
    assert ticket == cache._Ticket("g1", "team", "demo", 3, 2, "applied", None)
    cache._write_ticket(lattice_dir, ticket)  # and survives its own round trip
    assert cache._read_ticket(lattice_dir) == ticket


@pytest.mark.parametrize(
    "change",
    [
        {"generation": ""},
        {"remote": 5},
        {"project": None},
        {"started": -1},
        {"started": True},
        {"finished": 4},  # finished beyond started
        {"kind": "exploded"},
        {"detail": 7},
    ],
)
def test_an_invalid_record_reads_as_none(tmp_path: Path, change: dict) -> None:
    assert cache._read_ticket(_record(tmp_path, {**_VALID, **change})) is None
    assert cache._read_ticket(_record(tmp_path, ["not", "an", "object"])) is None
