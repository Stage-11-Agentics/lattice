"""Server transactions: every write wholly applied or wholly absent (SPEC §8.6).

Every operation a server runs, and every write the server starts itself
(``server.set_config``), is one :class:`Transaction` under the project's work
lock::

    txn.begin()          # 1. journal and receipt-file lengths; the undo log
    execute(..., on_mutation=txn.before_mutation)
                         # 2. an undo entry, fsynced, before each guarded change
                         # 3. the operation's own writes
    txn.commit(entry, result_data)
                         # 4. the receipt (the full result), fsynced
                         # 5. the journal line, fsynced: the single commit point
    txn.finish()         # 6. memory state, index, undo-log deletion, publication

Any failure after ``begin`` goes to :meth:`Transaction.recover` before the
project admits another request: a committed operation is finished and never
rolled back; an uncommitted one has its journal and receipt file truncated and
its board changes undone from the undo log, read back from disk in reverse;
a journal fsync that failed (durability unknown) or a recovery step that
failed quarantines the project (``BOARD_UNAVAILABLE``).

Undo logs live at ``hosted/undo/<token_id>--<op_id>.jsonl`` (``server`` for a
server-started write); the first line is ``{"epoch", "token_id", "op_id"}``,
then one entry per guarded change:

- ``{"path", "kind": "length", "existed", "length"}`` before the first append
  to a path;
- ``{"path", "kind": "content", "existed", "content_b64"}`` before a path is
  created, replaced, or unlinked, unless this operation already recorded its
  content (``content_b64`` is ``null`` when the path did not exist).

Paths are relative to ``.lattice/``. Startup recovery of undo logs a crash
left behind is H-22's (SPEC §8.7).

:func:`_fault` is a no-op test seam at every step (``tests/test_server/faults.py``).
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from lattice.ops.base import OpResult
from lattice.storage import fs
from lattice.storage.fs import (
    MutationKind,
    atomic_write,
    ensure_dir,
    remove_dir,
    truncate_file,
    unlink_path,
)
from lattice.storage.ownership import check_write, locate

if TYPE_CHECKING:
    from lattice.server.project import MutationTracker, Project

UNDO_DIR = "undo"
RECEIPTS_DIR = "receipts"


def _fault(point: str, **ctx: Any) -> None:
    """Deterministic fault-injection seam; a no-op outside tests."""


def result_json(result: OpResult) -> dict:
    """An :class:`OpResult` as the JSON a response and a receipt carry (no ``paths``)."""
    data = asdict(result)
    data.pop("paths", None)
    return data


def undo_log_name(token_id: str | None, op_id: str) -> str:
    return f"{token_id or 'server'}--{op_id}.jsonl"


def receipt_file_name(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%d") + ".jsonl"


def _dumps(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode("utf-8")


def _size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return None


# ---------------------------------------------------------------------------
# Server-control appends
# ---------------------------------------------------------------------------


class ControlFile:
    """An append-only server-control file (undo log, receipt file, journal).

    Opened once; each :meth:`append` is one write loop and one fsync, each a
    fault point (``<point>.write`` gets the fd and bytes, so a test can leave
    a real torn prefix). Creating the file fsyncs its directory entry.
    """

    def __init__(self, path: Path, point: str, *, exclusive: bool = False) -> None:
        """Open *path* for appending (``exclusive``: it must not exist yet). Call
        :meth:`sync_created` next, once the caller has recorded that it holds it."""
        target = locate(path)
        if target is not None:
            check_write(target)
        self.path = path
        self.point = point
        self.created = exclusive or not os.path.lexists(path)
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | (os.O_EXCL if exclusive else 0)
        self.fd: int | None = os.open(path, flags, 0o600)

    def sync_created(self) -> None:
        """Fsync the directory entry of a file this handle created."""
        if self.created:
            fs._fsync_directory(self.path.parent, strict=True)

    def write(self, data: bytes) -> None:
        assert self.fd is not None
        _fault(f"{self.point}.write", fd=self.fd, data=data)
        view = memoryview(data)
        while view:
            view = view[os.write(self.fd, view) :]

    def fsync(self) -> None:
        assert self.fd is not None
        _fault(f"{self.point}.fsync")
        os.fsync(self.fd)

    def append(self, data: bytes) -> None:
        self.write(data)
        self.fsync()

    def close(self) -> None:
        if self.fd is not None:
            fd, self.fd = self.fd, None
            try:
                _fault(f"{self.point}.close")
            finally:
                os.close(fd)


def append_control(path: Path, data: bytes, point: str) -> None:
    """Append one line to a server-control file and fsync it (open, append, close)."""
    handle = ControlFile(path, point)
    try:
        handle.sync_created()
        handle.append(data)
    finally:
        handle.close()


# ---------------------------------------------------------------------------
# The idempotency index (SPEC §8.6 "Replay")
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IndexEntry:
    fp: str | None
    epoch: str
    seq: int
    receipt: str  # the receipt file's name under hosted/receipts/
    offset: int
    length: int


def read_receipt(board: Path, entry: IndexEntry) -> dict:
    """The receipt line an index entry points at (committed lines never change)."""
    path = board / "hosted" / RECEIPTS_DIR / entry.receipt
    with open(path, "rb") as fh:
        fh.seek(entry.offset)
        raw = fh.read(entry.length)
    return json.loads(raw)


# ---------------------------------------------------------------------------
# The transaction
# ---------------------------------------------------------------------------


class Quarantine(Exception):
    """Recovery could not leave the project in a known state; quarantine it."""


@dataclass
class Transaction:
    project: Project
    op: str
    op_id: str
    token_id: str | None
    fp: str | None
    tracker: MutationTracker

    # begin
    epoch: str = ""
    journal_len: int = 0
    receipt_path: Path | None = None
    receipt_existed: bool = False
    receipt_len: int = 0
    undo_path: Path | None = None
    undo: ControlFile | None = None
    _length_recorded: set[str] = field(default_factory=set)
    _content_recorded: set[str] = field(default_factory=set)

    # commit
    seq: int = 0
    line: dict = field(default_factory=dict)
    raw: bytes = b""
    result_data: dict = field(default_factory=dict)
    events: list = field(default_factory=list)
    _receipt_bytes: bytes = b""
    journal_write_started: bool = False
    journal_fsync_started: bool = False
    committed: bool = False

    # finish
    done: set[str] = field(default_factory=set)
    publish_failed: bool = False

    @property
    def board(self) -> Path:
        return self.project.board

    @property
    def hosted(self) -> Path:
        return self.project.board / "hosted"

    # -- 1. begin -----------------------------------------------------------

    def begin(self) -> None:
        journal = self.project.journal
        assert journal is not None
        self.epoch = journal.epoch
        self.journal_len = _size(journal.path) or 0
        ensure_dir(self.hosted / UNDO_DIR)
        ensure_dir(self.hosted / RECEIPTS_DIR)
        self.receipt_path = self.hosted / RECEIPTS_DIR / receipt_file_name()
        size = _size(self.receipt_path)
        self.receipt_existed = size is not None
        self.receipt_len = size or 0
        self.undo_path = self.hosted / UNDO_DIR / undo_log_name(self.token_id, self.op_id)
        # O_EXCL: never adopt (and later delete) an undo log another attempt left.
        self.undo = ControlFile(self.undo_path, "undo", exclusive=True)
        self.undo.sync_created()
        self.undo.append(
            _dumps({"epoch": self.epoch, "token_id": self.token_id, "op_id": self.op_id})
        )

    # -- 2. undo entries (the write recorder's callback) --------------------

    def before_mutation(self, path: Path, kind: MutationKind) -> None:
        """Called by the storage primitives before every durable mutation."""
        self.tracker(path, kind)  # config.json guard; raising here writes nothing
        rel = _relative(self.board, path)
        entry: dict | None = None
        if kind == "append":
            if rel not in self._length_recorded:
                size = _size(path)
                entry = {
                    "path": rel,
                    "kind": "length",
                    "existed": size is not None,
                    "length": size or 0,
                }
                self._length_recorded.add(rel)
        elif rel not in self._content_recorded:
            if path.is_dir():
                entry = None  # an existing directory is never replaced or removed
            elif os.path.lexists(path):
                content = base64.b64encode(path.read_bytes()).decode("ascii")
                entry = {"path": rel, "kind": "content", "existed": True, "content_b64": content}
            else:
                entry = {"path": rel, "kind": "content", "existed": False, "content_b64": None}
            self._content_recorded.add(rel)
        if entry is not None:
            assert self.undo is not None
            self.undo.append(_dumps(entry))
        _fault("board.mutation", path=rel, kind=kind)

    # -- 4 and 5. receipt, then the commit point ----------------------------

    def commit(self, entry: dict, result_data: dict, events: list) -> None:
        journal = self.project.journal
        assert journal is not None and self.receipt_path is not None
        self.seq, self.line, self.raw = journal.prepare(entry)
        self.result_data = result_data
        self.events = events
        receipt = {
            "op_id": self.op_id,
            "token_id": self.token_id,
            "fp": self.fp,
            "epoch": self.epoch,
            "seq": self.seq,
            "result": result_data,
        }
        self._receipt_bytes = _dumps(receipt)
        append_control(self.receipt_path, self._receipt_bytes, "receipt")

        handle = ControlFile(journal.path, "journal")
        try:
            handle.sync_created()
            self.journal_write_started = True
            handle.write(self.raw + b"\n")
            self.journal_fsync_started = True
            handle.fsync()
            # The complete, fsynced line is the commit (SPEC §8.6): nothing after
            # this point, not even closing the file, can make it uncommitted.
            self.committed = True
            # Op status reads without the work lock; it sees the commit at once,
            # before the rest of finish runs (a dict store under the GIL).
            self.project.op_seqs[(self.token_id, self.op_id)] = self.seq
        finally:
            handle.close()

    # -- 6. finish ----------------------------------------------------------

    def finish(self) -> None:
        """The steps after the commit point; each runs once, so recovery can resume."""
        project = self.project
        if "accept" not in self.done:
            _fault("finish.accept")
            assert project.journal is not None
            project.journal.accept(self.line, self.raw)
            self.done.add("accept")
        if "index" not in self.done:
            _fault("finish.index")
            assert self.receipt_path is not None
            key = (self.token_id, self.op_id)
            project.index[key] = IndexEntry(
                fp=self.fp,
                epoch=self.epoch,
                seq=self.seq,
                receipt=self.receipt_path.name,
                offset=self.receipt_len,
                length=len(self._receipt_bytes),
            )
            project.op_seqs[key] = self.seq
            self.done.add("index")
        if "memory" not in self.done:
            project.floors.observe_events(self.events)
            project.remember_watched(self.line.get("paths") or [])
            self.done.add("memory")
        if "undo_delete" not in self.done:
            self._delete_undo_log()
            self.done.add("undo_delete")
        if "publish" not in self.done and not self.publish_failed:
            try:
                if project.publish is not None:
                    project.publish(self.line)
            except BaseException:
                self.publish_failed = True
                raise
            self.done.add("publish")

    def _delete_undo_log(self) -> None:
        if self.undo is not None:
            self.undo.close()
        _fault("finish.undo_delete")
        if self.undo_path is None:
            return
        if os.path.lexists(self.undo_path):
            unlink_path(self.undo_path)  # a strict write: fsyncs the directory entry
        else:
            # A retry after the unlink landed but its directory fsync failed.
            fs._fsync_directory(self.undo_path.parent, strict=True)

    # -- recovery -----------------------------------------------------------

    def recover(self) -> None:
        """Transaction recovery for this operation (SPEC §8.6); raises :class:`Quarantine`
        when the project cannot be left in a known state."""
        if self.committed:
            try:
                self.finish()
                if self.publish_failed:
                    self.project.publication_failed(self.seq)
            except BaseException as exc:
                raise Quarantine(f"finishing committed seq {self.seq} failed: {exc!r}") from exc
            return
        if self.journal_fsync_started:
            raise Quarantine(
                f"the journal fsync for {self.op_id} failed; whether it committed is unknown"
            )
        try:
            self._roll_back()
        except BaseException as exc:
            raise Quarantine(f"rolling back {self.op_id} failed: {exc!r}") from exc

    def _roll_back(self) -> None:
        journal = self.project.journal
        _fault("recover.truncate")
        if journal is not None and (_size(journal.path) or 0) > self.journal_len:
            truncate_file(journal.path, self.journal_len)
        if self.receipt_path is not None:
            size = _size(self.receipt_path)
            if size is not None:
                if not self.receipt_existed:
                    unlink_path(self.receipt_path)
                elif size > self.receipt_len:
                    truncate_file(self.receipt_path, self.receipt_len)
        if self.undo is None:
            return  # the undo log was never created: nothing was changed
        self.undo.close()
        assert self.undo_path is not None
        entries = read_undo_log(self.undo_path)
        for entry in reversed(entries):
            _fault("recover.rollback", path=entry["path"])
            self._undo(entry)
        _fault("recover.undo_delete")
        unlink_path(self.undo_path)

    def _undo(self, entry: dict) -> None:
        path = _board_path(self.board, entry["path"])
        if entry["kind"] == "content":
            if entry["existed"]:
                atomic_write(path, base64.b64decode(entry["content_b64"]))
            elif path.is_dir() and not path.is_symlink():
                remove_dir(path)
            elif os.path.lexists(path):
                unlink_path(path)
        elif entry["kind"] == "length":
            if not entry["existed"]:
                unlink_path(path, missing_ok=True)
            elif (_size(path) or 0) != entry["length"]:
                truncate_file(path, entry["length"])
        else:
            raise ValueError(f"unknown undo entry kind {entry['kind']!r}")


def read_undo_log(path: Path) -> list[dict]:
    """An undo log's entries (after its header line), a torn final line ignored:
    it guards a change that was never made."""
    data = path.read_bytes()
    lines = data.split(b"\n")
    lines.pop()  # a torn final line, or b"" when the file ends with a newline
    entries: list[dict] = []
    for number, raw in enumerate(lines):
        if number == 0:
            continue  # the header: {"epoch", "token_id", "op_id"}
        entry = json.loads(raw)
        entries.append(entry)
    return entries


def _relative(board: Path, path: Path) -> str:
    return Path(path).relative_to(board.resolve()).as_posix()


def _board_path(board: Path, rel: str) -> Path:
    """An undo entry's path under *board*; the primitives confine it again."""
    parts = PurePosixPath(rel).parts
    if not parts or PurePosixPath(rel).is_absolute() or ".." in parts:
        raise ValueError(f"undo entry path {rel!r} is not a path inside the board")
    return board.resolve().joinpath(*parts)
