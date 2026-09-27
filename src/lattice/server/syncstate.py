"""What the sync path reads: the manifest, deltas, resets, files, and events (SPEC §8.8).

Everything here runs under the project's work lock, except
:func:`fast_path_body`, which reads only the journal's immutable head tuple.

- **Manifest.** Every synced board file (durable and workspace, §6.1) with
  its SHA-256 and size, built at load and updated from each committed line's
  ``paths``. An append-only log keeps its ``hashlib`` state and the length it
  covers, so a log the operation only appended to is hashed from where the
  last hash stopped, not from byte 0. Resets and the ``manifest=1`` form read
  hashes from here and never rehash under the locks.
- **Sync bodies.** A delta covers every path in lines ``since+1..head``,
  coalesced to each path's current content, with append deltas for logs whose
  length history gives an append base at ``since``. A reset lists every
  manifest path, inlining until a cumulative :data:`RESET_INLINE_CAP`.
- **Events.** A journal entry's appended events, read back from the logs by
  ``event_ids``, so a live stream entry and a replayed one carry the same data.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import urllib.parse
from dataclasses import dataclass
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

from lattice.core.errors import OpError
from lattice.server.journal import Journal
from lattice.storage.ownership import PathClass, classify_path

#: A reset inlines file content until this many bytes in total (SPEC §8.8).
RESET_INLINE_CAP = 32 * 1024 * 1024

SYNCED_CLASSES = frozenset({PathClass.DURABLE, PathClass.WORKSPACE})
_CHUNK = 1024 * 1024


def is_synced_path(rel: str) -> bool:
    """A relative POSIX path the sync path may ever return: durable or workspace."""
    return bool(rel) and classify_path(rel) in SYNCED_CLASSES


def _regular_size(path: Path) -> int | None:
    """The size of a regular file (a symlink or anything else is ``None``)."""
    try:
        info = os.lstat(path)
    except OSError:
        return None
    return info.st_size if stat.S_ISREG(info.st_mode) else None


def board_file(board: Path, rel: str) -> Path | None:
    """*rel* under *board* when it is a regular file reached without any symlink,
    else ``None``: a board path never reads outside its board (SPEC §8.8)."""
    path = Path(board) / rel
    if _regular_size(path) is None:
        return None
    try:
        root = Path(board).resolve(strict=True)
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if resolved != root.joinpath(*rel.split("/")):
        return None  # a symlinked directory on the way
    return resolved


# ---------------------------------------------------------------------------
# The manifest
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ManifestEntry:
    sha256: str
    size: int


#: A staged manifest change: the new entry and hash state, or ``None`` (removed).
Staged = dict[str, "tuple[ManifestEntry, Any] | None"]


class Manifest:
    """Every synced board file's hash and size (SPEC §8.8 "Manifest"): an immutable
    value. :meth:`stage` computes a committed line's changes (reading files,
    changing nothing) and :meth:`applied` returns the next value, which the
    project publishes with the rest of its finalized memory in one assignment.
    """

    __slots__ = ("seq", "entries", "_hashers")

    def __init__(
        self,
        seq: int = 0,
        entries: Mapping[str, ManifestEntry] | None = None,
        hashers: Mapping[str, tuple[Any, int]] | None = None,
    ) -> None:
        #: The journal seq this manifest reflects.
        self.seq = seq
        self.entries: Mapping[str, ManifestEntry] = MappingProxyType(dict(entries or {}))
        #: ``*.jsonl`` path -> (hash state, bytes it covers), for incremental hashing.
        #: A stored hash state is never updated: extending one works on a copy.
        self._hashers: Mapping[str, tuple[Any, int]] = MappingProxyType(dict(hashers or {}))

    @classmethod
    def build(cls, board: Path, seq: int = 0) -> Manifest:
        """Hash every synced file; the result reflects journal *seq*."""
        return cls().applied(cls().stage(board, synced_files(board), frozenset()), seq)

    def get(self, rel: str) -> ManifestEntry | None:
        return self.entries.get(rel)

    def stage(self, board: Path, paths: list[str], appended: set[str] | frozenset[str]) -> Staged:
        """The manifest changes *paths* need now. A path in *appended* was only
        appended to, so its hash continues from the bytes already covered."""
        staged: Staged = {}
        for rel in paths:
            if not is_synced_path(rel):
                continue
            path = board_file(board, rel)
            if path is None:
                staged[rel] = None
                continue
            state = self._hashers.get(rel)
            if rel in appended and state is not None and state[1] <= path.stat().st_size:
                staged[rel] = _hash_from(path, state[0], state[1])
            else:
                staged[rel] = _hash_from(path, hashlib.sha256(), 0)
        return staged

    def applied(self, staged: Staged, seq: int) -> Manifest:
        """The manifest with *staged* applied, reflecting *seq*; this one is unchanged."""
        entries = dict(self.entries)
        hashers = dict(self._hashers)
        for rel, change in staged.items():
            if change is None:
                entries.pop(rel, None)
                hashers.pop(rel, None)
                continue
            entry, hasher = change
            entries[rel] = entry
            if rel.endswith(".jsonl"):
                hashers[rel] = (hasher, entry.size)
            else:
                hashers.pop(rel, None)
        return Manifest(seq, entries, hashers)

    def advanced(
        self, board: Path, paths: list[str], appended: set[str] | frozenset[str], seq: int
    ) -> Manifest:
        """:meth:`stage` then :meth:`applied`."""
        return self.applied(self.stage(board, paths, appended), seq)


def _hash_from(path: Path, hasher: Any, start: int) -> tuple[ManifestEntry, Any]:
    hasher = hasher.copy()
    covered = start
    with open(path, "rb") as fh:
        fh.seek(start)
        while chunk := fh.read(_CHUNK):
            hasher.update(chunk)
            covered += len(chunk)
    return ManifestEntry(hasher.hexdigest(), covered), hasher


def synced_files(board: Path) -> list[str]:
    """Every synced regular file under *board*, as sorted relative POSIX paths."""
    board = Path(board)
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(board):
        rel_dir = Path(dirpath).relative_to(board)
        keep = []
        for name in sorted(dirnames):
            rel = (rel_dir / name).as_posix()
            if is_synced_path(rel) and not os.path.islink(Path(dirpath) / name):
                keep.append(name)
        dirnames[:] = keep
        for name in sorted(filenames):
            rel = (rel_dir / name).as_posix()
            if is_synced_path(rel) and _regular_size(Path(dirpath) / name) is not None:
                found.append(rel)
    return sorted(found)


# ---------------------------------------------------------------------------
# Sync bodies
# ---------------------------------------------------------------------------


def needs_reset(journal: Journal, since: int, epoch: str | None, client_hash: str | None) -> bool:
    """SPEC §8.8: epoch absent or different, ``since`` past the head, or a history
    mismatch at ``since``."""
    if epoch != journal.epoch or since > journal.head_seq:
        return True
    return since > 0 and client_hash != journal.hash_at(since)


def _head_fields(journal: Journal) -> dict[str, Any]:
    body: dict[str, Any] = {"epoch": journal.epoch, "head_seq": journal.head_seq}
    if journal.head_hash is not None:
        body["head_hash"] = journal.head_hash
    return body


def fast_path_body(
    head: tuple[str, int, str | None], since: int, epoch: str | None, client_hash: str | None
) -> dict[str, Any] | None:
    """The answer to a sync at the head, from the journal's head tuple alone, or
    ``None`` when the request needs the locks."""
    head_epoch, head_seq, head_hash = head
    if epoch != head_epoch or since != head_seq:
        return None
    if since > 0 and client_hash != head_hash:
        return None
    body: dict[str, Any] = {"epoch": head_epoch, "head_seq": head_seq}
    if head_hash is not None:
        body["head_hash"] = head_hash
    body.update(reset=False, files={}, removed=[])
    return body


def href(slug: str, rel: str, sha256: str) -> str:
    """The files endpoint for *rel*, pinned to *sha256* (relative, same server)."""
    return (
        f"/v1/projects/{urllib.parse.quote(slug, safe='')}/files/"
        f"{urllib.parse.quote(rel, safe='/')}?sha256={sha256}"
    )


def manifest_body(journal: Journal, manifest: Manifest) -> dict[str, Any]:
    """``manifest=1``: every synced path's hash and size, from memory."""
    files = {
        rel: {"sha256": entry.sha256, "size": entry.size}
        for rel, entry in sorted(manifest.entries.items())
    }
    return {**_head_fields(journal), "reset": True, "files": files, "removed": []}


