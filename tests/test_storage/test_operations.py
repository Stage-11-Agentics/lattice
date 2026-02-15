"""Tests for lattice.storage.operations — the shared write path."""

from __future__ import annotations

import json
from pathlib import Path

from lattice.core.config import default_config, serialize_config
from lattice.core.events import create_event
from lattice.core.tasks import apply_event_to_snapshot
from lattice.storage.fs import atomic_write, ensure_lattice_dirs
from lattice.storage.operations import write_task_event


def _setup_lattice(tmp_path: Path) -> Path:
    """Create a minimal .lattice/ directory and return the lattice dir."""
    ensure_lattice_dirs(tmp_path)
    ld = tmp_path / ".lattice"
    atomic_write(ld / "config.json", serialize_config(default_config()))
    return ld


class TestWriteTaskEvent:
    """Verify the canonical write_task_event function."""

    def test_writes_event_and_snapshot(self, tmp_path: Path) -> None:
        ld = _setup_lattice(tmp_path)

        event = create_event(
            type="task_created",
            task_id="task_01AAAAAAAAAAAAAAAAAAAAAAAAAA",
            actor="human:test",
            data={
                "title": "Test",
                "status": "backlog",
                "priority": "medium",
                "type": "task",
            },
        )
        snapshot = apply_event_to_snapshot(None, event)

        write_task_event(ld, "task_01AAAAAAAAAAAAAAAAAAAAAAAAAA", [event], snapshot)

        # Snapshot written
        snap_path = ld / "tasks" / "task_01AAAAAAAAAAAAAAAAAAAAAAAAAA.json"
        assert snap_path.exists()
        snap = json.loads(snap_path.read_text())
        assert snap["title"] == "Test"

        # Event log written
        event_path = ld / "events" / "task_01AAAAAAAAAAAAAAAAAAAAAAAAAA.jsonl"
        assert event_path.exists()
        lines = event_path.read_text().strip().split("\n")
        assert len(lines) == 1
        assert json.loads(lines[0])["type"] == "task_created"

    def test_lifecycle_events_go_to_lifecycle_log(self, tmp_path: Path) -> None:
        ld = _setup_lattice(tmp_path)

        event = create_event(
            type="task_created",
            task_id="task_01BBBBBBBBBBBBBBBBBBBBBBBBBB",
            actor="human:test",
            data={
                "title": "Lifecycle test",
                "status": "backlog",
                "priority": "low",
                "type": "task",
            },
        )
        snapshot = apply_event_to_snapshot(None, event)

        write_task_event(ld, "task_01BBBBBBBBBBBBBBBBBBBBBBBBBB", [event], snapshot)

        lifecycle_path = ld / "events" / "_lifecycle.jsonl"
        content = lifecycle_path.read_text().strip()
        assert content  # not empty
        ev = json.loads(content.split("\n")[-1])
        assert ev["type"] == "task_created"

    def test_non_lifecycle_event_skips_lifecycle_log(self, tmp_path: Path) -> None:
        ld = _setup_lattice(tmp_path)
        task_id = "task_01CCCCCCCCCCCCCCCCCCCCCCCCCC"

        # First create the task
        create_ev = create_event(
            type="task_created",
            task_id=task_id,
            actor="human:test",
            data={
                "title": "Nonlifecycle",
                "status": "backlog",
                "priority": "medium",
                "type": "task",
            },
        )
        snap = apply_event_to_snapshot(None, create_ev)
        write_task_event(ld, task_id, [create_ev], snap)

        lifecycle_before = (ld / "events" / "_lifecycle.jsonl").read_text()

        # Now add a comment (non-lifecycle)
        comment_ev = create_event(
            type="comment_added",
            task_id=task_id,
            actor="human:test",
            data={"body": "test comment"},
        )
        snap2 = apply_event_to_snapshot(snap, comment_ev)
        write_task_event(ld, task_id, [comment_ev], snap2)

        lifecycle_after = (ld / "events" / "_lifecycle.jsonl").read_text()
        assert lifecycle_after == lifecycle_before  # unchanged

    def test_multiple_events_in_one_call(self, tmp_path: Path) -> None:
        ld = _setup_lattice(tmp_path)
        task_id = "task_01DDDDDDDDDDDDDDDDDDDDDDDDDD"

        create_ev = create_event(
            type="task_created",
            task_id=task_id,
            actor="human:test",
            data={
                "title": "Multi",
                "status": "backlog",
                "priority": "medium",
                "type": "task",
            },
        )
        snap = apply_event_to_snapshot(None, create_ev)
        write_task_event(ld, task_id, [create_ev], snap)

        # Two field updates in one call
        ev1 = create_event(
            type="field_updated",
            task_id=task_id,
            actor="human:test",
            data={"field": "title", "from": "Multi", "to": "Multi 2"},
        )
        ev2 = create_event(
            type="field_updated",
            task_id=task_id,
            actor="human:test",
            data={"field": "priority", "from": "medium", "to": "high"},
        )
        snap = apply_event_to_snapshot(snap, ev1)
        snap = apply_event_to_snapshot(snap, ev2)
        write_task_event(ld, task_id, [ev1, ev2], snap)

        event_path = ld / "events" / f"{task_id}.jsonl"
        lines = event_path.read_text().strip().split("\n")
        assert len(lines) == 3  # create + 2 field updates
