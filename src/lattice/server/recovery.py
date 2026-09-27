"""Startup recovery: what a project's load settles from disk (SPEC §8.7).

:meth:`lattice.server.project.Project.load` runs these steps in order, under
the project's locks, holding its owner lease:

1. lease, and an interrupted epoch rotation (``hosted/rotation.json``);
2. torn tails: :func:`drop_torn_tails` (receipt files and undo logs; the
   journal's own tail is dropped by :meth:`Journal.load`);
3. a missing journal: rotate when no undo log exists, else quarantine until
   ``lattice server project recover`` decides;
4. transactions: :func:`settle_undo_logs`, then :func:`rebuild_index`;
5. maintenance record and restore fingerprint (:func:`tree_fingerprint`);
6. foreign appends: :func:`foreign_appends`;
7. strict discovery; 8. in-memory state.

An undo log is matched to its journal line exactly: the line at the header's
``seq`` in the journal of the header's ``epoch`` must carry the same
``token_id`` and ``op_id``. A header without ``seq`` (written before the
field existed) cannot be classified, so the project waits for ``project
recover``. Undo logs are settled newest ``seq`` first, never by mtime.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from lattice.server.journal import HOSTED_DIR, JOURNAL, JOURNAL_META, Journal, log_lengths
from lattice.server.transactions import (
    RECEIPTS_DIR,
    UNDO_DIR,
    IndexEntry,
    read_undo_header,
    roll_back,
)
from lattice.storage.fs import atomic_write, truncate_file, unlink_path
from lattice.storage.ownership import PathClass, classify_path

if TYPE_CHECKING:
    from lattice.server.log import ServerLog

#: Receipts are kept this many days (SPEC §8.6 "Replay").
RECEIPT_RETENTION_DAYS = 7


class NeedsRecover(Exception):
    """Undo logs the journal cannot classify: ``project recover`` must decide."""


def recover_hint(slug: str) -> str:
    return f"run 'lattice server project recover {slug} --rollback | --keep', then load it"


# ---------------------------------------------------------------------------
# Step 2: torn tails
# ---------------------------------------------------------------------------


def undo_log_paths(board: Path) -> list[Path]:
    directory = Path(board) / HOSTED_DIR / UNDO_DIR
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    return [directory / n for n in sorted(names) if n.endswith(".jsonl") and not n.startswith(".")]


def _cut_to_last_newline(path: Path) -> None:
    data = path.read_bytes()
    if data and not data.endswith(b"\n"):
        truncate_file(path, data.rfind(b"\n") + 1)


def drop_torn_tails(board: Path) -> None:
    """Cut a torn final line from every receipt file and undo log. An undo log
    left without a complete header guards no change, so it is removed."""
    board = Path(board)
    receipts = board / HOSTED_DIR / RECEIPTS_DIR
    try:
        names = sorted(os.listdir(receipts))
    except OSError:
        names = []
    for name in names:
        if name.endswith(".jsonl"):
            _cut_to_last_newline(receipts / name)
    for path in undo_log_paths(board):
        if read_undo_header(path) is None:
            unlink_path(path)
        else:
            _cut_to_last_newline(path)


# ---------------------------------------------------------------------------
# Step 4: transactions
# ---------------------------------------------------------------------------


class JournalLines:
    """Journal lines by ``(epoch, seq)``: the current journal and the retained
    ``journal.<epoch>.jsonl`` files (which never change), read on demand."""

    def __init__(self, board: Path, journal: Journal | None) -> None:
        self.board = Path(board)
        self.journal = journal
        self._cache: dict[str, list[dict] | None] = {}

    def path_for(self, epoch: str) -> Path:
        hosted = self.board / HOSTED_DIR
        if self.journal is not None and epoch == self.journal.epoch:
            return hosted / JOURNAL
        return hosted / f"journal.{epoch}.jsonl"

    def lines(self, epoch: str) -> list[dict] | None:
        """Every complete line of *epoch*'s journal, or ``None`` if it is gone."""
        if epoch not in self._cache:
            try:
                data = self.path_for(epoch).read_bytes()
            except OSError:
                self._cache[epoch] = None
            else:
                lines = []
                for raw in data.split(b"\n")[:-1]:  # a torn tail is never committed
                    try:
                        entry = json.loads(raw)
                    except ValueError:
                        entry = {}
                    lines.append(entry if isinstance(entry, dict) else {})
                self._cache[epoch] = lines
        return self._cache[epoch]

    def at(self, epoch: Any, seq: Any) -> dict | None:
        if not isinstance(epoch, str) or not isinstance(seq, int) or seq < 1:
            return None
        lines = self.lines(epoch)
        if lines is None or seq > len(lines):
            return None
        return lines[seq - 1]

    def known(self, epoch: Any) -> bool:
        return isinstance(epoch, str) and self.lines(epoch) is not None


