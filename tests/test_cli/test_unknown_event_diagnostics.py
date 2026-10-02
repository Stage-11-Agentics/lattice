"""Unknown event types stay quiet on routine reads and remain visible to doctor."""

from __future__ import annotations

import json
from pathlib import Path

from lattice.cli import dashboard_cmd as dashboard_module
from lattice.dashboard import api as dashboard_api
from lattice.core.events import create_event, serialize_event
from lattice.core.tasks import apply_event_to_snapshot, serialize_snapshot
from lattice.storage.fs import LATTICE_DIR


def _append_unknown_event(
    lattice_dir: Path, task_id: str, event_type: str, *, archived: bool = False
) -> None:
    location = "archive/" if archived else ""
    event = create_event(
        event_type, task_id, "agent:newer-client", {"detail": "forward-compatible"}
    )
    event_path = lattice_dir / location / "events" / f"{task_id}.jsonl"
    with event_path.open("a", encoding="utf-8") as handle:
        handle.write(serialize_event(event))
    snapshot_path = lattice_dir / location / "tasks" / f"{task_id}.json"
    snapshot = apply_event_to_snapshot(json.loads(snapshot_path.read_text()), event)
    snapshot_path.write_text(serialize_snapshot(snapshot), encoding="utf-8")


def test_unknown_event_is_quiet_for_show_and_list_but_reported_by_doctor(
    initialized_root, create_task, invoke
) -> None:
    task = create_task("Forward-compatible task")
    task_id = task["id"]
    lattice_dir = initialized_root / LATTICE_DIR
    _append_unknown_event(lattice_dir, task_id, "process_started")

    shown = invoke("show", task_id)
    listed = invoke("list")
    assert shown.exit_code == listed.exit_code == 0
    assert shown.stderr == listed.stderr == ""

    reads = (
        invoke("next"),
        invoke("stats"),
        invoke("weather"),
        invoke("plan", "show", task_id),
    )
    assert all(result.exit_code == 0 for result in reads)
    assert all(result.stderr == "" for result in reads)

    doctor = invoke("doctor", "--json")
    findings = json.loads(doctor.output)["data"]["findings"]
    unknown = [finding for finding in findings if finding["check"] == "unknown_event_type"]
    assert len(unknown) == 1
    assert unknown[0]["level"] == "warning"
    assert unknown[0]["task_id"] == task_id
    assert "process_started" in unknown[0]["message"]

    doctor_plain = invoke("doctor")
    assert doctor_plain.exit_code == 0
    assert "unknown event type 'process_started'" in doctor_plain.output


def test_dashboard_read_does_not_warn_when_the_command_exits(
    initialized_root, create_task, invoke, monkeypatch
) -> None:
    task_id = create_task("Dashboard read with future event")["id"]
    lattice_dir = initialized_root / LATTICE_DIR
    _append_unknown_event(lattice_dir, task_id, "process_started")

    def serve(lattice_path, *_args):
        response = dashboard_api.route_get(lattice_path, "/api/tasks")
        assert response.status == 200
        return False

    monkeypatch.setattr(dashboard_module, "_serve", serve)
    exited = invoke("dashboard", "--port", "8879", "--json")

    assert exited.exit_code == 0, exited.output
    assert "Warning:" not in exited.stderr


def test_materializing_write_warns_and_keeps_forward_compatible_mutation(
    initialized_root, create_task, invoke
) -> None:
    task_id = create_task("Forward-compatible write")["id"]
    lattice_dir = initialized_root / LATTICE_DIR
    _append_unknown_event(lattice_dir, task_id, "process_started")

    updated = invoke(
        "update", task_id, "title=Updated after unknown event", "--actor", "human:test"
    )

    assert updated.exit_code == 0, updated.output
    assert updated.stderr.count("Warning:") == 1
    assert task_id in updated.stderr
    assert "process_started" in updated.stderr
    assert "newer or foreign Lattice" in updated.stderr
    assert "upgrade Lattice" in updated.stderr
    assert "lattice doctor" in updated.stderr

    shown = invoke("show", task_id, "--json")
    assert shown.exit_code == 0, shown.output
    assert shown.stderr == ""
    assert json.loads(shown.output)["data"]["title"] == "Updated after unknown event"


def test_archived_foreign_event_can_be_unarchived_with_one_warning(
    initialized_root, create_task, invoke
) -> None:
    task_id = create_task("Archived foreign event")["id"]
    archived = invoke("archive", task_id, "--actor", "human:test")
    assert archived.exit_code == 0, archived.output

    lattice_dir = initialized_root / LATTICE_DIR
    _append_unknown_event(lattice_dir, task_id, "future_archived_event", archived=True)
    unarchived = invoke("unarchive", task_id, "--actor", "human:test")

    assert unarchived.exit_code == 0, unarchived.output
    assert unarchived.stderr.count("Warning:") == 1
    assert task_id in unarchived.stderr
    assert "future_archived_event" in unarchived.stderr
    shown = invoke("show", task_id, "--json")
    assert shown.exit_code == 0, shown.output
    assert shown.stderr == ""


def test_rebuild_aggregates_unknown_task_and_type_pairs_including_archived_tasks(
    initialized_root, create_task, invoke
) -> None:
    active_id = create_task("Active with future events")["id"]
    archived_id = create_task("Archived with future events")["id"]
    archived = invoke("archive", archived_id, "--actor", "human:test")
    assert archived.exit_code == 0, archived.output

    lattice_dir = initialized_root / LATTICE_DIR
    _append_unknown_event(lattice_dir, active_id, "process_started")
    _append_unknown_event(lattice_dir, active_id, "process_completed")
    _append_unknown_event(lattice_dir, archived_id, "future_archived_event", archived=True)

    rebuilt = invoke("rebuild", "--all")

    assert rebuilt.exit_code == 0, rebuilt.output
    assert rebuilt.stderr.count("Warning:") == 1
    assert f"{active_id}: process_completed, process_started" in rebuilt.stderr
    assert f"{archived_id}: future_archived_event" in rebuilt.stderr
    assert "newer or foreign Lattice" in rebuilt.stderr
