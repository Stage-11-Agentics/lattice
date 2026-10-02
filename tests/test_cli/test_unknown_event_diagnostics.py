"""Unknown event types stay quiet on routine reads and remain visible to doctor."""

from __future__ import annotations

import json

from lattice.core.events import create_event, serialize_event
from lattice.core.tasks import apply_event_to_snapshot, serialize_snapshot
from lattice.storage.fs import LATTICE_DIR


def test_unknown_event_is_quiet_for_show_and_list_but_reported_by_doctor(
    initialized_root, create_task, invoke
) -> None:
    task = create_task("Forward-compatible task")
    task_id = task["id"]
    event = create_event(
        "process_started", task_id, "agent:older-client", {"process_id": "proc-1"}
    )
    lattice_dir = initialized_root / LATTICE_DIR
    with (lattice_dir / "events" / f"{task_id}.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(serialize_event(event))
    snapshot_path = lattice_dir / "tasks" / f"{task_id}.json"
    snapshot = apply_event_to_snapshot(json.loads(snapshot_path.read_text()), event)
    snapshot_path.write_text(serialize_snapshot(snapshot), encoding="utf-8")

    shown = invoke("show", task_id)
    listed = invoke("list")
    assert shown.exit_code == listed.exit_code == 0
    assert shown.stderr == listed.stderr == ""

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