def reset_body(
    board: Path, journal: Journal, manifest: Manifest, slug: str, inline_file_bytes: int
) -> dict[str, Any]:
    """Every board file: inline while the cumulative content stays within the cap,
    ``href`` for the rest; hashes and sizes from the manifest."""
    files: dict[str, dict[str, Any]] = {}
    budget = RESET_INLINE_CAP
    for rel, entry in sorted(manifest.entries.items()):
        spec: dict[str, Any] = {"sha256": entry.sha256, "size": entry.size}
        path = board_file(board, rel)
        if path is None:
            raise OpError("INTEGRITY_ERROR", f"{rel} is in the manifest but not a board file")
        if entry.size <= inline_file_bytes and entry.size <= budget:
            data = _read(path, 0, entry.size)
            budget -= len(data)
            spec["content_b64"] = _b64(data)
        else:
            spec["href"] = href(slug, rel, entry.sha256)
        files[rel] = spec
    return {**_head_fields(journal), "reset": True, "files": files, "removed": []}


def delta_body(
    board: Path,
    journal: Journal,
    manifest: Manifest,
    slug: str,
    since: int,
    inline_file_bytes: int,
) -> dict[str, Any]:
    """Every path changed in lines ``since+1..head``, coalesced to its current content.
    ``sha256`` and ``size`` always describe the whole current file (the manifest);
    an append delta's ``content_b64`` carries only bytes ``append_from..size``."""
    touched: set[str] = set()
    for _seq, raw in journal.read_lines(since):
        touched.update(json.loads(raw).get("paths") or ())
    files: dict[str, dict[str, Any]] = {}
    removed: list[str] = []
    for rel in sorted(touched):
        if not is_synced_path(rel):
            continue
        entry = manifest.get(rel)
        path = board_file(board, rel)
        if entry is None or path is None:
            if not os.path.lexists(board / rel):
                removed.append(rel)
            continue  # a directory is never sent
        spec: dict[str, Any] = {"sha256": entry.sha256, "size": entry.size}
        base = journal.length_at(rel, since) if journal.is_log(rel) else None
        if base is not None and base <= entry.size and entry.size - base <= inline_file_bytes:
            spec["append_from"] = base
            spec["content_b64"] = _b64(_read(path, base, entry.size))
            spec["href"] = href(slug, rel, entry.sha256)
        elif entry.size <= inline_file_bytes:
            spec["content_b64"] = _b64(_read(path, 0, entry.size))
        else:
            spec["href"] = href(slug, rel, entry.sha256)
        files[rel] = spec
    return {**_head_fields(journal), "reset": False, "files": files, "removed": removed}


