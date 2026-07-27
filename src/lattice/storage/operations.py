"""Shared, event-authoritative write-path operations."""

from __future__ import annotations

import contextlib
import json
from collections.abc import Generator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

from lattice.core.events import LIFECYCLE_EVENT_TYPES, serialize_event
from lattice.core.tasks import apply_event_to_snapshot, serialize_snapshot
from lattice.storage.fs import atomic_write, jsonl_append
from lattice.storage.hooks import execute_hooks
from lattice.storage.locks import lattice_lock, multi_lock
from lattice.storage.short_ids import load_id_index, save_id_index

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


MutationCallback = Callable[[TaskMutationContext], TaskMutationDecision]


def _location_paths(lattice_dir: Path, task_id: str, location: TaskLocation) -> dict[str, Path]:
    prefix = lattice_dir if location == "active" else lattice_dir / "archive"
    return {
        "event": prefix / "events" / f"{task_id}.jsonl",
        "snapshot": prefix / "tasks" / f"{task_id}.json",
        "plan": prefix / "plans" / f"{task_id}.md",
        "notes": prefix / "notes" / f"{task_id}.md",
    }


def _parse_authoritative_log(path: Path, task_id: str) -> tuple[tuple[dict, ...], dict, bytes]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise AuthoritativeLogError(str(exc), path=path) from exc
    if raw and not raw.endswith(b"\n"):
        raise AuthoritativeLogError(
            "truncated final JSONL record; run lattice doctor --fix first", path=path
        )

    events: list[dict] = []
    seen_ids: set[str] = set()
    snapshot: dict | None = None
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
            snapshot = apply_event_to_snapshot(snapshot, event)
        except (KeyError, TypeError, ValueError) as exc:
            raise AuthoritativeLogError(
                f"event {event_id!r} cannot be materialized: {exc}",
                path=path,
                line=line_number,
            ) from exc
        events.append(event)

    if not events or snapshot is None:
        raise AuthoritativeLogError("authoritative log is empty", path=path)

    return tuple(events), snapshot, raw


