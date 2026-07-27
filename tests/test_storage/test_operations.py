"""Tests for lattice.storage.operations — the shared write path."""

from __future__ import annotations

import json
import ast
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from urllib.request import Request, urlopen

import pytest

from lattice.core.config import default_config, serialize_config
from lattice.core.events import create_event, serialize_event
from lattice.core.ids import generate_artifact_id, generate_task_id
from lattice.core.tasks import serialize_snapshot
from lattice.storage.fs import atomic_write, ensure_lattice_dirs, jsonl_append
from lattice.storage.operations import (
    AuthoritativeLogError,
    TaskMutationDecision,
    discover_task_authorities,
    mutate_task,
    mutate_task_events,
    read_task_authority,
)


def _setup_lattice(tmp_path: Path) -> Path:
    """Create a minimal .lattice/ directory and return the lattice dir."""
    ensure_lattice_dirs(tmp_path)
    ld = tmp_path / ".lattice"
    atomic_write(ld / "config.json", serialize_config(default_config()))
    return ld


def _create_task(ld: Path, task_id: str) -> None:
    event = create_event(
        type="task_created",
        task_id=task_id,
        actor="human:test",
        data={
            "title": "Concurrent task",
            "status": "backlog",
            "priority": "medium",
            "type": "task",
        },
    )
    mutate_task_events(ld, task_id, [event], source="absent", may_emit_lifecycle=True)


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

    @pytest.mark.parametrize(
        "operation",
        [
            "status",
            "assignment",
            "update",
            "comment",
            "reaction",
            "complete",
            "criterion",
            "artifact",
            "archive",
        ],
    )
    def test_concurrent_state_decisions_replay_under_lock(
        self,
        tmp_path: Path,
        operation: str,
    ) -> None:
        """Every state-dependent writer decision sees the preceding commit."""
        ld = _setup_lattice(tmp_path)
        task_id = f"task_01RACE{operation.upper():0<20}"[:31]
        _create_task(ld, task_id)
        comment_id: str | None = None
        if operation == "reaction":
            comment = create_event(
                type="comment_added",
                task_id=task_id,
                actor="human:test",
                data={"body": "React here"},
            )
            mutate_task_events(ld, task_id, [comment])
            comment_id = comment["id"]

        barrier = Barrier(2)

        def write(index: int) -> None:
            barrier.wait()

            def decide(context):  # noqa: ANN001, ANN202
                snapshot = context.snapshot
                assert snapshot is not None
                if operation == "status":
                    if snapshot["status"] == "in_planning":
                        return TaskMutationDecision(idempotent=True)
                    event_type = "status_changed"
                    data = {"from": snapshot["status"], "to": "in_planning"}
                elif operation == "assignment":
                    if snapshot.get("assigned_to") == "agent:race":
                        return TaskMutationDecision(idempotent=True)
                    event_type = "assignment_changed"
                    data = {"from": snapshot.get("assigned_to"), "to": "agent:race"}
                elif operation == "update":
                    desired = ("high", "low")[index]
                    event_type = "field_updated"
                    data = {
                        "field": "priority",
                        "from": snapshot.get("priority"),
                        "to": desired,
                    }
                elif operation == "comment":
                    event_type = "comment_added"
                    data = {"body": f"Comment {index}"}
                elif operation == "reaction":
                    if any(
                        event["type"] == "reaction_added"
                        and event["actor"] == "agent:race"
                        and event["data"] == {"comment_id": comment_id, "emoji": "eyes"}
                        for event in context.events
                    ):
                        return TaskMutationDecision(idempotent=True)
                    event_type = "reaction_added"
                    data = {"comment_id": comment_id, "emoji": "eyes"}
                elif operation == "complete":
                    if snapshot["status"] == "done":
                        return TaskMutationDecision(idempotent=True)
                    event_type = "status_changed"
                    data = {"from": snapshot["status"], "to": "done"}
                elif operation == "criterion":
                    next_number = len(snapshot.get("acceptance_criteria", [])) + 1
                    event_type = "acceptance_criterion_added"
                    data = {
                        "criterion_id": f"AC-{next_number}",
                        "outcome": f"Outcome {index}",
                        "revision": 1,
                    }
                elif operation == "artifact":
                    if "art_01RACE0000000000000000000" in snapshot.get("artifact_refs", []):
                        return TaskMutationDecision(idempotent=True)
                    event_type = "artifact_attached"
                    data = {"artifact_id": "art_01RACE0000000000000000000"}
                else:
                    if context.location == "archived":
                        return TaskMutationDecision(idempotent=True)
                    event_type = "task_archived"
                    data = {}
                event = create_event(
                    type=event_type,
                    task_id=task_id,
                    actor="agent:race",
                    data=data,
                )
                return TaskMutationDecision(events=[event])

            mutate_task(
                ld,
                task_id,
                decide,
                source="either" if operation == "archive" else "active",
                destination="archived" if operation == "archive" else None,
                may_emit_lifecycle=operation == "archive",
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(write, range(2)))

        authority = read_task_authority(ld, task_id)
        assert authority is not None
        prefix = ld / "archive" if authority.location == "archived" else ld
        assert (prefix / "tasks" / f"{task_id}.json").read_bytes() == serialize_snapshot(
            authority.snapshot
        ).encode()

    def test_split_placement_uses_latest_lifecycle_event(self, tmp_path: Path) -> None:
        ld = _setup_lattice(tmp_path)
        task_id = "task_01SPLITPLACEMENT0000000000"
        _create_task(ld, task_id)
        active_event = ld / "events" / f"{task_id}.jsonl"
        archived_event = ld / "archive" / "events" / f"{task_id}.jsonl"
        archived_event.parent.mkdir(parents=True, exist_ok=True)
        archived_event.write_bytes(active_event.read_bytes())
        archive_event = create_event("task_archived", task_id, "human:test", {})
        jsonl_append(archived_event, json.dumps(archive_event) + "\n")

        authority = read_task_authority(ld, task_id)
        assert authority is not None
        assert authority.location == "archived"
        assert len(discover_task_authorities(ld)) == 1

        active_event.write_bytes(archived_event.read_bytes())
        unarchive_event = create_event("task_unarchived", task_id, "human:test", {})
        jsonl_append(active_event, json.dumps(unarchive_event) + "\n")
        authority = read_task_authority(ld, task_id)
        assert authority is not None
        assert authority.location == "active"
        assert authority.events[-1]["id"] == unarchive_event["id"]

    @pytest.mark.parametrize("direction", ["archive", "unarchive"])
    def test_active_discovery_scans_both_event_directories_before_filtering(
        self, tmp_path: Path, direction: str
    ) -> None:
        ld = _setup_lattice(tmp_path)
        task_id = f"task_01DISCOVERY{direction.upper():0<16}"[:31]
        _create_task(ld, task_id)
        active = ld / "events" / f"{task_id}.jsonl"
        archived = ld / "archive" / "events" / f"{task_id}.jsonl"
        archived.parent.mkdir(parents=True, exist_ok=True)
        if direction == "archive":
            event = create_event("task_archived", task_id, "human:test", {})
            archived.write_bytes(active.read_bytes() + serialize_event(event).encode())
            active.unlink()
            assert discover_task_authorities(ld, include_archived=False) == []
        else:
            archive_event = create_event("task_archived", task_id, "human:test", {})
            unarchive_event = create_event("task_unarchived", task_id, "human:test", {})
            archived.write_bytes(
                active.read_bytes()
                + serialize_event(archive_event).encode()
                + serialize_event(unarchive_event).encode()
            )
            active.unlink()
            discovered = discover_task_authorities(ld, include_archived=False)
            assert [authority.task_id for authority in discovered] == [task_id]
            assert discovered[0].location == "active"

    @pytest.mark.parametrize(
        ("event_type", "data", "message"),
        [
            ("task_unarchived", {}, "must alternate"),
            (
                "comment_edited",
                {"comment_id": "ev_missing", "body": "nope"},
                "not found",
            ),
            ("comment_deleted", {"comment_id": "ev_missing"}, "not found"),
            (
                "reaction_added",
                {"comment_id": "ev_missing", "emoji": "eyes"},
                "not found",
            ),
            (
                "reaction_removed",
                {"comment_id": "ev_missing", "emoji": "eyes"},
                "not found",
            ),
        ],
    )
    def test_strict_replay_rejects_invalid_one_shot_semantics(
        self,
        tmp_path: Path,
        event_type: str,
        data: dict,
        message: str,
    ) -> None:
        ld = _setup_lattice(tmp_path)
        task_id = f"task_01STRICT{event_type.upper():0<18}"[:31]
        _create_task(ld, task_id)
        event_path = ld / "events" / f"{task_id}.jsonl"
        event = create_event(event_type, task_id, "human:test", data)
        event_path.write_bytes(event_path.read_bytes() + serialize_event(event).encode())
        with pytest.raises(AuthoritativeLogError, match=message):
            read_task_authority(ld, task_id)

    def test_strict_replay_rejects_duplicate_delete_and_reaction_lifecycle(
        self, tmp_path: Path
    ) -> None:
        ld = _setup_lattice(tmp_path)
        task_id = "task_01STRICTONESHOT0000000000"
        _create_task(ld, task_id)
        comment = create_event("comment_added", task_id, "human:test", {"body": "one shot"})
        reaction = create_event(
            "reaction_added",
            task_id,
            "human:test",
            {"comment_id": comment["id"], "emoji": "eyes"},
        )
        delete = create_event(
            "comment_deleted",
            task_id,
            "human:test",
            {"comment_id": comment["id"]},
        )
        mutate_task_events(ld, task_id, [comment, reaction, delete])
        event_path = ld / "events" / f"{task_id}.jsonl"
        duplicate = create_event(
            "comment_deleted",
            task_id,
            "human:test",
            {"comment_id": comment["id"]},
        )
        event_path.write_bytes(event_path.read_bytes() + serialize_event(duplicate).encode())
        with pytest.raises(AuthoritativeLogError, match="already deleted"):
            read_task_authority(ld, task_id)

    @pytest.mark.parametrize("direction", ["archive", "unarchive"])
    @pytest.mark.parametrize(
        "fault_boundary",
        [
            "task_event_appended",
            "task_event_fsynced",
            "lifecycle_appended",
            "lifecycle_fsynced",
            "destination_event_copied",
            "destination_snapshot_written",
            "destination_plan_copied",
            "destination_notes_copied",
            "source_snapshot_removed",
            "source_plan_removed",
            "source_notes_removed",
            "source_event_removed",
        ],
    )
    def test_archive_unarchive_fault_boundaries_retry_to_strict_replay(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        direction: str,
        fault_boundary: str,
    ) -> None:
        ld = _setup_lattice(tmp_path)
        task_id = f"task_01FAULT{direction.upper():0<19}"[:31]
        _create_task(ld, task_id)
        (ld / "plans" / f"{task_id}.md").write_bytes(b"# exact plan\n")
        (ld / "notes" / f"{task_id}.md").write_bytes(b"# exact notes\n")

        def placement_decision(context):  # noqa: ANN001, ANN202
            expected = "archived" if direction == "archive" else "active"
            if context.location == expected:
                return TaskMutationDecision(idempotent=True)
            event_type = "task_archived" if direction == "archive" else "task_unarchived"
            return TaskMutationDecision(
                events=[create_event(event_type, task_id, "human:test", {})]
            )

        if direction == "unarchive":
            mutate_task(
                ld,
                task_id,
                lambda context: TaskMutationDecision(
                    events=[create_event("task_archived", task_id, "human:test", {})]
                ),
                source="either",
                destination="archived",
                may_emit_lifecycle=True,
            )

        fired = False

        def inject(name: str, _ld: Path, _task_id: str) -> None:
            nonlocal fired
            if name == fault_boundary and not fired:
                fired = True
                raise OSError(f"fault at {name}")

        monkeypatch.setattr("lattice.storage.operations._mutation_boundary", inject)
        with pytest.raises(OSError, match=fault_boundary):
            mutate_task(
                ld,
                task_id,
                placement_decision,
                source="either",
                destination="archived" if direction == "archive" else "active",
                may_emit_lifecycle=True,
            )
        assert fired is True

        monkeypatch.setattr(
            "lattice.storage.operations._mutation_boundary",
            lambda _name, _ld, _task_id: None,
        )
        result = mutate_task(
            ld,
            task_id,
            placement_decision,
            source="either",
            destination="archived" if direction == "archive" else "active",
            may_emit_lifecycle=True,
        )
        authority = read_task_authority(ld, task_id)
        assert authority is not None
        expected_location = "archived" if direction == "archive" else "active"
        assert authority.location == expected_location
        event_type = "task_archived" if direction == "archive" else "task_unarchived"
        assert sum(event["type"] == event_type for event in authority.events) == 1
        lifecycle = [
            json.loads(line)
            for line in (ld / "events" / "_lifecycle.jsonl").read_text().splitlines()
            if line
        ]
        placement_event_ids = {
            event["id"] for event in authority.events if event["type"] == event_type
        }
        assert sum(event["id"] in placement_event_ids for event in lifecycle) == 1
        base = ld / "archive" if expected_location == "archived" else ld
        other = ld if expected_location == "archived" else ld / "archive"
        assert (base / "plans" / f"{task_id}.md").read_bytes() == b"# exact plan\n"
        assert (base / "notes" / f"{task_id}.md").read_bytes() == b"# exact notes\n"
        for directory, suffix in (
            ("events", ".jsonl"),
            ("tasks", ".json"),
            ("plans", ".md"),
            ("notes", ".md"),
        ):
            assert (base / directory / f"{task_id}{suffix}").exists()
            assert not (other / directory / f"{task_id}{suffix}").exists()
        assert (base / "tasks" / f"{task_id}.json").read_bytes() == serialize_snapshot(
            authority.snapshot
        ).encode()
        assert result.snapshot == authority.snapshot

    @pytest.mark.parametrize(
        "fault_boundary",
        [
            "missing_lifecycle",
            "missing_target_event",
            "missing_target_snapshot",
            "missing_target_plan",
            "missing_target_notes",
            "stale_source_event",
            "stale_source_snapshot",
            "stale_source_plan",
            "stale_source_notes",
        ],
    )
    def test_repeat_archive_heals_each_storage_boundary(
        self,
        tmp_path: Path,
        fault_boundary: str,
    ) -> None:
        ld = _setup_lattice(tmp_path)
        task_id = "task_01ARCHIVEHEAL000000000000"
        _create_task(ld, task_id)
        plan = ld / "plans" / f"{task_id}.md"
        notes = ld / "notes" / f"{task_id}.md"
        plan.write_text("# Plan\n", encoding="utf-8")
        notes.write_text("# Notes\n", encoding="utf-8")

        def archive_decision(context):  # noqa: ANN001, ANN202
            if context.location == "archived":
                return TaskMutationDecision(idempotent=True)
            event = create_event("task_archived", task_id, "human:test", {})
            return TaskMutationDecision(events=[event])

        mutate_task(
            ld,
            task_id,
            archive_decision,
            source="either",
            destination="archived",
            may_emit_lifecycle=True,
        )
        event_path = ld / "archive" / "events" / f"{task_id}.jsonl"
        event_before = event_path.read_bytes()
        lifecycle_path = ld / "events" / "_lifecycle.jsonl"
        lifecycle_events = [
            json.loads(line) for line in lifecycle_path.read_text().splitlines() if line.strip()
        ]
        archive_id = json.loads(event_before.splitlines()[-1])["id"]
        if fault_boundary == "missing_lifecycle":
            lifecycle_path.write_text(
                "".join(
                    json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n"
                    for event in lifecycle_events
                    if event["id"] != archive_id
                ),
                encoding="utf-8",
            )
        else:
            placement, name = fault_boundary.split("_", 2)[1:]
            directory, suffix = {
                "event": ("events", ".jsonl"),
                "snapshot": ("tasks", ".json"),
                "plan": ("plans", ".md"),
                "notes": ("notes", ".md"),
            }[name]
            active_path = ld / directory / f"{task_id}{suffix}"
            archived_path = ld / "archive" / directory / f"{task_id}{suffix}"
            active_path.parent.mkdir(parents=True, exist_ok=True)
            active_path.write_bytes(archived_path.read_bytes())
            if placement == "target":
                archived_path.unlink()

        result = mutate_task(
            ld,
            task_id,
            archive_decision,
            source="either",
            destination="archived",
            may_emit_lifecycle=True,
        )
        assert result.idempotent is True
        assert event_path.read_bytes() == event_before
        assert (ld / "archive" / "tasks" / f"{task_id}.json").read_bytes() == serialize_snapshot(
            result.snapshot
        ).encode()
        for relative in (
            Path("events") / f"{task_id}.jsonl",
            Path("tasks") / f"{task_id}.json",
            Path("plans") / f"{task_id}.md",
            Path("notes") / f"{task_id}.md",
        ):
            assert (ld / "archive" / relative).exists()
            assert not (ld / relative).exists()
        assert (
            sum(
                json.loads(line)["id"] == archive_id
                for line in lifecycle_path.read_text().splitlines()
                if line.strip()
            )
            == 1
        )

    @pytest.mark.parametrize("direction", ["archive", "unarchive"])
    def test_placement_hooks_run_after_locks_and_full_durability(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        direction: str,
    ) -> None:
        ld = _setup_lattice(tmp_path)
        task_id = generate_task_id()
        _create_task(ld, task_id)
        (ld / "plans" / f"{task_id}.md").write_text("# Hook plan\n")
        if direction == "unarchive":
            mutate_task(
                ld,
                task_id,
                lambda _context: TaskMutationDecision(
                    events=[create_event("task_archived", task_id, "human:test", {})]
                ),
                source="either",
                destination="archived",
                may_emit_lifecycle=True,
            )
        observed: list[str] = []

        def hook(_config, hook_ld, hook_task_id, event):  # noqa: ANN001
            authority = read_task_authority(hook_ld, hook_task_id)
            assert authority is not None
            expected = "archived" if direction == "archive" else "active"
            assert authority.location == expected
            base = hook_ld / "archive" if expected == "archived" else hook_ld
            other = hook_ld if expected == "archived" else hook_ld / "archive"
            assert (base / "events" / f"{task_id}.jsonl").exists()
            assert (base / "tasks" / f"{task_id}.json").exists()
            assert (base / "plans" / f"{task_id}.md").read_text() == "# Hook plan\n"
            assert not (other / "events" / f"{task_id}.jsonl").exists()
            observed.append(event["type"])

        monkeypatch.setattr("lattice.storage.operations.execute_hooks", hook)
        event_type = "task_archived" if direction == "archive" else "task_unarchived"
        mutate_task(
            ld,
            task_id,
            lambda _context: TaskMutationDecision(
                events=[create_event(event_type, task_id, "human:test", {})]
            ),
            config={"hooks": {"enabled": True}},
            source="either",
            destination="archived" if direction == "archive" else "active",
            may_emit_lifecycle=True,
        )
        assert observed == [event_type]


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


