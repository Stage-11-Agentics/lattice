"""Shared, event-authoritative write-path operations."""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
import sys
from collections.abc import Collection, Generator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

from lattice.core.errors import StateConflict
from lattice.core.visibility import require_not_tombstoned
from lattice.core.events import LIFECYCLE_EVENT_TYPES, serialize_event
from lattice.core.origin import stamp_origin
from lattice.core.comments import materialize_comments, validate_comment_for_delete
from lattice.core.comments import validate_comment_for_edit, validate_comment_for_react
from lattice.core.tasks import (
    HISTORY_RECONCILED,
    FromMismatchError,
    apply_event_to_snapshot,
    is_stale_from,
    reconciled_event_ids,
    serialize_snapshot,
)
from lattice.storage.fs import atomic_write, ensure_dir, jsonl_append, unlink_path
from lattice.storage.hooks import execute_hooks
from lattice.storage.locks import lattice_lock, multi_lock, task_locks
from lattice.storage.short_ids import (
    load_id_index,
    next_short_id,
    save_id_index,
    short_id_inventory,
)

TaskLocation = Literal["active", "archived"]
TaskSource = Literal["active", "archived", "either", "absent"]


class AuthoritativeLogError(ValueError):
    """The immutable per-task history cannot be replayed safely."""

    def __init__(self, message: str, *, path: Path | None = None, line: int | None = None):
        details = message
        if path is not None:
            details = f"{path}: {details}"
        if line is not None:
            details = f"{details} (line {line})"
        super().__init__(details)
        self.path = path
        self.line = line


class TaskPlacementError(AuthoritativeLogError):
    """The task is absent, or not at the placement the mutation requires."""


@dataclass(frozen=True)
class TaskMutationContext:
    """Authoritative state supplied to a task mutation callback."""

    snapshot: dict | None
    events: tuple[dict, ...]
    location: TaskLocation | None
    reserved_short_id: str | None = None


@dataclass
class TaskMutationDecision:
    """Events and caller data returned by a mutation callback."""

    events: list[dict] = field(default_factory=list)
    value: Any = None
    idempotent: bool = False


@dataclass
class TaskMutationResult:
    """Durable result of :func:`mutate_task`."""

    snapshot: dict
    location: TaskLocation
    appended_events: list[dict]
    callback_value: Any = None
    idempotent: bool = False
    snapshot_reconciled: bool = False
    placement_reconciled: bool = False
    lifecycle_reconciled: bool = False

    @property
    def changed(self) -> bool:
        return bool(
            self.appended_events
            or self.snapshot_reconciled
            or self.placement_reconciled
            or self.lifecycle_reconciled
        )


@dataclass(frozen=True)
class ResolvedTaskAuthority:
    """A validated task history and its event-selected placement."""

    task_id: str
    events: tuple[dict, ...]
    snapshot: dict
    location: TaskLocation
    event_bytes: bytes
    active_event_path: Path
    archived_event_path: Path
    #: The log whose bytes were replayed (with two equal copies, the one at
    #: ``location``). History repair appends here and nowhere else.
    event_path: Path | None = None
    #: Named events replay applied as recorded, their ``from`` stale (SPEC §11).
    reconciled: tuple[str, ...] = ()
    #: Lenient replay only: unnamed events whose ``from`` is stale.
    stale: tuple[str, ...] = ()


MutationCallback = Callable[[TaskMutationContext], TaskMutationDecision]


def _mutation_boundary(_name: str, _lattice_dir: Path, _task_id: str) -> None:
    """Deterministic test seam for crash recovery at durable mutation boundaries."""


def _validate_semantic_event(events: list[dict], event: dict, location: TaskLocation) -> None:
    """Validate stateful one-shot contracts omitted from the snapshot view.

    This lives in strict replay rather than the permissive public reducer so
    forward-compatible standalone materialization remains unchanged.
    """
    event_type = event.get("type")
    data = event.get("data", {})
    if event_type == "task_archived":
        if location != "active":
            raise ValueError("task_archived must alternate from active authority")
        return
    if event_type == "task_unarchived":
        if location != "archived":
            raise ValueError("task_unarchived must alternate from archived authority")
        return
    if event_type in {"task_tombstoned", "task_untombstoned"}:
        erased = False
        for prior in events:
            if prior.get("type") in {"task_tombstoned", "task_untombstoned"}:
                erased = prior["type"] == "task_tombstoned"
        if event_type == "task_tombstoned" and (erased or location != "active"):
            raise ValueError("task_tombstoned must alternate from an active, unerased task")
        if event_type == "task_untombstoned" and not erased:
            raise ValueError("task_untombstoned must follow task_tombstoned")
        return
    if event_type == "comment_edited":
        previous_body, _previous_role = validate_comment_for_edit(events, data.get("comment_id"))
        if "previous_body" in data and data["previous_body"] != previous_body:
            raise ValueError("comment_edited previous_body does not match authoritative state")
        return
    if event_type == "comment_deleted":
        validate_comment_for_delete(events, data.get("comment_id"))
        return
    if event_type in {"reaction_added", "reaction_removed"}:
        comment_id = data.get("comment_id")
        validate_comment_for_react(events, comment_id)
        emoji = data.get("emoji")
        actor = event.get("actor")
        present = any(
            candidate["id"] == comment_id
            and actor in candidate.get("reactions", {}).get(emoji, [])
            for comment in materialize_comments(events)
            for candidate in [comment, *comment.get("replies", [])]
        )
        if event_type == "reaction_added" and present:
            raise ValueError("reaction_added duplicates an existing actor reaction")
        if event_type == "reaction_removed" and not present:
            raise ValueError("reaction_removed has no matching actor reaction")


def _location_paths(lattice_dir: Path, task_id: str, location: TaskLocation) -> dict[str, Path]:
    prefix = lattice_dir if location == "active" else lattice_dir / "archive"
    return {
        "event": prefix / "events" / f"{task_id}.jsonl",
        "snapshot": prefix / "tasks" / f"{task_id}.json",
        "plan": prefix / "plans" / f"{task_id}.md",
        "notes": prefix / "notes" / f"{task_id}.md",
    }