def resolve_task_authority(
    lattice_dir: Path,
    task_id: str,
    *,
    allow_missing: bool = False,
) -> ResolvedTaskAuthority | None:
    """Resolve and strictly replay active/archive event-log candidates.

    Callers that mutate or repair state must hold the stable task locks.
    """
    active_path = _location_paths(lattice_dir, task_id, "active")["event"]
    archived_path = _location_paths(lattice_dir, task_id, "archived")["event"]
    candidates: list[tuple[Path, tuple[dict, ...], dict, bytes]] = []
    for path in (active_path, archived_path):
        if path.exists():
            events, snapshot, raw = _parse_authoritative_log(path, task_id)
            candidates.append((path, events, snapshot, raw))

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
        if left[3] == right[3]:
            chosen = left
        elif right[3].startswith(left[3]):
            chosen = right
        elif left[3].startswith(right[3]):
            chosen = left
        else:
            raise AuthoritativeLogError(
                "active and archived event logs diverge; manual recovery required",
                path=active_path,
            )

    _, events, snapshot, raw = chosen
    location: TaskLocation = "active"
    for event in events:
        if event["type"] == "task_archived":
            location = "archived"
        elif event["type"] == "task_unarchived":
            location = "active"
    return ResolvedTaskAuthority(
        task_id=task_id,
        events=events,
        snapshot=snapshot,
        location=location,
        event_bytes=raw,
        active_event_path=active_path,
        archived_event_path=archived_path,
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
    """
    with multi_lock(
        lattice_dir / "locks",
        [f"events_{task_id}", f"tasks_{task_id}"],
    ):
        return resolve_task_authority(lattice_dir, task_id, allow_missing=allow_missing)


def discover_task_authorities(
    lattice_dir: Path,
    *,
    include_archived: bool = True,
) -> list[ResolvedTaskAuthority]:
    """Return validated task authorities discovered from event logs.

    IDs are collected from both placements first, then each task is resolved
    through :func:`read_task_authority`. Split copies therefore yield one
    logical task at the placement selected by immutable history.
    """
    task_ids: set[str] = set()
    event_dirs = [lattice_dir / "events"]
    if include_archived:
        event_dirs.append(lattice_dir / "archive" / "events")
    for event_dir in event_dirs:
        if not event_dir.is_dir():
            continue
        task_ids.update(
            path.stem
            for path in event_dir.glob("task_*.jsonl")
            if path.name not in {"_lifecycle.jsonl", "_global.jsonl"}
        )
    authorities: list[ResolvedTaskAuthority] = []
    for task_id in sorted(task_ids):
        authority = read_task_authority(lattice_dir, task_id)
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


def _reconcile_lifecycle_event(lifecycle_path: Path, event: dict) -> bool:
    for existing in _read_lifecycle_events(lifecycle_path):
        if existing.get("id") != event["id"]:
            continue
        if existing != event:
            raise AuthoritativeLogError(
                f"lifecycle event {event['id']} conflicts with per-task authority",
                path=lifecycle_path,
            )
        return False
    jsonl_append(lifecycle_path, serialize_event(event))
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
        path.parent.mkdir(parents=True, exist_ok=True)

    if not target["event"].exists() or target["event"].read_bytes() != event_bytes:
        atomic_write(target["event"], event_bytes)
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

    for name in ("plan", "notes"):
        if _reconcile_auxiliary_file(other[name], target[name]):
            placement_changed = True

    for name in ("snapshot", "plan", "notes", "event"):
        if other[name].exists():
            if name in {"plan", "notes"} and target[name].exists():
                if other[name].read_bytes() != target[name].read_bytes():
                    raise AuthoritativeLogError(
                        "active and archived supplementary files diverge; manual recovery required",
                        path=other[name],
                    )
            other[name].unlink()
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


def _parse_project_short_id(short_id: str, prefix: str) -> int:
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
) -> tuple[str, bool]:
    index = _load_strict_id_index(lattice_dir)
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
            suffix = _parse_project_short_id(short_id, prefix)
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
            valid_reservations.append((_parse_project_short_id(sid, prefix), sid))
        except AuthoritativeLogError:
            continue
    if len(valid_reservations) == 1:
        return valid_reservations[0][1], False

    seq = next_seqs.get(prefix, 1)
    if not isinstance(seq, int) or seq < 1:
        raise AuthoritativeLogError(f"ids.json next_seqs[{prefix!r}] is malformed")
    while f"{prefix}-{seq}" in mapping:
        seq += 1
    short_id = f"{prefix}-{seq}"
    mapping[short_id] = task_id
    next_seqs[prefix] = seq + 1
    save_id_index(lattice_dir, index)
    return short_id, True


def mutate_task(
    lattice_dir: Path,
    task_id: str,
    callback: MutationCallback,
    config: dict | None = None,
    *,
    source: TaskSource = "active",
    destination: TaskLocation | None = None,
    may_emit_lifecycle: bool = False,
    project_prefix: str | None = None,
    allow_short_id_backfill: bool = False,
) -> TaskMutationResult:
    """Replay, validate, mutate, and materialize one task under stable locks."""
    locks_dir = lattice_dir / "locks"
    lock_keys = [f"events_{task_id}", f"tasks_{task_id}"]
    if may_emit_lifecycle:
        lock_keys.append("events__lifecycle")
    if project_prefix is not None:
        lock_keys.append("ids_json")

    appended_events: list[dict] = []
    callback_value: Any = None
    idempotent = False
    snapshot_reconciled = False
    placement_reconciled = False
    lifecycle_reconciled = False
    final_snapshot: dict | None = None
    final_location: TaskLocation | None = None

    with multi_lock(locks_dir, lock_keys):
        authority = resolve_task_authority(
            lattice_dir, task_id, allow_missing=(source == "absent")
        )
        if authority is not None:
            if source == "active" and authority.location != "active":
                raise AuthoritativeLogError(f"Task {task_id} is archived.")
            if source == "archived" and authority.location != "archived":
                raise AuthoritativeLogError(f"Task {task_id} is active.")
        elif source not in {"absent", "either"}:
            raise AuthoritativeLogError(f"Task {task_id} does not exist.")

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
        callback_value = decision.value
        idempotent = decision.idempotent

        working = authority.snapshot if authority is not None else None
        seen_ids = {event["id"] for event in context.events}
        for event in decision.events:
            if event.get("task_id") != task_id:
                raise ValueError("mutation event task_id does not match target task")
            if event.get("id") in seen_ids:
                raise ValueError(f"duplicate event id {event.get('id')!r}")
            if event.get("type") in LIFECYCLE_EVENT_TYPES and not may_emit_lifecycle:
                raise ValueError("lifecycle event requires may_emit_lifecycle=True")
            if working is None and event.get("type") != "task_created":
                raise ValueError("first task event must be task_created")
            working = apply_event_to_snapshot(working, event)
            seen_ids.add(event["id"])

        if working is None:
            raise ValueError("mutation produced no task snapshot")

        current_location: TaskLocation = authority.location if authority is not None else "active"
        requested_location = destination or current_location
        for event in decision.events:
            if event["type"] == "task_archived":
                requested_location = "archived"
            elif event["type"] == "task_unarchived":
                requested_location = "active"

        event_path = (
            _location_paths(lattice_dir, task_id, current_location)["event"]
            if authority is not None
            else _location_paths(lattice_dir, task_id, "active")["event"]
        )
        if authority is not None and (
            not event_path.exists() or event_path.read_bytes() != authority.event_bytes
        ):
            event_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(event_path, authority.event_bytes)
            placement_reconciled = True
        for event in decision.events:
            jsonl_append(event_path, serialize_event(event))
            appended_events.append(event)

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
                lifecycle_reconciled |= _reconcile_lifecycle_event(lifecycle_path, event)

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
    if config:
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
    source: TaskSource = "active",
    destination: TaskLocation | None = None,
    may_emit_lifecycle: bool = False,
) -> TaskMutationResult:
    """Commit a state-independent event batch through :func:`mutate_task`."""
    return mutate_task(
        lattice_dir,
        task_id,
        lambda _context: TaskMutationDecision(events=events),
        config,
        source=source,
        destination=destination,
        may_emit_lifecycle=may_emit_lifecycle,
    )


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

    plan_path.parent.mkdir(parents=True, exist_ok=True)

    heading = f"# {short_id}: {title}" if short_id else f"# {title}"
    lines = [heading, ""]

    if description:
        lines.append(description)
        lines.append("")

    plan_path.write_text("\n".join(lines), encoding="utf-8")


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

    notes_path.write_text("\n".join(lines), encoding="utf-8")


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
    locks_dir.mkdir(parents=True, exist_ok=True)
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
    _caller_holds_lock: bool = False,
) -> None:
    """Write resource event(s) and snapshot atomically with proper locking.

    This is the canonical write path for all resource mutations.

    Args:
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
    resource_dir.mkdir(parents=True, exist_ok=True)

    def _do_writes() -> None:
        # Event-first: append to per-resource event log
        event_path = lattice_dir / "events" / f"{resource_id}.jsonl"
        for event in events:
            jsonl_append(event_path, serialize_event(event))

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
    if config:
        from lattice.storage.hooks import execute_resource_hooks

        for event in events:
            execute_resource_hooks(config, lattice_dir, resource_id, resource_name, event)
