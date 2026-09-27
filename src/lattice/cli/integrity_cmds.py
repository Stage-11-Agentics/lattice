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
from lattice.cli.maintenance import (
    maintenance_gate,
    offline_maintenance_option,
    refuse_on_hosted_checkout,
)
from lattice.core.config import configured_event_prefix
from lattice.core.errors import OpError
from lattice.core.events import LIFECYCLE_EVENT_TYPES, serialize_event
from lattice.storage.fs import atomic_write, ensure_dir
from lattice.storage.integrity import (
    _build_rebuilt_id_index,
    _collect_resource_event_files,
    _collect_task_ids,
    _require_valid_short_ids,
    _task_paths,
    check_board,
    inspect_task_authority,
    repair_task_derived_files,
)
from lattice.storage.locks import multi_lock
from lattice.storage.operations import (
    AuthoritativeLogError,
    ResolvedTaskAuthority,
    TaskMutationDecision,
    _load_strict_id_index,
    mutate_task,
    resolve_task_authority,
)
from lattice.storage.ownership import board_state
from lattice.storage.short_ids import max_observed_short_ids, save_id_index

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


__all__ = ["inspect_task_authority"]  # re-exported for lattice.mcp.tools


# ---------------------------------------------------------------------------
# lattice doctor
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--fix", is_flag=True, help="Attempt to fix detected issues.")
@click.option("--json", "output_json", is_flag=True, help="Output structured JSON.")
@offline_maintenance_option
def doctor(fix: bool, output_json: bool, offline_maintenance: bool) -> None:
    """Check project integrity and report issues."""
    is_json = output_json
    if fix:
        refuse_on_hosted_checkout("doctor --fix", is_json)
    lattice_dir = require_root(is_json)
    if fix or offline_maintenance:
        maintenance_gate(
            lattice_dir, "doctor --fix" if fix else "doctor", is_json, offline_maintenance
        )
    if board_state(lattice_dir) == "cache":
        _doctor_cache(lattice_dir, is_json)
        return
    _doctor_report(lattice_dir, fix, is_json)


def _doctor_cache(lattice_dir: Path, is_json: bool) -> None:
    """Doctor on a hosted cache (SPEC §9.6).

    Catches up and fetches the server's manifest outside the cache's read
    lock, then runs every check, the manifest comparison included, under it,
    so no sync can change the tree between an enumeration and its reads. Only
    an unreachable or busy server becomes a warning; every other remote
    failure is an error with its own code.
    """
    from lattice.remote.cache import cache_check

    try:
        with cache_check(lattice_dir.parent) as cache_findings:
            _doctor_report(lattice_dir, False, is_json, cache_findings=cache_findings)
    except OpError as exc:
        output_error(exc.message, exc.code, is_json)


def _doctor_report(
    lattice_dir: Path, fix: bool, is_json: bool, *, cache_findings: list[dict] | None = None
) -> None:
    """Run every check on *lattice_dir*, print the report, and exit 1 on errors.

    ``cache_findings``: on a hosted cache, the comparison with the server's
    manifest, reported as one more check.
    """
    report = check_board(lattice_dir, fix=fix)
    findings = report.findings
    task_count, event_count = report.task_count, report.event_count
    artifact_count, resource_count = report.artifact_count, report.resource_count
    json_ok, jsonl_ok, drift_ok = report.json_ok, report.jsonl_ok, report.drift_ok
    refs_ok, artifacts_ok, selflink_ok = report.refs_ok, report.artifacts_ok, report.selflink_ok
    dupes_ok, ids_ok, global_ok = report.dupes_ok, report.ids_ok, report.global_ok
    alias_ok, resource_ok = report.alias_ok, report.resource_ok

    # -----------------------------------------------------------------
    # Check 12: a hosted cache against the server's manifest (SPEC §9.6)
    # -----------------------------------------------------------------
    cache_checked = cache_findings is not None
    if cache_findings:
        findings.extend(cache_findings)

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
                if f["check"] in ("json_parse", "config"):
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

        for f in findings:
            if f["check"] == "missing_task_file":
                click.echo(f"\u26a0 {f['message']}")

        if cache_checked:
            cache_findings = [f for f in findings if f["check"].startswith("cache_")]
            if not cache_findings:
                click.echo("\u2713 Cache matches the server")
            for f in cache_findings:
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
        run_hooks=False,
        allow_tombstoned=True,
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
    event_prefix = configured_event_prefix(load_project_config(lattice_dir))
    task_ids = sorted(_collect_task_ids(lattice_dir))
    lock_keys = [
        "ids_json",
        *(key for task_id in task_ids for key in (f"events_{task_id}", f"tasks_{task_id}")),
    ]

    with multi_lock(lattice_dir / "locks", lock_keys):
        current = _load_strict_id_index(lattice_dir)
        authorities: dict[str, ResolvedTaskAuthority] = {}
        for task_id in task_ids:
            event_exists = any(
                _task_paths(lattice_dir, task_id, archived)["event"].exists()
                for archived in (False, True)
            )
            if not event_exists:
                continue
            authority = resolve_task_authority(lattice_dir, task_id)
            assert authority is not None
            authorities[task_id] = authority
        validated = _require_valid_short_ids(authorities, event_prefix)
        save_id_index(
            lattice_dir,
            _build_rebuilt_id_index(current, validated, max_observed_short_ids(lattice_dir)),
        )


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
@offline_maintenance_option
def rebuild(
    task_id: str | None, rebuild_all: bool, output_json: bool, offline_maintenance: bool
) -> None:
    """Rebuild task snapshots from event logs."""
    is_json = output_json
    refuse_on_hosted_checkout("rebuild", is_json)
    lattice_dir = require_root(is_json)
    maintenance_gate(lattice_dir, "rebuild", is_json, offline_maintenance)

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
        try:
            rebuilt_ids = repair_task_derived_files(lattice_dir, reconcile_placement=True)
        except (AuthoritativeLogError, ValueError, json.JSONDecodeError) as exc:
            output_error(
                f"Rebuild refused malformed or divergent authority: {exc}",
                "REBUILD_ERROR",
                is_json,
            )

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
                ensure_dir(resource_dir)
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
            if "no authoritative event log exists" in str(
                exc
            ) and "for existing snapshot" not in str(exc):
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
