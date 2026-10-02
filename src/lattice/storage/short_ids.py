"""Short ID index management: load, save, allocate, resolve, register."""

from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import tempfile
import threading
from collections import OrderedDict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from lattice.core.ids import SHORT_ID_RE
from lattice.storage.fs import atomic_write
from lattice.storage.locks import lattice_lock


def _default_index() -> dict:
    """Return a fresh empty v2 index structure."""
    return {"schema_version": 2, "next_seqs": {}, "map": {}}


def _migrate_v1_to_v2(index: dict, project_code: str | None = None) -> dict:
    """Migrate a v1 index (single next_seq) to v2 (per-prefix next_seqs).

    If *project_code* is provided, the old ``next_seq`` is assigned to that
    prefix.  Otherwise the prefix is inferred from the first entry in ``map``.
    """
    if index.get("schema_version", 1) >= 2:
        return index  # already v2

    old_seq = index.get("next_seq", 1)
    id_map = index.get("map", {})

    # Infer prefix from map entries if no project_code given
    if not project_code and id_map:
        first_key = next(iter(id_map))
        # rsplit on last '-' to get prefix
        project_code = first_key.rsplit("-", 1)[0]

    next_seqs: dict[str, int] = {}

    if project_code:
        next_seqs[project_code] = old_seq

    # Also scan the map to discover any prefixes and ensure next_seqs covers them
    prefix_maxes: dict[str, int] = {}
    for short_id in id_map:
        prefix, num_str = short_id.rsplit("-", 1)
        try:
            num = int(num_str)
        except ValueError:
            continue
        if prefix not in prefix_maxes or num > prefix_maxes[prefix]:
            prefix_maxes[prefix] = num

    for prefix, max_num in prefix_maxes.items():
        needed = max_num + 1
        if prefix not in next_seqs or next_seqs[prefix] < needed:
            next_seqs[prefix] = needed

    return {
        "schema_version": 2,
        "next_seqs": next_seqs,
        "map": id_map,
    }


# Event types that issue ``data.short_id`` to the log's own task.
SHORT_ID_EVENT_TYPES = frozenset({"task_created", "task_short_id_assigned"})
_SHORT_ID_KEY = b'"short_id"'


@dataclass(frozen=True)
class ShortIdOccurrence:
    """One valid short-ID occurrence and the task that recorded it, if known."""

    short_id: str
    task_id: str | None
    path: Path
    line: int
    event_id: str | None
    event_type: str | None
    source: str


@dataclass(frozen=True)
class ShortIdInventory:
    """All assigned IDs, with event-history separated from map reservations."""

    max_observed: Mapping[str, int]
    max_in_events: Mapping[str, int]
    event_short_ids: frozenset[str]
    occurrences: tuple[ShortIdOccurrence, ...]


@dataclass(frozen=True)
class _EventFileInventory:
    """Contribution from one event log, cached against its file metadata."""

    signature: tuple[int, int]
    identity: tuple[int, int]
    complete_offset: int
    line_count: int
    max_observed: Mapping[str, int]
    event_short_ids: frozenset[str]
    occurrences: tuple[ShortIdOccurrence, ...] | None


_EVENT_INVENTORY_CACHE: OrderedDict[Path, _EventFileInventory] = OrderedDict()
_EVENT_INVENTORY_CACHE_LOCK = threading.Lock()
_EVENT_INVENTORY_CACHE_LIMIT = 4096


def split_short_id(short_id: object) -> tuple[str, int] | None:
    """Return ``(prefix, seq)`` for a short ID matching the grammar, else ``None``."""
    if not isinstance(short_id, str) or not SHORT_ID_RE.match(short_id):
        return None
    prefix, suffix = short_id.rsplit("-", 1)
    if int(suffix) < 1:
        return None
    return prefix, int(suffix)


