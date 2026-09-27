"""The AC-42 clients' filesystem (EVALUATION AC-42, Architect PR #90): client
checkouts and caches live on a filesystem other than the server root's, since
they stand in for separate machines. ``/dev/shm`` on Linux; on macOS a RAM disk
created for the run (``hdiutil attach -nomount ram://...``, then ``diskutil
erasevolume APFS``) and always detached. When that cannot be arranged, the test
FAILS, never skips, and the two directories are checked to be on different
devices (``st_dev``) before any client exists.

Copied from H-15's shared harness (``tests/torture/load.py``, PR #84) so
``test_with_dashboards`` has it before that lands; the union of the two
``test_load.py`` modules replaces this file with H-15's.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path

#: The macOS RAM disk's size: room for 30 caches of a 1,000-task board and more.
RAM_DISK_BYTES = 2 * 1024**3

#: Provides a mount point on a filesystem other than the server root's, for the
#: duration of the ``with``. The real one allocates; tests of the policy inject a fake.
MountProvider = Callable[[], AbstractContextManager[Path]]


@contextmanager
def platform_mount() -> Iterator[Path]:
    """``/dev/shm`` on Linux; on macOS a RAM disk created for the block and always
    detached. Raises ``AssertionError`` naming the reason when neither is possible."""
    if sys.platform == "darwin":
        with _mac_ram_disk() as mount:
            yield mount
        return
    shm = Path("/dev/shm")
    if not (shm.is_dir() and os.access(shm, os.W_OK)):
        raise AssertionError(
            "AC-42 needs the clients on a filesystem separate from the server's; "
            f"/dev/shm is not a writable directory on this {sys.platform} host"
        )
    yield shm


@contextmanager
def separate_client_filesystem(mount: MountProvider | None = None) -> Iterator[Path]:
    """A fresh directory under *mount*'s mount point, removed afterwards however
    the block ends. *mount* defaults to :func:`platform_mount`, looked up at call time."""
    with (mount or platform_mount)() as mount_point:
        path = Path(tempfile.mkdtemp(prefix="lattice-load-", dir=mount_point))
        try:
            yield path
        finally:
            shutil.rmtree(path, ignore_errors=True)


@contextmanager
def _mac_ram_disk() -> Iterator[Path]:
    sectors = RAM_DISK_BYTES // 512
    attach = subprocess.run(
        ["hdiutil", "attach", "-nomount", f"ram://{sectors}"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if attach.returncode != 0:
        raise AssertionError(f"AC-42 could not create a RAM disk for the clients: {attach.stderr}")
    device = attach.stdout.strip().split()[0]
    try:
        name = f"LatticeLoad{os.getpid()}"
        erase = subprocess.run(
            ["diskutil", "erasevolume", "APFS", name, device],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if erase.returncode != 0:
            raise AssertionError(f"AC-42 could not format the RAM disk {device}: {erase.stderr}")
        yield Path("/Volumes") / name
    finally:
        subprocess.run(["hdiutil", "detach", device, "-force"], capture_output=True, timeout=60)


def assert_separate_filesystems(
    client_dir: Path, server_root: Path, *, device: Callable[[Path], int] | None = None
) -> None:
    """Fail unless the two paths are on different devices (``st_dev``)."""
    device = device or (lambda path: os.stat(path).st_dev)
    client_dev, server_dev = device(client_dir), device(server_root)
    assert client_dev != server_dev, (
        f"client dir {client_dir} and server root {server_root} are on one filesystem "
        f"(st_dev {client_dev}); AC-42's clients stand in for separate machines"
    )