def classify(header: dict, lines: JournalLines) -> bool | None:
    """``True``: committed; ``False``: uncommitted; ``None``: cannot tell (no
    ``seq`` in the header, or the epoch's journal is gone)."""
    epoch, seq = header.get("epoch"), header.get("seq")
    if not isinstance(seq, int) or not lines.known(epoch):
        return None
    line = lines.at(epoch, seq)
    return (
        line is not None
        and line.get("seq") == seq
        and line.get("token_id") == header.get("token_id")
        and line.get("op_id") == header.get("op_id")
    )


def _by_seq(board: Path) -> list[tuple[Path, dict]]:
    """Undo logs with their headers, newest seq first (a log without ``seq`` last)."""
    logs = []
    for path in undo_log_paths(board):
        header = read_undo_header(path)
        if header is not None:
            logs.append((path, header))

    def key(item: tuple[Path, dict]) -> tuple[int, str]:
        seq = item[1].get("seq")
        return (seq if isinstance(seq, int) else -1, item[0].name)

    return sorted(logs, key=key, reverse=True)


@dataclass
class Settled:
    committed: list[str]
    rolled_back: list[str]
    kept: list[str]


def settle_undo_logs(
    board: Path,
    journal: Journal | None,
    *,
    unclassified: str | None = None,
    log: ServerLog | None = None,
    slug: str | None = None,
) -> Settled:
    """Delete each committed undo log; roll back each uncommitted one.

    A log the journal cannot classify raises :class:`NeedsRecover` unless
    *unclassified* says what to do with it (``"rollback"`` or ``"keep"``,
    ``project recover``'s flags). Nothing is changed before every log is known
    to be classifiable, so a refusal leaves the disk as it was.
    """
    board = Path(board)
    lines = JournalLines(board, journal)
    logs = [(path, header, classify(header, lines)) for path, header in _by_seq(board)]
    unknown = [path.name for path, _, verdict in logs if verdict is None]
    if unknown and unclassified is None:
        raise NeedsRecover(
            f"{len(unknown)} undo log(s) cannot be matched to a journal line "
            f"({', '.join(unknown)})"
        )
    settled = Settled([], [], [])
    for path, header, verdict in logs:
        if verdict is True:
            unlink_path(path)
            settled.committed.append(path.name)
            continue
        if verdict is None and unclassified == "keep":
            unlink_path(path)
            settled.kept.append(path.name)
            continue
        restored = roll_back(board, path)
        settled.rolled_back.append(path.name)
        if log is not None:
            log.warning(
                "recovery_rollback",
                project=slug,
                op_id=header.get("op_id"),
                token_id=header.get("token_id"),
                epoch=header.get("epoch"),
                seq=header.get("seq"),
                paths=restored,
            )
    return settled


# ---------------------------------------------------------------------------
# Step 4, continued: the idempotency index and the op-status map
# ---------------------------------------------------------------------------


def receipt_date(name: str) -> date | None:
    try:
        return datetime.strptime(name.removesuffix(".jsonl"), "%Y-%m-%d").date()
    except ValueError:
        return None


def utc_today() -> date:
    """Today's UTC date: the one clock receipt retention reads (tests move it)."""
    return datetime.now(timezone.utc).date()


def receipt_expired(name: str, today: date | None = None) -> bool:
    """Whether the receipt file *name* (``YYYY-MM-DD.jsonl``) is past retention."""
    day = receipt_date(name) if name.endswith(".jsonl") else None
    cutoff = (today or utc_today()) - timedelta(days=RECEIPT_RETENTION_DAYS)
    return day is not None and day < cutoff


def expired_receipt_files(board: Path, today: date | None = None) -> list[Path]:
    """Receipt files older than the retention window."""
    directory = Path(board) / HOSTED_DIR / RECEIPTS_DIR
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    return [directory / name for name in names if receipt_expired(name, today)]