@pytest.mark.parametrize(
    "operation",
    [
        "status",
        "assignment",
        "update",
        "comment_edit",
        "comment_delete",
        "reaction",
        "complete",
        "archive",
    ],
)
def test_production_cli_stateful_callbacks_race_under_authority_lock(
    tmp_path: Path, operation: str
) -> None:
    """Exercise real Click callbacks, not handcrafted storage decisions."""
    ld = _setup_lattice(tmp_path)
    task_id = generate_task_id()
    _create_task(ld, task_id)
    comment_id: str | None = None
    if operation in {"comment_edit", "comment_delete", "reaction"}:
        comment = create_event("comment_added", task_id, "human:test", {"body": "original"})
        mutate_task_events(ld, task_id, [comment])
        comment_id = comment["id"]
    if operation == "complete":
        mutate_task_events(
            ld,
            task_id,
            [
                create_event(
                    "status_changed",
                    task_id,
                    "human:test",
                    {"from": "backlog", "to": "review"},
                )
            ],
        )

    commands = {
        "status": ["status", task_id, "in_planning", "--actor", "human:test"],
        "assignment": ["assign", task_id, "agent:race", "--actor", "human:test"],
        "update": ["update", task_id, "priority=high", "--actor", "human:test"],
        "comment_edit": [
            "comment-edit",
            task_id,
            comment_id,
            "edited",
            "--actor",
            "human:test",
        ],
        "comment_delete": [
            "comment-delete",
            task_id,
            comment_id,
            "--actor",
            "human:test",
        ],
        "reaction": [
            "react",
            task_id,
            comment_id,
            "eyes",
            "--actor",
            "human:test",
        ],
        "complete": [
            "complete",
            task_id,
            "--review",
            "Race review.",
            "--actor",
            "human:test",
        ],
        "archive": ["archive", task_id, "--actor", "human:test"],
    }
    barrier = Barrier(2)

    def invoke(_index: int):  # noqa: ANN202
        barrier.wait()
        return subprocess.run(
            [str(Path(sys.executable).parent / "lattice"), *commands[operation]],
            env={**os.environ, "LATTICE_ROOT": str(tmp_path)},
            capture_output=True,
            text=True,
            check=False,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(invoke, range(2)))
    assert any(result.returncode == 0 for result in results), [
        (result.returncode, result.stdout, result.stderr) for result in results
    ]
    authority = read_task_authority(ld, task_id)
    assert authority is not None
    base = ld / "archive" if authority.location == "archived" else ld
    assert (base / "tasks" / f"{task_id}.json").read_bytes() == serialize_snapshot(
        authority.snapshot
    ).encode()
    event_type = {
        "status": "status_changed",
        "assignment": "assignment_changed",
        "update": "field_updated",
        "comment_edit": "comment_edited",
        "comment_delete": "comment_deleted",
        "reaction": "reaction_added",
        "archive": "task_archived",
    }.get(operation)
    if event_type is not None and operation != "comment_edit":
        assert sum(event["type"] == event_type for event in authority.events) == 1


