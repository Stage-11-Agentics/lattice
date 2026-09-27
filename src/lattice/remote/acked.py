"""Acknowledged writes and ``lattice remote verify`` (SPEC §9.2, §9.5).

As soon as the server acknowledges a write, before the post-write sync,
:meth:`HostedBoard.execute` appends one line to the checkout's
``cache/acked.jsonl``::

    {"op_id", "project", "epoch", "seq", "at"}

``epoch`` is the epoch the cache knew at that moment (the op response carries
none), or ``null``. It is informational: verification asks the server by
``op_id``. An append first cuts a torn final line (a client killed
mid-append), so it never joins one.

:func:`verify` asks the server, through op status, about every line. A line
the server does not hold (``not_found``) is reported and kept, so it is
reported again until someone deals with it. A confirmed line is kept too,
marked ``confirmed_at``, so a later restore of an older backup that loses an
already verified write is still caught. Lines older than 90 days are
dropped.

``cache/`` is cache control (SPEC §6.1), written only by the syncer, the
follower, and the hosted client. Appends and verify's rewrite take the
exclusive flock ``cache/acked.lock``, so a write finishing during a verify is
never lost. A write can land before the checkout's first sync (a fresh clone
whose first command writes without reading), so taking the lock first creates
``.lattice/`` and ``cache/`` with the cache's private mode (SPEC §9.4). Neither
is a cache marker nor synced, so the next catch-up still bootstraps the cache
with a reset. Both must be real directories: each is opened without following
a symlink, and everything under it is reached through that descriptor, so a
file or a symlink there fails the record before anything is changed or written
through it.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ACKED_FILE = "acked.jsonl"
LOCK_FILE = "acked.lock"
RETENTION_DAYS = 90
#: ``.lattice/`` and ``cache/`` are owner-only and writable (SPEC §9.4).
PRIVATE_DIR_MODE = 0o700


def _now() -> datetime:
    """The clock line ages are read on (a test seam)."""
    return datetime.now(timezone.utc)


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(stamp: Any) -> datetime | None:
    if not isinstance(stamp, str):
        return None
    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def _private_dir(name: str, parent_fd: int, path: Path) -> int:
    """Open *name* under *parent_fd* as a real directory, creating it where
    missing, and set it to 0700 whatever the umask (an existing ``.lattice/``
    may be a fresh clone's runtime leftovers, SPEC §9.3, made with a looser
    mode). A symlink or a non-directory there is refused unchanged."""
    with contextlib.suppress(FileExistsError):
        os.mkdir(name, PRIVATE_DIR_MODE, dir_fd=parent_fd)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW, dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise NotADirectoryError(
                errno.ENOTDIR,
                "not a real directory (a symlink or a file); left untouched",
                str(path),
            ) from exc
        raise
    try:
        os.fchmod(fd, PRIVATE_DIR_MODE)
    except BaseException:
        os.close(fd)
        raise
    return fd


@contextlib.contextmanager
def _locked(cache_dir: Path) -> Iterator[int]:
    """Hold ``acked.lock``; yields a descriptor of *cache_dir* (``.lattice/cache``),
    through which the ledger is written."""
    import fcntl

    root_fd = os.open(cache_dir.parent.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        lattice_fd = _private_dir(cache_dir.parent.name, root_fd, cache_dir.parent)
    finally:
        os.close(root_fd)
    try:
        dir_fd = _private_dir(cache_dir.name, lattice_fd, cache_dir)
    finally:
        os.close(lattice_fd)
    try:
        fd = os.open(LOCK_FILE, os.O_RDWR | os.O_CREAT | _NOFOLLOW, 0o600, dir_fd=dir_fd)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield dir_fd
        finally:
            os.close(fd)  # releases the flock
    finally:
        os.close(dir_fd)


def record(cache_dir: Path, *, op_id: str, project: str, epoch: str | None, seq: Any) -> None:
    """Append one acknowledged write (one write and one fsync, under the lock)."""
    line = {
        "op_id": op_id,
        "project": project,
        "epoch": epoch,
        "seq": seq,
        "at": _stamp(_now()),
    }
    data = (json.dumps(line, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    with _locked(cache_dir) as dir_fd:
        flags = os.O_RDWR | os.O_APPEND | os.O_CREAT | _NOFOLLOW
        fd = os.open(ACKED_FILE, flags, 0o600, dir_fd=dir_fd)
        try:
            _cut_torn_tail(fd)
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
        finally:
            os.close(fd)


def _cut_torn_tail(fd: int) -> None:
    """Truncate an unterminated final line (a client killed mid-append), so the
    next record starts a line of its own instead of joining the torn one."""
    size = os.fstat(fd).st_size
    if size == 0:
        return
    if os.pread(fd, 1, size - 1) == b"\n":
        return
    start = max(0, size - 65536)
    while True:
        chunk = os.pread(fd, size - start, start)
        cut = chunk.rfind(b"\n")
        if cut >= 0:
            os.ftruncate(fd, start + cut + 1)
            return
        if start == 0:
            os.ftruncate(fd, 0)
            return
        start = max(0, start - 65536)


def read(cache_dir: Path) -> list[dict]:
    """Every complete line; a torn final line (a client killed mid-append) is skipped."""
    try:
        data = (cache_dir / ACKED_FILE).read_bytes()
    except FileNotFoundError:
        return []
    lines = []
    for raw in data.split(b"\n")[:-1]:
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if isinstance(entry, dict) and isinstance(entry.get("op_id"), str):
            lines.append(entry)
    return lines


@dataclass
class Report:
    checked: int = 0
    confirmed: int = 0
    dropped: int = 0
    missing: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "checked": self.checked,
            "confirmed": self.confirmed,
            "dropped": self.dropped,
            "missing": self.missing,
        }


def verify(cache_dir: Path, status: Callable[[str], dict]) -> Report:
    """Check every line against the server (*status* is op status for one op_id),
    one lookup per line.

    Asks the server first, then rewrites the file under the lock: lines that
    arrived meanwhile are kept unchanged, and nothing changes if any lookup
    fails (the error propagates).
    """
    now = _now()
    cutoff = now - timedelta(days=RETENTION_DAYS)
    report = Report()
    # One lookup per ledger line (a replayed op may be recorded twice; each
    # line is checked on its own), keyed by the line's position and content.
    verdicts: dict[tuple[int, str], bool] = {}
    for number, entry in enumerate(read(cache_dir)):
        at = _parse(entry.get("at"))
        if at is not None and at < cutoff:
            continue
        verdicts[_key(number, entry)] = status(entry["op_id"]).get("state") == "committed"
    with _locked(cache_dir) as dir_fd:
        kept: list[dict] = []
        for number, entry in enumerate(read(cache_dir)):
            at = _parse(entry.get("at"))
            if at is not None and at < cutoff:
                report.dropped += 1
                continue
            held = verdicts.get(_key(number, entry))
            if held is None:  # acknowledged after this verify asked: next time
                kept.append(entry)
                continue
            report.checked += 1
            if held:
                report.confirmed += 1
                entry = {**entry, "confirmed_at": _stamp(now)}
            else:
                report.missing.append(entry)
            kept.append(entry)
        _rewrite(dir_fd, kept)
    return report


def _key(number: int, entry: dict) -> tuple[int, str]:
    """A ledger line's identity between verify's two reads: appends only add at
    the end, so a line keeps its position; its content guards the rest."""
    return number, json.dumps(entry, sort_keys=True)


def _rewrite(dir_fd: int, lines: list[dict]) -> None:
    """Replace the ledger in the cache directory *dir_fd* with *lines*."""
    try:
        os.stat(ACKED_FILE, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        if not lines:
            return
    body = "".join(json.dumps(x, sort_keys=True, separators=(",", ":")) + "\n" for x in lines)
    tmp = f".{ACKED_FILE}.{os.getpid()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _NOFOLLOW
    fd = os.open(tmp, flags, 0o600, dir_fd=dir_fd)
    try:
        view = memoryview(body.encode("utf-8"))
        while view:
            view = view[os.write(fd, view) :]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, ACKED_FILE, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
