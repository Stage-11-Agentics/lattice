"""File locking and deterministic lock ordering.

Task locks sit under a board-wide *task gate*. :func:`task_locks` holds the
gate shared plus each named task's ``events_<id>`` and ``tasks_<id>`` keys, so
holders of one task exclude each other and holders of different tasks run
concurrently. :func:`all_task_locks` holds the gate exclusively, which excludes
every task holder (including one creating a task) with one descriptor, where
locking each task's keys would hold two descriptors per task and exhaust a
256-descriptor limit on a large board. The gate is taken before any other key.
"""

from __future__ import annotations

import contextlib
import os
import threading
import time
from collections.abc import Generator, Iterable
from pathlib import Path

from filelock import FileLock, Timeout


class LockTimeout(Exception):
    """Raised when a lock cannot be acquired within the timeout period."""


@contextlib.contextmanager
def lattice_lock(
    locks_dir: Path,
    key: str,
    timeout: float = 10,
) -> Generator[None, None, None]:
    """Acquire a single file lock at ``locks_dir/<key>.lock``.

    Args:
        locks_dir: Directory where lock files are stored.
        key: Lock key (used as the lock file basename).
        timeout: Seconds to wait before giving up.

    Raises:
        LockTimeout: If the lock cannot be acquired within *timeout* seconds.
    """
    lock_path = locks_dir / f"{key}.lock"
    lock = FileLock(lock_path, timeout=timeout)
    try:
        lock.acquire()
    except Timeout:
        raise LockTimeout(f"Could not acquire lock '{key}' within {timeout}s") from None
    try:
        yield
    finally:
        lock.release()


@contextlib.contextmanager
def multi_lock(
    locks_dir: Path,
    keys: list[str],
    timeout: float = 10,
) -> Generator[None, None, None]:
    """Acquire multiple locks in deterministic (sorted) order.

    Keys are sorted lexicographically before acquisition to prevent deadlocks.
    Locks are released in reverse acquisition order on exit (including on
    exception).

    Args:
        locks_dir: Directory where lock files are stored.
        keys: Lock keys to acquire.
        timeout: Seconds to wait *per lock* before giving up.

    Raises:
        LockTimeout: If any lock cannot be acquired within *timeout* seconds.
    """
    sorted_keys = sorted(keys)
    acquired: list[FileLock] = []
    try:
        for key in sorted_keys:
            lock_path = locks_dir / f"{key}.lock"
            lock = FileLock(lock_path, timeout=timeout)
            try:
                lock.acquire()
            except Timeout:
                raise LockTimeout(f"Could not acquire lock '{key}' within {timeout}s") from None
            acquired.append(lock)
        yield
    finally:
        for lock in reversed(acquired):
            lock.release()


_TASK_GATE_KEY = "task_gate"
_POLL_INTERVAL = 0.01
_held_gates = threading.local()


def task_lock_keys(task_ids: Iterable[str]) -> list[str]:
    """The per-task lock keys (event log and snapshot) for *task_ids*."""
    return [key for task_id in task_ids for key in (f"events_{task_id}", f"tasks_{task_id}")]


_frozen = threading.local()


@contextlib.contextmanager
def frozen_board(locks_dir: Path) -> Generator[None, None, None]:
    """Declare, for this thread, that the board of *locks_dir* cannot change
    until the block ends: a hosted cache under its shared read lock, which only
    the syncer's exclusive apply can change. :func:`task_locks` without extra
    keys is then a no-op, since there is no writer for it to exclude. Keyed by
    process id, so a forked child starts with nothing frozen."""
    key = (os.getpid(), os.path.abspath(locks_dir))
    counts = getattr(_frozen, "counts", None)
    if counts is None:
        counts = _frozen.counts = {}
    counts[key] = counts.get(key, 0) + 1
    try:
        yield
    finally:
        counts[key] -= 1


def _is_frozen(locks_dir: Path) -> bool:
    counts = getattr(_frozen, "counts", None)
    return bool(counts) and counts.get((os.getpid(), os.path.abspath(locks_dir)), 0) > 0


@contextlib.contextmanager
def task_locks(
    locks_dir: Path,
    task_ids: Iterable[str],
    extra_keys: Iterable[str] = (),
    timeout: float = 10,
) -> Generator[None, None, None]:
    """Lock the named tasks: the task gate shared, then the tasks' keys and
    *extra_keys* through :func:`multi_lock`. Nothing to lock on a board this
    thread holds frozen (:func:`frozen_board`)."""
    extra_keys = list(extra_keys)
    if not extra_keys and _is_frozen(locks_dir):
        yield
        return
    with _task_gate(locks_dir, exclusive=False, timeout=timeout):
        with multi_lock(locks_dir, [*task_lock_keys(task_ids), *extra_keys], timeout=timeout):
            yield


@contextlib.contextmanager
def all_task_locks(
    locks_dir: Path,
    extra_keys: Iterable[str] = (),
    timeout: float = 10,
) -> Generator[None, None, None]:
    """Lock every task on the board: the task gate exclusive, then *extra_keys*.

    It holds ``1 + len(extra_keys)`` descriptors whatever the board's size.
    Code under it may still take :func:`task_locks` (the gate is reentrant per
    thread); taking this while the thread holds :func:`task_locks` raises.
    """
    with _task_gate(locks_dir, exclusive=True, timeout=timeout):
        with multi_lock(locks_dir, list(extra_keys), timeout=timeout):
            yield


@contextlib.contextmanager
def _task_gate(locks_dir: Path, *, exclusive: bool, timeout: float) -> Generator[None, None, None]:
    """Hold the task gate, reentrantly per thread (a nested hold is a no-op)."""
    path = os.path.realpath(locks_dir / f"{_TASK_GATE_KEY}.lock")
    held: dict[str, bool] = _held_gates.__dict__.setdefault("gates", {})
    if path in held:
        if exclusive and not held[path]:
            raise RuntimeError("all_task_locks cannot be taken while this thread holds task_locks")
        yield
        return
    release = _acquire_gate(path, exclusive=exclusive, timeout=timeout)
    held[path] = exclusive
    try:
        yield
    finally:
        del held[path]
        release()


def _acquire_gate(path: str, *, exclusive: bool, timeout: float):  # noqa: ANN202
    """Take the gate's flock, shared or exclusive; return its release function.

    Without ``fcntl`` (Windows) the gate is always exclusive: coarser, still safe.
    """
    try:
        import fcntl
    except ImportError:
        lock = FileLock(path, timeout=timeout)
        try:
            lock.acquire()
        except Timeout:
            raise LockTimeout(
                f"Could not acquire lock '{_TASK_GATE_KEY}' within {timeout}s"
            ) from None
        return lock.release

    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    operation = (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, operation)
                return lambda: os.close(fd)
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LockTimeout(
                        f"Could not acquire lock '{_TASK_GATE_KEY}' within {timeout}s"
                    ) from None
                time.sleep(_POLL_INTERVAL)
    except BaseException:
        os.close(fd)
        raise
