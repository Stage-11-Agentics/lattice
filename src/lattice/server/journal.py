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
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
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


_EMPTY: Mapping[str, Any] = MappingProxyType({})


def _history_entries(entry: dict, is_log: Callable[[str], bool]) -> list[tuple[str, int | None]]:
    """The length-history entries line *entry* adds: ``(path, None)`` for a known log
    it changed other than by appending, then ``(path, length)`` for each append."""
    lengths = entry.get("lengths") or {}
    history: list[tuple[str, int | None]] = [
        (path, None) for path in entry.get("paths") or () if path not in lengths and is_log(path)
    ]
    history.extend(lengths.items())
    return history


@dataclass(frozen=True)
class JournalIndex:
    """The journal's in-memory index for one epoch: an immutable value.

    A committed line produces a new index (:meth:`advance`, which changes
    nothing); a project publishes it with its other finalized memory in one
    assignment, so no reader ever sees a half-advanced index.
    """

    epoch: str
    baseline: Mapping[str, int] = _EMPTY
    #: ``line_offsets[seq - 1]`` is the byte offset where line ``seq`` starts;
    #: ``end_offset`` is the journal's length after the head line.
    line_offsets: tuple[int, ...] = ()
    #: ``line_hashes[seq - 1]`` is line ``seq``'s hash.
    line_hashes: tuple[str, ...] = ()
    end_offset: int = 0
    head_seq: int = 0
    #: ``(epoch, head_seq, head_hash)``.
    head: tuple[str, int, str | None] = ("", 0, None)
    #: Each log's length history in this epoch (SPEC §8.8 "Append deltas"):
    #: ``(seq, length)`` for every ``lengths`` entry, and ``(seq, None)`` when a
    #: known log was changed some other way (created whole, replaced, unlinked,
    #: relocated), which leaves no append base at that seq.
    length_history: Mapping[str, tuple[tuple[int, int | None], ...]] = _EMPTY
    #: The last known length of every log: ``baseline`` updated by each ``lengths``.
    known_lengths: Mapping[str, int] = _EMPTY

    @classmethod
    def build(
        cls, epoch: str, baseline: dict[str, int], lines: list[tuple[dict, bytes]]
    ) -> JournalIndex:
        """The index of an epoch's lines, in one pass (loading a journal)."""
        offsets: list[int] = []
        hashes: list[str] = []
        history: dict[str, list[tuple[int, int | None]]] = {}
        known = dict(baseline)
        end = 0
        for entry, raw in lines:
            seq = entry["seq"]
            if seq != len(hashes) + 1:
                raise JournalError(f"journal line {seq} does not follow head {len(hashes)}")
            for path, length in _history_entries(entry, lambda p: p in baseline or p in history):
                history.setdefault(path, []).append((seq, length))
            known.update(entry.get("lengths") or {})
            offsets.append(end)
            end += len(raw.rstrip(b"\n")) + 1
            hashes.append(line_hash(raw))
        head_seq = len(hashes)
        return cls(
            epoch=epoch,
            baseline=MappingProxyType(dict(baseline)),
            line_offsets=tuple(offsets),
            line_hashes=tuple(hashes),
            end_offset=end,
            head_seq=head_seq,
            head=(epoch, head_seq, hashes[-1] if hashes else None),
            length_history=MappingProxyType({k: tuple(v) for k, v in history.items()}),
            known_lengths=MappingProxyType(known),
        )

    def advance(self, entry: dict, raw: bytes) -> JournalIndex:
        """The index after committed line *entry*: a new value; this one is unchanged."""
        seq = entry["seq"]
        if seq != self.head_seq + 1:
            raise JournalError(f"journal line {seq} does not follow head {self.head_seq}")
        history = dict(self.length_history)
        for path, length in _history_entries(entry, self.is_log):
            history[path] = (*history.get(path, ()), (seq, length))
        known = dict(self.known_lengths)
        known.update(entry.get("lengths") or {})
        digest = line_hash(raw)
        return replace(
            self,
            line_offsets=(*self.line_offsets, self.end_offset),
            line_hashes=(*self.line_hashes, digest),
            end_offset=self.end_offset + len(raw.rstrip(b"\n")) + 1,
            head_seq=seq,
            head=(self.epoch, seq, digest),
            length_history=MappingProxyType(history),
            known_lengths=MappingProxyType(known),
        )

    # -- reads ----------------------------------------------------------------

    @property
    def head_hash(self) -> str | None:
        return self.head[2]

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


