"""Short ID index management: load, save, allocate, resolve, register."""

from __future__ import annotations

import json
import os
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
    event_short_ids: frozenset[str]
    occurrences: tuple[ShortIdOccurrence, ...]


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
) -> list[tuple[int, dict]]:
    """Return ``(line, event)`` for every event in a log carrying ``data.short_id``.

    Only lines containing the ``"short_id"`` key are parsed, so a long log
    costs one byte search. Unparseable lines are skipped: every writer that
    issues a short ID holds the allocation lock, so a torn line seen here is
    never an issued ID.
    """
    found: list[tuple[int, dict]] = []
    start = raw.find(_SHORT_ID_KEY)
    line_number = 1
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


def short_id_inventory(
    lattice_dir: Path,
    index: Mapping[str, object] | None = None,
    *,
    include_occurrences: bool = True,
) -> ShortIdInventory:
    """Scan task logs, lifecycle projections, and ``ids.json`` for valid IDs.

    A same-task ID reservation in the map is an allocation floor, but it is not
    event history. Keeping those two sets separate lets an interrupted create
    retry its own map-only reservation while every historical assignment stays
    burned. Event logs are read in full on every call; the server holds its
    loaded floor in project memory and advances it from committed writes.
    """
    max_observed: dict[str, int] = {}
    event_short_ids: set[str] = set()
    occurrences: list[ShortIdOccurrence] = []
    for path in _short_id_event_paths(lattice_dir):
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            continue
        for line, event in short_id_events_in_log(raw):
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
        event_short_ids=frozenset(event_short_ids),
        occurrences=tuple(occurrences),
    )


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
            short_id_inventory(lattice_dir, index, include_occurrences=False).max_observed,
        )
        if task_ulid is None:
            del index["map"][short_id]
        save_id_index(lattice_dir, index)
    return short_id, index


def resolve_short_id(lattice_dir: Path, short_id: str) -> str | None:
    """Look up a short ID and return the corresponding ULID, or None."""
    index = load_id_index(lattice_dir)
    return index.get("map", {}).get(short_id.upper())