def _read(path: Path, start: int, end: int) -> bytes:
    with open(path, "rb") as fh:
        fh.seek(start)
        return fh.read(end - start)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


# ---------------------------------------------------------------------------
# The files endpoint
# ---------------------------------------------------------------------------


def check_file_path(rel: str) -> str:
    """A requested board path as a clean relative POSIX path, checked before any
    filesystem access: traversal is ``VALIDATION_ERROR``; a path that is not a
    synced board path is ``NOT_FOUND`` (it never reveals what else exists)."""
    bad = OpError("VALIDATION_ERROR", "invalid board path")
    if not rel or "\\" in rel or "\x00" in rel or rel.startswith("/"):
        raise bad
    parts = rel.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise bad
    if PurePosixPath(rel).is_absolute() or not is_synced_path(rel):
        raise OpError("NOT_FOUND", f"no board file {rel}")
    return rel


def read_board_file(board: Path, rel: str, sha256: str | None) -> bytes:
    """A synced board file's bytes, confined to the board.

    A request pinned to *sha256* gets ``STALE_VERSION`` whenever the file no
    longer has that hash, including when it no longer exists (removed, or
    relocated by an archive after the sync answer was built): the client then
    syncs again (SPEC §8.8). An unpinned request for a missing file is
    ``NOT_FOUND``.
    """
    stale = OpError(
        "STALE_VERSION",
        f"{rel} no longer has sha256 {sha256}; sync again from your head_seq",
        {"path": rel},
    )
    path = board_file(board, rel)
    if path is None:
        if sha256 is not None:
            raise stale
        raise OpError("NOT_FOUND", f"no board file {rel}")
    data = path.read_bytes()
    if sha256 is not None and hashlib.sha256(data).hexdigest() != sha256:
        raise stale
    return data