def test_production_mcp_criterion_add_and_edit_callbacks_race(
    tmp_path: Path,
) -> None:
    from lattice.mcp.tools import lattice_attach, lattice_criterion_add, lattice_criterion_edit

    ld = _setup_lattice(tmp_path)
    task_id = generate_task_id()
    _create_task(ld, task_id)
    root = str(tmp_path)
    barrier = Barrier(2)

    def add(index: int) -> dict:
        barrier.wait()
        return lattice_criterion_add(
            task_id=task_id,
            outcome=f"Outcome {index}",
            actor="human:test",
            lattice_root=root,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(add, range(2)))
    authority = read_task_authority(ld, task_id)
    assert authority is not None
    assert {criterion["id"] for criterion in authority.snapshot["acceptance_criteria"]} == {
        "AC-1",
        "AC-2",
    }

    barrier = Barrier(2)

    def edit(index: int) -> dict:
        barrier.wait()
        return lattice_criterion_edit(
            task_id=task_id,
            criterion_id="AC-1",
            outcome=f"Edited {index}",
            actor="human:test",
            lattice_root=root,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(edit, range(2)))
    authority = read_task_authority(ld, task_id)
    assert authority is not None
    criterion = next(
        item for item in authority.snapshot["acceptance_criteria"] if item["id"] == "AC-1"
    )
    assert criterion["revision"] == 3
    assert [revision["revision"] for revision in criterion["revisions"]] == [1, 2, 3]

    artifact_id = generate_artifact_id()
    barrier = Barrier(2)

    def attach(_index: int) -> dict:
        barrier.wait()
        return lattice_attach(
            task_id=task_id,
            source="https://example.com/race-evidence",
            title="Race evidence",
            actor="human:test",
            artifact_id=artifact_id,
            lattice_root=root,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(attach, range(2)))
    authority = read_task_authority(ld, task_id)
    assert authority is not None
    assert (
        sum(
            event["type"] == "artifact_attached" and event["data"]["artifact_id"] == artifact_id
            for event in authority.events
        )
        == 1
    )
    assert (ld / "tasks" / f"{task_id}.json").read_bytes() == serialize_snapshot(
        authority.snapshot
    ).encode()


