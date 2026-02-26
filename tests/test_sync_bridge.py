"""Tests for sync/bridge.py — bidirectional event↔CRDT translation."""

from __future__ import annotations

import json

import pytest

automerge = pytest.importorskip("automerge")

from lattice.core.config import default_config, serialize_config  # noqa: E402
from lattice.core.events import create_event  # noqa: E402
from lattice.core.ids import generate_task_id  # noqa: E402
from lattice.core.tasks import apply_event_to_snapshot  # noqa: E402
from lattice.storage.fs import atomic_write, ensure_lattice_dirs  # noqa: E402
from lattice.storage.operations import write_task_event  # noqa: E402
from lattice.sync.bridge import SyncBridge  # noqa: E402
from lattice.sync.documents import automerge_to_snapshot_fields  # noqa: E402
from lattice.sync.store import AutomergeStore  # noqa: E402


@pytest.fixture
def lattice_env(tmp_path):
    """Set up a minimal Lattice instance for testing."""
    ensure_lattice_dirs(tmp_path)
    lattice_dir = tmp_path / ".lattice"
    config = dict(default_config())
    atomic_write(lattice_dir / "config.json", serialize_config(config))
    sync_dir = lattice_dir / "sync"
    store = AutomergeStore(sync_dir)
    bridge = SyncBridge(lattice_dir, store, config)
    return lattice_dir, store, bridge, config


def _create_task(lattice_dir, config, title="Test Task"):
    """Create a task and return (task_id, snapshot)."""
    task_id = generate_task_id()
    ev = create_event(
        type="task_created",
        task_id=task_id,
        actor="human:test",
        data={
            "title": title,
            "status": "backlog",
            "type": "task",
            "priority": "medium",
            "tags": ["test"],
        },
    )
    snapshot = apply_event_to_snapshot(None, ev)
    write_task_event(lattice_dir, task_id, [ev], snapshot, config)
    return task_id, snapshot


class TestBootstrap:
    def test_bootstrap_creates_crdt_doc(self, lattice_env):
        lattice_dir, store, bridge, config = lattice_env
        task_id, snapshot = _create_task(lattice_dir, config)

        bridge.bootstrap_task(task_id)

        assert store.has(task_id)
        doc = store.get_or_create(task_id)
        fields = automerge_to_snapshot_fields(doc)
        assert fields["title"] == "Test Task"
        assert fields["status"] == "backlog"


class TestLocalWriteToCRDT:
    def test_status_change_updates_crdt(self, lattice_env):
        lattice_dir, store, bridge, config = lattice_env
        task_id, snapshot = _create_task(lattice_dir, config)
        bridge.bootstrap_task(task_id)

        # Simulate a status change
        ev = create_event(
            type="status_changed",
            task_id=task_id,
            actor="human:test",
            data={"from": "backlog", "to": "in_progress"},
        )
        new_snapshot = apply_event_to_snapshot(snapshot, ev)
        bridge.on_local_write(task_id, [ev], new_snapshot)

        doc = store.get_or_create(task_id)
        fields = automerge_to_snapshot_fields(doc)
        assert fields["status"] == "in_progress"

    def test_assignment_change_updates_crdt(self, lattice_env):
        lattice_dir, store, bridge, config = lattice_env
        task_id, snapshot = _create_task(lattice_dir, config)
        bridge.bootstrap_task(task_id)

        ev = create_event(
            type="assignment_changed",
            task_id=task_id,
            actor="human:test",
            data={"from": None, "to": "agent:claude"},
        )
        new_snapshot = apply_event_to_snapshot(snapshot, ev)
        bridge.on_local_write(task_id, [ev], new_snapshot)

        doc = store.get_or_create(task_id)
        fields = automerge_to_snapshot_fields(doc)
        assert fields["assigned_to"] == "agent:claude"


class TestFeedbackLoopGuard:
    def test_remote_write_does_not_loop(self, lattice_env):
        lattice_dir, store, bridge, config = lattice_env
        task_id, snapshot = _create_task(lattice_dir, config)
        bridge.bootstrap_task(task_id)

        # Simulate a remote CRDT change
        old_state = {"status": "backlog", "title": "Test Task"}
        new_state = {"status": "in_progress", "title": "Test Task"}

        # This should set _applying_remote = True during execution
        bridge.on_crdt_change(task_id, old_state, new_state)

        # After completion, guard should be released
        assert not bridge._applying_remote

        # Verify the local snapshot was updated
        snap_path = lattice_dir / "tasks" / f"{task_id}.json"
        updated_snap = json.loads(snap_path.read_text())
        assert updated_snap["status"] == "in_progress"


class TestCRDTChangeToEvents:
    def test_status_change_generates_event(self, lattice_env):
        lattice_dir, store, bridge, config = lattice_env
        task_id, snapshot = _create_task(lattice_dir, config)
        bridge.bootstrap_task(task_id)

        old_state = {"status": "backlog", "title": "Test Task"}
        new_state = {"status": "review", "title": "Test Task"}

        bridge.on_crdt_change(task_id, old_state, new_state)

        # Verify event was written
        event_path = lattice_dir / "events" / f"{task_id}.jsonl"
        events = [json.loads(line) for line in event_path.read_text().strip().split("\n")]
        status_events = [e for e in events if e["type"] == "status_changed"]
        assert len(status_events) >= 1
        last = status_events[-1]
        assert last["data"]["to"] == "review"
        assert last.get("provenance", {}).get("triggered_by") == "crdt_sync"

    def test_title_change_generates_field_updated(self, lattice_env):
        lattice_dir, store, bridge, config = lattice_env
        task_id, snapshot = _create_task(lattice_dir, config)
        bridge.bootstrap_task(task_id)

        old_state = {"status": "backlog", "title": "Test Task"}
        new_state = {"status": "backlog", "title": "Updated Title"}

        bridge.on_crdt_change(task_id, old_state, new_state)

        event_path = lattice_dir / "events" / f"{task_id}.jsonl"
        events = [json.loads(line) for line in event_path.read_text().strip().split("\n")]
        field_events = [e for e in events if e["type"] == "field_updated"]
        assert len(field_events) >= 1
        last = field_events[-1]
        assert last["data"]["field"] == "title"
        assert last["data"]["to"] == "Updated Title"
