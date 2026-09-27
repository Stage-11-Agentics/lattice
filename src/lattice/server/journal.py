"""The project journal and its epoch (SPEC §8.6 "Journal and metadata", §8.2 rotation).

``<board>/hosted/journal.jsonl`` holds one line per committed operation,
including no-ops: ``{seq, ts, op, op_id, fp, token_id, task_id, event_ids,
paths, lengths}``. ``seq`` starts at 1 per epoch. ``hosted/journal_meta.json``
holds ``{epoch, created_at, baseline, clean_shutdown}``; ``baseline`` is every
append-only log's byte length when the epoch began.

All writes here run in the owning server's context (the owner flag, or the
admin's own flock while it creates a project), under the project's work
lock. The journal line is the operation's commit point, so it is appended
with one write and one fsync.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lattice.core.ids import generate_instance_id
from lattice.storage.fs import atomic_write, ensure_dir, jsonl_append, unlink_path
from lattice.storage.ownership import PathClass, check_write, classify_path, locate

HOSTED_DIR = "hosted"
JOURNAL = "journal.jsonl"
JOURNAL_META = "journal_meta.json"
ROTATION = "rotation.json"


class JournalError(Exception):
    """The journal or its metadata is missing or unparseable beyond a torn final line."""


def new_epoch() -> str:
    return "ep_" + generate_instance_id().removeprefix("inst_")


def now_ms() -> str:
    """UTC timestamp with millisecond precision."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def fingerprint(
    op: str,
    params: Any,
    actor: Any,
    actor_name: Any,
    attestations: Any,
    expect_last_event_id: Any,
) -> str:
    """The request fingerprint ``fp`` (SPEC §8.6): 32 hex chars of SHA-256."""
    body = {
        "op": op,
        "params": params,
        "actor": actor,
        "actor_name": actor_name,
        "attestations": attestations,
        "expect_last_event_id": expect_last_event_id,
    }
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()[:32]


def line_hash(line: bytes) -> str:
    """A journal line's hash: 32 hex chars of SHA-256 over its bytes, no newline."""
    return hashlib.sha256(line.rstrip(b"\n")).hexdigest()[:32]


def log_lengths(board: Path) -> dict[str, int]:
    """Every durable append-only log (``*.jsonl``) under *board*, with its byte length."""
    board = Path(board)
    lengths: dict[str, int] = {}
    for dirpath, dirnames, filenames in os.walk(board):
        rel_dir = Path(dirpath).relative_to(board)
        if rel_dir.parts and classify_path(rel_dir) is not PathClass.DURABLE:
            dirnames[:] = []
            continue
        dirnames.sort()
        for name in sorted(filenames):
            if not name.endswith(".jsonl"):
                continue
            rel = (rel_dir / name).as_posix()
            if classify_path(rel) is PathClass.DURABLE:
                try:
                    lengths[rel] = (Path(dirpath) / name).stat().st_size
                except OSError:
                    continue
    return lengths