class Journal:
    """One project's journal file for the current epoch, and its index.

    The index is an immutable :class:`JournalIndex`. A standalone journal keeps
    its own (:meth:`accept` replaces it). A project's journal is *bound* to the
    project's published state (:meth:`bind`), so it advances only through the
    project's committed-line finalizer, in the same assignment as the rest of
    that state.
    """

    def __init__(
        self,
        board: Path,
        epoch: str,
        created_at: str,
        baseline: dict[str, int],
        clean_shutdown: Any = None,
        index: JournalIndex | None = None,
    ) -> None:
        self.board = board
        self.epoch = epoch
        self.created_at = created_at
        self.baseline = baseline
        self.clean_shutdown = clean_shutdown
        self._index = index or JournalIndex.build(epoch, baseline, [])
        self._source: Callable[[], JournalIndex] | None = None

    @property
    def index(self) -> JournalIndex:
        return self._source() if self._source is not None else self._index

    def bind(self, source: Callable[[], JournalIndex]) -> None:
        """Read the index from *source* (the owning project's published state) from now on."""
        self._source = source

    # The index's fields, read through the current value.

    @property
    def head_seq(self) -> int:
        return self.index.head_seq

    @property
    def head(self) -> tuple[str, int, str | None]:
        return self.index.head

    @property
    def head_hash(self) -> str | None:
        return self.index.head_hash

    @property
    def line_hashes(self) -> tuple[str, ...]:
        return self.index.line_hashes

    @property
    def line_offsets(self) -> tuple[int, ...]:
        return self.index.line_offsets

    @property
    def end_offset(self) -> int:
        return self.index.end_offset

    @property
    def length_history(self) -> Mapping[str, tuple[tuple[int, int | None], ...]]:
        return self.index.length_history

    @property
    def known_lengths(self) -> Mapping[str, int]:
        return self.index.known_lengths

    def is_log(self, path: str) -> bool:
        return self.index.is_log(path)

    def length_at(self, path: str, seq: int) -> int | None:
        return self.index.length_at(path, seq)

    def hash_at(self, seq: int) -> str | None:
        return self.index.hash_at(seq)

    @property
    def hosted(self) -> Path:
        return self.board / HOSTED_DIR

    @property
    def path(self) -> Path:
        return self.hosted / JOURNAL

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
    def _from_meta(
        cls, board: Path, meta: dict, lines: list[tuple[dict, bytes]] | None = None
    ) -> Journal:
        baseline = dict(meta.get("baseline") or {})
        return cls(
            board=board,
            epoch=meta["epoch"],
            created_at=meta.get("created_at", ""),
            baseline=baseline,
            clean_shutdown=meta.get("clean_shutdown"),
            index=JournalIndex.build(meta["epoch"], baseline, lines or []),
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
        lines: list[tuple[dict, bytes]] = []
        for number, raw in enumerate(data.splitlines(), start=1):
            try:
                entry = json.loads(raw)
            except ValueError as exc:
                raise JournalError(f"{journal_path} line {number} is not JSON") from exc
            if not isinstance(entry, dict) or entry.get("seq") != number:
                raise JournalError(f"{journal_path} line {number} has seq {entry!r:.60}")
            lines.append((entry, raw))
        return cls._from_meta(board, meta, lines)

    # -- the sync path's reads (call under the work lock) -------------------

    def read_lines(self, after: int, upto: int | None = None) -> list[tuple[int, bytes]]:
        """``(seq, raw line without newline)`` for ``after < seq <= upto`` (default the
        head), read by offset."""
        index = self.index
        upto = index.head_seq if upto is None else min(upto, index.head_seq)
        if upto <= after:
            return []
        start = index.line_offsets[after]
        with open(self.path, "rb") as fh:
            fh.seek(start)
            data = fh.read(index.end_offset - start)
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
        """Account for a line written and fsynced, on a standalone journal. A bound
        journal advances only through its project's finalizer."""
        if self._source is not None:
            raise RuntimeError("a project's journal advances only through its finalizer")
        if line["seq"] == self._index.head_seq + 1:
            self._index = self._index.advance(line, raw)

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
