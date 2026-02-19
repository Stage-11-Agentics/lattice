"""Tests for rebuild determinism: verify rebuild produces byte-identical output."""

from __future__ import annotations

import json


class TestRebuildDeterminism:
    """Verify that rebuild from events produces byte-identical snapshots."""

    def test_single_task_byte_identical_rebuild(self, invoke, create_task, initialized_root):
        """Create task + change status + update field. Rebuild must be byte-equal."""
        task = create_task("Rebuild determinism test")
        task_id = task["id"]

        # Add some operations to build up event history
        invoke("status", task_id, "in_planning", "--actor", "human:test")
        invoke("update", task_id, "priority=high", "--actor", "human:test")

        # Read snapshot bytes before rebuild
        lattice_dir = initialized_root / ".lattice"
        snap_path = lattice_dir / "tasks" / f"{task_id}.json"
        before = snap_path.read_bytes()

        # Rebuild
        result = invoke("rebuild", task_id)
        assert result.exit_code == 0

        # Read after rebuild
        after = snap_path.read_bytes()
        assert before == after

    def test_double_rebuild_idempotent(self, invoke, create_task, initialized_root):
        """Rebuild twice in a row must produce identical bytes each time."""
        task = create_task("Double rebuild test")
        task_id = task["id"]

        # Build up some event history
        invoke("status", task_id, "in_planning", "--actor", "human:test")
        invoke("comment", task_id, "A comment", "--actor", "human:test")
        invoke("update", task_id, "priority=high", "--actor", "human:test")

        lattice_dir = initialized_root / ".lattice"
        snap_path = lattice_dir / "tasks" / f"{task_id}.json"

        # First rebuild
        result = invoke("rebuild", task_id)
        assert result.exit_code == 0
        first = snap_path.read_bytes()

        # Second rebuild
        result = invoke("rebuild", task_id)
        assert result.exit_code == 0
        second = snap_path.read_bytes()

        assert first == second

    def test_rebuild_all_idempotent(self, invoke, create_task, initialized_root):
        """Create 3 tasks. Rebuild --all twice. All snapshots must be identical."""
        tasks = [create_task(f"All idempotent {i}") for i in range(3)]

        # Add some operations to each task
        for task in tasks:
            invoke("status", task["id"], "in_planning", "--actor", "human:test")

        lattice_dir = initialized_root / ".lattice"

        # First rebuild --all
        result = invoke("rebuild", "--all")
        assert result.exit_code == 0

        first_snapshots = {}
        for task in tasks:
            snap_path = lattice_dir / "tasks" / f"{task['id']}.json"
            first_snapshots[task["id"]] = snap_path.read_bytes()

        # Second rebuild --all
        result = invoke("rebuild", "--all")
        assert result.exit_code == 0

        for task in tasks:
            snap_path = lattice_dir / "tasks" / f"{task['id']}.json"
            second = snap_path.read_bytes()
            assert first_snapshots[task["id"]] == second, (
                f"Snapshot for {task['id']} differs between rebuilds"
            )

    def test_lifecycle_log_deterministic(self, invoke, create_task, initialized_root):
        """Lifecycle log must be byte-identical across successive rebuild --all."""
        create_task("Lifecycle determinism 1")
        create_task("Lifecycle determinism 2")
        create_task("Lifecycle determinism 3")

        lattice_dir = initialized_root / ".lattice"
        lifecycle_path = lattice_dir / "events" / "_lifecycle.jsonl"

        # First rebuild --all
        result = invoke("rebuild", "--all")
        assert result.exit_code == 0
        first_lifecycle = lifecycle_path.read_bytes()

        # Second rebuild --all
        result = invoke("rebuild", "--all")
        assert result.exit_code == 0
        second_lifecycle = lifecycle_path.read_bytes()

        assert first_lifecycle == second_lifecycle

    def test_complex_event_history_rebuild(self, invoke, create_task, initialized_root):
        """Create task with many operations, rebuild, verify each field."""
        task_a = create_task("Complex history task")
        task_b = create_task("Relationship target")
        task_a_id = task_a["id"]
        task_b_id = task_b["id"]

        # Build up complex event history on task_a
        invoke("status", task_a_id, "in_planning", "--actor", "human:test")
        invoke("assign", task_a_id, "agent:claude", "--actor", "human:test")
        invoke("comment", task_a_id, "Working on this", "--actor", "agent:claude")
        invoke("link", task_a_id, "blocks", task_b_id, "--actor", "human:test")
        invoke(
            "update",
            task_a_id,
            "custom_fields.effort=large",
            "--actor",
            "human:test",
        )
        invoke("update", task_a_id, "priority=high", "--actor", "human:test")

        lattice_dir = initialized_root / ".lattice"
        snap_path = lattice_dir / "tasks" / f"{task_a_id}.json"

        # Read snapshot before rebuild
        before_bytes = snap_path.read_bytes()

        # Rebuild
        result = invoke("rebuild", task_a_id)
        assert result.exit_code == 0

        # Read after rebuild
        after_bytes = snap_path.read_bytes()

        # Byte-level equality
        assert before_bytes == after_bytes

        # Also verify individual fields are correct
        after = json.loads(after_bytes)
        assert after["status"] == "in_planning"
        assert after["assigned_to"] == "agent:claude"
        assert after["priority"] == "high"
        assert after["custom_fields"]["effort"] == "large"
        assert len(after["relationships_out"]) == 1
        assert after["relationships_out"][0]["type"] == "blocks"
        assert after["relationships_out"][0]["target_task_id"] == task_b_id

    def test_rebuild_preserves_custom_fields(self, invoke, create_task, initialized_root):
        """Custom fields set via update must survive rebuild."""
        task = create_task("Custom fields rebuild")
        task_id = task["id"]

        # Set multiple custom fields
        invoke(
            "update",
            task_id,
            "custom_fields.x=hello",
            "custom_fields.y=world",
            "--actor",
            "human:test",
        )

        lattice_dir = initialized_root / ".lattice"
        snap_path = lattice_dir / "tasks" / f"{task_id}.json"

        before = snap_path.read_bytes()

        # Rebuild
        result = invoke("rebuild", task_id)
        assert result.exit_code == 0

        after = snap_path.read_bytes()
        assert before == after

        # Verify the custom fields are present
        rebuilt = json.loads(after)
        assert rebuilt["custom_fields"]["x"] == "hello"
        assert rebuilt["custom_fields"]["y"] == "world"

    def test_individual_vs_batch_rebuild(self, invoke, create_task, initialized_root):
        """Rebuilding tasks individually must match rebuilding with --all."""
        tasks = [create_task(f"Individual vs batch {i}") for i in range(3)]

        # Add operations to make snapshots non-trivial
        for i, task in enumerate(tasks):
            invoke("status", task["id"], "in_planning", "--actor", "human:test")
            if i > 0:
                invoke(
                    "link",
                    task["id"],
                    "depends_on",
                    tasks[0]["id"],
                    "--actor",
                    "human:test",
                )

        lattice_dir = initialized_root / ".lattice"

        # Rebuild each individually and store bytes
        individual_snapshots = {}
        for task in tasks:
            result = invoke("rebuild", task["id"])
            assert result.exit_code == 0
            snap_path = lattice_dir / "tasks" / f"{task['id']}.json"
            individual_snapshots[task["id"]] = snap_path.read_bytes()

        # Now rebuild --all
        result = invoke("rebuild", "--all")
        assert result.exit_code == 0

        # Compare: batch rebuild snapshots must match individual rebuilds
        for task in tasks:
            snap_path = lattice_dir / "tasks" / f"{task['id']}.json"
            batch_bytes = snap_path.read_bytes()
            assert individual_snapshots[task["id"]] == batch_bytes, (
                f"Snapshot for {task['id']} differs between individual and batch rebuild"
            )

    def test_rebuild_after_status_round_trip(
        self, invoke, create_task, initialized_root, fill_plan
    ):
        """Status changes through multiple transitions must rebuild identically."""
        task = create_task("Status round trip")
        task_id = task["id"]

        # Move through several statuses
        invoke("status", task_id, "in_planning", "--actor", "human:test")
        fill_plan(task_id, "Status round trip")
        invoke("status", task_id, "planned", "--actor", "human:test")
        invoke("status", task_id, "in_progress", "--actor", "human:test")

        lattice_dir = initialized_root / ".lattice"
        snap_path = lattice_dir / "tasks" / f"{task_id}.json"

        before = snap_path.read_bytes()

        result = invoke("rebuild", task_id)
        assert result.exit_code == 0

        after = snap_path.read_bytes()
        assert before == after

        # Verify final status is correct
        rebuilt = json.loads(after)
        assert rebuilt["status"] == "in_progress"

    def test_rebuild_json_output_consistent(self, invoke, create_task, invoke_json):
        """Rebuild --json envelope is consistent across runs."""
        task = create_task("JSON consistency")
        task_id = task["id"]

        invoke("status", task_id, "in_planning", "--actor", "human:test")

        data1, exit1 = invoke_json("rebuild", task_id)
        assert exit1 == 0
        assert data1["ok"] is True
        assert data1["data"]["rebuilt_tasks"] == [task_id]
        assert data1["data"]["global_log_rebuilt"] is False

        data2, exit2 = invoke_json("rebuild", task_id)
        assert exit2 == 0
        assert data2 == data1