def _rename(src: Path, dst: Path) -> None:
    """Rename a server-control file, checked like any primitive write, then fsync the dir."""
    for path in (src, dst):
        target = locate(path)
        if target is not None:
            check_write(target)
    os.replace(src, dst)
    fd = os.open(str(dst.parent), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@dataclass(frozen=True)
class LineDelta:
    """What accepting one journal line changes (:meth:`Journal.stage`)."""

    seq: int
    digest: str
    size: int  # the line's bytes on disk, newline included
    history: tuple[tuple[str, int | None], ...]
    lengths: tuple[tuple[str, int], ...]


@dataclass
class Journal:
    """The in-memory view of one project's journal for the current epoch."""

    board: Path
    epoch: str
    created_at: str
    baseline: dict[str, int]
    head_seq: int = 0
    #: ``line_hashes[seq - 1]`` is line ``seq``'s hash.
    line_hashes: list[str] = field(default_factory=list)
    #: ``line_offsets[seq - 1]`` is the byte offset where line ``seq`` starts;
    #: ``end_offset`` is the journal's length after the last accepted line.
    line_offsets: list[int] = field(default_factory=list)
    end_offset: int = 0
    #: The last known length of every log: ``baseline`` updated by each line's ``lengths``.
    known_lengths: dict[str, int] = field(default_factory=dict)
    #: Each log's length history in this epoch (SPEC §8.8 "Append deltas"):
    #: ``(seq, length)`` for every ``lengths`` entry, and ``(seq, None)`` when a
    #: known log was changed some other way (created whole, replaced, unlinked,
    #: relocated), which leaves no append base at that seq.
    length_history: dict[str, list[tuple[int, int | None]]] = field(default_factory=dict)
    clean_shutdown: Any = None
    #: ``(epoch, head_seq, head_hash)``, replaced in one assignment after each
    #: accepted line, so a reader without the work lock sees a consistent head.
    head: tuple[str, int, str | None] = ("", 0, None)

    @property
    def hosted(self) -> Path:
        return self.board / HOSTED_DIR

    @property
    def path(self) -> Path:
        return self.hosted / JOURNAL

    @property
    def head_hash(self) -> str | None:
        return self.head[2]

    # -- creation and loading ------------------------------------------------

    @classmethod
    def create(cls, board: Path) -> Journal:
        """Start a journal at a new epoch, seq 0, with the board's current log lengths."""
        board = Path(board)
        ensure_dir(board / HOSTED_DIR)
        meta = {
            "epoch": new_epoch(),
            "created_at": now_ms(),
            "baseline": log_lengths(board),
            "clean_shutdown": None,
        }
        atomic_write(board / HOSTED_DIR / JOURNAL_META, _dump_meta(meta))
        atomic_write(board / HOSTED_DIR / JOURNAL, b"")
        return cls._from_meta(board, meta)

    @classmethod
    def _from_meta(cls, board: Path, meta: dict) -> Journal:
        baseline = dict(meta.get("baseline") or {})
        return cls(
            board=board,
            epoch=meta["epoch"],
            created_at=meta.get("created_at", ""),
            baseline=baseline,
            known_lengths=dict(baseline),
            clean_shutdown=meta.get("clean_shutdown"),
            head=(meta["epoch"], 0, None),
        )

    @classmethod
    def load(cls, board: Path) -> Journal:
        """Read the journal, dropping a torn final line (its operation never committed).

        Raises ``JournalError`` when the journal or its metadata is missing or
        unparseable beyond the final line.
        """
        board = Path(board)
        meta_path = board / HOSTED_DIR / JOURNAL_META
        journal_path = board / HOSTED_DIR / JOURNAL
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise JournalError(f"{meta_path} is missing or unreadable: {exc}") from exc
        if not isinstance(meta, dict) or not str(meta.get("epoch", "")).startswith("ep_"):
            raise JournalError(f"{meta_path} has no epoch")
        try:
            data = journal_path.read_bytes()
        except OSError as exc:
            raise JournalError(f"{journal_path} is missing or unreadable: {exc}") from exc
        if data and not data.endswith(b"\n"):
            data = data[: data.rfind(b"\n") + 1]
            atomic_write(journal_path, data)
        journal = cls._from_meta(board, meta)
        for number, raw in enumerate(data.splitlines(), start=1):
            try:
                entry = json.loads(raw)
            except ValueError as exc:
                raise JournalError(f"{journal_path} line {number} is not JSON") from exc
            if not isinstance(entry, dict) or entry.get("seq") != number:
                raise JournalError(f"{journal_path} line {number} has seq {entry!r:.60}")
            journal._account(entry, raw)
        return journal

    def _account(self, entry: dict, raw: bytes) -> None:
        self.commit(self.stage(entry, raw))

    def stage(self, entry: dict, raw: bytes) -> LineDelta:
        """Everything accepting line *entry* changes, computed without changing
        anything (the committed-line finalizer's first half)."""
        seq = entry["seq"]
        if seq != self.head_seq + 1:
            raise JournalError(f"journal line {seq} does not follow head {self.head_seq}")
        lengths = {p: n for p, n in (entry.get("lengths") or {}).items()}
        history: list[tuple[str, int | None]] = [
            (path, None)
            for path in entry.get("paths") or ()
            if path not in lengths and self.is_log(path)
        ]
        history.extend(lengths.items())
        return LineDelta(
            seq=seq,
            digest=line_hash(raw),
            size=len(raw.rstrip(b"\n")) + 1,
            history=tuple(history),
            lengths=tuple(lengths.items()),
        )

    def commit(self, delta: LineDelta) -> None:
        """Apply a staged line: in-memory assignments only, the head last.

        Idempotent by ``seq``: a line already accounted is skipped, and anything an
        interrupted earlier commit left past the head is dropped before applying.
        """
        head = self.head_seq
        if delta.seq <= head:
            return
        del self.line_hashes[head:]
        del self.line_offsets[head:]
        for path, _length in delta.history:
            entries = self.length_history.get(path)
            while entries and entries[-1][0] > head:
                entries.pop()
            if entries == []:
                del self.length_history[path]
        for path, length in delta.history:
            self.length_history.setdefault(path, []).append((delta.seq, length))
        for path, length in delta.lengths:
            self.known_lengths[path] = length
        self.line_offsets.append(self.end_offset)
        self.line_hashes.append(delta.digest)
        self.end_offset += delta.size
        self.head_seq = delta.seq
        self.head = (self.epoch, delta.seq, delta.digest)

    # -- the sync path's reads (call under the work lock) -------------------

    def is_log(self, path: str) -> bool:
        """Whether *path* is an append-only log this epoch knows (baseline or ``lengths``)."""
        return path in self.baseline or path in self.length_history

    def length_at(self, path: str, seq: int) -> int | None:
        """The log's length as of *seq*: its latest history value at or before *seq*,
        else its ``baseline`` length. ``None``: it did not exist then, or was last
        changed by something other than an append (no append base)."""
        length = self.baseline.get(path)
        for at, value in self.length_history.get(path, ()):
            if at > seq:
                break
            length = value
        return length

    def hash_at(self, seq: int) -> str | None:
        """Line *seq*'s hash (``None`` for 0 or beyond the head)."""
        if 0 < seq <= self.head_seq:
            return self.line_hashes[seq - 1]
        return None

    def read_lines(self, after: int, upto: int | None = None) -> list[tuple[int, bytes]]:
        """``(seq, raw line without newline)`` for ``after < seq <= upto`` (default the
        head), read by offset."""
        upto = self.head_seq if upto is None else min(upto, self.head_seq)
        if upto <= after:
            return []
        start = self.line_offsets[after]
        with open(self.path, "rb") as fh:
            fh.seek(start)
            data = fh.read(self.end_offset - start)
        lines = data.split(b"\n")
        return [(after + i + 1, lines[i]) for i in range(upto - after)]

    # -- appending -----------------------------------------------------------

    def prepare(self, entry: dict[str, Any]) -> tuple[int, dict, bytes]:
        """The next line for *entry*: assigns ``seq`` and ``ts``. Returns ``(seq, line,
        raw)``, where *raw* is the line's bytes without the newline. Nothing changes
        until the line is on disk and :meth:`accept` is called."""
        seq = self.head_seq + 1
        line = {"seq": seq, "ts": now_ms(), **entry}
        raw = json.dumps(line, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return seq, line, raw.encode("utf-8")

    def accept(self, line: dict, raw: bytes) -> None:
        """Account for a line written and fsynced (the in-memory head, hash, lengths)."""
        if line["seq"] == self.head_seq + 1:
            self.commit(self.stage(line, raw))

    def append(self, entry: dict[str, Any]) -> tuple[int, dict]:
        """Append one line outside a transaction and account for it; returns
        ``(seq, line)``. Operations commit through :mod:`lattice.server.transactions`."""
        seq, line, raw = self.write(entry)
        self.accept(line, raw)
        return seq, line

    def write(self, entry: dict[str, Any]) -> tuple[int, dict, bytes]:
        """Append and fsync one line without accounting for it (the caller's
        finalizer does, :meth:`accept`); returns ``(seq, line, raw)``."""
        seq, line, raw = self.prepare(entry)
        try:
            before = self.path.stat().st_size
        except FileNotFoundError:
            before = 0
        try:
            jsonl_append(self.path, raw.decode("utf-8") + "\n")
        except BaseException:
            # Never leave a partial line for the next append to bury mid-file.
            self._truncate(before)
            raise
        return seq, line, raw

    def _truncate(self, length: int) -> None:
        try:
            with open(self.path, "r+b") as fh:
                fh.truncate(length)
                fh.flush()
                os.fsync(fh.fileno())
        except OSError:
            pass  # the caller quarantines the project; load drops a torn tail

    def read_entries(self, after: int = 0) -> list[dict]:
        """Journal entries with ``seq > after`` (reads the file)."""
        return [json.loads(raw) for _seq, raw in self.read_lines(after)]

    # -- epoch rotation (SPEC §8.2) -----------------------------------------

    def rotate(self) -> Journal:
        """Start a new epoch; returns the new journal. Every step is fsynced and idempotent."""
        return rotate_epoch(self.board, old_epoch=self.epoch)


def _dump_meta(meta: dict) -> str:
    return json.dumps(meta, sort_keys=True, indent=2) + "\n"


def rotate_epoch(board: Path, *, old_epoch: str | None) -> Journal:
    """Rotate the journal epoch: (1) ``rotation.json``, (2) keep the old journal as
    ``journal.<old epoch>.jsonl``, (3) new ``journal_meta.json`` with a fresh
    ``baseline``, (4) empty ``journal.jsonl``, (5) remove ``rotation.json``.

    *old_epoch* is ``None`` when the old journal is missing or unreadable.
    """
    board = Path(board)
    hosted = board / HOSTED_DIR
    ensure_dir(hosted)
    marker = {"old_epoch": old_epoch, "new_epoch": new_epoch()}
    atomic_write(hosted / ROTATION, json.dumps(marker, sort_keys=True) + "\n")
    return finish_rotation(board)


def finish_rotation(board: Path) -> Journal:
    """Complete the rotation ``hosted/rotation.json`` records (safe to repeat)."""
    board = Path(board)
    hosted = board / HOSTED_DIR
    marker = json.loads((hosted / ROTATION).read_text(encoding="utf-8"))
    old_epoch, epoch = marker.get("old_epoch"), marker["new_epoch"]
    journal_path = hosted / JOURNAL
    meta_path = hosted / JOURNAL_META
    current_meta: dict = {}
    try:
        current_meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    already_new = current_meta.get("epoch") == epoch
    if not already_new:
        if journal_path.exists():
            kept = hosted / f"journal.{old_epoch or 'unknown-' + epoch}.jsonl"
            if not kept.exists():
                _rename(journal_path, kept)
        meta = {
            "epoch": epoch,
            "created_at": now_ms(),
            "baseline": log_lengths(board),
            "clean_shutdown": None,
        }
        atomic_write(meta_path, _dump_meta(meta))
        current_meta = meta
    if not already_new or not journal_path.exists():
        atomic_write(journal_path, b"")
    unlink_path(hosted / ROTATION, missing_ok=True)
    return Journal._from_meta(board, current_meta)
