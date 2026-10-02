"""Board integrity: doctor's board scan and the task-derived repair.

Pure storage functions shared by ``lattice doctor`` and ``lattice rebuild``
(CLI) and ``lattice server project import`` (server). ``lattice.server`` may
import nothing from ``lattice.cli`` (SPEC §14, G-5), so the scan and the
repair live here; the CLI keeps its gating, cache orchestration, and output.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from lattice.core.config import configured_event_prefix
from lattice.core.events import LIFECYCLE_EVENT_TYPES, serialize_event
from lattice.core.ids import parse_short_id, validate_id, validate_short_id
from lattice.core.tasks import serialize_snapshot
from lattice.storage.fs import atomic_write, ensure_dir, unlink_path
from lattice.storage.locks import all_task_locks
from lattice.storage.operations import (
    AuthoritativeLogError,
    ResolvedTaskAuthority,
    _load_strict_id_index,
    _reconcile_placement,
    parse_project_short_id,
    resolve_task_authority,
)
from lattice.storage.short_ids import (
    SHORT_ID_EVENT_TYPES,
    max_observed_short_ids,
    save_id_index,
    short_id_inventory,
    split_short_id,
)


def _parse_jsonl_file(path: Path) -> tuple[list[dict], list[dict]]:
    """Parse a JSONL file line by line.

    Returns (valid_events, findings) where findings contain any parse errors.
    """
    findings: list[dict] = []
    events: list[dict] = []
    lines = path.read_text().splitlines()

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            events.append(json.loads(stripped))
        except json.JSONDecodeError:
            is_last = i == len(lines) - 1
            findings.append(
                {
                    "level": "error" if not is_last else "warning",
                    "check": "jsonl_parse",
                    "message": (
                        f"{'Truncated final line' if is_last else 'Invalid JSON at line ' + str(i + 1)}"
                        f" in {path.name}"
                    ),
                    "task_id": path.stem if path.stem != "_lifecycle" else None,
                    "file": str(path),
                    "line": i + 1,
                    "is_truncated_final": is_last,
                }
            )

    return events, findings


def _fix_truncated_jsonl(path: Path) -> bool:
    """Remove a truncated final line from a JSONL file.

    Returns True if a fix was applied.
    """
    lines = path.read_text().splitlines()
    if not lines:
        return False

    # Check if last non-empty line is invalid JSON
    last_idx = len(lines) - 1
    while last_idx >= 0 and not lines[last_idx].strip():
        last_idx -= 1

    if last_idx < 0:
        return False

    try:
        json.loads(lines[last_idx])
        return False  # Last line is valid
    except json.JSONDecodeError:
        # Remove the truncated line and rewrite atomically
        good_lines = lines[:last_idx]
        content = "\n".join(good_lines)
        if good_lines:
            content += "\n"
        atomic_write(path, content)
        return True


def _collect_task_files(lattice_dir: Path) -> list[Path]:
    """Collect all task snapshot files from tasks/ and archive/tasks/."""
    result = []
    for d in [lattice_dir / "tasks", lattice_dir / "archive" / "tasks"]:
        if d.is_dir():
            result.extend(sorted(d.glob("*.json")))
    return result


def _collect_event_files(lattice_dir: Path) -> list[Path]:
    """Collect all per-task event files from events/ and archive/events/.

    Excludes ``_lifecycle.jsonl`` and ``res_*`` resource event files.
    """
    result = []
    for d in [lattice_dir / "events", lattice_dir / "archive" / "events"]:
        if d.is_dir():
            for f in sorted(d.glob("*.jsonl")):
                if f.name == "_lifecycle.jsonl":
                    continue
                if f.stem.startswith("res_"):
                    continue
                result.append(f)
    return result


def _missing_task_file_findings(
    lattice_dir: Path, lifecycle_events: list[dict], event_files: list[Path]
) -> list[dict]:
    """``missing_task_file``: a task named by ``_lifecycle.jsonl`` or ``ids.json``
    with no event log, active or archived (SPEC §7). Erasing keeps every file,
    so a missing log is always a finding."""
    present = {f.stem for f in event_files}
    referenced: dict[str, str] = {}
    for ev in lifecycle_events:
        task_id = ev.get("task_id")
        if isinstance(task_id, str) and task_id:
            referenced.setdefault(task_id, "_lifecycle.jsonl")
    try:
        id_map = json.loads((lattice_dir / "ids.json").read_text()).get("map", {})
    except (OSError, ValueError, AttributeError):
        id_map = {}  # an unreadable index is alias_integrity's finding
    if isinstance(id_map, dict):
        for target in id_map.values():
            if isinstance(target, str) and target:
                referenced.setdefault(target, "ids.json")
    return [
        {
            "level": "error",
            "check": "missing_task_file",
            "message": (
                f"Task {task_id} is referenced by {source} but its event log is missing "
                f"(events/{task_id}.jsonl)"
            ),
            "task_id": task_id,
        }
        for task_id, source in sorted(referenced.items())
        if task_id not in present
    ]


def _collect_resource_event_files(lattice_dir: Path) -> list[Path]:
    """Collect all per-resource event files (``res_*.jsonl``)."""
    result = []
    events_dir = lattice_dir / "events"
    if events_dir.is_dir():
        for f in sorted(events_dir.glob("res_*.jsonl")):
            result.append(f)
    return result


def _collect_resource_snapshot_files(lattice_dir: Path) -> list[Path]:
    """Collect all resource snapshot files from resources/*/resource.json."""
    result = []
    resources_dir = lattice_dir / "resources"
    if resources_dir.is_dir():
        for res_dir in sorted(resources_dir.iterdir()):
            if res_dir.is_dir():
                snap_path = res_dir / "resource.json"
                if snap_path.exists():
                    result.append(snap_path)
    return result


def _collect_artifact_meta_files(lattice_dir: Path) -> list[Path]:
    """Collect all artifact metadata files."""
    meta_dir = lattice_dir / "artifacts" / "meta"
    if meta_dir.is_dir():
        return sorted(meta_dir.glob("*.json"))
    return []


def _collect_task_ids(lattice_dir: Path) -> set[str]:
    """Return task IDs named by any active/archive event or snapshot candidate."""
    return {
        path.stem
        for path in [*_collect_task_files(lattice_dir), *_collect_event_files(lattice_dir)]
    }


def _task_paths(lattice_dir: Path, task_id: str, archived: bool) -> dict[str, Path]:
    base = lattice_dir / "archive" if archived else lattice_dir
    return {
        "event": base / "events" / f"{task_id}.jsonl",
        "snapshot": base / "tasks" / f"{task_id}.json",
        "plan": base / "plans" / f"{task_id}.md",
        "notes": base / "notes" / f"{task_id}.md",
    }


def _authority_log_context(authority: ResolvedTaskAuthority, short_id: object) -> tuple[Path, int]:
    """Return the path/line carrying an effective authoritative short ID."""
    line_number = next(
        (
            line
            for line, event in reversed(list(enumerate(authority.events, 1)))
            if event.get("data", {}).get("short_id") == short_id
        ),
        1,
    )
    path = (
        authority.active_event_path
        if authority.active_event_path.exists()
        else authority.archived_event_path
    )
    return path, line_number


def _validate_authoritative_short_ids(
    authorities: dict[str, ResolvedTaskAuthority],
    prefix: str | None,
) -> tuple[list[tuple[str, str, int, Path, int]], list[AuthoritativeLogError]]:
    """Validate unique event-authoritative aliases, collecting every problem.

    Returns the valid aliases (the first holder of a duplicated ID among them)
    and one contextual error per malformed, out-of-prefix, or duplicate alias.
    """
    validated: list[tuple[str, str, int, Path, int]] = []
    problems: list[AuthoritativeLogError] = []
    seen: dict[str, tuple[str, Path, int]] = {}

    def problem(kind: str, message: str, path: Path, line: int) -> None:
        # ``kind`` tells doctor --fix's history repair what it may reassign
        # (SPEC §11): "prefix" and "duplicate"; never "malformed".
        error = AuthoritativeLogError(message, path=path, line=line)
        error.kind = kind  # type: ignore[attr-defined]
        problems.append(error)

    for task_id, authority in authorities.items():
        short_id = authority.snapshot.get("short_id")
        path, line = _authority_log_context(authority, short_id)
        if short_id is None and prefix is None:
            continue
        if not isinstance(short_id, str):
            problem(
                "malformed",
                f"task {task_id} has malformed authoritative short ID {short_id!r}; "
                "manual immutable-log recovery required",
                path,
                line,
            )
            continue
        try:
            if prefix is not None:
                suffix = parse_project_short_id(short_id, prefix)
            else:
                parsed_prefix, suffix = parse_short_id(short_id)
                if not parsed_prefix or suffix < 1:
                    raise ValueError(short_id)
        except (AuthoritativeLogError, ValueError):
            detail = (
                f"task {task_id} has authoritative short ID {short_id!r} outside "
                f"configured prefix {prefix!r}"
                if prefix is not None
                else f"task {task_id} has malformed authoritative short ID {short_id!r}"
            )
            problem(
                "prefix" if prefix is not None else "malformed",
                f"{detail}; manual immutable-log recovery required",
                path,
                line,
            )
            continue
        previous = seen.get(short_id)
        if previous is not None and previous[0] != task_id:
            problem(
                "duplicate",
                f"duplicate authoritative short ID {short_id}: "
                f"{previous[0]} at {previous[1]}:{previous[2]} and {task_id}; "
                "manual immutable-log recovery required",
                path,
                line,
            )
            continue
        seen[short_id] = (task_id, path, line)
        validated.append((short_id, task_id, suffix, path, line))
    return validated, problems


def _moved_off_by_supersedes(authority: ResolvedTaskAuthority, short_id: str) -> bool:
    """Did this task leave *short_id* through a ``task_short_id_assigned`` that
    ``supersedes`` it, after the last event that issued it to the task?"""
    moved = False
    for event in authority.events:
        data = event.get("data")
        if event.get("type") not in SHORT_ID_EVENT_TYPES or not isinstance(data, dict):
            continue
        if data.get("short_id") == short_id:
            moved = False
        elif event.get("type") == "task_short_id_assigned" and data.get("supersedes") == short_id:
            moved = True
    return moved


def _historical_short_id_duplicates(
    authorities: dict[str, ResolvedTaskAuthority],
    lattice_dir: Path,
    *,
    include_id_map: bool = True,
) -> list[tuple[str, str]]:
    """Report duplicate assignments across task logs, lifecycle, and ids.json.

    A lifecycle record mirrors its task-log event. Coalesce those copies by
    event ID and owner, while retaining separate assignment events that reuse
    an ID, including repeated assignments by one task. Map-only reservations
    are not duplicates; once an event assigned that ID, a map entry for a
    different task is.
    """
    effective: dict[str, set[str]] = {}
    for task_id, authority in authorities.items():
        effective_id = authority.snapshot.get("short_id")
        if split_short_id(effective_id) is not None:
            effective.setdefault(effective_id, set()).add(task_id)

    inventory = short_id_inventory(lattice_dir)
    issued: dict[str, list[tuple[str, Path, int, str]]] = {}
    seen_events: set[tuple[str, str, str]] = set()
    map_owners: dict[str, str] = {}
    for occurrence in inventory.occurrences:
        if occurrence.source == "ids.json":
            if include_id_map and occurrence.task_id is not None:
                map_owners[occurrence.short_id] = occurrence.task_id
            continue
        if occurrence.event_type not in SHORT_ID_EVENT_TYPES or occurrence.task_id is None:
            continue
        event_key = (
            occurrence.short_id,
            occurrence.task_id,
            occurrence.event_id or f"{occurrence.path}:{occurrence.line}",
        )
        if event_key in seen_events:
            continue
        seen_events.add(event_key)
        issued.setdefault(occurrence.short_id, []).append(
            (occurrence.task_id, occurrence.path, occurrence.line, event_key[2])
        )

    results: list[tuple[str, str]] = []
    for short_id in sorted(set(issued) | set(map_owners)):
        assignments = issued.get(short_id, [])
        holders = {task_id for task_id, _path, _line, _event_key in assignments}
        map_owner = map_owners.get(short_id)
        if assignments and map_owner is not None:
            holders.add(map_owner)
        current = effective.get(short_id, set())
        assignments_per_task: dict[str, int] = {}
        for task_id, _path, _line, _event_key in assignments:
            assignments_per_task[task_id] = assignments_per_task.get(task_id, 0) + 1
        repeated = any(count > 1 for count in assignments_per_task.values())
        if len(holders) < 2 and not repeated:
            continue
        if len(current) > 1:
            continue
        where_parts = [
            f"{task_id} at {path}:{line}" for task_id, path, line, _event_key in assignments
        ]
        if (
            map_owner is not None
            and assignments
            and map_owner not in {task_id for task_id, _path, _line, _event_key in assignments}
        ):
            where_parts.append(f"{map_owner} in {lattice_dir / 'ids.json'}")
        where = " and ".join(where_parts)
        event_holders = {task_id for task_id, _path, _line, _event_key in assignments}
        map_conflict = assignments and map_owner is not None and map_owner not in event_holders
        repaired = (
            len(holders) > 1
            and not repeated
            and not map_conflict
            and all(
                task_id in authorities and _moved_off_by_supersedes(authorities[task_id], short_id)
                for task_id in event_holders
                if task_id not in current
            )
        )
        if repaired:
            keeper = f"; {next(iter(current))} keeps it" if current else ""
            results.append(
                (
                    "info",
                    f"short ID {short_id} was issued to more than one task: {where}; "
                    f"repaired by supersedes reassignment{keeper}",
                )
            )
            continue
        if repeated and len(holders) == 1:
            detail = f"short ID {short_id} was assigned more than once: {where}"
        else:
            detail = f"short ID {short_id} was issued to more than one task: {where}"
        results.append(("error", f"{detail}; manual immutable-log recovery required"))
    return results


def _require_valid_short_ids(
    authorities: dict[str, ResolvedTaskAuthority],
    prefix: str | None,
) -> list[tuple[str, str, int, Path, int]]:
    """Return the validated aliases, or raise naming every problem (repair paths)."""
    validated, problems = _validate_authoritative_short_ids(authorities, prefix)
    if len(problems) == 1:
        raise problems[0]
    if problems:
        raise AuthoritativeLogError("; ".join(str(problem) for problem in problems))
    return validated


def _build_rebuilt_id_index(
    current: dict,
    validated: list[tuple[str, str, int, Path, int]],
    max_observed: dict[str, int],
) -> dict:
    """Build a replacement map while preserving every valid high-water mark.

    Each counter ends at least one past the highest sequence any task log ever
    issued for its prefix (the log floor, SPEC §5), including historical IDs
    that no longer appear in the effective alias map.
    """
    next_seqs = dict(current["next_seqs"])
    for prefix, observed in max_observed.items():
        next_seqs[prefix] = max(next_seqs.get(prefix, 1), observed + 1)
    for short_id in current["map"]:
        prefix, suffix = parse_short_id(short_id)
        next_seqs[prefix] = max(next_seqs.get(prefix, 1), suffix + 1)
    rebuilt_map: dict[str, str] = {}
    for short_id, task_id, suffix, _path, _line in validated:
        prefix, _ = parse_short_id(short_id)
        rebuilt_map[short_id] = task_id
        next_seqs[prefix] = max(next_seqs.get(prefix, 1), suffix + 1)
    return {"schema_version": 2, "next_seqs": next_seqs, "map": rebuilt_map}


def _inspect_task_authority_unlocked(
    lattice_dir: Path,
    *,
    skip_task_ids: set[str] | None = None,
) -> tuple[dict[str, ResolvedTaskAuthority], list[dict]]:
    """Strictly replay every task placement set and report repair boundaries."""
    authorities: dict[str, ResolvedTaskAuthority] = {}
    findings: list[dict] = []

    for task_id in sorted(_collect_task_ids(lattice_dir)):
        if skip_task_ids and task_id in skip_task_ids:
            continue
        active = _task_paths(lattice_dir, task_id, False)
        archived = _task_paths(lattice_dir, task_id, True)
        event_candidates = [path for path in (active["event"], archived["event"]) if path.exists()]
        snapshot_candidates = [
            path for path in (active["snapshot"], archived["snapshot"]) if path.exists()
        ]
        if not event_candidates:
            if snapshot_candidates:
                findings.append(
                    {
                        "level": "error",
                        "check": "authoritative_log",
                        "message": (
                            f"Task {task_id} has snapshot data but no authoritative event log; "
                            "manual recovery is required."
                        ),
                        "task_id": task_id,
                    }
                )
            continue

        try:
            authority = resolve_task_authority(lattice_dir, task_id)
        except AuthoritativeLogError as exc:
            finding = {
                "level": "error",
                "check": "authoritative_log",
                "message": (
                    f"Authoritative log error for {task_id}: {exc}. "
                    "Rebuild will refuse to overwrite data; manual recovery is required."
                ),
                "task_id": task_id,
            }
            if _stale_only(lattice_dir, task_id):
                # Only stale ``from`` values: doctor --fix --actor repairs it (SPEC §11).
                finding["repair"] = "stale_from"
            findings.append(finding)
            continue

        assert authority is not None
        authorities[task_id] = authority
        findings.extend(_task_file_findings(lattice_dir, task_id, authority))

    return authorities, findings


def _stale_only(lattice_dir: Path, task_id: str) -> bool:
    """Does the task replay once its unnamed stale ``from`` values are accepted?"""
    try:
        lenient = resolve_task_authority(lattice_dir, task_id, lenient=True)
    except AuthoritativeLogError:
        return False
    return lenient is not None and bool(lenient.stale)


def _task_file_findings(
    lattice_dir: Path, task_id: str, authority: ResolvedTaskAuthority
) -> list[dict]:
    """Placement, snapshot, and plan/notes findings for one resolved task."""
    findings: list[dict] = []
    active = _task_paths(lattice_dir, task_id, False)
    archived = _task_paths(lattice_dir, task_id, True)
    event_candidates = [path for path in (active["event"], archived["event"]) if path.exists()]
    expected_archived = authority.location == "archived"
    target = archived if expected_archived else active
    other = active if expected_archived else archived
    repair = "Run lattice rebuild to restore authoritative placement."

    if len(event_candidates) == 2:
        left = active["event"].read_bytes()
        right = archived["event"].read_bytes()
        relation = "byte-identical" if left == right else "exact-prefix"
        findings.append(
            {
                "level": "warning",
                "check": "placement_drift",
                "message": (f"Task {task_id} has {relation} duplicate event logs. {repair}"),
                "task_id": task_id,
            }
        )
    elif not target["event"].exists():
        findings.append(
            {
                "level": "warning",
                "check": "placement_drift",
                "message": (
                    f"Task {task_id} event log is in the wrong location for "
                    f"{authority.location} state. {repair}"
                ),
                "task_id": task_id,
            }
        )

    expected_snapshot = serialize_snapshot(authority.snapshot)
    snapshot_matches = False
    if target["snapshot"].exists():
        try:
            snapshot_matches = target["snapshot"].read_text(encoding="utf-8") == expected_snapshot
        except (OSError, UnicodeDecodeError):
            snapshot_matches = False
    if not snapshot_matches:
        findings.append(
            {
                "level": "warning",
                "check": "snapshot_drift",
                "message": (
                    f"Snapshot drift: {task_id} differs from full authoritative replay "
                    "(even if last_event_id matches). Run lattice rebuild."
                ),
                "task_id": task_id,
            }
        )
    if other["snapshot"].exists():
        findings.append(
            {
                "level": "warning",
                "check": "placement_drift",
                "message": (
                    f"Task {task_id} has a duplicate or wrong-location snapshot. {repair}"
                ),
                "task_id": task_id,
            }
        )

    for name in ("plan", "notes"):
        target_file = target[name]
        other_file = other[name]
        if not target_file.exists() and not other_file.exists():
            if name == "plan":
                findings.append(
                    {
                        "level": "warning",
                        "check": "placement_drift",
                        "message": (
                            f"Task {task_id} has no plan file; this legacy file cannot "
                            "be reconstructed automatically."
                        ),
                        "task_id": task_id,
                    }
                )
            continue
        if target_file.exists() and other_file.exists():
            if target_file.read_bytes() != other_file.read_bytes():
                findings.append(
                    {
                        "level": "error",
                        "check": "placement_drift",
                        "message": (
                            f"Task {task_id} has divergent active/archive {name} files; "
                            "manual recovery is required."
                        ),
                        "task_id": task_id,
                    }
                )
            else:
                findings.append(
                    {
                        "level": "warning",
                        "check": "placement_drift",
                        "message": (
                            f"Task {task_id} has duplicate byte-identical {name} files. {repair}"
                        ),
                        "task_id": task_id,
                    }
                )
        elif other_file.exists():
            findings.append(
                {
                    "level": "warning",
                    "check": "placement_drift",
                    "message": (f"Task {task_id} {name} file is in the wrong location. {repair}"),
                    "task_id": task_id,
                }
            )

    return findings


def inspect_task_authority(
    lattice_dir: Path,
    *,
    skip_task_ids: set[str] | None = None,
) -> tuple[dict[str, ResolvedTaskAuthority], list[dict]]:
    """Inspect all candidate bytes under one stable, deterministic lock set."""
    with all_task_locks(lattice_dir / "locks"):
        return _inspect_task_authority_unlocked(lattice_dir, skip_task_ids=skip_task_ids)


def _write_snapshot_in_place(
    lattice_dir: Path, task_id: str, authority: ResolvedTaskAuthority
) -> None:
    """Write one task's snapshot where authority places it; drop one at the other place."""
    target = _task_paths(lattice_dir, task_id, authority.location == "archived")["snapshot"]
    other = _task_paths(lattice_dir, task_id, authority.location != "archived")["snapshot"]
    expected = serialize_snapshot(authority.snapshot)
    try:
        current = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        current = None
    if current != expected:
        ensure_dir(target.parent)
        atomic_write(target, expected)
    if other.exists():
        unlink_path(other)


# ---------------------------------------------------------------------------
# The doctor scan
# ---------------------------------------------------------------------------


@dataclass
class DoctorReport:
    """Every finding of one board scan, its counts, and each check's pass flag."""

    findings: list[dict]
    task_count: int
    event_count: int
    artifact_count: int
    resource_count: int
    json_ok: bool
    jsonl_ok: bool
    drift_ok: bool
    refs_ok: bool
    artifacts_ok: bool
    selflink_ok: bool
    dupes_ok: bool
    ids_ok: bool
    global_ok: bool
    alias_ok: bool
    resource_ok: bool

    @property
    def warnings(self) -> int:
        return sum(1 for f in self.findings if f["level"] == "warning")

    @property
    def errors(self) -> int:
        return sum(1 for f in self.findings if f["level"] == "error")


def check_board(lattice_dir: Path, *, fix: bool = False) -> DoctorReport:
    """Run doctor's board checks on *lattice_dir* and return the report.

    Read-only unless *fix*, which removes a truncated final line of a JSONL
    log. Reads take the board's lock files, as every read does.
    """
    findings: list[dict] = []

    # Gather files
    task_files = _collect_task_files(lattice_dir)
    event_files = _collect_event_files(lattice_dir)
    artifact_meta_files = _collect_artifact_meta_files(lattice_dir)

    # Count stats
    task_count = len(_collect_task_ids(lattice_dir))
    artifact_count = len(artifact_meta_files)

    # Track all parsed snapshots keyed by task ID
    snapshots: dict[str, dict] = {}
    # Track all known task IDs (active + archived) for relationship validation
    known_task_ids: set[str] = set()
    # Track all known artifact IDs
    known_artifact_ids: set[str] = set()

    # -----------------------------------------------------------------
    # Check 1: JSON parseability (task snapshots, artifact meta, config)
    # -----------------------------------------------------------------
    json_files: list[Path] = list(task_files) + list(artifact_meta_files)
    config_path = lattice_dir / "config.json"
    json_ok = True
    config: object = None  # config.json as parsed here; a parse failure is a finding
    if config_path.exists():
        json_files.append(config_path)
    else:
        json_ok = False
        findings.append(
            {
                "level": "error",
                "check": "config",
                "message": "config.json is missing; this board has no configuration",
                "task_id": None,
            }
        )
    for jf in json_files:
        try:
            data = json.loads(jf.read_text())
            # Store snapshot data for later checks
            if jf == config_path:
                config = data
                if not isinstance(data, dict):
                    json_ok = False
                    findings.append(
                        {
                            "level": "error",
                            "check": "config",
                            "message": (
                                f"config.json must hold a JSON object, not {type(data).__name__}"
                            ),
                            "task_id": None,
                        }
                    )
            elif jf.parent.name in ("tasks",) and jf.suffix == ".json":
                # A snapshot that is not an object is an unusable cache: the
                # snapshot checks skip it, and authority replaces it below.
                if isinstance(data, dict):
                    snapshots[jf.stem] = data
                known_task_ids.add(jf.stem)
            elif jf.parent.parent.name == "archive" and jf.parent.name == "tasks":
                if isinstance(data, dict):
                    snapshots[jf.stem] = data
                known_task_ids.add(jf.stem)
            elif jf.parent.name == "meta":
                known_artifact_ids.add(jf.stem)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            json_ok = False
            findings.append(
                {
                    "level": "error",
                    "check": "json_parse",
                    "message": f"Invalid JSON in {jf.name}: {e}",
                    "task_id": jf.stem if jf.stem.startswith("task_") else None,
                }
            )

    # -----------------------------------------------------------------
    # Check 2: JSONL parseability
    # -----------------------------------------------------------------
    all_jsonl_files = list(event_files)
    lifecycle_log_path = lattice_dir / "events" / "_lifecycle.jsonl"
    if lifecycle_log_path.exists():
        all_jsonl_files.append(lifecycle_log_path)

    jsonl_ok = True
    per_task_events: dict[str, list[dict]] = {}
    global_events: list[dict] = []
    total_event_count = 0

    for jf in all_jsonl_files:
        events, parse_findings = _parse_jsonl_file(jf)
        if parse_findings:
            jsonl_ok = False
            if fix:
                for finding in parse_findings:
                    if finding.get("is_truncated_final"):
                        if _fix_truncated_jsonl(jf):
                            finding["message"] += " (fixed)"
                            finding["level"] = "warning"
            findings.extend(parse_findings)

        if jf.name == "_lifecycle.jsonl":
            global_events = events
        else:
            task_id = jf.stem
            per_task_events[task_id] = events
            total_event_count += len(events)

    event_count = total_event_count

    # -----------------------------------------------------------------
    # Check 3: Strict authority, placement, and full-byte snapshot drift
    # -----------------------------------------------------------------
    truncated_task_ids = {
        finding["task_id"]
        for finding in findings
        if finding.get("is_truncated_final")
        and "(fixed)" not in finding["message"]
        and finding.get("task_id")
    }
    authorities, authority_findings = inspect_task_authority(
        lattice_dir, skip_task_ids=truncated_task_ids
    )
    findings.extend(authority_findings)
    known_task_ids.update(_collect_task_ids(lattice_dir))
    for task_id, authority in authorities.items():
        snapshots[task_id] = authority.snapshot
        per_task_events[task_id] = list(authority.events)

    # A corrupt cache is replay-repairable when strict authority succeeds, and
    # so is a task that history repair can make replay (SPEC §11).
    stale_only = {
        finding["task_id"]
        for finding in authority_findings
        if finding.get("repair") == "stale_from"
    }
    for finding in findings:
        if finding["check"] == "json_parse" and finding.get("task_id") in authorities:
            finding["level"] = "warning"
            finding["message"] += " (snapshot cache is rebuildable from valid authority)"
        elif finding["check"] == "json_parse" and finding.get("task_id") in stale_only:
            finding["level"] = "warning"
            finding["message"] += " (snapshot cache is rebuilt by lattice doctor --fix --actor)"

    # Stale ``from`` values a reconciliation names: information, not errors.
    for task_id, authority in authorities.items():
        for event_id in authority.reconciled:
            findings.append(
                {
                    "level": "info",
                    "check": "history_repair",
                    "message": (
                        f"Task {task_id}: event {event_id} has a stale from value, "
                        "reconciled by task_history_reconciled"
                    ),
                    "task_id": task_id,
                }
            )

    drift_ok = not any(
        finding["check"] in {"snapshot_drift", "placement_drift"} for finding in findings
    )

    # -----------------------------------------------------------------
    # Check 4: Missing relationship targets
    # -----------------------------------------------------------------
    refs_ok = True
    for task_id, snap in snapshots.items():
        for rel in snap.get("relationships_out", []):
            target = rel.get("target_task_id")
            if target and target not in known_task_ids:
                refs_ok = False
                findings.append(
                    {
                        "level": "warning",
                        "check": "missing_reference",
                        "message": (
                            f"Task {task_id} has relationship to non-existent target {target}"
                        ),
                        "task_id": task_id,
                    }
                )

    # -----------------------------------------------------------------
    # Check 5: Missing artifacts
    # -----------------------------------------------------------------
    artifacts_ok = True
    for task_id, snap in snapshots.items():
        # Read artifact refs from evidence_refs (new) or artifact_refs (legacy)
        evidence_refs = snap.get("evidence_refs")
        if evidence_refs is not None:
            art_ids = [ref["id"] for ref in evidence_refs if ref.get("source_type") == "artifact"]
        else:
            art_ids = [
                (ref["id"] if isinstance(ref, dict) else ref)
                for ref in snap.get("artifact_refs", [])
            ]
        for art_id in art_ids:
            if art_id not in known_artifact_ids:
                artifacts_ok = False
                findings.append(
                    {
                        "level": "warning",
                        "check": "missing_artifact",
                        "message": (f"Task {task_id} references non-existent artifact {art_id}"),
                        "task_id": task_id,
                    }
                )

    # -----------------------------------------------------------------
    # Check 6: Self-links
    # -----------------------------------------------------------------
    selflink_ok = True
    for task_id, snap in snapshots.items():
        for rel in snap.get("relationships_out", []):
            if rel.get("target_task_id") == task_id:
                selflink_ok = False
                findings.append(
                    {
                        "level": "warning",
                        "check": "self_link",
                        "message": f"Task {task_id} has a self-referential relationship",
                        "task_id": task_id,
                    }
                )

    # -----------------------------------------------------------------
    # Check 7: Duplicate edges
    # -----------------------------------------------------------------
    dupes_ok = True
    for task_id, snap in snapshots.items():
        seen_edges: set[tuple[str, str]] = set()
        for rel in snap.get("relationships_out", []):
            edge = (rel.get("type", ""), rel.get("target_task_id", ""))
            if edge in seen_edges:
                dupes_ok = False
                findings.append(
                    {
                        "level": "warning",
                        "check": "duplicate_edge",
                        "message": (
                            f"Task {task_id} has duplicate {edge[0]} relationship to {edge[1]}"
                        ),
                        "task_id": task_id,
                    }
                )
            seen_edges.add(edge)

    # -----------------------------------------------------------------
    # Check 8: Malformed IDs
    # -----------------------------------------------------------------
    ids_ok = True
    for task_id in known_task_ids:
        if not validate_id(task_id, "task"):
            ids_ok = False
            findings.append(
                {
                    "level": "warning",
                    "check": "malformed_id",
                    "message": f"Malformed task ID: {task_id}",
                    "task_id": task_id,
                }
            )
    for events in per_task_events.values():
        for ev in events:
            ev_id = ev.get("id", "")
            if ev_id and not validate_id(ev_id, "ev"):
                ids_ok = False
                findings.append(
                    {
                        "level": "warning",
                        "check": "malformed_id",
                        "message": f"Malformed event ID: {ev_id}",
                        "task_id": ev.get("task_id"),
                    }
                )
    for art_id in known_artifact_ids:
        if not validate_id(art_id, "art"):
            ids_ok = False
            findings.append(
                {
                    "level": "warning",
                    "check": "malformed_id",
                    "message": f"Malformed artifact ID: {art_id}",
                    "task_id": None,
                }
            )

    # -----------------------------------------------------------------
    # Check 9: Lifecycle log consistency
    # -----------------------------------------------------------------
    global_ok = True
    global_by_id: dict[str, dict] = {}
    for ev in global_events:
        event_id = ev.get("id", "")
        existing = global_by_id.get(event_id)
        if existing is not None:
            global_ok = False
            findings.append(
                {
                    "level": "warning",
                    "check": "global_log_consistency",
                    "message": (
                        f"Lifecycle log has a "
                        f"{'duplicate' if existing == ev else 'mismatched duplicate'} "
                        f"event {event_id}; run lattice rebuild --all"
                    ),
                    "task_id": ev.get("task_id"),
                }
            )
        else:
            global_by_id[event_id] = ev

    # Every lifecycle event in per-task logs should be in global
    for task_id, events in per_task_events.items():
        for ev in events:
            if ev.get("type") in LIFECYCLE_EVENT_TYPES:
                ev_id = ev.get("id", "")
                global_copy = global_by_id.get(ev_id)
                if global_copy is None:
                    global_ok = False
                    findings.append(
                        {
                            "level": "warning",
                            "check": "global_log_consistency",
                            "message": (
                                f"Lifecycle event {ev_id} ({ev.get('type')}) "
                                f"for {task_id} missing from _lifecycle.jsonl"
                            ),
                            "task_id": task_id,
                        }
                    )
                elif global_copy != ev:
                    global_ok = False
                    findings.append(
                        {
                            "level": "warning",
                            "check": "global_log_consistency",
                            "message": (
                                f"Lifecycle event {ev_id} for {task_id} does not match "
                                "per-task authority; run lattice rebuild --all"
                            ),
                            "task_id": task_id,
                        }
                    )

    # Also check the reverse: every event in global should exist in a per-task log
    # Build set of all event IDs from per-task logs
    all_per_task_event_ids: set[str] = set()
    for events in per_task_events.values():
        for ev in events:
            all_per_task_event_ids.add(ev.get("id", ""))

    for ev in global_events:
        ev_id = ev.get("id", "")
        if ev_id not in all_per_task_event_ids:
            global_ok = False
            findings.append(
                {
                    "level": "warning",
                    "check": "global_log_consistency",
                    "message": (
                        f"Lifecycle log event {ev_id} ({ev.get('type')}) "
                        f"has no matching per-task event"
                    ),
                    "task_id": ev.get("task_id"),
                }
            )

    # -----------------------------------------------------------------
    # Check 10: Short ID / alias integrity
    # -----------------------------------------------------------------
    alias_ok = True
    event_prefix = configured_event_prefix(config if isinstance(config, dict) else {})
    has_project_code = event_prefix is not None
    ids_json_path = lattice_dir / "ids.json"

    if has_project_code and not ids_json_path.exists():
        alias_ok = False
        findings.append(
            {
                "level": "warning",
                "check": "alias_integrity",
                "message": "project_code is configured but ids.json is missing",
                "task_id": None,
            }
        )

    authoritative_short_ids: dict[str, tuple[str, Path, int]] = {}
    if has_project_code:
        validated_short_ids, short_id_problems = _validate_authoritative_short_ids(
            authorities, event_prefix
        )
        for problem in short_id_problems:
            alias_ok = False
            findings.append(
                {
                    "level": "error",
                    "check": "alias_integrity",
                    "message": str(problem),
                    "task_id": None,
                    "repair": getattr(problem, "kind", None),
                }
            )
        for level, message in _historical_short_id_duplicates(authorities, lattice_dir):
            if level == "info":
                findings.append(
                    {
                        "level": "info",
                        "check": "history_repair",
                        "message": message,
                        "task_id": None,
                    }
                )
                continue
            alias_ok = False
            findings.append(
                {
                    "level": "error",
                    "check": "alias_integrity",
                    "message": message,
                    "task_id": None,
                    "repair": "historical_duplicate",
                }
            )
        for short_id, task_id_key, _suffix, log_path, short_id_line in validated_short_ids:
            authoritative_short_ids[short_id] = (task_id_key, log_path, short_id_line)

    if ids_json_path.exists():
        try:
            id_index = _load_strict_id_index(lattice_dir)
        except AuthoritativeLogError as exc:
            alias_ok = False
            findings.append(
                {
                    "level": "error",
                    "check": "alias_integrity",
                    "message": f"Invalid derived short-ID index: {exc}",
                    "task_id": None,
                }
            )
            id_index = {"map": {}, "next_seqs": {}}
        id_map = id_index["map"]
        next_seqs = id_index["next_seqs"]

        # Check the derived alias index against authoritative task_created replay,
        # never against the snapshot cache.
        for short_id, target_ulid in id_map.items():
            if target_ulid not in authorities:
                alias_ok = False
                findings.append(
                    {
                        "level": "warning",
                        "check": "alias_integrity",
                        "message": (
                            f"ids.json maps {short_id} to a task without valid authority "
                            f"({target_ulid}); run lattice rebuild --all after recovery"
                        ),
                        "task_id": target_ulid,
                    }
                )
            if not validate_short_id(short_id):
                alias_ok = False
                findings.append(
                    {
                        "level": "warning",
                        "check": "alias_integrity",
                        "message": f"Invalid short ID format in ids.json: {short_id}",
                        "task_id": None,
                    }
                )

        # Check: every valid authoritative creation short ID has the exact mapping.
        for snap_short_id, (task_id_key, _log_path, _line) in authoritative_short_ids.items():
            if id_map.get(snap_short_id) != task_id_key:
                alias_ok = False
                findings.append(
                    {
                        "level": "warning",
                        "check": "alias_integrity",
                        "message": (
                            f"Authoritative task {task_id_key} has short_id "
                            f"{snap_short_id} but ids.json does not map it exactly; "
                            "run lattice rebuild --all"
                        ),
                        "task_id": task_id_key,
                    }
                )

        # Check: per-prefix next_seqs stays above every event and map assignment.
        log_max = max_observed_short_ids(lattice_dir)
        # Every prefix seen in the inventory is checked: one missing from next_seqs
        # has the implicit counter 1.
        counter_behind_logs: set[str] = set()
        for prefix in sorted(log_max):
            prefix_next = next_seqs.get(prefix, 1)
            if log_max[prefix] >= prefix_next:
                shown = prefix_next if prefix in next_seqs else "unset, implicitly 1"
                counter_behind_logs.add(prefix)
                alias_ok = False
                findings.append(
                    {
                        "level": "warning",
                        "check": "alias_integrity",
                        "message": (
                            f"next_seqs['{prefix}'] ({shown}) is at or below the max "
                            f"short-ID seq in recorded assignments ({log_max[prefix]}); "
                            "run lattice rebuild --all"
                        ),
                        "task_id": None,
                    }
                )

        prefix_max: dict[str, int] = {}
        for short_id in id_map:
            try:
                prefix, num = parse_short_id(short_id)
                if prefix not in prefix_max or num > prefix_max[prefix]:
                    prefix_max[prefix] = num
            except ValueError:
                pass
        for prefix, max_num in prefix_max.items():
            prefix_next = next_seqs.get(prefix, 1)
            if max_num >= prefix_next and prefix not in counter_behind_logs:
                alias_ok = False
                findings.append(
                    {
                        "level": "warning",
                        "check": "alias_integrity",
                        "message": (
                            f"next_seqs['{prefix}'] ({prefix_next}) is not greater than "
                            f"max assigned seq ({max_num})"
                        ),
                        "task_id": None,
                    }
                )

    # -----------------------------------------------------------------
    # Check 11: Resource snapshot drift & stale holders
    # -----------------------------------------------------------------
    resource_ok = True
    resource_snap_files = _collect_resource_snapshot_files(lattice_dir)
    resource_event_files = _collect_resource_event_files(lattice_dir)
    resource_count = len(resource_snap_files)

    # Parse resource snapshots
    resource_snapshots: dict[str, dict] = {}
    for rsf in resource_snap_files:
        try:
            rsnap = json.loads(rsf.read_text())
            res_id = rsnap.get("id", "")
            resource_snapshots[res_id] = rsnap
        except json.JSONDecodeError:
            resource_ok = False
            findings.append(
                {
                    "level": "error",
                    "check": "resource_integrity",
                    "message": f"Invalid JSON in resource snapshot {rsf.name}",
                    "task_id": None,
                }
            )

    # Parse resource event files and check drift
    per_resource_events: dict[str, list[dict]] = {}
    for ref in resource_event_files:
        res_id = ref.stem
        r_events, r_findings = _parse_jsonl_file(ref)
        if r_findings:
            resource_ok = False
            findings.extend(r_findings)
        per_resource_events[res_id] = r_events

    # Check snapshot drift for resources
    for res_id, rsnap in resource_snapshots.items():
        last_event_id = rsnap.get("last_event_id")
        r_events = per_resource_events.get(res_id, [])
        if r_events:
            actual_last_id = r_events[-1].get("id")
            if last_event_id != actual_last_id:
                resource_ok = False
                findings.append(
                    {
                        "level": "warning",
                        "check": "resource_integrity",
                        "message": (
                            f"Resource snapshot drift: {rsnap.get('name', res_id)} "
                            f"(snapshot last_event_id={last_event_id}, "
                            f"actual last event={actual_last_id})"
                        ),
                        "task_id": None,
                    }
                )

    # Report stale holders
    from lattice.core.events import utc_now

    now = utc_now()
    for res_id, rsnap in resource_snapshots.items():
        for holder in rsnap.get("holders", []):
            expires_at = holder.get("expires_at")
            if expires_at and expires_at < now:
                findings.append(
                    {
                        "level": "warning",
                        "check": "resource_integrity",
                        "message": (
                            f"Stale holder on {rsnap.get('name', res_id)}: "
                            f"{holder.get('actor')} expired at {expires_at}"
                        ),
                        "task_id": None,
                    }
                )

    # -----------------------------------------------------------------
    # Check 11: Task files referenced but missing
    # -----------------------------------------------------------------
    findings.extend(_missing_task_file_findings(lattice_dir, global_events, event_files))

    return DoctorReport(
        findings=findings,
        task_count=task_count,
        event_count=event_count,
        artifact_count=artifact_count,
        resource_count=resource_count,
        json_ok=json_ok,
        jsonl_ok=jsonl_ok,
        drift_ok=drift_ok,
        refs_ok=refs_ok,
        artifacts_ok=artifacts_ok,
        selflink_ok=selflink_ok,
        dupes_ok=dupes_ok,
        ids_ok=ids_ok,
        global_ok=global_ok,
        alias_ok=alias_ok,
        resource_ok=resource_ok,
    )


# ---------------------------------------------------------------------------
# The task-derived repair
# ---------------------------------------------------------------------------


def repair_task_derived_files(lattice_dir: Path, *, reconcile_placement: bool) -> list[str]:
    """Rebuild the task-derived files from strict authority, as one lock epoch.

    The derived files are the task snapshots (``tasks/``, ``archive/tasks/``),
    ``events/_lifecycle.jsonl``, and ``ids.json``, whose counters are raised to
    the log floor of SPEC §5 (``max(current, max_observed + 1)``). Every
    authority, index, and prefix check runs before the first write; a failure
    raises ``AuthoritativeLogError`` and writes nothing.

    ``rebuild --all`` passes ``reconcile_placement=True``, which also moves event
    logs, plans, and notes into their authoritative location. Import passes
    ``False``: it writes each snapshot where authority places it, removes a
    snapshot left at the other location, and touches no other file (SPEC §11).
    Resource snapshots are not task-derived and are never touched here.
    """
    with all_task_locks(lattice_dir / "locks", ["events__lifecycle", "ids_json"]):
        return _repair_task_derived_files_unlocked(
            lattice_dir, reconcile_placement=reconcile_placement
        )


def _lifecycle_events(authorities: dict[str, ResolvedTaskAuthority]) -> list[dict]:
    """Every lifecycle event in the task logs, by ``(ts, id)``; a conflict raises."""
    lifecycle_by_id: dict[str, dict] = {}
    for task_id, authority in authorities.items():
        for event in authority.events:
            if event.get("type") not in LIFECYCLE_EVENT_TYPES:
                continue
            event_id = event["id"]
            existing = lifecycle_by_id.get(event_id)
            if existing is not None and existing != event:
                path, line = _authority_log_context(
                    authority, event.get("data", {}).get("short_id")
                )
                raise AuthoritativeLogError(
                    f"conflicting lifecycle event {event_id} for {task_id}",
                    path=path,
                    line=line,
                )
            lifecycle_by_id[event_id] = event
    return sorted(lifecycle_by_id.values(), key=lambda event: (event.get("ts", ""), event["id"]))


def _repair_task_derived_files_unlocked(
    lattice_dir: Path, *, reconcile_placement: bool
) -> list[str]:
    """:func:`repair_task_derived_files` for a caller that holds its locks."""
    task_ids = sorted(_collect_task_ids(lattice_dir))
    authorities, findings = _inspect_task_authority_unlocked(lattice_dir)
    failures = [finding["message"] for finding in findings if finding["level"] == "error"]
    missing = sorted(set(task_ids) - set(authorities))
    failures.extend(f"{task_id}: no valid authoritative event log" for task_id in missing)
    if failures:
        raise AuthoritativeLogError("; ".join(failures))

    # Complete index and configured-prefix validation happens before the
    # first snapshot, placement, lifecycle, or ids.json write.
    current_index = _load_strict_id_index(lattice_dir)
    event_prefix = configured_event_prefix(json.loads((lattice_dir / "config.json").read_text()))
    validated_short_ids = _require_valid_short_ids(authorities, event_prefix)
    rebuilt_index = _build_rebuilt_id_index(
        current_index, validated_short_ids, max_observed_short_ids(lattice_dir)
    )

    lifecycle_events = _lifecycle_events(authorities)
    lifecycle_content = "".join(serialize_event(event) for event in lifecycle_events)

    # All failure-prone authority/prose/index parsing is complete. Durable
    # repair writes begin only here, while the complete stable lock set is
    # still held.
    for task_id, authority in authorities.items():
        if reconcile_placement:
            _reconcile_placement(
                lattice_dir,
                task_id,
                authority.location,
                authority.event_bytes,
                authority.snapshot,
                inject_faults=False,
            )
        else:
            _write_snapshot_in_place(lattice_dir, task_id, authority)
    atomic_write(lattice_dir / "events" / "_lifecycle.jsonl", lifecycle_content)
    save_id_index(lattice_dir, rebuilt_index)
    return sorted(authorities)
