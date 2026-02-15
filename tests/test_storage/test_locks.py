"""Tests for file locking and deterministic lock ordering."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from filelock import FileLock

from lattice.storage.locks import LockTimeout, lattice_lock, multi_lock


class TestLatticeLock:
    """lattice_lock() acquires and releases a single file lock."""

    def test_acquire_and_release(self, tmp_path: Path) -> None:
        """Lock is acquired on enter and released on exit."""
        lock_file = tmp_path / "mykey.lock"

        with lattice_lock(tmp_path, "mykey"):
            assert lock_file.exists()
            # While held, a non-blocking attempt should fail
            probe = FileLock(lock_file, timeout=0)
            with pytest.raises(Exception):  # noqa: B017
                probe.acquire(timeout=0)

        # After exit, a non-blocking acquire should succeed
        probe = FileLock(lock_file, timeout=0)
        probe.acquire()
        probe.release()

    def test_creates_lock_file(self, tmp_path: Path) -> None:
        with lattice_lock(tmp_path, "alpha"):
            assert (tmp_path / "alpha.lock").exists()

    def test_releases_on_normal_exit(self, tmp_path: Path) -> None:
        with lattice_lock(tmp_path, "rel"):
            pass

        # Should be acquirable again immediately
        with lattice_lock(tmp_path, "rel", timeout=0.1):
            pass

    def test_releases_on_exception(self, tmp_path: Path) -> None:
        with pytest.raises(RuntimeError, match="boom"):
            with lattice_lock(tmp_path, "exc"):
                raise RuntimeError("boom")

        # Lock should still be released despite the exception
        with lattice_lock(tmp_path, "exc", timeout=0.1):
            pass

    def test_timeout_raises_lock_timeout(self, tmp_path: Path) -> None:
        """If a lock is already held, a second attempt times out."""
        lock_file = tmp_path / "busy.lock"
        blocker = FileLock(lock_file)
        blocker.acquire()
        try:
            with pytest.raises(LockTimeout, match="Could not acquire lock 'busy'"):
                with lattice_lock(tmp_path, "busy", timeout=0.1):
                    pass  # pragma: no cover
        finally:
            blocker.release()


class TestMultiLock:
    """multi_lock() acquires locks in sorted order and releases all on exit."""

    def test_acquires_all_locks(self, tmp_path: Path) -> None:
        keys = ["charlie", "alpha", "bravo"]
        with multi_lock(tmp_path, keys):
            for key in keys:
                assert (tmp_path / f"{key}.lock").exists()

    def test_sorted_acquisition_order(self, tmp_path: Path) -> None:
        """Locks are acquired in lexicographic order regardless of input order."""
        acquisition_order: list[str] = []
        original_acquire = FileLock.acquire

        def tracking_acquire(self: FileLock, *args: object, **kwargs: object) -> None:
            # Extract key from lock file name (e.g. "/path/to/charlie.lock" -> "charlie")
            key = Path(self.lock_file).stem
            acquisition_order.append(key)
            return original_acquire(self, *args, **kwargs)

        keys = ["charlie", "alpha", "bravo"]

        import unittest.mock

        with unittest.mock.patch.object(FileLock, "acquire", tracking_acquire):
            with multi_lock(tmp_path, keys):
                pass

        assert acquisition_order == ["alpha", "bravo", "charlie"]

    def test_releases_on_normal_exit(self, tmp_path: Path) -> None:
        keys = ["x", "y", "z"]
        with multi_lock(tmp_path, keys):
            pass

        # All should be re-acquirable
        for key in keys:
            with lattice_lock(tmp_path, key, timeout=0.1):
                pass

    def test_releases_on_exception(self, tmp_path: Path) -> None:
        keys = ["a", "b", "c"]
        with pytest.raises(ValueError, match="oops"):
            with multi_lock(tmp_path, keys):
                raise ValueError("oops")

        # All locks should still be released
        for key in keys:
            with lattice_lock(tmp_path, key, timeout=0.1):
                pass

    def test_timeout_on_held_lock(self, tmp_path: Path) -> None:
        """If one lock is already held, multi_lock times out with LockTimeout."""
        blocker = FileLock(tmp_path / "bravo.lock")
        blocker.acquire()
        try:
            with pytest.raises(LockTimeout, match="bravo"):
                with multi_lock(tmp_path, ["alpha", "bravo", "charlie"], timeout=0.1):
                    pass  # pragma: no cover
        finally:
            blocker.release()

    def test_partial_acquire_releases_on_timeout(self, tmp_path: Path) -> None:
        """If multi_lock fails mid-way, already-acquired locks are released."""
        blocker = FileLock(tmp_path / "bravo.lock")
        blocker.acquire()
        try:
            with pytest.raises(LockTimeout):
                with multi_lock(tmp_path, ["alpha", "bravo", "charlie"], timeout=0.1):
                    pass  # pragma: no cover

            # "alpha" was acquired before "bravo" failed — it must be released
            with lattice_lock(tmp_path, "alpha", timeout=0.1):
                pass
        finally:
            blocker.release()

    def test_empty_keys(self, tmp_path: Path) -> None:
        """An empty key list should be a no-op context manager."""
        with multi_lock(tmp_path, []):
            pass  # Should not raise

    def test_concurrent_access_blocked(self, tmp_path: Path) -> None:
        """A second thread cannot acquire the same lock while held."""
        acquired_in_thread = threading.Event()
        timed_out_in_thread = threading.Event()

        def hold_lock() -> None:
            with lattice_lock(tmp_path, "shared", timeout=5):
                acquired_in_thread.set()
                time.sleep(0.5)

        def try_lock() -> None:
            acquired_in_thread.wait(timeout=2)
            try:
                with lattice_lock(tmp_path, "shared", timeout=0.1):
                    pass  # pragma: no cover
            except LockTimeout:
                timed_out_in_thread.set()

        t1 = threading.Thread(target=hold_lock)
        t2 = threading.Thread(target=try_lock)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert timed_out_in_thread.is_set(), "Second thread should have timed out"
