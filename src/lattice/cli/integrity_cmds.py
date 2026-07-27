"""Integrity commands: doctor, rebuild."""

from __future__ import annotations

import json
from pathlib import Path

import click

from lattice.cli.helpers import (
    json_envelope,
    load_project_config,
    output_error,
    require_root,
)
from lattice.cli.main import cli
from lattice.core.events import LIFECYCLE_EVENT_TYPES, serialize_event
from lattice.core.ids import validate_id, validate_short_id, parse_short_id
from lattice.core.tasks import serialize_snapshot
from lattice.storage.fs import atomic_write
from lattice.storage.locks import multi_lock
from lattice.storage.operations import (
    AuthoritativeLogError,
    ResolvedTaskAuthority,
    TaskMutationDecision,
    mutate_task,
    resolve_task_authority,
)
from lattice.storage.short_ids import load_id_index, save_id_index


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


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
            findings.append(
                {
                    "level": "error",
                    "check": "authoritative_log",
                    "message": (
                        f"Authoritative log error for {task_id}: {exc}. "
                        "Rebuild will refuse to overwrite data; manual recovery is required."
                    ),
                    "task_id": task_id,
                }
            )
            continue

        assert authority is not None
        authorities[task_id] = authority
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
                    "message": (
                        f"Task {task_id} has {relation} duplicate event logs. {repair}"
                    ),
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
                snapshot_matches = (
                    target["snapshot"].read_text(encoding="utf-8") == expected_snapshot
                )
            except OSError:
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
                                f"Task {task_id} has duplicate byte-identical {name} files. "
                                f"{repair}"
                            ),
                            "task_id": task_id,
                        }
                    )
            elif other_file.exists():
                findings.append(
                    {
                        "level": "warning",
                        "check": "placement_drift",
                        "message": (
                            f"Task {task_id} {name} file is in the wrong location. {repair}"
                        ),
                        "task_id": task_id,
                    }
                )

    return authorities, findings


def inspect_task_authority(
    lattice_dir: Path,
    *,
    skip_task_ids: set[str] | None = None,
) -> tuple[dict[str, ResolvedTaskAuthority], list[dict]]:
    """Inspect all candidate bytes under one stable, deterministic lock set."""
    task_ids = sorted(_collect_task_ids(lattice_dir))
    lock_keys = [
        key
        for task_id in task_ids
        for key in (f"events_{task_id}", f"tasks_{task_id}")
    ]
    with multi_lock(lattice_dir / "locks", lock_keys):
        return _inspect_task_authority_unlocked(
            lattice_dir, skip_task_ids=skip_task_ids
        )