def test_production_dashboard_update_callbacks_race_across_servers(
    tmp_path: Path,
) -> None:
    from lattice.dashboard.server import create_server

    ld = _setup_lattice(tmp_path)
    task_id = generate_task_id()
    _create_task(ld, task_id)
    servers = [
        create_server(ld, "127.0.0.1", 0),
        create_server(ld, "127.0.0.1", 0),
    ]
    with ThreadPoolExecutor(max_workers=2) as server_pool:
        futures = [server_pool.submit(server.serve_forever) for server in servers]
        barrier = Barrier(2)

        def update(index: int) -> int:
            barrier.wait()
            payload = json.dumps(
                {
                    "actor": "human:test",
                    "fields": {"priority": ("high", "low")[index]},
                }
            ).encode()
            request = Request(
                f"http://127.0.0.1:{servers[index].server_port}/api/tasks/{task_id}/update",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request) as response:
                return response.status

        try:
            with ThreadPoolExecutor(max_workers=2) as clients:
                assert list(clients.map(update, range(2))) == [200, 200]
        finally:
            for server in servers:
                server.shutdown()
                server.server_close()
            for future in futures:
                future.result(timeout=5)
    authority = read_task_authority(ld, task_id)
    assert authority is not None
    updates = [
        event
        for event in authority.events
        if event["type"] == "field_updated" and event["data"]["field"] == "priority"
    ]
    assert len(updates) == 2
    assert updates[1]["data"]["from"] == updates[0]["data"]["to"]
    assert (ld / "tasks" / f"{task_id}.json").read_bytes() == serialize_snapshot(
        authority.snapshot
    ).encode()
