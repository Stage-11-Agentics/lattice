"""Performance and scale smoke tests.

These tests verify that Lattice operations complete within reasonable time
bounds at moderate scale (100-500 items). They use direct file writes to
set up data quickly, then time CLI operations.
"""

from __future__ import annotations

import json
import time

import pytest
from ulid import ULID

from lattice.core.events import create_event, serialize_event
from lattice.core.tasks import apply_event_to_snapshot, serialize_snapshot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_task_files(lattice_dir, count: int) -> list[str]:
    """Direct-write *count* task snapshots and event logs.

    Returns list of generated task IDs.
    """
    tasks_dir = lattice_dir / "tasks"
    events_dir = lattice_dir / "events"
    task_ids: list[str] = []

    for i in range(count):
        task_id = f"task_{ULID()}"
        event = create_event(
            type="task_created",
            task_id=task_id,
            actor="human:test",
            data={
                "title": f"Scale task {i}",
                "status": "backlog",
                "priority": "medium",
                "type": "task",
            },
        )
        snapshot = apply_event_to_snapshot(None, event)

        (tasks_dir / f"{task_id}.json").write_text(serialize_snapshot(snapshot))
        (events_dir / f"{task_id}.jsonl").write_text(serialize_event(event))

        # Also append to lifecycle log
        lifecycle_path = events_dir / "_lifecycle.jsonl"
        with lifecycle_path.open("a") as f:
            f.write(serialize_event(event))

        task_ids.append(task_id)

    return task_ids


def _create_task_with_events(lattice_dir, event_count: int) -> str:
    """Direct-write a single task with *event_count* events in its log.

    The first event is always task_created; the rest alternate between
    field_updated and comment_added.
    """
    tasks_dir = lattice_dir / "tasks"
    events_dir = lattice_dir / "events"

    task_id = f"task_{ULID()}"
    created = create_event(
        type="task_created",
        task_id=task_id,
        actor="human:test",
        data={
            "title": "Many-event task",
            "status": "backlog",
            "priority": "medium",
            "type": "task",
        },
    )
    snapshot = apply_event_to_snapshot(None, created)
    event_lines = serialize_event(created)

    for i in range(1, event_count):
        if i % 2 == 0:
            ev = create_event(
                type="comment_added",
                task_id=task_id,
                actor="human:test",
                data={"body": f"Comment number {i}"},
            )
        else:
            ev = create_event(
                type="field_updated",
                task_id=task_id,
                actor="human:test",
                data={
                    "field": "title",
                    "from": f"Title v{i - 1}",
                    "to": f"Title v{i}",
                },
            )
        snapshot = apply_event_to_snapshot(snapshot, ev)
        event_lines += serialize_event(ev)

    (tasks_dir / f"{task_id}.json").write_text(serialize_snapshot(snapshot))
    (events_dir / f"{task_id}.jsonl").write_text(event_lines)

    # Lifecycle entry
    lifecycle_path = events_dir / "_lifecycle.jsonl"
    with lifecycle_path.open("a") as f:
        f.write(serialize_event(created))

    return task_id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_list_500_tasks_under_10s(invoke, initialized_root):
    """Listing 500 tasks via CLI should complete in under 10 seconds."""
    lattice_dir = initialized_root / ".lattice"
    _create_task_files(lattice_dir, 500)

    start = time.monotonic()
    result = invoke("list", "--json")
    duration = time.monotonic() - start

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert len(data["data"]) == 500
    assert duration < 10, f"list took {duration:.2f}s (limit: 10s)"


@pytest.mark.slow
def test_rebuild_500_events_under_5s(invoke, initialized_root):
    """Rebuilding a task with 500 events should complete in under 5 seconds."""
    lattice_dir = initialized_root / ".lattice"
    task_id = _create_task_with_events(lattice_dir, 500)

    start = time.monotonic()
    result = invoke("rebuild", task_id, "--json")
    duration = time.monotonic() - start

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert task_id in data["data"]["rebuilt_tasks"]
    assert duration < 5, f"rebuild took {duration:.2f}s (limit: 5s)"


@pytest.mark.slow
def test_doctor_100_tasks_under_5s(invoke, initialized_root):
    """Running doctor on 100 tasks (5 events each) should complete in under 5 seconds."""
    lattice_dir = initialized_root / ".lattice"

    for _ in range(100):
        _create_task_with_events(lattice_dir, 5)

    start = time.monotonic()
    result = invoke("doctor", "--json")
    duration = time.monotonic() - start

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["data"]["summary"]["tasks"] == 100
    assert duration < 5, f"doctor took {duration:.2f}s (limit: 5s)"


@pytest.mark.slow
def test_rebuild_all_100_tasks_under_10s(invoke, initialized_root):
    """Rebuilding all 100 tasks should complete in under 10 seconds."""
    lattice_dir = initialized_root / ".lattice"

    for _ in range(100):
        _create_task_with_events(lattice_dir, 5)

    start = time.monotonic()
    result = invoke("rebuild", "--all", "--json")
    duration = time.monotonic() - start

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert len(data["data"]["rebuilt_tasks"]) == 100
    assert data["data"]["global_log_rebuilt"] is True
    assert duration < 10, f"rebuild --all took {duration:.2f}s (limit: 10s)"