# ---------------------------------------------------------------------------
# lattice doctor
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--fix", is_flag=True, help="Attempt to fix detected issues.")
@click.option("--json", "output_json", is_flag=True, help="Output structured JSON.")
def doctor(fix: bool, output_json: bool) -> None:
    """Check project integrity and report issues."""
    is_json = output_json
    lattice_dir = require_root(is_json)

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
    if config_path.exists():
        json_files.append(config_path)

    json_ok = True
    for jf in json_files:
        try:
            data = json.loads(jf.read_text())
            # Store snapshot data for later checks
            if jf.parent.name in ("tasks",) and jf.suffix == ".json":
                snapshots[jf.stem] = data
                known_task_ids.add(jf.stem)
            elif jf.parent.parent.name == "archive" and jf.parent.name == "tasks":
                snapshots[jf.stem] = data
                known_task_ids.add(jf.stem)
            elif jf.parent.name == "meta":
                known_artifact_ids.add(jf.stem)
        except json.JSONDecodeError as e:
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

    # A corrupt cache is replay-repairable when strict authority succeeds.
    for finding in findings:
        if (
            finding["check"] == "json_parse"
            and finding.get("task_id") in authorities
        ):
            finding["level"] = "warning"
            finding["message"] += " (snapshot cache is rebuildable from valid authority)"

    drift_ok = not any(
        finding["check"] in {"snapshot_drift", "placement_drift"}
        for finding in findings
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
    config = load_project_config(lattice_dir)
    has_project_code = bool(config.get("project_code"))
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

    if ids_json_path.exists():
        id_index = load_id_index(lattice_dir)
        id_map = id_index.get("map", {})
        next_seqs = id_index.get("next_seqs", {})

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

        # Check: every authoritative creation short ID has the exact mapping.
        for task_id_key, authority in authorities.items():
            snap = authority.snapshot
            snap_short_id = snap.get("short_id")
            if snap_short_id:
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

        # Check: no duplicate short IDs across snapshots
        seen_short_ids: dict[str, str] = {}
        for task_id_key, authority in authorities.items():
            snap = authority.snapshot
            snap_short_id = snap.get("short_id")
            if snap_short_id:
                if snap_short_id in seen_short_ids:
                    alias_ok = False
                    findings.append(
                        {
                            "level": "error",
                            "check": "alias_integrity",
                            "message": (
                                f"Duplicate short ID {snap_short_id}: "
                                f"{seen_short_ids[snap_short_id]} and {task_id_key}"
                            ),
                            "task_id": task_id_key,
                        }
                    )
                seen_short_ids[snap_short_id] = task_id_key

        # Check: per-prefix next_seqs > max assigned per prefix
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
            if max_num >= prefix_next:
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
    # Output
    # -----------------------------------------------------------------
    warnings = sum(1 for f in findings if f["level"] == "warning")
    errors = sum(1 for f in findings if f["level"] == "error")

    if is_json:
        # Strip internal fields from findings
        clean_findings = []
        for f in findings:
            clean = {
                "level": f["level"],
                "check": f["check"],
                "message": f["message"],
                "task_id": f.get("task_id"),
            }
            clean_findings.append(clean)

        click.echo(
            json_envelope(
                True,
                data={
                    "findings": clean_findings,
                    "summary": {
                        "tasks": task_count,
                        "events": event_count,
                        "artifacts": artifact_count,
                        "resources": resource_count,
                        "warnings": warnings,
                        "errors": errors,
                    },
                },
            )
        )
    else:
        click.echo(
            f"Checking {task_count} tasks, {event_count} events, {artifact_count} artifacts..."
        )

        # Report each check category
        if json_ok:
            click.echo("\u2713 All JSON files valid")
        else:
            for f in findings:
                if f["check"] == "json_parse":
                    click.echo(f"\u26a0 {f['message']}")

        if jsonl_ok:
            click.echo("\u2713 All JSONL files valid")
        else:
            for f in findings:
                if f["check"] == "jsonl_parse":
                    click.echo(f"\u26a0 {f['message']}")

        if drift_ok:
            click.echo("\u2713 All snapshots consistent with event logs")
        else:
            for f in findings:
                if f["check"] in {"snapshot_drift", "placement_drift"}:
                    click.echo(f"\u26a0 {f['message']}")

        for f in findings:
            if f["check"] == "authoritative_log":
                click.echo(f"\u26a0 {f['message']}")

        if refs_ok:
            click.echo("\u2713 All relationship targets exist")
        else:
            for f in findings:
                if f["check"] == "missing_reference":
                    click.echo(f"\u26a0 {f['message']}")

        if artifacts_ok:
            click.echo("\u2713 All artifact references valid")
        else:
            for f in findings:
                if f["check"] == "missing_artifact":
                    click.echo(f"\u26a0 {f['message']}")

        if selflink_ok:
            click.echo("\u2713 No self-links")
        else:
            for f in findings:
                if f["check"] == "self_link":
                    click.echo(f"\u26a0 {f['message']}")

        if dupes_ok:
            click.echo("\u2713 No duplicate edges")
        else:
            for f in findings:
                if f["check"] == "duplicate_edge":
                    click.echo(f"\u26a0 {f['message']}")

        if ids_ok:
            click.echo("\u2713 All IDs well-formed")
        else:
            for f in findings:
                if f["check"] == "malformed_id":
                    click.echo(f"\u26a0 {f['message']}")

        if global_ok:
            click.echo("\u2713 Lifecycle log consistent")
        else:
            for f in findings:
                if f["check"] == "global_log_consistency":
                    click.echo(f"\u26a0 {f['message']}")

        if alias_ok:
            click.echo("\u2713 Short ID aliases consistent")
        else:
            for f in findings:
                if f["check"] == "alias_integrity":
                    click.echo(f"\u26a0 {f['message']}")

        if resource_count > 0:
            if resource_ok:
                click.echo(f"\u2713 All {resource_count} resource(s) consistent")
            else:
                for f in findings:
                    if f["check"] == "resource_integrity":
                        click.echo(f"\u26a0 {f['message']}")

        total = warnings + errors
        if total == 0:
            click.echo("\nNo issues found.")
        else:
            parts = []
            if warnings:
                parts.append(f"{warnings} warning{'s' if warnings != 1 else ''}")
            if errors:
                parts.append(f"{errors} error{'s' if errors != 1 else ''}")
            click.echo(f"\n{' and '.join(parts)} found.")

    # Exit with non-zero if there are errors (not warnings)
    if errors > 0:
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# lattice rebuild
# ---------------------------------------------------------------------------


def _rebuild_task(lattice_dir: Path, task_id: str) -> dict:
    """Strictly replay and reconcile one task into event-selected placement."""
    result = mutate_task(
        lattice_dir,
        task_id,
        lambda _context: TaskMutationDecision(idempotent=True),
        source="either",
    )
    return result.snapshot


def _rebuild_lifecycle_log(lattice_dir: Path) -> list[str]:
    """Rebuild _lifecycle.jsonl from all per-task event logs.

    Returns list of rebuilt task IDs (for reporting).
    """
    lifecycle_by_id: dict[str, dict] = {}

    # Scan all per-task event logs (active + archive)
    for directory in [
        lattice_dir / "events",
        lattice_dir / "archive" / "events",
    ]:
        if not directory.is_dir():
            continue
        for jsonl_file in sorted(directory.glob("*.jsonl")):
            if jsonl_file.name == "_lifecycle.jsonl":
                continue
            for line in jsonl_file.read_text().splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    event = json.loads(stripped)
                except json.JSONDecodeError:
                    continue  # skip malformed lines during rebuild
                if event.get("type") in LIFECYCLE_EVENT_TYPES:
                    event_id = event.get("id")
                    existing = lifecycle_by_id.get(event_id)
                    if existing is not None and existing != event:
                        raise AuthoritativeLogError(
                            f"conflicting lifecycle event {event_id}; manual recovery required",
                            path=jsonl_file,
                        )
                    lifecycle_by_id[event_id] = event

    # Sort by (ts, id) for deterministic ordering
    all_lifecycle_events = list(lifecycle_by_id.values())
    all_lifecycle_events.sort(key=lambda e: (e.get("ts", ""), e.get("id", "")))

    # Write atomically
    lifecycle_path = lattice_dir / "events" / "_lifecycle.jsonl"
    content = "".join(serialize_event(e) for e in all_lifecycle_events)

    locks_dir = lattice_dir / "locks"
    with multi_lock(locks_dir, ["events__lifecycle"]):
        atomic_write(lifecycle_path, content)

    return [e.get("task_id", "") for e in all_lifecycle_events]


def _rebuild_id_index(lattice_dir: Path) -> None:
    """Rebuild ``ids.json`` from strict authoritative task creation replay."""
    id_map: dict[str, str] = {}
    max_seq: dict[str, int] = {}  # per-prefix max seq
    task_ids = sorted(_collect_task_ids(lattice_dir))
    lock_keys = [
        "ids_json",
        *(
            key
            for task_id in task_ids
            for key in (f"events_{task_id}", f"tasks_{task_id}")
        ),
    ]

    with multi_lock(lattice_dir / "locks", lock_keys):
        for task_id in task_ids:
            event_exists = any(
                _task_paths(lattice_dir, task_id, archived)["event"].exists()
                for archived in (False, True)
            )
            if not event_exists:
                continue
            authority = resolve_task_authority(lattice_dir, task_id)
            assert authority is not None
            short_id = authority.snapshot.get("short_id")
            if short_id and validate_short_id(short_id):
                existing = id_map.get(short_id)
                if existing is not None and existing != task_id:
                    raise AuthoritativeLogError(
                        f"duplicate authoritative short ID {short_id}: "
                        f"{existing} and {task_id}"
                    )
                id_map[short_id] = task_id
                prefix, num = parse_short_id(short_id)
                max_seq[prefix] = max(max_seq.get(prefix, 0), num)

        # Preserve valid reservation high-water marks while replacing the map
        # from immutable creation authority.
        current = load_id_index(lattice_dir)
        current_next = current.get("next_seqs", {})
        next_seqs: dict[str, int] = {
            prefix: value
            for prefix, value in current_next.items()
            if isinstance(prefix, str) and isinstance(value, int) and value >= 1
        }
        for prefix, max_num in max_seq.items():
            next_seqs[prefix] = max(next_seqs.get(prefix, 1), max_num + 1)

        index = {
            "schema_version": 2,
            "next_seqs": next_seqs,
            "map": id_map,
        }
        save_id_index(lattice_dir, index)


def _rebuild_resource(lattice_dir: Path, resource_id: str) -> dict:
    """Rebuild a single resource snapshot from its event log.

    Returns the rebuilt snapshot dict.
    Raises FileNotFoundError if the event log does not exist.
    """
    from lattice.core.resources import apply_resource_event_to_snapshot

    event_path = lattice_dir / "events" / f"{resource_id}.jsonl"
    if not event_path.exists():
        raise FileNotFoundError(f"No event log found for resource {resource_id}")

    events: list[dict] = []
    for line in event_path.read_text().splitlines():
        stripped = line.strip()
        if stripped:
            events.append(json.loads(stripped))

    if not events:
        raise ValueError(f"Event log for resource {resource_id} is empty")

    snapshot: dict | None = None
    for event in events:
        snapshot = apply_resource_event_to_snapshot(snapshot, event)

    assert snapshot is not None
    return snapshot


@cli.command()
@click.argument("task_id", required=False, default=None)
@click.option("--all", "rebuild_all", is_flag=True, help="Rebuild all tasks.")
@click.option("--json", "output_json", is_flag=True, help="Output structured JSON.")
def rebuild(task_id: str | None, rebuild_all: bool, output_json: bool) -> None:
    """Rebuild task snapshots from event logs."""
    is_json = output_json
    lattice_dir = require_root(is_json)

    # Validate arguments: exactly one of task_id or --all
    if task_id is not None and rebuild_all:
        output_error(
            "Cannot specify both a task ID and --all.",
            "VALIDATION_ERROR",
            is_json,
        )
    if task_id is None and not rebuild_all:
        output_error(
            "Provide a task ID or use --all.",
            "VALIDATION_ERROR",
            is_json,
        )

    if rebuild_all:
        # Rebuild all tasks (active + archived)
        rebuilt_ids: list[str] = []
        failures: list[str] = []
        for tid in sorted(_collect_task_ids(lattice_dir)):
            if not any(
                _task_paths(lattice_dir, tid, archived)["event"].exists()
                for archived in (False, True)
            ):
                failures.append(f"{tid}: no authoritative event log")
                continue
            try:
                _rebuild_task(lattice_dir, tid)
            except (AuthoritativeLogError, ValueError, json.JSONDecodeError) as exc:
                failures.append(f"{tid}: {exc}")
                continue
            rebuilt_ids.append(tid)

        if failures:
            output_error(
                "Rebuild refused malformed or divergent authority: "
                + "; ".join(failures),
                "REBUILD_ERROR",
                is_json,
            )

        # Rebuild lifecycle log
        _rebuild_lifecycle_log(lattice_dir)

        # Rebuild ids.json from snapshots
        _rebuild_id_index(lattice_dir)

        # Rebuild resource snapshots
        rebuilt_resources: list[str] = []
        resource_event_files = _collect_resource_event_files(lattice_dir)
        for ref in resource_event_files:
            res_id = ref.stem
            try:
                from lattice.core.resources import serialize_resource_snapshot

                res_snapshot = _rebuild_resource(lattice_dir, res_id)
                res_name = res_snapshot.get("name", res_id)
                resource_dir = lattice_dir / "resources" / res_name
                resource_dir.mkdir(parents=True, exist_ok=True)
                snapshot_path = resource_dir / "resource.json"
                locks_dir = lattice_dir / "locks"
                with multi_lock(locks_dir, [f"resources_{res_name}"]):
                    atomic_write(snapshot_path, serialize_resource_snapshot(res_snapshot))
                rebuilt_resources.append(res_name)
            except (FileNotFoundError, ValueError, json.JSONDecodeError) as e:
                if is_json:
                    output_error(str(e), "REBUILD_ERROR", is_json)
                else:
                    click.echo(f"Error rebuilding resource {res_id}: {e}", err=True)

        if is_json:
            click.echo(
                json_envelope(
                    True,
                    data={
                        "rebuilt_tasks": rebuilt_ids,
                        "rebuilt_resources": rebuilt_resources,
                        "global_log_rebuilt": True,
                    },
                )
            )
        else:
            parts = [f"Rebuilt {len(rebuilt_ids)} task{'s' if len(rebuilt_ids) != 1 else ''}"]
            if rebuilt_resources:
                parts.append(
                    f"{len(rebuilt_resources)} resource{'s' if len(rebuilt_resources) != 1 else ''}"
                )
            parts.append("regenerated lifecycle log")
            click.echo(", ".join(parts))
    else:
        # Single task rebuild
        assert task_id is not None
        try:
            _rebuild_task(lattice_dir, task_id)
        except AuthoritativeLogError as exc:
            if (
                "no authoritative event log exists" in str(exc)
                and "for existing snapshot" not in str(exc)
            ):
                output_error(
                    f"No event log found for {task_id}.",
                    "NOT_FOUND",
                    is_json,
                )
            output_error(str(exc), "REBUILD_ERROR", is_json)
        except (ValueError, json.JSONDecodeError) as e:
            output_error(str(e), "REBUILD_ERROR", is_json)

        if is_json:
            click.echo(
                json_envelope(
                    True,
                    data={
                        "rebuilt_tasks": [task_id],
                        "global_log_rebuilt": False,
                    },
                )
            )
        else:
            click.echo(f"Rebuilt {task_id}")
