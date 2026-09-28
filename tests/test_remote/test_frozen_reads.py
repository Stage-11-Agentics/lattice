"""LAT-330: under the cache's shared read lock a hosted cache cannot change
(only the syncer's exclusive apply and ``cache clear`` change it), so reads
take no per-task storage locks (SPEC §9.4)."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from lattice.remote import cache
from tests.test_remote.conftest import create_task
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


def test_a_read_under_the_cache_read_lock_takes_no_task_locks(
    client_root: Path, stub: StubServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the syncer's exclusive apply changes a cache, so a thread holding
    the shared read lock resolves tasks without the per-task file locks (on a
    1,000-task board they were most of a ``list``)."""
    from lattice.storage import locks
    from lattice.storage.operations import discover_task_authorities

    taken: list[list[str]] = []
    real = locks.multi_lock

    def recording(locks_dir, keys, timeout=10):  # noqa: ANN001, ANN202
        taken.append(sorted(keys))
        return real(locks_dir, keys, timeout)

    monkeypatch.setattr(locks, "multi_lock", recording)
    create_task(stub)
    create_task(stub, "second")
    cache.catch_up(client_root)
    taken.clear()
    with cache.read_lock(client_root) as lattice_dir:
        found = discover_task_authorities(lattice_dir)
        assert taken == []  # frozen: no task locks
        with locks.task_locks(lattice_dir / "locks", ["task_x"], extra_keys=["extra"]):
            pass
        assert taken == [["events_task_x", "extra", "tasks_task_x"]]  # extra keys still lock
    assert len(found) == 2

    # Another thread, not holding the read lock, still locks; so does this one after.
    taken.clear()
    with cache.read_lock(client_root):
        thread = _Thread(lambda: discover_task_authorities(client_root / ".lattice"))
        thread.start()
        thread.join(WAIT)
    assert thread.error is None and len(taken) == 2
    discover_task_authorities(client_root / ".lattice")
    assert len(taken) == 4