_RECONCILED_MARKERS = (b"task_history_reconciled", b"\\u")


def reconciliations_in_log(raw: bytes) -> list[tuple[int, dict]]:
    """``(line, event)`` for every decoded ``task_history_reconciled`` in *raw*.

    Only lines that contain the type's literal name or a ``\\u`` escape are
    parsed: a letter or ``_`` in a JSON string is written either literally or
    as ``\\uXXXX``, so no other line can decode to that type. Undecodable
    lines are skipped here; strict replay reports them.
    """
    if not any(marker in raw for marker in _RECONCILED_MARKERS):
        return []
    found: list[tuple[int, dict]] = []
    for line_number, raw_line in enumerate(raw.splitlines(), 1):
        if not any(marker in raw_line for marker in _RECONCILED_MARKERS):
            continue
        try:
            event = json.loads(raw_line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(event, dict) and event.get("type") == HISTORY_RECONCILED:
            found.append((line_number, event))
    return found


def _named_event_ids(raw: bytes, path: Path) -> set[str]:
    """Every event ID the log's reconciliations name; a malformed one raises."""
    named: set[str] = set()
    for line_number, event in reconciliations_in_log(raw):
        try:
            named.update(reconciled_event_ids(event))
        except ValueError as exc:
            raise AuthoritativeLogError(
                f"event {event.get('id')!r} cannot be materialized: {exc}",
                path=path,
                line=line_number,
            ) from exc
    return named


@dataclass(frozen=True)
class _ParsedLog:
    events: tuple[dict, ...]
    snapshot: dict
    raw: bytes
    #: Named events that replay applied as recorded (their ``from`` was stale).
    reconciled: tuple[str, ...]
    #: Lenient replay only: unnamed events with a stale ``from``, in log order.
    stale: tuple[str, ...]


def _parse_authoritative_log(
    path: Path,
    task_id: str,
    *,
    raw: bytes | None = None,
    sibling_named: frozenset[str] = frozenset(),
    lenient: bool = False,
) -> _ParsedLog:
    """Strictly replay one task log (SPEC §11's reconciliation rule included).

    Replay first collects the event IDs the log's ``task_history_reconciled``
    events name, plus *sibling_named* (those a compatible active/archive copy
    names). A named event must have a stale ``from`` and is applied as
    recorded; any other ``from`` mismatch fails. A name of the log's own that
    matches no event in it fails after the full pass. *raw*: replay these
    bytes instead of reading *path*. *lenient* (doctor's repair planning
    only): an unnamed stale ``from`` is applied as recorded and collected in
    ``stale`` instead of failing; nothing else is relaxed.
    """
    if raw is None:
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise AuthoritativeLogError(str(exc), path=path) from exc
    if raw and not raw.endswith(b"\n"):
        raise AuthoritativeLogError(
            "truncated final JSONL record; run lattice doctor --fix first", path=path
        )
    own_named = _named_event_ids(raw, path)
    named = own_named | sibling_named

    events: list[dict] = []
    seen_ids: set[str] = set()
    snapshot: dict | None = None
    location: TaskLocation = "active"
    reconciled: list[str] = []
    stale: list[str] = []
    for line_number, raw_line in enumerate(raw.splitlines(), 1):
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise AuthoritativeLogError(
                f"invalid JSONL record: {exc}", path=path, line=line_number
            ) from exc
        if not isinstance(event, dict):
            raise AuthoritativeLogError(
                "event record must be an object", path=path, line=line_number
            )
        if event.get("task_id") != task_id:
            raise AuthoritativeLogError(
                f"event task_id {event.get('task_id')!r} does not match {task_id}",
                path=path,
                line=line_number,
            )
        event_id = event.get("id")
        if not isinstance(event_id, str) or event_id in seen_ids:
            raise AuthoritativeLogError(
                f"missing or duplicate event id {event_id!r}", path=path, line=line_number
            )
        seen_ids.add(event_id)
        if not events and event.get("type") != "task_created":
            raise AuthoritativeLogError(
                "first event must be task_created", path=path, line=line_number
            )
        if events and event.get("type") == "task_created":
            raise AuthoritativeLogError(
                "task_created may appear exactly once", path=path, line=line_number
            )
        try:
            _validate_semantic_event(events, event, location)
            accept = False
            if event_id in named:
                if not is_stale_from(snapshot, event):
                    raise ValueError(
                        f"{HISTORY_RECONCILED} names it, but its from value is not stale"
                    )
                accept = True
                reconciled.append(event_id)
            elif lenient and is_stale_from(snapshot, event):
                accept = True
                stale.append(event_id)
            snapshot = apply_event_to_snapshot(snapshot, event, accept_stale_from=accept)
        except (KeyError, TypeError, ValueError) as exc:
            raise AuthoritativeLogError(
                f"event {event_id!r} cannot be materialized: {exc}",
                path=path,
                line=line_number,
            ) from exc
        events.append(event)
        if event.get("type") == "task_archived":
            location = "archived"
        elif event.get("type") == "task_unarchived":
            location = "active"

    if not events or snapshot is None:
        raise AuthoritativeLogError("authoritative log is empty", path=path)
    absent = sorted(own_named - seen_ids)
    if absent:
        raise AuthoritativeLogError(
            f"{HISTORY_RECONCILED} names event(s) absent from the log: {', '.join(absent)}",
            path=path,
        )

    return _ParsedLog(tuple(events), snapshot, raw, tuple(reconciled), tuple(stale))


def _compatible(left: bytes, right: bytes) -> bool:
    return left.startswith(right) or right.startswith(left)


def resolve_task_authority(
    lattice_dir: Path,
    task_id: str,
    *,
    allow_missing: bool = False,
    lenient: bool = False,
    override: Mapping[Path, bytes] | None = None,
) -> ResolvedTaskAuthority | None:
    """Resolve and strictly replay active/archive event-log candidates.

    Callers that mutate or repair state must hold the stable task locks.
    *lenient* and *override* serve doctor's history repair only: replay
    relaxed to collect unnamed stale ``from`` values (``stale``), and log
    bytes to replay in place of a path's on-disk bytes.
    """
    active_path = _location_paths(lattice_dir, task_id, "active")["event"]
    archived_path = _location_paths(lattice_dir, task_id, "archived")["event"]
    raws: dict[Path, bytes] = {}
    for path in (active_path, archived_path):
        if override is not None and path in override:
            raws[path] = override[path]
        elif path.exists():
            try:
                raws[path] = path.read_bytes()
            except OSError as exc:
                raise AuthoritativeLogError(str(exc), path=path) from exc

    # A compatible copy (equal, or an exact prefix) replays with the names the
    # other copy's reconciliations carry, so an untouched copy that still holds
    # a stale event does not block the repaired one (SPEC §11).
    sibling: dict[Path, frozenset[str]] = {path: frozenset() for path in raws}
    if len(raws) == 2 and _compatible(raws[active_path], raws[archived_path]):
        for path, other in ((active_path, archived_path), (archived_path, active_path)):
            if raws[other].endswith(b"\n"):
                try:
                    sibling[path] = frozenset(_named_event_ids(raws[other], other))
                except AuthoritativeLogError:
                    pass  # reported when the other copy is replayed

    candidates: list[tuple[Path, _ParsedLog]] = []
    for path, raw in raws.items():
        parsed = _parse_authoritative_log(
            path, task_id, raw=raw, sibling_named=sibling[path], lenient=lenient
        )
        candidates.append((path, parsed))

    if not candidates:
        snapshot_exists = any(
            _location_paths(lattice_dir, task_id, location)["snapshot"].exists()
            for location in ("active", "archived")
        )
        if allow_missing and not snapshot_exists:
            return None
        raise AuthoritativeLogError(
            "no authoritative event log exists"
            + (" for existing snapshot" if snapshot_exists else ""),
            path=active_path,
        )

    chosen = candidates[0]
    if len(candidates) == 2:
        left, right = candidates
        if left[1].raw == right[1].raw:
            chosen = left
        elif right[1].raw.startswith(left[1].raw):
            chosen = right
        elif left[1].raw.startswith(right[1].raw):
            chosen = left
        else:
            raise AuthoritativeLogError(
                "active and archived event logs diverge; manual recovery required",
                path=active_path,
            )

    event_path, parsed = chosen
    location: TaskLocation = "active"
    for event in parsed.events:
        if event["type"] == "task_archived":
            location = "archived"
        elif event["type"] == "task_unarchived":
            location = "active"
    if len(candidates) == 2 and candidates[0][1].raw == candidates[1][1].raw:
        event_path = archived_path if location == "archived" else active_path
    return ResolvedTaskAuthority(
        task_id=task_id,
        events=parsed.events,
        snapshot=parsed.snapshot,
        location=location,
        event_bytes=parsed.raw,
        active_event_path=active_path,
        archived_event_path=archived_path,
        event_path=event_path,
        reconciled=parsed.reconciled,
        stale=parsed.stale,
    )


def read_task_authority(
    lattice_dir: Path,
    task_id: str,
    *,
    allow_missing: bool = False,
) -> ResolvedTaskAuthority | None:
    """Resolve one task's event-selected state under the task read locks.

    This is the canonical read path for callers that care whether a task is
    active or archived.  It never selects placement from snapshot presence and
    returns the replayed snapshot, so a stale cache cannot resurrect a task.

    Inside :func:`authority_cache` (a server's hosted dashboard read, under the
    project's work lock) a replay whose event logs are unchanged is reused.
    """
    cache = _AUTHORITY_CACHE.get()
    if cache is not None:
        return cache.read(lattice_dir, task_id, allow_missing)
    return _read_task_authority_locked(lattice_dir, task_id, allow_missing=allow_missing)


def _read_task_authority_locked(
    lattice_dir: Path,
    task_id: str,
    *,
    allow_missing: bool = False,
) -> ResolvedTaskAuthority | None:
    with task_locks(lattice_dir / "locks", [task_id]):
        return resolve_task_authority(lattice_dir, task_id, allow_missing=allow_missing)


class AuthorityCache:
    """Strict replays reused by a process that serializes every write of the
    board against its reads: the server's hosted dashboard, whose reads run
    under the project's work lock (SPEC §8.5, §10). Callers must treat cached
    authorities as read-only, and must call :meth:`begin` with the board's
    current scope before each read under the lock.

    Two layers. Within one *scope* (the server passes ``(epoch, head_seq)``;
    a new load starts a new cache) nothing changes the board, so a replay made
    in that scope is reused as is. In a later scope an entry is reused only
    if the task's two event logs are byte-for-byte the ones it was replayed
    from: a replay is a pure function of those bytes, so this is as strict as
    replaying again, whatever the files' size, mtime, or inode say. The cache
    holds at most *max_bytes* of log bytes, least recently used first out.
    """

    def __init__(self, max_bytes: int = 64 * 1024 * 1024) -> None:
        from collections import OrderedDict

        self.max_bytes = max_bytes
        self.bytes = 0
        self.scope: object = None
        self._fresh: set[tuple[str, bool]] = set()
        #: Discovery results of the current scope, by ``(lattice_dir, include_archived)``.
        self._discovered: dict[tuple[str, bool], list[ResolvedTaskAuthority]] = {}
        self._entries: OrderedDict[
            tuple[str, bool], tuple[tuple[bytes | None, bytes | None], ResolvedTaskAuthority]
        ] = OrderedDict()

    def begin(self, scope: object) -> None:
        """Enter *scope*; on a change, every entry must be revalidated before use."""
        if scope != self.scope:
            self.scope = scope
            self._fresh = set()
            self._discovered = {}

    @staticmethod
    def _sources(lattice_dir: Path, task_id: str) -> tuple[bytes | None, bytes | None]:
        """Both event logs' bytes (``None`` when absent); plain ``os`` calls, since
        this runs for every task of every scope."""
        base = os.fspath(lattice_dir)
        out: list[bytes | None] = []
        for directory in ("events", "archive/events"):
            try:
                fd = os.open(f"{base}/{directory}/{task_id}.jsonl", os.O_RDONLY)
            except FileNotFoundError:
                out.append(None)
                continue
            try:
                chunks = []
                while chunk := os.read(fd, 1 << 20):
                    chunks.append(chunk)
            finally:
                os.close(fd)
            out.append(b"".join(chunks))
        return out[0], out[1]

    def discovered(
        self, lattice_dir: Path, include_archived: bool, discover: Callable[[], list]
    ) -> list[ResolvedTaskAuthority]:
        """One discovery per scope and argument set (nothing changes within a scope)."""
        key = (os.fspath(lattice_dir), include_archived)
        found = self._discovered.get(key)
        if found is None:
            found = self._discovered[key] = discover()
        return list(found)

    def read(
        self, lattice_dir: Path, task_id: str, allow_missing: bool
    ) -> ResolvedTaskAuthority | None:
        slot = (task_id, allow_missing)
        hit = self._entries.get(slot)
        if hit is not None and slot in self._fresh:
            self._entries.move_to_end(slot)
            return hit[1]
        sources = self._sources(lattice_dir, task_id)
        if hit is not None and hit[0] == sources:
            self._entries.move_to_end(slot)
            self._fresh.add(slot)
            return hit[1]
        # The caller holds the board's single work lock, so the per-task read
        # locks guard nothing more here.
        authority = resolve_task_authority(lattice_dir, task_id, allow_missing=allow_missing)
        self._drop(slot)
        if authority is not None and sources == self._sources(lattice_dir, task_id):
            self._entries[slot] = (sources, authority)
            self._fresh.add(slot)
            self.bytes += sum(len(b) for b in sources if b is not None)
            while self.bytes > self.max_bytes and self._entries:
                self._drop(next(iter(self._entries)))
        return authority

    def _drop(self, slot: tuple[str, bool]) -> None:
        old = self._entries.pop(slot, None)
        self._fresh.discard(slot)
        if old is not None:
            self.bytes -= sum(len(b) for b in old[0] if b is not None)

    def __len__(self) -> int:
        return len(self._entries)


_AUTHORITY_CACHE: contextvars.ContextVar[AuthorityCache | None] = contextvars.ContextVar(
    "lattice_authority_cache", default=None
)


@contextlib.contextmanager
def authority_cache(cache: AuthorityCache) -> Generator[None, None, None]:
    """Reuse *cache* for :func:`read_task_authority` in this context (see
    :class:`AuthorityCache` for who may)."""
    token = _AUTHORITY_CACHE.set(cache)
    try:
        yield
    finally:
        _AUTHORITY_CACHE.reset(token)


def resolve_task_prose_path(
    lattice_dir: Path,
    task_id: str,
    name: Literal["plan", "notes"],
) -> tuple[Path | None, ResolvedTaskAuthority]:
    """Resolve plan/notes under task locks without trusting directory order.

    During an interrupted placement move the only durable prose copy may
    temporarily remain on the wrong side.  A byte-identical duplicate is safe;
    divergent copies fail closed.
    """
    with task_locks(lattice_dir / "locks", [task_id]):
        authority = resolve_task_authority(lattice_dir, task_id)
        assert authority is not None
        target = _location_paths(lattice_dir, task_id, authority.location)[name]
        other_location: TaskLocation = "archived" if authority.location == "active" else "active"
        other = _location_paths(lattice_dir, task_id, other_location)[name]
        if target.exists() and other.exists() and target.read_bytes() != other.read_bytes():
            raise AuthoritativeLogError(
                f"active and archived {name} files diverge; manual recovery required",
                path=other,
            )
        if target.exists():
            return target, authority
        if other.exists():
            return other, authority
        return None, authority


def discover_task_authorities(
    lattice_dir: Path,
    *,
    include_archived: bool = True,
) -> list[ResolvedTaskAuthority]:
    """Return validated task authorities discovered from event logs.

    IDs are collected from both placements first, then each task is resolved
    through :func:`read_task_authority`. Split copies therefore yield one
    logical task at the placement selected by immutable history.

    Inside :func:`authority_cache`, one scope discovers once per argument set.
    """
    cache = _AUTHORITY_CACHE.get()
    if cache is not None:
        # Discovery without archived tasks resolves the same IDs and keeps the
        # active placements, so it is the full discovery filtered.
        found = cache.discovered(
            lattice_dir,
            True,
            lambda: _discover_task_authorities(lattice_dir, include_archived=True),
        )
        return found if include_archived else [a for a in found if a.location == "active"]
    return _discover_task_authorities(lattice_dir, include_archived=include_archived)


def _discover_task_authorities(
    lattice_dir: Path,
    *,
    include_archived: bool = True,
) -> list[ResolvedTaskAuthority]:
    active_task_ids: set[str] = set()
    archived_task_ids: set[str] = set()
    event_dirs = [
        (lattice_dir / "events", active_task_ids),
        (lattice_dir / "archive" / "events", archived_task_ids),
    ]
    for event_dir, task_ids in event_dirs:
        if not event_dir.is_dir():
            continue
        task_ids.update(
            path.stem
            for path in event_dir.glob("task_*.jsonl")
            if path.name not in {"_lifecycle.jsonl", "_global.jsonl"}
        )
    authorities: list[ResolvedTaskAuthority] = []
    for task_id in sorted(active_task_ids | archived_task_ids):
        try:
            authority = read_task_authority(lattice_dir, task_id)
        except AuthoritativeLogError as exc:
            active_event_path = _location_paths(lattice_dir, task_id, "active")["event"]
            if task_id in active_task_ids or active_event_path.exists():
                raise
            print(
                f"Warning: skipping corrupt archived task authority {task_id}: {exc}",
                file=sys.stderr,
            )
            continue
        assert authority is not None
        if include_archived or authority.location == "active":
            authorities.append(authority)
    return authorities


def _read_lifecycle_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    events: list[dict] = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise AuthoritativeLogError(
                f"invalid lifecycle JSONL record: {exc}", path=path, line=line_number
            ) from exc
        events.append(event)
    return events


def _reconcile_lifecycle_event(
    lifecycle_path: Path, event: dict, lattice_dir: Path, task_id: str
) -> bool:
    for existing in _read_lifecycle_events(lifecycle_path):
        if existing.get("id") != event["id"]:
            continue
        if existing != event:
            raise AuthoritativeLogError(
                f"lifecycle event {event['id']} conflicts with per-task authority",
                path=lifecycle_path,
            )
        return False
    jsonl_append(
        lifecycle_path,
        serialize_event(event),
        after_write=lambda: _mutation_boundary("lifecycle_appended", lattice_dir, task_id),
        after_fsync=lambda: _mutation_boundary("lifecycle_fsynced", lattice_dir, task_id),
    )
    return True


def _copy_atomic(source: Path, destination: Path) -> bool:
    data = source.read_bytes()
    if destination.exists() and destination.read_bytes() == data:
        return False
    atomic_write(destination, data)
    return True


def _reconcile_auxiliary_file(source: Path, destination: Path) -> bool:
    if not source.exists():
        return False
    if destination.exists():
        if source.read_bytes() != destination.read_bytes():
            raise AuthoritativeLogError(
                "active and archived supplementary files diverge; manual recovery required",
                path=source,
            )
        return False
    return _copy_atomic(source, destination)


def _reconcile_placement(
    lattice_dir: Path,
    task_id: str,
    location: TaskLocation,
    event_bytes: bytes,
    snapshot: dict,
    *,
    inject_faults: bool = True,
) -> tuple[bool, bool]:
    """Copy-first placement reconciliation. Returns (placement, snapshot)."""
    target = _location_paths(lattice_dir, task_id, location)
    other_location: TaskLocation = "archived" if location == "active" else "active"
    other = _location_paths(lattice_dir, task_id, other_location)
    placement_changed = False

    # Validate non-authoritative prose before writing or deleting anything.
    # A rebuild may select placement from valid event authority, but it must
    # never guess between divergent human-authored plan/notes copies.
    for name in ("plan", "notes"):
        if (
            target[name].exists()
            and other[name].exists()
            and target[name].read_bytes() != other[name].read_bytes()
        ):
            raise AuthoritativeLogError(
                "active and archived supplementary files diverge; manual recovery required",
                path=other[name],
            )

    for path in target.values():
        ensure_dir(path.parent)

    if not target["event"].exists() or target["event"].read_bytes() != event_bytes:
        atomic_write(target["event"], event_bytes)
        if inject_faults:
            _mutation_boundary("destination_event_copied", lattice_dir, task_id)
        placement_changed = True

    expected_snapshot = serialize_snapshot(snapshot)
    snapshot_changed = True
    if target["snapshot"].exists():
        try:
            snapshot_changed = target["snapshot"].read_text(encoding="utf-8") != expected_snapshot
        except OSError:
            snapshot_changed = True
    if snapshot_changed:
        atomic_write(target["snapshot"], expected_snapshot)
        if inject_faults:
            _mutation_boundary("destination_snapshot_written", lattice_dir, task_id)

    for name in ("plan", "notes"):
        if _reconcile_auxiliary_file(other[name], target[name]):
            if inject_faults:
                _mutation_boundary(f"destination_{name}_copied", lattice_dir, task_id)
            placement_changed = True

    for name in ("snapshot", "plan", "notes", "event"):
        if other[name].exists():
            if name in {"plan", "notes"} and target[name].exists():
                if other[name].read_bytes() != target[name].read_bytes():
                    raise AuthoritativeLogError(
                        "active and archived supplementary files diverge; manual recovery required",
                        path=other[name],
                    )
            unlink_path(other[name])
            if inject_faults:
                _mutation_boundary(f"source_{name}_removed", lattice_dir, task_id)
            placement_changed = True
    return placement_changed, snapshot_changed


def _load_strict_id_index(lattice_dir: Path) -> dict:
    path = lattice_dir / "ids.json"
    if not path.exists():
        return {"schema_version": 2, "next_seqs": {}, "map": {}}
    try:
        index = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise AuthoritativeLogError(f"invalid ids.json: {exc}", path=path) from exc
    if not isinstance(index, dict):
        raise AuthoritativeLogError("ids.json must contain an object", path=path)
    index = load_id_index(lattice_dir)
    mapping = index.get("map")
    next_seqs = index.get("next_seqs")
    if not isinstance(mapping, dict) or not isinstance(next_seqs, dict):
        raise AuthoritativeLogError("ids.json has malformed map/next_seqs", path=path)
    for short_id, task_id in mapping.items():
        if (
            not isinstance(short_id, str)
            or not isinstance(task_id, str)
            or not task_id.startswith("task_")
        ):
            raise AuthoritativeLogError(
                f"ids.json has malformed mapping {short_id!r}: {task_id!r}",
                path=path,
            )
        marker, separator, suffix = short_id.rpartition("-")
        if not separator or not marker or not suffix.isdigit() or int(suffix) < 1:
            raise AuthoritativeLogError(
                f"ids.json has malformed short ID {short_id!r}",
                path=path,
            )
    for prefix, next_seq in next_seqs.items():
        if (
            not isinstance(prefix, str)
            or not prefix
            or not isinstance(next_seq, int)
            or isinstance(next_seq, bool)
            or next_seq < 1
        ):
            raise AuthoritativeLogError(
                f"ids.json has malformed counter {prefix!r}: {next_seq!r}",
                path=path,
            )
    return index


def parse_project_short_id(short_id: str, prefix: str) -> int:
    marker = f"{prefix}-"
    if not isinstance(short_id, str) or not short_id.startswith(marker):
        raise AuthoritativeLogError(
            f"authoritative short_id {short_id!r} does not use configured prefix {prefix!r}"
        )
    suffix = short_id[len(marker) :]
    if not suffix.isdigit() or int(suffix) < 1:
        raise AuthoritativeLogError(f"authoritative short_id {short_id!r} is malformed")
    return int(suffix)


def _reserve_or_reconcile_short_id(
    lattice_dir: Path,
    task_id: str,
    prefix: str,
    authority: ResolvedTaskAuthority | None,
    *,
    allow_backfill: bool = False,
    max_observed: Mapping[str, int] | None = None,
    event_short_ids: Collection[str] | None = None,
) -> tuple[str, bool]:
    """Return the task's short ID, issuing one above the log floor if needed.

    Runs under the ``ids_json`` lock. The caller may supply both the full
    allocation floor and exact IDs found in event history (a server keeps
    both in memory); ``None`` rescans the board.
    """
    index = _load_strict_id_index(lattice_dir)
    inventory = None
    if max_observed is None or event_short_ids is None:
        inventory = short_id_inventory(
            lattice_dir,
            index,
            include_occurrences=False,
            persist_cache=True,
        )
    if max_observed is None:
        max_observed = inventory.max_observed
    if event_short_ids is None:
        event_short_ids = inventory.event_short_ids
    mapping: dict[str, str] = index["map"]
    next_seqs: dict[str, int] = index["next_seqs"]
    changed = False

    if authority is not None:
        short_id = authority.snapshot.get("short_id")
        if short_id is None and not allow_backfill:
            raise AuthoritativeLogError(
                "task_created is missing the configured project short_id",
                path=(
                    authority.active_event_path
                    if authority.active_event_path.exists()
                    else authority.archived_event_path
                ),
                line=1,
            )
        if short_id is not None:
            suffix = parse_project_short_id(short_id, prefix)
            existing_target = mapping.get(short_id)
            if existing_target not in (None, task_id):
                raise AuthoritativeLogError(
                    f"ids.json maps authoritative {short_id} to {existing_target}, not {task_id}"
                )
            if existing_target is None:
                mapping[short_id] = task_id
                changed = True
            high_water = next_seqs.get(prefix, 1)
            if high_water <= suffix:
                next_seqs[prefix] = suffix + 1
                changed = True
            if changed:
                save_id_index(lattice_dir, index)
            return short_id, changed

    reservations = [
        sid
        for sid, target in mapping.items()
        if target == task_id and sid.startswith(f"{prefix}-")
    ]
    valid_reservations = []
    for sid in reservations:
        try:
            valid_reservations.append((parse_project_short_id(sid, prefix), sid))
        except AuthoritativeLogError:
            continue
    if len(valid_reservations) == 1:
        # A map-only reservation left by an interrupted create may resume
        # only for its original task. Any event-history occurrence burns it,
        # even when that history belongs to the same task.
        seq, sid = valid_reservations[0]
        if sid not in event_short_ids:
            return sid, False

    seq = next_seqs.get(prefix, 1)
    if not isinstance(seq, int) or seq < 1:
        raise AuthoritativeLogError(f"ids.json next_seqs[{prefix!r}] is malformed")
    short_id = next_short_id(index, prefix, task_id, max_observed)
    save_id_index(lattice_dir, index)
    return short_id, True


def mutate_task(
    lattice_dir: Path,
    task_id: str,
    callback: MutationCallback,
    config: dict | None = None,
    *,
    run_hooks: bool,
    expect_last_event_id: str | None = None,
    source: TaskSource = "active",
    destination: TaskLocation | None = None,
    may_emit_lifecycle: bool = False,
    may_emit_short_id: bool = False,
    project_prefix: str | None = None,
    allow_short_id_backfill: bool = False,
    short_id_floor: Mapping[str, int] | None = None,
    event_short_ids: Collection[str] | None = None,
    allow_tombstoned: bool = False,
) -> TaskMutationResult:
    """Replay, validate, mutate, and materialize one task under stable locks.

    ``run_hooks``: run the board's hooks from *config* for each appended event
    after the locks are released (a server passes ``False`` and still passes
    *config* for its rules). ``expect_last_event_id``: raise ``CONFLICT``
    before the callback runs unless the task's last event is this one.
    ``short_id_floor`` and ``event_short_ids``: respectively the complete
    allocation floor and exact event-history IDs when the caller keeps them
    in memory (a server); omitted values are read under the allocation lock.
    ``may_emit_short_id`` takes the allocation lock for a custom event carrying
    a direct ``data.short_id``, so it cannot race a create's floor scan.
    ``allow_tombstoned``: an erased task raises ``TASK_ERASED`` before the
    callback runs unless this is set (``unerase``, and ``rebuild``, which
    appends nothing); even then, any event but ``task_untombstoned`` raises it.
    """
    locks_dir = lattice_dir / "locks"
    extra_lock_keys = []
    if may_emit_lifecycle:
        extra_lock_keys.append("events__lifecycle")
    if may_emit_short_id:
        extra_lock_keys.append("ids_json")
    if project_prefix is not None:
        extra_lock_keys.append("ids_json")

    appended_events: list[dict] = []
    callback_value: Any = None
    idempotent = False
    snapshot_reconciled = False
    placement_reconciled = False
    lifecycle_reconciled = False
    final_snapshot: dict | None = None
    final_location: TaskLocation | None = None

    with task_locks(locks_dir, [task_id], extra_lock_keys):
        authority = resolve_task_authority(
            lattice_dir, task_id, allow_missing=(source == "absent")
        )
        if authority is not None:
            if source == "active" and authority.location != "active":
                raise TaskPlacementError(f"Task {task_id} is archived.")
            if source == "archived" and authority.location != "archived":
                raise TaskPlacementError(f"Task {task_id} is active.")
        elif source not in {"absent", "either"}:
            raise TaskPlacementError(f"Task {task_id} does not exist.")
        if authority is not None and not allow_tombstoned:
            require_not_tombstoned(authority.snapshot)

        if expect_last_event_id is not None:
            found = authority.snapshot.get("last_event_id") if authority is not None else None
            if found != expect_last_event_id:
                raise StateConflict(
                    f"Task {task_id} changed: expected last event {expect_last_event_id}, "
                    f"found {found}.",
                    authority.snapshot if authority is not None else None,
                )

        preexisting_snapshot_drift = False
        if authority is not None:
            snapshot_path = _location_paths(lattice_dir, task_id, authority.location)["snapshot"]
            try:
                preexisting_snapshot_drift = not snapshot_path.exists() or snapshot_path.read_text(
                    encoding="utf-8"
                ) != serialize_snapshot(authority.snapshot)
            except OSError:
                preexisting_snapshot_drift = True

        reserved_short_id = None
        if project_prefix is not None:
            reserved_short_id, _ = _reserve_or_reconcile_short_id(
                lattice_dir,
                task_id,
                project_prefix,
                authority,
                allow_backfill=allow_short_id_backfill,
                max_observed=short_id_floor,
                event_short_ids=event_short_ids,
            )

        context = TaskMutationContext(
            snapshot=authority.snapshot if authority is not None else None,
            events=authority.events if authority is not None else (),
            location=authority.location if authority is not None else None,
            reserved_short_id=reserved_short_id,
        )
        decision = callback(context)
        if not isinstance(decision, TaskMutationDecision):
            raise TypeError("task mutation callback must return TaskMutationDecision")
        if authority is not None and any(
            event.get("type") != "task_untombstoned" for event in decision.events
        ):
            # Even with allow_tombstoned, unerase is the only write an erased task takes.
            require_not_tombstoned(authority.snapshot)
        callback_value = decision.value
        idempotent = decision.idempotent

        working = authority.snapshot if authority is not None else None
        validation_events = list(context.events)
        seen_ids = {event["id"] for event in validation_events}
        current_location: TaskLocation = authority.location if authority is not None else "active"
        logical_location = current_location
        requested_location = destination or current_location
        for event in decision.events:
            stamp_origin(event)
            if event.get("task_id") != task_id:
                raise ValueError("mutation event task_id does not match target task")
            if event.get("id") in seen_ids:
                raise ValueError(f"duplicate event id {event.get('id')!r}")
            if event.get("type") in LIFECYCLE_EVENT_TYPES and not may_emit_lifecycle:
                raise ValueError("lifecycle event requires may_emit_lifecycle=True")
            if working is None and event.get("type") != "task_created":
                raise ValueError("first task event must be task_created")
            _validate_semantic_event(validation_events, event, logical_location)
            try:
                working = apply_event_to_snapshot(working, event)
            except FromMismatchError as exc:
                raise StateConflict(
                    str(exc),
                    authority.snapshot if authority is not None else None,
                ) from exc
            validation_events.append(event)
            seen_ids.add(event["id"])
            if event["type"] == "task_archived":
                logical_location = "archived"
                requested_location = "archived"
            elif event["type"] == "task_unarchived":
                logical_location = "active"
                requested_location = "active"

        if working is None:
            raise ValueError("mutation produced no task snapshot")

        event_path = (
            _location_paths(lattice_dir, task_id, current_location)["event"]
            if authority is not None
            else _location_paths(lattice_dir, task_id, "active")["event"]
        )
        if authority is not None and (
            not event_path.exists() or event_path.read_bytes() != authority.event_bytes
        ):
            ensure_dir(event_path.parent)
            atomic_write(event_path, authority.event_bytes)
            placement_reconciled = True
        if decision.events:
            # One write and one fsync for the whole decision; the bytes equal
            # appending the events one at a time.
            jsonl_append(
                event_path,
                "".join(serialize_event(event) for event in decision.events),
                after_write=lambda: _mutation_boundary(
                    "task_event_appended", lattice_dir, task_id
                ),
                after_fsync=lambda: _mutation_boundary("task_event_fsynced", lattice_dir, task_id),
            )
            appended_events.extend(decision.events)

        authoritative_bytes = event_path.read_bytes()
        lifecycle_path = lattice_dir / "events" / "_lifecycle.jsonl"
        lifecycle_events = [
            *(
                event
                for event in (authority.events if authority is not None else ())
                if event["type"] in LIFECYCLE_EVENT_TYPES
            ),
            *(event for event in decision.events if event["type"] in LIFECYCLE_EVENT_TYPES),
        ]
        if lifecycle_events and may_emit_lifecycle:
            for event in lifecycle_events:
                lifecycle_reconciled |= _reconcile_lifecycle_event(
                    lifecycle_path, event, lattice_dir, task_id
                )

        placement_changed, _snapshot_written = _reconcile_placement(
            lattice_dir,
            task_id,
            requested_location,
            authoritative_bytes,
            working,
        )
        placement_reconciled |= placement_changed
        snapshot_reconciled = preexisting_snapshot_drift
        final_snapshot = working
        final_location = requested_location

    assert final_snapshot is not None and final_location is not None
    _mutation_boundary("locks_released_and_durable", lattice_dir, task_id)
    if run_hooks and config:
        for event in appended_events:
            execute_hooks(config, lattice_dir, task_id, event)
    return TaskMutationResult(
        snapshot=final_snapshot,
        location=final_location,
        appended_events=appended_events,
        callback_value=callback_value,
        idempotent=idempotent,
        snapshot_reconciled=snapshot_reconciled,
        placement_reconciled=placement_reconciled,
        lifecycle_reconciled=lifecycle_reconciled,
    )


def mutate_task_events(
    lattice_dir: Path,
    task_id: str,
    events: list[dict],
    config: dict | None = None,
    *,
    run_hooks: bool,
    source: TaskSource = "active",
    destination: TaskLocation | None = None,
    may_emit_lifecycle: bool = False,
    may_emit_short_id: bool = False,
) -> TaskMutationResult:
    """Commit a state-independent event batch through :func:`mutate_task`."""
    return mutate_task(
        lattice_dir,
        task_id,
        lambda _context: TaskMutationDecision(events=events),
        config,
        run_hooks=run_hooks,
        source=source,
        destination=destination,
        may_emit_lifecycle=may_emit_lifecycle,
        may_emit_short_id=may_emit_short_id,
    )


def append_repair_events(event_path: Path, events: list[dict]) -> None:
    """Append doctor's history-repair events to one task log (SPEC §11).

    The only per-task append outside :func:`mutate_task`, which strictly
    replays the log first and so cannot take a task whose history needs the
    repair. The caller holds every task lock and has already replayed the
    resulting bytes strictly; one write and one fsync, no hooks.
    """
    for event in events:
        stamp_origin(event)
    jsonl_append(event_path, "".join(serialize_event(event) for event in events))


def scaffold_plan(
    lattice_dir: Path,
    task_id: str,
    title: str,
    short_id: str | None,
    description: str | None,
) -> None:
    """Create the initial plan markdown file for a new task.

    Non-authoritative — this is a convenience scaffold for humans and agents
    to use as a structured planning document. Skipped silently if the file
    already exists (idempotent create).

    The scaffold is intentionally minimal: just the title and description.
    No prescribed section headings — the planning agent writes whatever
    structure the task needs.
    """
    plan_path = lattice_dir / "plans" / f"{task_id}.md"
    if plan_path.exists():
        return

    from lattice.core.plans import scaffold_plan_text

    ensure_dir(plan_path.parent)
    atomic_write(plan_path, scaffold_plan_text(title, short_id, description))


def scaffold_notes(
    lattice_dir: Path,
    task_id: str,
    title: str,
    short_id: str | None,
    description: str | None,
) -> None:
    """Create the initial notes markdown file for a new task.

    Non-authoritative — this is a convenience scaffold for humans and agents
    to use as a working document. Skipped silently if the file already exists
    (idempotent create).

    Notes are NOT scaffolded on task creation (plans are). This function
    exists for explicit on-demand creation (e.g., dashboard "open notes")
    or direct file writes.
    """
    notes_path = lattice_dir / "notes" / f"{task_id}.md"
    if notes_path.exists():
        return

    heading = f"# {short_id}: {title}" if short_id else f"# {title}"
    lines = [heading, ""]

    lines.append("<!-- Scratchpad — working notes, debug logs, context dumps, open questions. -->")
    lines.append("")

    atomic_write(notes_path, "\n".join(lines))


@contextlib.contextmanager
def resource_write_context(
    lattice_dir: Path,
    resource_name: str,
    timeout: float = 10,
) -> Generator[None, None, None]:
    """Acquire resource-level lock for read-check-write operations.

    Use this to wrap the entire read → check → decide → write sequence
    and prevent TOCTOU races.  Call ``write_resource_event()`` with
    ``_caller_holds_lock=True`` inside this context to avoid deadlock.
    """
    locks_dir = lattice_dir / "locks"
    ensure_dir(locks_dir)
    with lattice_lock(locks_dir, f"resources_{resource_name}", timeout=timeout):
        yield


def write_resource_event(
    lattice_dir: Path,
    resource_id: str,
    resource_name: str,
    events: list[dict],
    snapshot: dict,
    config: dict | None = None,
    *,
    run_hooks: bool,
    _caller_holds_lock: bool = False,
) -> None:
    """Write resource event(s) and snapshot atomically with proper locking.

    This is the canonical write path for all resource mutations.

    Args:
        run_hooks: Run the board's resource hooks from *config* after the
            locks are released.
        _caller_holds_lock: If True, skip acquiring the resource lock (caller
            already holds it via ``resource_write_context``).  The event-file
            lock is still acquired independently.

    Steps:
    1. Ensure resource directory exists
    2. Acquire locks in sorted order (unless caller holds resource lock)
    3. Append events to per-resource JSONL (in events/ dir, keyed by resource_id)
    4. Atomic-write resource snapshot
    5. Release locks
    6. Fire hooks (after locks released, data is durable)
    """
    from lattice.core.resources import serialize_resource_snapshot

    locks_dir = lattice_dir / "locks"

    # Ensure resource directory exists
    resource_dir = lattice_dir / "resources" / resource_name
    ensure_dir(resource_dir)

    for event in events:
        stamp_origin(event)

    def _do_writes() -> None:
        # Event-first: append to per-resource event log, one write for all
        event_path = lattice_dir / "events" / f"{resource_id}.jsonl"
        if events:
            jsonl_append(event_path, "".join(serialize_event(event) for event in events))

        # Then materialize snapshot
        snapshot_path = resource_dir / "resource.json"
        atomic_write(snapshot_path, serialize_resource_snapshot(snapshot))

    if _caller_holds_lock:
        # Caller holds resource lock; only lock the event file
        with lattice_lock(locks_dir, f"events_{resource_id}"):
            _do_writes()
    else:
        # Full locking for standalone callers
        lock_keys = [f"events_{resource_id}", f"resources_{resource_name}"]
        lock_keys.sort()
        with multi_lock(locks_dir, lock_keys):
            _do_writes()

    # Fire hooks after locks are released (data is durable)
    if run_hooks and config:
        from lattice.storage.hooks import execute_resource_hooks

        for event in events:
            execute_resource_hooks(config, lattice_dir, resource_id, resource_name, event)


def write_issue_events(
    lattice_dir: Path, issue_id: str, events: list[dict], snapshot: dict
) -> None:
    """Append *events* to the issue's log, then write *snapshot* (LAT-361).

    The issue-log counterpart of :func:`write_resource_event`: event first,
    then the snapshot replayed from the whole log. The caller holds the
    issue's lock (``storage.issues.issue_write_context``). Issue logs live in
    ``issues/events/``, never beside task logs.
    """
    from lattice.core.issues import serialize_issue_snapshot

    issues = Path(lattice_dir) / "issues"
    ensure_dir(issues / "events")
    for event in events:
        stamp_origin(event)
    if events:
        jsonl_append(
            issues / "events" / f"{issue_id}.jsonl",
            "".join(serialize_event(event) for event in events),
        )
    atomic_write(issues / f"{issue_id}.json", serialize_issue_snapshot(snapshot))
