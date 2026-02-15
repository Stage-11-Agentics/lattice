"""File locking and deterministic lock ordering."""

from __future__ import annotations

import contextlib
from collections.abc import Generator
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
