"""Dashboard-specific fixtures."""

from __future__ import annotations

import json
import socket
import threading
from pathlib import Path

import pytest

from lattice.core.config import default_config, serialize_config
from lattice.core.events import create_event, serialize_event
from lattice.core.ids import generate_artifact_id, generate_task_id
from lattice.core.tasks import apply_event_to_snapshot, serialize_snapshot
from lattice.dashboard.server import create_server
from lattice.storage.fs import atomic_write, ensure_lattice_dirs


def _write_task(lattice_dir: Path, events: list[dict]) -> dict:
    """Apply a sequence of events, write snapshot + event log, return snapshot."""
    snapshot = None
    for ev in events:
        snapshot = apply_event_to_snapshot(snapshot, ev)

    task_id = snapshot["id"]
    atomic_write(lattice_dir / "tasks" / f"{task_id}.json", serialize_snapshot(snapshot))

    event_path = lattice_dir / "events" / f"{task_id}.jsonl"
    with open(event_path, "w", encoding="utf-8") as fh:
        for ev in events:
            fh.write(serialize_event(ev))

    return snapshot


@pytest.fixture()
def populated_lattice_dir(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """Create a .lattice/ directory with realistic test data.

    Returns (lattice_dir, task_ids) where lattice_dir is the .lattice/ path
    and task_ids maps role names to generated IDs.
    """
    root = tmp_path
    ensure_lattice_dirs(root)
    ld = root / ".lattice"

    # Write default config
    atomic_write(ld / "config.json", serialize_config(default_config()))

    # --- Task 1: backlog ---
    t1_id = generate_task_id()
    t1_events = [
        create_event(
            type="task_created",
            task_id=t1_id,
            actor="human:atin",
            data={
                "title": "Fix login redirect",
                "status": "backlog",
                "priority": "high",
                "urgency": "normal",
                "type": "bug",
                "description": "Redirect fails after OAuth.",
                "tags": ["auth", "bug"],
                "assigned_to": None,
                "custom_fields": {},
            },
            ts="2025-01-10T10:00:00Z",
        ),
    ]
    _write_task(ld, t1_events)

    # --- Task 2: in_progress with relationship to task 1, comment, and artifact ---
    t2_id = generate_task_id()
    art_id = generate_artifact_id()
    t2_events = [
        create_event(
            type="task_created",
            task_id=t2_id,
            actor="human:atin",
            data={
                "title": "Update dependencies",
                "status": "backlog",
                "priority": "medium",
                "urgency": "normal",
                "type": "chore",
                "description": None,
                "tags": [],
                "assigned_to": "agent:claude",
                "custom_fields": {},
            },
            ts="2025-01-10T11:00:00Z",
        ),
        create_event(
            type="status_changed",
            task_id=t2_id,
            actor="human:atin",
            data={"from": "backlog", "to": "in_progress"},
            ts="2025-01-10T12:00:00Z",
        ),
        create_event(
            type="relationship_added",
            task_id=t2_id,
            actor="human:atin",
            data={"type": "blocks", "target_task_id": t1_id, "note": "needs new deps"},
            ts="2025-01-10T12:05:00Z",
        ),
        create_event(
            type="comment_added",
            task_id=t2_id,
            actor="agent:claude",
            data={"body": "Starting dependency audit."},
            ts="2025-01-10T12:10:00Z",
        ),
        create_event(
            type="artifact_attached",
            task_id=t2_id,
            actor="human:atin",
            data={"artifact_id": art_id},
            ts="2025-01-10T12:15:00Z",
        ),
    ]
    _write_task(ld, t2_events)

    # Write artifact metadata
    art_meta = {"id": art_id, "title": "dep-report.txt", "type": "text/plain"}
    atomic_write(
        ld / "artifacts" / "meta" / f"{art_id}.json",
        json.dumps(art_meta, sort_keys=True, indent=2) + "\n",
    )

    # --- Task 3: done ---
    t3_id = generate_task_id()
    t3_events = [
        create_event(
            type="task_created",
            task_id=t3_id,
            actor="human:atin",
            data={
                "title": "Write README",
                "status": "backlog",
                "priority": "low",
                "urgency": "low",
                "type": "task",
                "description": None,
                "tags": [],
                "assigned_to": None,
                "custom_fields": {},
            },
            ts="2025-01-10T13:00:00Z",
        ),
        create_event(
            type="status_changed",
            task_id=t3_id,
            actor="human:atin",
            data={"from": "backlog", "to": "done"},
            ts="2025-01-10T14:00:00Z",
        ),
    ]
    _write_task(ld, t3_events)

    # --- Write a notes file for task 1 ---
    (ld / "notes" / f"{t1_id}.md").write_text("# Notes\nSome notes here.\n")

    # --- Archived task ---
    t4_id = generate_task_id()
    t4_events = [
        create_event(
            type="task_created",
            task_id=t4_id,
            actor="human:atin",
            data={
                "title": "Old spike task",
                "status": "done",
                "priority": "low",
                "urgency": "low",
                "type": "spike",
                "description": None,
                "tags": [],
                "assigned_to": None,
                "custom_fields": {},
            },
            ts="2025-01-01T08:00:00Z",
        ),
        create_event(
            type="task_archived",
            task_id=t4_id,
            actor="human:atin",
            data={},
            ts="2025-01-05T08:00:00Z",
        ),
    ]
    archived_snap = None
    for ev in t4_events:
        archived_snap = apply_event_to_snapshot(archived_snap, ev)

    atomic_write(
        ld / "archive" / "tasks" / f"{t4_id}.json",
        serialize_snapshot(archived_snap),
    )
    event_path = ld / "archive" / "events" / f"{t4_id}.jsonl"
    with open(event_path, "w", encoding="utf-8") as fh:
        for ev in t4_events:
            fh.write(serialize_event(ev))

    # Write _lifecycle.jsonl with lifecycle events
    lifecycle_path = ld / "events" / "_lifecycle.jsonl"
    with open(lifecycle_path, "w", encoding="utf-8") as fh:
        for ev in t1_events + [t2_events[0]] + [t3_events[0]]:
            fh.write(serialize_event(ev))

    task_ids = {
        "backlog": t1_id,
        "in_progress": t2_id,
        "done": t3_id,
        "archived": t4_id,
        "artifact": art_id,
    }

    return ld, task_ids


def _get_free_port() -> int:
    """Find an available TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def dashboard_server(populated_lattice_dir: tuple[Path, dict[str, str]]):
    """Start a dashboard server on a random port, yield (base_url, lattice_dir, task_ids)."""
    ld, task_ids = populated_lattice_dir
    port = _get_free_port()
    host = "127.0.0.1"
    server = create_server(ld, host, port)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    base_url = f"http://{host}:{port}"
    yield base_url, ld, task_ids

    server.shutdown()
    server.server_close()
