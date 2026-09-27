"""LAT-330: writer preference on the cache's read lock (SPEC §9.4).

``flock`` grants a new shared lock while an exclusive one waits, so readers
that overlap could hold an apply off forever. The turnstile
(``locks/cache_turnstile.lock``; lock order ``cache_sync`` → ``cache_turnstile``
→ ``cache_rw``) makes a reader that arrives while an apply or a clear waits
queue behind it, while a thread that already holds the read lock may nest.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from lattice.remote import cache
from tests.test_remote.conftest import assert_mirror, create_task
from tests.test_remote.stub_sync_server import StubServer

WAIT = 10.0


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


def _turnstile(client: Path) -> Path:
    return client / ".lattice" / "locks" / cache.TURNSTILE_LOCK


def _wait_until_turnstile_held(client: Path) -> None:
    deadline = time.monotonic() + WAIT
    while True:
        fd = cache._lock(_turnstile(client), True, deadline=time.monotonic())
        if fd is None:
            return
        os.close(fd)
        assert time.monotonic() < deadline, "nobody took the turnstile"
        time.sleep(0.01)


def _head(lattice_dir: Path) -> int | None:
    return cache._read_json(lattice_dir / "cache" / "state.json").get("head_seq")


def _hold_reader(client: Path) -> tuple[_Thread, threading.Event, threading.Event]:
    holding, done = threading.Event(), threading.Event()

    def reader() -> None:
        with cache.read_lock(client):
            holding.set()
            done.wait(WAIT)

    thread = _Thread(reader)
    thread.start()
    assert holding.wait(WAIT)
    return thread, holding, done


@pytest.mark.parametrize("bulk", [True, False], ids=["follower-or-sync", "probe"])
def test_a_reader_arriving_while_an_apply_waits_queues_behind_it(
    client_root: Path, stub: StubServer, bulk: bool
) -> None:
    create_task(stub)
    cache.catch_up(client_root)
    create_task(stub, "second")
    first, _holding, release_first = _hold_reader(client_root)
    sync = _Thread(lambda: cache.catch_up(client_root, bulk=bulk))
    sync.start()
    _wait_until_turnstile_held(client_root)  # the apply waits for the first reader
    entered = threading.Event()

    def late_reader() -> int | None:
        with cache.read_lock(client_root) as lattice_dir:
            entered.set()
            return _head(lattice_dir)

    late = _Thread(late_reader)
    late.start()
    time.sleep(0.2)
    assert not entered.is_set()  # another thread of this process does not overtake
    release_first.set()
    for thread in (first, sync, late):
        thread.join(WAIT)
    assert (first.error, sync.error, late.error) == (None, None, None)
    assert sync.result.kind == "applied"
    assert late.result == stub.head  # it read after the apply, never before it
    assert_mirror(client_root, stub)


def test_a_nested_read_in_the_same_thread_does_not_deadlock_with_a_waiting_apply(
    client_root: Path, stub: StubServer
) -> None:
    create_task(stub)
    cache.catch_up(client_root)
    create_task(stub, "second")
    holding, go = threading.Event(), threading.Event()

    def reader() -> int | None:
        with cache.read_lock(client_root) as lattice_dir:
            holding.set()
            go.wait(WAIT)
            with cache.read_lock(client_root):  # nested: skips the turnstile
                return _head(lattice_dir)

    read = _Thread(reader)
    read.start()
    assert holding.wait(WAIT)
    before = _head(client_root / ".lattice")
    sync = _Thread(lambda: cache.catch_up(client_root, bulk=True))
    sync.start()
    _wait_until_turnstile_held(client_root)
    go.set()
    read.join(WAIT)
    assert not read.is_alive(), "the nested read deadlocked behind the waiting apply"
    sync.join(WAIT)
    assert read.error is None and sync.error is None, (read.error, sync.error)
    assert read.result == before  # still inside the first read: the old tree
    assert sync.result.kind == "applied"
    assert_mirror(client_root, stub)


def test_a_reader_arriving_while_a_clear_waits_sees_the_cleared_tree(
    client_root: Path, stub: StubServer
) -> None:
    create_task(stub)
    cache.catch_up(client_root)
    first, _holding, release_first = _hold_reader(client_root)
    clear = _Thread(lambda: cache.clear_cache(client_root))
    clear.start()
    _wait_until_turnstile_held(client_root)
    entered = threading.Event()

    def late_reader() -> bool:
        with cache.read_lock(client_root) as lattice_dir:
            entered.set()
            return (lattice_dir / "tasks").exists()

    late = _Thread(late_reader)
    late.start()
    time.sleep(0.2)
    assert not entered.is_set()
    release_first.set()
    for thread in (first, clear, late):
        thread.join(WAIT)
    assert (first.error, clear.error, late.error) == (None, None, None)
    assert late.result is False  # after the clear, never a half-cleared tree


#: Run in a fresh single-threaded process: hold the read lock, take the
#: turnstile as a waiting apply would, fork, and check that the child's read
#: queues at the turnstile rather than nesting through it. Exit 0 on success.
FORK_CHILD = """
import os, signal, sys, time
from pathlib import Path
from lattice.remote import cache
root = Path(sys.argv[1])
turnstile = root / ".lattice" / "locks" / cache.TURNSTILE_LOCK
read_r, read_w = os.pipe()
with cache.read_lock(root):
    blocker = cache._lock(turnstile, True, deadline=None)
    pid = os.fork()
    if pid == 0:
        status = 1
        try:
            signal.alarm(20)
            os.close(read_r)
            os.close(blocker)  # the parent's copy keeps the turnstile held
            with cache.read_lock(root):
                os.write(read_w, b"entered")
            status = 0
        finally:
            os._exit(status)
    os.close(read_w)
    time.sleep(0.3)
    os.set_blocking(read_r, False)
    try:
        os.read(read_r, 16)
        sys.exit("the child nested through the turnstile")
    except BlockingIOError:
        pass
    os.set_blocking(read_r, True)
    os.close(blocker)
    assert os.read(read_r, 16) == b"entered"
    _, status = os.waitpid(pid, 0)
    sys.exit(os.waitstatus_to_exitcode(status))
"""


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs fork")
@pytest.mark.timeout(60)
def test_a_forked_child_does_not_inherit_the_parents_nesting(
    client_root: Path, stub: StubServer
) -> None:
    """The parent holds the read lock; a child forked meanwhile starts with no
    nesting, so its read passes the turnstile like any other process."""
    create_task(stub)
    cache.catch_up(client_root)
    proc = subprocess.run(
        [sys.executable, "-c", FORK_CHILD, str(client_root)],
        capture_output=True,
        text=True,
        timeout=50,
    )
    assert proc.returncode == 0, proc.stderr