def rebuild_index(
    board: Path, journal: Journal, today: date | None = None
) -> tuple[dict[tuple[str | None, str], IndexEntry], dict[tuple[str | None, str], int], int]:
    """The idempotency index and the current epoch's op-status map, from disk.

    Receipt files past retention are deleted. A receipt line counts only if
    the journal of its epoch holds a line at its ``seq`` with the same
    ``token_id`` and ``op_id``; every other line is an orphan of an
    uncommitted operation and is rewritten out of its file. Returns
    ``(index, op_seqs, orphans_removed)``.
    """
    board = Path(board)
    for path in expired_receipt_files(board, today):
        unlink_path(path)
    lines = JournalLines(board, journal)
    index: dict[tuple[str | None, str], IndexEntry] = {}
    orphans = 0
    directory = board / HOSTED_DIR / RECEIPTS_DIR
    try:
        names = sorted(n for n in os.listdir(directory) if n.endswith(".jsonl"))
    except OSError:
        names = []
    for name in names:
        path = directory / name
        data = path.read_bytes()
        kept: list[bytes] = []
        entries: list[tuple[tuple[str | None, str], dict, int, int]] = []
        offset = 0
        changed = False
        for raw in data.split(b"\n")[:-1]:
            try:
                receipt = json.loads(raw)
            except ValueError:
                receipt = None
            ok = isinstance(receipt, dict) and _receipt_committed(receipt, lines)
            if not ok:
                changed = True
                orphans += 1
                continue
            assert isinstance(receipt, dict)
            entries.append(
                ((receipt.get("token_id"), receipt["op_id"]), receipt, offset, len(raw) + 1)
            )
            kept.append(raw + b"\n")
            offset += len(raw) + 1
        if changed or not data:  # an empty file: a crash right after creating it
            if kept:
                atomic_write(path, b"".join(kept))
            else:
                unlink_path(path)
        for key, receipt, at, length in entries:
            index[key] = IndexEntry(
                fp=receipt.get("fp"),
                epoch=receipt["epoch"],
                seq=receipt["seq"],
                receipt=name,
                offset=at,
                length=length,
            )
    op_seqs: dict[tuple[str | None, str], int] = {}
    for line in lines.lines(journal.epoch) or []:
        op_id = line.get("op_id")
        if isinstance(op_id, str):
            op_seqs[(line.get("token_id"), op_id)] = line["seq"]
    return index, op_seqs, orphans


def _receipt_committed(receipt: dict, lines: JournalLines) -> bool:
    line = lines.at(receipt.get("epoch"), receipt.get("seq"))
    return (
        line is not None
        and isinstance(receipt.get("op_id"), str)
        and line.get("token_id") == receipt.get("token_id")
        and line.get("op_id") == receipt.get("op_id")
    )


# ---------------------------------------------------------------------------
# Step 5: the durable tree's fingerprint and clean_shutdown
# ---------------------------------------------------------------------------


def durable_files(board: Path) -> list[tuple[str, os.stat_result]]:
    """Every durable board file (SPEC §6.1, workspace included), sorted by path."""
    board = Path(board)
    out = []
    for dirpath, dirnames, filenames in os.walk(board):
        rel_dir = Path(dirpath).relative_to(board)
        if rel_dir.parts and classify_path(rel_dir) not in (
            PathClass.DURABLE,
            PathClass.WORKSPACE,
        ):
            dirnames[:] = []
            continue
        dirnames.sort()
        for name in filenames:
            rel = (rel_dir / name).as_posix()
            if classify_path(rel) not in (PathClass.DURABLE, PathClass.WORKSPACE):
                continue
            try:
                st = os.lstat(Path(dirpath) / name)
            except OSError:
                continue
            out.append((rel, st))
    return sorted(out, key=lambda item: item[0])


def tree_fingerprint(board: Path) -> str:
    """A hash over each durable file's path, size, and mtime (SPEC §8.7 step 5)."""
    digest = hashlib.sha256()
    for rel, st in durable_files(board):
        digest.update(f"{rel}\0{st.st_size}\0{st.st_mtime_ns}\n".encode())
    return digest.hexdigest()[:32]


def write_meta(journal: Journal, clean_shutdown: dict | None) -> None:
    """Rewrite ``journal_meta.json`` with *clean_shutdown* (the rest unchanged)."""
    meta = {
        "epoch": journal.epoch,
        "created_at": journal.created_at,
        "baseline": journal.baseline,
        "clean_shutdown": clean_shutdown,
    }
    atomic_write(
        journal.board / HOSTED_DIR / JOURNAL_META,
        json.dumps(meta, sort_keys=True, indent=2) + "\n",
    )
    journal.clean_shutdown = clean_shutdown


# ---------------------------------------------------------------------------
# Step 6: foreign appends
# ---------------------------------------------------------------------------


def last_known_lengths(journal: Journal) -> dict[str, int]:
    """Each log's last known length: ``baseline``, updated by every line's
    ``lengths``. A log a line changed some other way (created whole, replaced,
    unlinked, relocated: in ``paths`` but not ``lengths``) has no known length
    until a later append records one."""
    known = dict(journal.baseline)
    for line in journal.read_entries():
        lengths = line.get("lengths") or {}
        for path in line.get("paths") or ():
            if path not in lengths:
                known.pop(path, None)
        known.update(lengths)
    return known


def foreign_appends(board: Path, journal: Journal) -> dict[str, int]:
    """Logs longer than their last known length, with their current length."""
    known = last_known_lengths(journal)
    current = log_lengths(board)
    return {
        path: size
        for path, size in sorted(current.items())
        if path in known and size > known[path]
    }
