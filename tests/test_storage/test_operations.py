"""Tests for lattice.storage.operations — the shared write path."""

from __future__ import annotations

import json
import ast
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from lattice.core.config import default_config, serialize_config
from lattice.core.events import create_event
from lattice.core.tasks import serialize_snapshot
from lattice.storage.fs import atomic_write, ensure_lattice_dirs, jsonl_append
from lattice.storage.operations import (
    AuthoritativeLogError,
    TaskMutationDecision,
    mutate_task,
    mutate_task_events,
)


def _setup_lattice(tmp_path: Path) -> Path:
    """Create a minimal .lattice/ directory and return the lattice dir."""
    ensure_lattice_dirs(tmp_path)
    ld = tmp_path / ".lattice"
    atomic_write(ld / "config.json", serialize_config(default_config()))
    return ld


class TestMutateTask:
    """Verify the canonical event-authoritative mutation function."""

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
        mutate_task_events(
            ld,
            "task_01AAAAAAAAAAAAAAAAAAAAAAAAAA",
            [event],
            source="absent",
            may_emit_lifecycle=True,
        )

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
        mutate_task_events(
            ld,
            "task_01BBBBBBBBBBBBBBBBBBBBBBBBBB",
            [event],
            source="absent",
            may_emit_lifecycle=True,
        )

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
        mutate_task_events(ld, task_id, [create_ev], source="absent", may_emit_lifecycle=True)

        lifecycle_before = (ld / "events" / "_lifecycle.jsonl").read_text()

        # Now add a comment (non-lifecycle)
        comment_ev = create_event(
            type="comment_added",
            task_id=task_id,
            actor="human:test",
            data={"body": "test comment"},
        )
        mutate_task_events(ld, task_id, [comment_ev])

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
        mutate_task_events(ld, task_id, [create_ev], source="absent", may_emit_lifecycle=True)

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
        mutate_task_events(ld, task_id, [ev1, ev2])

        event_path = ld / "events" / f"{task_id}.jsonl"
        lines = event_path.read_text().strip().split("\n")
        assert len(lines) == 3  # create + 2 field updates

    def test_zero_event_retry_heals_event_only_snapshot(self, tmp_path: Path) -> None:
        ld = _setup_lattice(tmp_path)
        task_id = "task_01EEEEEEEEEEEEEEEEEEEEEEEEEE"
        event = create_event(
            type="task_created",
            task_id=task_id,
            actor="human:test",
            data={
                "title": "Event only",
                "status": "backlog",
                "priority": "medium",
                "type": "task",
            },
        )
        jsonl_append(ld / "events" / f"{task_id}.jsonl", json.dumps(event) + "\n")

        result = mutate_task(
            ld,
            task_id,
            lambda context: TaskMutationDecision(idempotent=True),
            source="absent",
            may_emit_lifecycle=True,
        )

        assert result.idempotent is True
        assert result.appended_events == []
        assert result.snapshot_reconciled is True
        assert (ld / "tasks" / f"{task_id}.json").read_text() == serialize_snapshot(
            result.snapshot
        )
        assert len((ld / "events" / f"{task_id}.jsonl").read_text().splitlines()) == 1

    def test_malformed_authority_prevents_callback_and_writes(self, tmp_path: Path) -> None:
        ld = _setup_lattice(tmp_path)
        task_id = "task_01FFFFFFFFFFFFFFFFFFFFFFFFFF"
        event_path = ld / "events" / f"{task_id}.jsonl"
        event_path.write_text('{"broken":', encoding="utf-8")
        before = event_path.read_bytes()
        called = False

        def callback(_context):  # noqa: ANN001, ANN202
            nonlocal called
            called = True
            return TaskMutationDecision()

        try:
            mutate_task(ld, task_id, callback)
        except AuthoritativeLogError:
            pass
        else:
            raise AssertionError("malformed authority must fail")
        assert called is False
        assert event_path.read_bytes() == before

    def test_project_short_id_event_only_retry_reconciles_index(self, tmp_path: Path) -> None:
        ld = _setup_lattice(tmp_path)
        task_id = "task_01GGGGGGGGGGGGGGGGGGGGGGGGGG"
        event = create_event(
            type="task_created",
            task_id=task_id,
            actor="human:test",
            data={
                "title": "Short retry",
                "status": "backlog",
                "priority": "medium",
                "type": "task",
                "short_id": "LAT-7",
            },
        )
        jsonl_append(ld / "events" / f"{task_id}.jsonl", json.dumps(event) + "\n")

        result = mutate_task(
            ld,
            task_id,
            lambda context: TaskMutationDecision(value=context.reserved_short_id, idempotent=True),
            source="absent",
            may_emit_lifecycle=True,
            project_prefix="LAT",
        )

        index = json.loads((ld / "ids.json").read_text())
        assert result.callback_value == "LAT-7"
        assert index["map"]["LAT-7"] == task_id
        assert index["next_seqs"]["LAT"] == 8
        assert result.snapshot["short_id"] == "LAT-7"
        assert len((ld / "events" / f"{task_id}.jsonl").read_text().splitlines()) == 1

    def test_concurrent_project_creates_allocate_distinct_ids(self, tmp_path: Path) -> None:
        ld = _setup_lattice(tmp_path)
        task_ids = [f"task_01H{index:023d}" for index in range(1, 9)]

        def create(task_id: str) -> str:
            def decide(context):  # noqa: ANN001, ANN202
                event = create_event(
                    type="task_created",
                    task_id=task_id,
                    actor="human:test",
                    data={
                        "title": task_id,
                        "status": "backlog",
                        "priority": "medium",
                        "type": "task",
                        "short_id": context.reserved_short_id,
                    },
                )
                return TaskMutationDecision(events=[event])

            return mutate_task(
                ld,
                task_id,
                decide,
                source="absent",
                may_emit_lifecycle=True,
                project_prefix="LAT",
            ).snapshot["short_id"]

        with ThreadPoolExecutor(max_workers=8) as pool:
            short_ids = list(pool.map(create, task_ids))
        assert len(set(short_ids)) == len(task_ids)


def test_production_task_writers_use_canonical_storage_api() -> None:
    """No production layer may append a per-task log or use the removed API."""
    source_root = Path(__file__).parents[2] / "src" / "lattice"
    allowed = {
        source_root / "storage" / "operations.py",
        source_root / "storage" / "fs.py",
        source_root / "cli" / "integrity_cmds.py",
    }
    violations: list[str] = []
    for path in source_root.rglob("*.py"):
        if path in allowed:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr
                if isinstance(node.func, ast.Attribute)
                else ""
            )
            if name in {"write_task_event", "jsonl_append"}:
                violations.append(f"{path.relative_to(source_root)}:{node.lineno}:{name}")
    assert violations == []
