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
never lost.
"""

from __future__ import annotations

import contextlib
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


@contextlib.contextmanager
def _locked(cache_dir: Path) -> Iterator[None]:
    import fcntl

    fd = os.open(cache_dir / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # releases the flock


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
    with _locked(cache_dir):
        fd = os.open(cache_dir / ACKED_FILE, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
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
    with _locked(cache_dir):
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
        _rewrite(cache_dir, kept)
    return report


def _key(number: int, entry: dict) -> tuple[int, str]:
    """A ledger line's identity between verify's two reads: appends only add at
    the end, so a line keeps its position; its content guards the rest."""
    return number, json.dumps(entry, sort_keys=True)


def _rewrite(cache_dir: Path, lines: list[dict]) -> None:
    path = cache_dir / ACKED_FILE
    if not lines and not path.exists():
        return
    body = "".join(json.dumps(x, sort_keys=True, separators=(",", ":")) + "\n" for x in lines)
    tmp = path.with_name(f".{ACKED_FILE}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        view = memoryview(body.encode("utf-8"))
        while view:
            view = view[os.write(fd, view) :]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
