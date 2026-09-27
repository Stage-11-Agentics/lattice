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
    #: The last known length of every log: ``baseline`` updated by each line's ``lengths``.
    known_lengths: dict[str, int] = field(default_factory=dict)
    clean_shutdown: Any = None

    @property
    def hosted(self) -> Path:
        return self.board / HOSTED_DIR

    @property
    def path(self) -> Path:
        return self.hosted / JOURNAL

    @property
    def head_hash(self) -> str | None:
        return self.line_hashes[-1] if self.line_hashes else None

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
        self.head_seq = entry["seq"]
        self.line_hashes.append(line_hash(raw))
        for path, length in (entry.get("lengths") or {}).items():
            self.known_lengths[path] = length

    # -- appending -----------------------------------------------------------

    def append(self, entry: dict[str, Any]) -> tuple[int, dict]:
        """Append one committed operation; assigns ``seq`` and ``ts``. Returns ``(seq, line)``."""
        seq = self.head_seq + 1
        line = {"seq": seq, "ts": now_ms(), **entry}
        raw = json.dumps(line, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        jsonl_append(self.path, raw + "\n")
        self._account(line, raw.encode("utf-8"))
        return seq, line

    def read_entries(self, after: int = 0) -> list[dict]:
        """Journal entries with ``seq > after`` (reads the file)."""
        entries = []
        for raw in self.path.read_bytes().splitlines():
            entry = json.loads(raw)
            if entry["seq"] > after:
                entries.append(entry)
        return entries

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
