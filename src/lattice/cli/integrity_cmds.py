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
from lattice.core.tasks import apply_event_to_snapshot, serialize_snapshot
from lattice.storage.fs import atomic_write
from lattice.storage.locks import multi_lock
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
    task_count = len(task_files)
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
    # Check 3: Snapshot drift
    # -----------------------------------------------------------------
    drift_ok = True
    # Only check active tasks (in tasks/, not archive/tasks/)
    active_tasks_dir = lattice_dir / "tasks"
    for task_id, snap in snapshots.items():
        snap_path = active_tasks_dir / f"{task_id}.json"
        if not snap_path.exists():
            continue  # archived task, skip drift check
        last_event_id = snap.get("last_event_id")
        events = per_task_events.get(task_id, [])
        if events:
            actual_last_id = events[-1].get("id")
            if last_event_id != actual_last_id:
                drift_ok = False
                findings.append(
                    {
                        "level": "warning",
                        "check": "snapshot_drift",
                        "message": (
                            f"Snapshot drift: {task_id} "
                            f"(snapshot last_event_id={last_event_id}, "
                            f"actual last event={actual_last_id})"
                        ),
                        "task_id": task_id,
                    }
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
        for art_ref in snap.get("artifact_refs", []):
            # Handle both old format (str) and new enriched format (dict)
            art_id = art_ref["id"] if isinstance(art_ref, dict) else art_ref
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
    # Build a set of (event_id) from global log
    global_event_ids: set[str] = set()
    for ev in global_events:
        global_event_ids.add(ev.get("id", ""))

    # Every lifecycle event in per-task logs should be in global
    for task_id, events in per_task_events.items():
        for ev in events:
            if ev.get("type") in LIFECYCLE_EVENT_TYPES:
                ev_id = ev.get("id", "")
                if ev_id not in global_event_ids:
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

        # Check: every entry in ids.json.map points to an existing snapshot
        for short_id, target_ulid in id_map.items():
            if target_ulid not in known_task_ids:
                alias_ok = False
                findings.append(
                    {
                        "level": "warning",
                        "check": "alias_integrity",
                        "message": f"ids.json maps {short_id} to non-existent task {target_ulid}",
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

        # Check: every snapshot with short_id has matching entry in ids.json
        for task_id_key, snap in snapshots.items():
            snap_short_id = snap.get("short_id")
            if snap_short_id:
                if snap_short_id not in id_map:
                    alias_ok = False
                    findings.append(
                        {
                            "level": "warning",
                            "check": "alias_integrity",
                            "message": (
                                f"Task {task_id_key} has short_id {snap_short_id} "
                                "but it's missing from ids.json"
                            ),
                            "task_id": task_id_key,
                        }
                    )

        # Check: no duplicate short IDs across snapshots
        seen_short_ids: dict[str, str] = {}
        for task_id_key, snap in snapshots.items():
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

    if fix and not alias_ok:
        _rebuild_id_index(lattice_dir)
        # Re-check after fix
        for f in findings:
            if f["check"] == "alias_integrity":
                f["message"] += " (fixed by rebuilding ids.json)"

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
                if f["check"] == "snapshot_drift":
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
    """Rebuild a single task snapshot from its event log.

    Returns the rebuilt snapshot dict.
    Raises FileNotFoundError if the event log does not exist.
    """
    # Check both active and archive locations
    event_path = lattice_dir / "events" / f"{task_id}.jsonl"
    if not event_path.exists():
        event_path = lattice_dir / "archive" / "events" / f"{task_id}.jsonl"
    if not event_path.exists():
        raise FileNotFoundError(f"No event log found for {task_id}")

    # Parse events
    events: list[dict] = []
    for line in event_path.read_text().splitlines():
        stripped = line.strip()
        if stripped:
            events.append(json.loads(stripped))

    if not events:
        raise ValueError(f"Event log for {task_id} is empty")

    # Replay events
    snapshot: dict | None = None
    for event in events:
        snapshot = apply_event_to_snapshot(snapshot, event)

    assert snapshot is not None
    return snapshot


def _rebuild_lifecycle_log(lattice_dir: Path) -> list[str]:
    """Rebuild _lifecycle.jsonl from all per-task event logs.

    Returns list of rebuilt task IDs (for reporting).
    """
    all_lifecycle_events: list[dict] = []

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
                    all_lifecycle_events.append(event)

    # Sort by (ts, id) for deterministic ordering
    all_lifecycle_events.sort(key=lambda e: (e.get("ts", ""), e.get("id", "")))

    # Write atomically
    lifecycle_path = lattice_dir / "events" / "_lifecycle.jsonl"
    content = "".join(serialize_event(e) for e in all_lifecycle_events)

    locks_dir = lattice_dir / "locks"
    with multi_lock(locks_dir, ["events__lifecycle"]):
        atomic_write(lifecycle_path, content)

    return [e.get("task_id", "") for e in all_lifecycle_events]


def _rebuild_id_index(lattice_dir: Path) -> None:
    """Rebuild ``ids.json`` from all task snapshots (active + archived)."""
    id_map: dict[str, str] = {}
    max_seq: dict[str, int] = {}  # per-prefix max seq

    for directory in [lattice_dir / "tasks", lattice_dir / "archive" / "tasks"]:
        if not directory.is_dir():
            continue
        for snap_file in sorted(directory.glob("*.json")):
            try:
                snap = json.loads(snap_file.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            short_id = snap.get("short_id")
            if short_id and validate_short_id(short_id):
                task_ulid = snap.get("id", snap_file.stem)
                id_map[short_id] = task_ulid
                prefix, num = parse_short_id(short_id)
                if prefix not in max_seq or num > max_seq[prefix]:
                    max_seq[prefix] = num

    # Compute per-prefix next_seqs (v2 schema)
    next_seqs: dict[str, int] = {}
    for prefix, max_num in max_seq.items():
        next_seqs[prefix] = max_num + 1

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

        # Collect event files from both active and archive directories
        event_dirs = [
            (lattice_dir / "events", lattice_dir / "tasks"),
            (lattice_dir / "archive" / "events", lattice_dir / "archive" / "tasks"),
        ]

        for event_dir, target_tasks_dir in event_dirs:
            if not event_dir.is_dir():
                continue
            for jsonl_file in sorted(event_dir.glob("*.jsonl")):
                if jsonl_file.name == "_lifecycle.jsonl":
                    continue
                # Skip resource event files (handled separately)
                if jsonl_file.stem.startswith("res_"):
                    continue
                tid = jsonl_file.stem
                try:
                    snapshot = _rebuild_task(lattice_dir, tid)
                except (FileNotFoundError, ValueError, json.JSONDecodeError) as e:
                    if is_json:
                        output_error(str(e), "REBUILD_ERROR", is_json)
                    else:
                        click.echo(f"Error rebuilding {tid}: {e}", err=True)
                    continue

                # Write snapshot to the correct location (active or archive)
                snapshot_path = target_tasks_dir / f"{tid}.json"
                locks_dir = lattice_dir / "locks"
                with multi_lock(locks_dir, [f"tasks_{tid}"]):
                    atomic_write(snapshot_path, serialize_snapshot(snapshot))
                rebuilt_ids.append(tid)

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
                parts.append(f"{len(rebuilt_resources)} resource{'s' if len(rebuilt_resources) != 1 else ''}")
            parts.append("regenerated lifecycle log")
            click.echo(", ".join(parts))
    else:
        # Single task rebuild
        assert task_id is not None
        try:
            snapshot = _rebuild_task(lattice_dir, task_id)
        except FileNotFoundError:
            output_error(
                f"No event log found for {task_id}.",
                "NOT_FOUND",
                is_json,
            )
        except (ValueError, json.JSONDecodeError) as e:
            output_error(str(e), "REBUILD_ERROR", is_json)

        # Determine target path (active or archive)
        snapshot_path = lattice_dir / "tasks" / f"{task_id}.json"
        archive_path = lattice_dir / "archive" / "tasks" / f"{task_id}.json"
        if archive_path.exists() and not snapshot_path.exists():
            snapshot_path = archive_path

        locks_dir = lattice_dir / "locks"
        with multi_lock(locks_dir, [f"tasks_{task_id}"]):
            atomic_write(snapshot_path, serialize_snapshot(snapshot))

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