# ---------------------------------------------------------------------------
# Events for a journal entry (the stream's ``data.events``)
# ---------------------------------------------------------------------------

_RELOCATED = (("events/", "archive/events/"), ("archive/events/", "events/"))


def _counterpart(rel: str) -> str | None:
    for src, dst in _RELOCATED:
        if rel.startswith(src):
            return dst + rel[len(src) :]
    return None


def _read_range(path: Path, start: int, end: int) -> bytes | None:
    try:
        with open(path, "rb") as fh:
            fh.seek(start)
            return fh.read(max(0, end - start))
    except OSError:
        return None


def _collect(data: bytes, wanted: set[str], found: dict[str, dict]) -> None:
    for raw in data.split(b"\n"):
        if not raw.strip():
            continue
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if isinstance(event, dict):
            event_id = event.get("id")
            if event_id in wanted and event_id not in found:
                found[event_id] = event


def entry_events(board: Path, journal: Journal, line: dict) -> list[dict]:
    """The events line *line* appended, read back from the logs by ``event_ids``.

    Reads each appended log's bytes from its length at ``seq - 1`` to its length
    in ``lengths`` (a relocated log is read at the same offsets in its archive
    counterpart; relocation copies bytes). Ids still missing are looked for in
    the line's other ``.jsonl`` paths and the task's active and archived log.
    """
    ids = [e for e in line.get("event_ids") or () if isinstance(e, str)]
    if not ids:
        return []
    wanted = set(ids)
    found: dict[str, dict] = {}
    seq = int(line.get("seq") or 0)
    for rel, end in sorted((line.get("lengths") or {}).items()):
        start = journal.length_at(rel, seq - 1) or 0
        data = _read_range(board / rel, start, end)
        if data is None and (other := _counterpart(rel)) is not None:
            data = _read_range(board / other, start, end)
        if data is not None:
            _collect(data, wanted, found)
    if len(found) < len(wanted):
        candidates = [p for p in line.get("paths") or () if p.endswith(".jsonl")]
        task_id = line.get("task_id")
        if isinstance(task_id, str) and "/" not in task_id:
            candidates += [f"events/{task_id}.jsonl", f"archive/events/{task_id}.jsonl"]
        for rel in dict.fromkeys(candidates):
            if len(found) == len(wanted):
                break
            if not is_synced_path(rel):
                continue
            try:
                data = (board / rel).read_bytes()
            except OSError:
                continue
            _collect(data, wanted, found)
    return [found[i] for i in dict.fromkeys(ids) if i in found]