def short_id_events_in_log(
    raw: bytes,
    *,
    line_number_offset: int = 0,
) -> list[tuple[int, dict]]:
    """Return ``(line, event)`` for every event in a log carrying ``data.short_id``.

    Only lines containing the ``"short_id"`` key are parsed, so a long log
    costs one byte search. Unparseable lines are skipped: every writer that
    issues a short ID holds the allocation lock, so a torn line seen here is
    never an issued ID.
    """
    found: list[tuple[int, dict]] = []
    start = raw.find(_SHORT_ID_KEY)
    line_number = line_number_offset + 1
    counted_through = 0
    while start != -1:
        line_start = raw.rfind(b"\n", 0, start) + 1
        line_end = raw.find(b"\n", start)
        if line_end == -1:
            line_end = len(raw)
        if line_start > counted_through:
            line_number += raw.count(b"\n", counted_through, line_start)
            counted_through = line_start
        try:
            event = json.loads(raw[line_start:line_end])
        except (json.JSONDecodeError, UnicodeDecodeError):
            event = None
        if isinstance(event, dict):
            data = event.get("data")
            if isinstance(data, dict) and isinstance(data.get("short_id"), str):
                found.append((line_number, event))
        start = raw.find(_SHORT_ID_KEY, line_end)
    return found


def short_ids_in_log(raw: bytes) -> list[str]:
    """Return every well-formed ``data.short_id`` in a log, whatever the event type.

    Any short ID present anywhere in the event history is part of the floor
    (AC-2), so custom events count too; counting more can only raise it.
    """
    return [
        event["data"]["short_id"]
        for _line, event in short_id_events_in_log(raw)
        if split_short_id(event["data"]["short_id"]) is not None
    ]


def task_log_paths(lattice_dir: Path) -> Iterable[Path]:
    """Yield every per-task event log, active and archived."""
    for events_dir in (lattice_dir / "events", lattice_dir / "archive" / "events"):
        try:
            entries = list(os.scandir(events_dir))
        except FileNotFoundError:
            continue
        for entry in sorted(entries, key=lambda entry: entry.name):
            if entry.name.startswith("task_") and entry.name.endswith(".jsonl"):
                yield Path(entry.path)


def _short_id_event_paths(lattice_dir: Path) -> Iterable[Path]:
    """Yield task logs and the derived lifecycle log in stable source order."""
    yield from task_log_paths(lattice_dir)
    lifecycle = lattice_dir / "events" / "_lifecycle.jsonl"
    if lifecycle.is_file():
        yield lifecycle


def _persistent_cache_location(lattice_dir: Path) -> tuple[Path, str]:
    """Return a user-private, board-scoped cache path and its board identity."""
    board = str(lattice_dir.resolve())
    user_id = getattr(os, "getuid", lambda: "user")()
    directory = Path(tempfile.gettempdir()) / f"lattice-short-id-floor-{user_id}"
    digest = hashlib.sha256(os.fsencode(board)).hexdigest()
    return directory / f"{digest}.sqlite3", board


def _full_event_inventory(lattice_dir: Path) -> tuple[dict[str, int], frozenset[str]]:
    """Correctly scan the event sources when the disposable cache is unavailable."""
    max_observed: dict[str, int] = {}
    event_short_ids: set[str] = set()
    for path in _short_id_event_paths(lattice_dir):
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            continue
        for _line, event in short_id_events_in_log(raw):
            short_id = event["data"]["short_id"]
            parsed = split_short_id(short_id)
            if parsed is None:
                continue
            event_short_ids.add(short_id)
            prefix, seq = parsed
            max_observed[prefix] = max(max_observed.get(prefix, 0), seq)
    return max_observed, frozenset(event_short_ids)


def _open_persistent_inventory_db(cache_path: Path, board: str) -> sqlite3.Connection:
    """Open a private SQLite cache whose updates are transactionally atomic."""
    directory = cache_path.parent
    if directory.is_symlink():
        raise OSError("short-ID cache directory cannot be a symlink")
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    if cache_path.is_symlink():
        raise OSError("short-ID cache file cannot be a symlink")
    if not cache_path.exists():
        try:
            fd = os.open(cache_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
    elif not cache_path.is_file():
        raise OSError("short-ID cache path is not a file")
    os.chmod(cache_path, 0o600)

    connection = sqlite3.connect(cache_path, timeout=5)
    try:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS files (
                path TEXT PRIMARY KEY,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                device INTEGER NOT NULL,
                inode INTEGER NOT NULL,
                complete_offset INTEGER NOT NULL,
                line_count INTEGER NOT NULL
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS assignments (
                path TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
                short_id TEXT NOT NULL,
                prefix TEXT NOT NULL,
                seq INTEGER NOT NULL,
                PRIMARY KEY (path, short_id)
            )"""
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS assignments_by_short_id ON assignments(short_id)"
        )
        row = connection.execute("SELECT value FROM metadata WHERE key = 'board'").fetchone()
        if row is not None and row[0] != board:
            with connection:
                connection.execute("DELETE FROM assignments")
                connection.execute("DELETE FROM files")
                connection.execute("DELETE FROM metadata")
        with connection:
            connection.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES ('board', ?)", (board,)
            )
        return connection
    except BaseException:
        connection.close()
        raise


def _discard_persistent_inventory_db(cache_path: Path) -> None:
    """Remove a broken disposable cache so the next writer can rebuild it."""
    for suffix in ("", "-journal", "-wal", "-shm"):
        try:
            Path(f"{cache_path}{suffix}").unlink(missing_ok=True)
        except OSError:
            pass


def _persistent_event_inventory(lattice_dir: Path) -> tuple[dict[str, int], frozenset[str]]:
    """Incrementally index per-file contributions in a user-private SQLite cache."""
    cache_path, board = _persistent_cache_location(lattice_dir)
    try:
        connection = _open_persistent_inventory_db(cache_path, board)
    except sqlite3.DatabaseError:
        _discard_persistent_inventory_db(cache_path)
        try:
            connection = _open_persistent_inventory_db(cache_path, board)
        except (OSError, sqlite3.Error):
            return _full_event_inventory(lattice_dir)
    except (OSError, sqlite3.Error):
        return _full_event_inventory(lattice_dir)

    try:
        old_files = {
            row[0]: row[1:]
            for row in connection.execute(
                "SELECT path, size, mtime_ns, device, inode, complete_offset, line_count FROM files"
            )
        }
        current_paths: set[str] = set()
        with connection:
            for path in _short_id_event_paths(lattice_dir):
                try:
                    stat = path.stat()
                except FileNotFoundError:
                    continue
                key = str(path.resolve())
                current_paths.add(key)
                signature = (stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino)
                old = old_files.get(key)
                if old is not None and tuple(old[:4]) == signature:
                    continue

                can_read_tail = (
                    old is not None
                    and old[2:4] == signature[2:4]
                    and old[0] < signature[0]
                    and old[4] == old[0]
                )
                if can_read_tail:
                    base_offset = old[4]
                    line_offset = old[5]
                    try:
                        with path.open("rb") as handle:
                            handle.seek(base_offset)
                            raw = handle.read()
                    except FileNotFoundError:
                        current_paths.discard(key)
                        continue
                else:
                    base_offset = 0
                    line_offset = 0
                    try:
                        raw = path.read_bytes()
                    except FileNotFoundError:
                        current_paths.discard(key)
                        continue
                    connection.execute("DELETE FROM assignments WHERE path = ?", (key,))

                new_ids: set[tuple[str, str, int]] = set()
                for _line, event in short_id_events_in_log(raw, line_number_offset=line_offset):
                    short_id = event["data"]["short_id"]
                    parsed = split_short_id(short_id)
                    if parsed is None:
                        continue
                    prefix, seq = parsed
                    new_ids.add((short_id, prefix, seq))
                last_newline = raw.rfind(b"\n")
                complete_offset = (
                    base_offset + last_newline + 1 if last_newline >= 0 else base_offset
                )
                line_count = line_offset + raw.count(b"\n")
                connection.execute(
                    """INSERT INTO files(
                        path, size, mtime_ns, device, inode, complete_offset, line_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(path) DO UPDATE SET
                        size = excluded.size,
                        mtime_ns = excluded.mtime_ns,
                        device = excluded.device,
                        inode = excluded.inode,
                        complete_offset = excluded.complete_offset,
                        line_count = excluded.line_count""",
                    (key, *signature, complete_offset, line_count),
                )
                connection.executemany(
                    "INSERT OR IGNORE INTO assignments(path, short_id, prefix, seq) VALUES (?, ?, ?, ?)",
                    ((key, short_id, prefix, seq) for short_id, prefix, seq in new_ids),
                )

            stale_paths = set(old_files).difference(current_paths)
            if stale_paths:
                connection.executemany(
                    "DELETE FROM files WHERE path = ?", ((path,) for path in stale_paths)
                )

        event_short_ids = frozenset(
            row[0] for row in connection.execute("SELECT DISTINCT short_id FROM assignments")
        )
        max_observed = {
            row[0]: row[1]
            for row in connection.execute(
                "SELECT prefix, MAX(seq) FROM assignments GROUP BY prefix"
            )
        }
        return max_observed, event_short_ids
    except sqlite3.DatabaseError:
        connection.close()
        _discard_persistent_inventory_db(cache_path)
        return _full_event_inventory(lattice_dir)
    finally:
        try:
            connection.close()
        except sqlite3.Error:
            pass


def _parse_event_file(
    path: Path,
    raw: bytes,
    *,
    include_occurrences: bool,
    line_number_offset: int = 0,
    base_max_observed: Mapping[str, int] | None = None,
    base_event_short_ids: Iterable[str] = (),
) -> tuple[dict[str, int], frozenset[str], tuple[ShortIdOccurrence, ...] | None]:
    max_observed = dict(base_max_observed or {})
    event_short_ids = set(base_event_short_ids)
    occurrences: list[ShortIdOccurrence] = []
    for line, event in short_id_events_in_log(raw, line_number_offset=line_number_offset):
        short_id = event["data"]["short_id"]
        parsed = split_short_id(short_id)
        if parsed is None:
            continue
        event_short_ids.add(short_id)
        prefix, seq = parsed
        max_observed[prefix] = max(max_observed.get(prefix, 0), seq)
        if include_occurrences:
            task_id = event.get("task_id")
            if not isinstance(task_id, str) or not task_id:
                task_id = path.stem if path.stem != "_lifecycle" else None
            event_id = event.get("id")
            event_type = event.get("type")
            occurrences.append(
                ShortIdOccurrence(
                    short_id=short_id,
                    task_id=task_id,
                    path=path,
                    line=line,
                    event_id=event_id if isinstance(event_id, str) else None,
                    event_type=event_type if isinstance(event_type, str) else None,
                    source="event",
                )
            )
    return (
        max_observed,
        frozenset(event_short_ids),
        tuple(occurrences) if include_occurrences else None,
    )


def _event_file_inventory(
    path: Path,
    *,
    include_occurrences: bool,
    persistent_prior: _EventFileInventory | None = None,
) -> _EventFileInventory | None:
    """Read or reuse one log's contribution, including safe append-only tails."""
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    signature = (stat.st_size, stat.st_mtime_ns)
    identity = (stat.st_dev, stat.st_ino)
    key = path.resolve()
    with _EVENT_INVENTORY_CACHE_LOCK:
        cached = _EVENT_INVENTORY_CACHE.get(key)
        if cached is not None and cached.signature == signature and cached.identity == identity:
            if not include_occurrences or cached.occurrences is not None:
                _EVENT_INVENTORY_CACHE.move_to_end(key)
                return cached
    if (
        persistent_prior is not None
        and persistent_prior.signature == signature
        and persistent_prior.identity == identity
    ):
        if not include_occurrences:
            with _EVENT_INVENTORY_CACHE_LOCK:
                _EVENT_INVENTORY_CACHE[key] = persistent_prior
                _EVENT_INVENTORY_CACHE.move_to_end(key)
            return persistent_prior
    prior_candidates = [
        candidate
        for candidate in (cached, persistent_prior)
        if candidate is not None
        and candidate.identity == identity
        and candidate.signature[0] <= signature[0]
    ]
    prior = max(prior_candidates, key=lambda item: item.signature[0], default=None)
    can_read_tail = (
        not include_occurrences
        and prior is not None
        and prior.signature[0] < signature[0]
        and prior.complete_offset == prior.signature[0]
    )

    try:
        with path.open("rb") as handle:
            if can_read_tail:
                handle.seek(prior.complete_offset)
            raw = handle.read()
    except FileNotFoundError:
        return None
    if can_read_tail:
        base_max = prior.max_observed
        base_ids = prior.event_short_ids
        line_offset = prior.line_count
        offset = prior.complete_offset
    else:
        base_max = None
        base_ids = ()
        line_offset = 0
        offset = 0
    max_observed, event_short_ids, occurrences = _parse_event_file(
        path,
        raw,
        include_occurrences=include_occurrences,
        line_number_offset=line_offset,
        base_max_observed=base_max,
        base_event_short_ids=base_ids,
    )
    last_newline = raw.rfind(b"\n")
    contribution = _EventFileInventory(
        signature=signature,
        identity=identity,
        complete_offset=offset + last_newline + 1 if last_newline >= 0 else offset,
        line_count=line_offset + raw.count(b"\n"),
        max_observed=max_observed,
        event_short_ids=event_short_ids,
        occurrences=occurrences,
    )
    with _EVENT_INVENTORY_CACHE_LOCK:
        _EVENT_INVENTORY_CACHE[key] = contribution
        _EVENT_INVENTORY_CACHE.move_to_end(key)
        while len(_EVENT_INVENTORY_CACHE) > _EVENT_INVENTORY_CACHE_LIMIT:
            _EVENT_INVENTORY_CACHE.popitem(last=False)
    return contribution


def short_id_inventory(
    lattice_dir: Path,
    index: Mapping[str, object] | None = None,
    *,
    include_occurrences: bool = True,
    persist_cache: bool = False,
) -> ShortIdInventory:
    """Collect valid IDs from task logs, lifecycle projections, and ``ids.json``.

    A same-task ID reservation in the map is an allocation floor, but it is not
    event history. Keeping those two sets separate lets an interrupted create
    retry its own map-only reservation while every historical assignment stays
    burned.
    """
    max_observed: dict[str, int] = {}
    max_in_events: dict[str, int] = {}
    event_short_ids: set[str] = set()
    occurrences: list[ShortIdOccurrence] = []
    if persist_cache and not include_occurrences:
        max_in_events, cached_event_short_ids = _persistent_event_inventory(lattice_dir)
        event_short_ids.update(cached_event_short_ids)
        max_observed.update(max_in_events)
    else:
        for path in _short_id_event_paths(lattice_dir):
            contribution = _event_file_inventory(path, include_occurrences=include_occurrences)
            if contribution is None:
                continue
            for prefix, seq in contribution.max_observed.items():
                max_in_events[prefix] = max(max_in_events.get(prefix, 0), seq)
                max_observed[prefix] = max(max_observed.get(prefix, 0), seq)
            event_short_ids.update(contribution.event_short_ids)
            if include_occurrences and contribution.occurrences is not None:
                occurrences.extend(contribution.occurrences)

    if index is None:
        index = load_id_index(lattice_dir)
    id_map = index.get("map") if isinstance(index, Mapping) else None
    if isinstance(id_map, Mapping):
        index_path = lattice_dir / "ids.json"
        for short_id, task_id in id_map.items():
            parsed = split_short_id(short_id)
            if parsed is None:
                continue
            prefix, seq = parsed
            max_observed[prefix] = max(max_observed.get(prefix, 0), seq)
            if include_occurrences:
                occurrences.append(
                    ShortIdOccurrence(
                        short_id=short_id,
                        task_id=task_id if isinstance(task_id, str) else None,
                        path=index_path,
                        line=1,
                        event_id=None,
                        event_type=None,
                        source="ids.json",
                    )
                )

    return ShortIdInventory(
        max_observed=max_observed,
        max_in_events=max_in_events,
        event_short_ids=frozenset(event_short_ids),
        occurrences=tuple(occurrences),
    )


def observed_short_ids(lattice_dir: Path) -> set[str]:
    """Return every valid short ID recorded in logs or the ID map."""
    return {occurrence.short_id for occurrence in short_id_inventory(lattice_dir).occurrences}


def max_observed_short_ids(lattice_dir: Path) -> dict[str, int]:
    """Return the highest short-ID sequence assigned per prefix in any source.

    This is the allocation floor of SPEC §5: a sequence at or below it has
    appeared in task history, the lifecycle projection, or ``ids.json`` and
    must never be issued to a different task. A server computes it once at
    load and keeps it in memory, passing it to :func:`next_short_id` on each
    allocation.
    """
    return dict(short_id_inventory(lattice_dir, include_occurrences=False).max_observed)


def next_short_id(
    index: dict, prefix: str, task_ulid: str, max_observed: Mapping[str, int]
) -> str:
    """Issue the next short ID for *prefix* into *index* (pure, no I/O).

    ``next`` is one beyond the maximum of ``next_seqs``, observed event/map
    assignments, and valid keys in the map currently loaded under the lock.
    Registers the mapping and advances the counter.
    The caller supplies the floor and holds the allocation lock.
    """
    next_seqs = index.setdefault("next_seqs", {})
    mapping = index.setdefault("map", {})
    mapped_floor = max(
        (
            parsed[1]
            for short_id in mapping
            if (parsed := split_short_id(short_id)) is not None and parsed[0] == prefix
        ),
        default=0,
    )
    seq = max(
        next_seqs.get(prefix, 1),
        max_observed.get(prefix, 0) + 1,
        mapped_floor + 1,
    )
    while f"{prefix}-{seq}" in mapping:
        seq += 1
    short_id = f"{prefix}-{seq}"
    mapping[short_id] = task_ulid
    next_seqs[prefix] = seq + 1
    return short_id


def load_id_index(lattice_dir: Path) -> dict:
    """Load and parse ``.lattice/ids.json``, transparently migrating v1 to v2."""
    ids_path = lattice_dir / "ids.json"
    if not ids_path.exists():
        return _default_index()
    try:
        index = json.loads(ids_path.read_text())
    except (json.JSONDecodeError, OSError):
        return _default_index()

    # Lazy migration: convert v1 -> v2 in memory (persisted on next save)
    if index.get("schema_version", 1) < 2:
        index = _migrate_v1_to_v2(index)

    return index


def save_id_index(lattice_dir: Path, index: dict) -> None:
    """Atomic write of the ID index to ``.lattice/ids.json``."""
    ids_path = lattice_dir / "ids.json"
    content = json.dumps(index, sort_keys=True, indent=2) + "\n"
    atomic_write(ids_path, content)


def register_short_id(index: dict, short_id: str, task_ulid: str) -> dict:
    """Add a mapping to the index dict (pure, no I/O). Returns the index."""
    index["map"][short_id] = task_ulid
    return index


def allocate_short_id(
    lattice_dir: Path, prefix: str, task_ulid: str | None = None
) -> tuple[str, dict]:
    """Allocate the next short ID for *prefix* under lock, above the log floor.

    If *task_ulid* is provided, the mapping from short_id → task_ulid is
    registered atomically under the same lock, preventing race conditions
    between allocation and registration.

    Returns (short_id, updated_index). The index is saved to disk
    and the lock is released before returning.
    """
    locks_dir = lattice_dir / "locks"
    with lattice_lock(locks_dir, "ids_json"):
        index = load_id_index(lattice_dir)
        short_id = next_short_id(
            index,
            prefix,
            task_ulid or "",
            short_id_inventory(
                lattice_dir,
                index,
                include_occurrences=False,
                persist_cache=True,
            ).max_observed,
        )
        if task_ulid is None:
            del index["map"][short_id]
        save_id_index(lattice_dir, index)
    return short_id, index


def resolve_short_id(lattice_dir: Path, short_id: str) -> str | None:
    """Look up a short ID and return the corresponding ULID, or None."""
    index = load_id_index(lattice_dir)
    return index.get("map", {}).get(short_id.upper())
