"""The v2 write-path changes in ``mutate_task`` and ``write_resource_event``:
one batched append per decision, local crash ordering, ``run_hooks``,
``expect_last_event_id``, and ``CONFLICT`` from a ``from`` mismatch."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lattice.core.config import default_config, serialize_config
from lattice.core.errors import OpError, StateConflict
from lattice.core.events import create_event, create_resource_event, serialize_event
from lattice.core.tasks import serialize_snapshot
from lattice.storage.fs import atomic_write, ensure_lattice_dirs, jsonl_append
from lattice.storage.operations import (
    TaskMutationDecision,
    mutate_task,
    mutate_task_events,
    read_task_authority,
    write_resource_event,
)

TASK = "task_01J9ZABCDEFGHJKMNPQRSTVWXY"
HOOKS = {"hooks": {"post_event": "true"}}


@pytest.fixture()
def ld(tmp_path: Path) -> Path:
    ensure_lattice_dirs(tmp_path)
    lattice_dir = tmp_path / ".lattice"
    atomic_write(lattice_dir / "config.json", serialize_config(default_config()))
    return lattice_dir


def _created(task_id: str = TASK) -> dict:
    return create_event(
        "task_created",
        task_id,
        "human:t",
        {"title": "t", "status": "backlog", "priority": "medium", "type": "task"},
    )


def _batch(task_id: str = TASK) -> list[dict]:
    """A multi-event decision that includes a lifecycle event."""
    return [
        _created(task_id),
        create_event("comment_added", task_id, "human:t", {"body": "one"}),
        create_event(
            "status_changed", task_id, "human:t", {"from": "backlog", "to": "in_planning"}
        ),
    ]


def _lifecycle(ld: Path) -> list[dict]:
    text = (ld / "events" / "_lifecycle.jsonl").read_text()
    return [json.loads(line) for line in text.splitlines() if line]


class TestBatchedAppend:
    def test_bytes_equal_per_event_appends(self, ld: Path, tmp_path: Path) -> None:
        events = _batch()
        mutate_task_events(
            ld, TASK, events, source="absent", may_emit_lifecycle=True, run_hooks=False
        )
        reference = tmp_path / "reference.jsonl"
        for event in events:
            jsonl_append(reference, serialize_event(event))
        assert (ld / "events" / f"{TASK}.jsonl").read_bytes() == reference.read_bytes()

    def test_bytes_equal_after_a_torn_final_newline(self, ld: Path, tmp_path: Path) -> None:
        # A file missing its final newline gets exactly one separator, before
        # the first line, in both the batched and the per-event form.
        batched = tmp_path / "batched.jsonl"
        single = tmp_path / "single.jsonl"
        for path in (batched, single):
            path.write_bytes(b'{"partial":true}')
        events = _batch()
        jsonl_append(batched, "".join(serialize_event(e) for e in events))
        for event in events:
            jsonl_append(single, serialize_event(event))
        assert batched.read_bytes() == single.read_bytes()

    def test_resource_batch_bytes(self, ld: Path, tmp_path: Path) -> None:
        rid = "res_01J9ZABCDEFGHJKMNPQRSTVWXY"
        events = [
            create_resource_event("resource_created", rid, "agent:t", {"name": "db"}),
            create_resource_event("resource_acquired", rid, "agent:t", {}),
        ]
        write_resource_event(ld, rid, "db", events, {"id": rid, "name": "db"}, run_hooks=False)
        reference = tmp_path / "reference.jsonl"
        for event in events:
            jsonl_append(reference, serialize_event(event))
        assert (ld / "events" / f"{rid}.jsonl").read_bytes() == reference.read_bytes()


class TestLocalCrashOrdering:
    """SPEC §3.10: events first (one append, one fsync), then the lifecycle
    log, then snapshot and placement."""

    def test_boundary_order_for_a_multi_event_decision(
        self, ld: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[tuple[str, int]] = []
        log = ld / "events" / f"{TASK}.jsonl"

        def record(name: str, _ld: Path, _task_id: str) -> None:
            lines = len(log.read_text().splitlines()) if log.exists() else 0
            seen.append((name, lines))

        monkeypatch.setattr("lattice.storage.operations._mutation_boundary", record)
        mutate_task_events(
            ld, TASK, _batch(), source="absent", may_emit_lifecycle=True, run_hooks=False
        )
        # One append (still in the write buffer) and one fsync for all three
        # events, then the lifecycle log, then the snapshot.
        assert seen == [
            ("task_event_appended", 0),
            ("task_event_fsynced", 3),
            ("lifecycle_appended", 3),
            ("lifecycle_fsynced", 3),
            ("destination_snapshot_written", 3),
            ("locks_released_and_durable", 3),
        ]

    @pytest.mark.parametrize(
        ("boundary", "lifecycle_written"),
        [("task_event_fsynced", False), ("lifecycle_fsynced", True)],
    )
    def test_crash_leaves_events_before_derived_state_and_retries_clean(
        self,
        ld: Path,
        monkeypatch: pytest.MonkeyPatch,
        boundary: str,
        lifecycle_written: bool,
    ) -> None:
        def crash(name: str, _ld: Path, _task_id: str) -> None:
            if name == boundary:
                raise OSError(f"crash at {name}")

        monkeypatch.setattr("lattice.storage.operations._mutation_boundary", crash)
        events = _batch()
        with pytest.raises(OSError, match=boundary):
            mutate_task_events(
                ld, TASK, events, source="absent", may_emit_lifecycle=True, run_hooks=False
            )

        log_lines = (ld / "events" / f"{TASK}.jsonl").read_text().splitlines()
        assert [json.loads(line)["id"] for line in log_lines] == [e["id"] for e in events]
        assert bool(_lifecycle(ld)) is lifecycle_written
        assert not (ld / "tasks" / f"{TASK}.json").exists()

        # The next mutation reconciles the derived state from the events.
        monkeypatch.setattr("lattice.storage.operations._mutation_boundary", lambda *_args: None)
        mutate_task(
            ld,
            TASK,
            lambda _c: TaskMutationDecision(idempotent=True),
            may_emit_lifecycle=True,
            run_hooks=False,
        )
        authority = read_task_authority(ld, TASK)
        assert authority is not None and authority.snapshot["status"] == "in_planning"
        assert [e["id"] for e in _lifecycle(ld)] == [events[0]["id"]]
        assert (ld / "tasks" / f"{TASK}.json").read_text() == serialize_snapshot(
            authority.snapshot
        )


class TestRunHooks:
    def _observe(self, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        fired: list[str] = []
        monkeypatch.setattr(
            "lattice.storage.operations.execute_hooks",
            lambda _config, _ld, _task_id, event: fired.append(event["type"]),
        )
        return fired

    @pytest.mark.parametrize(
        ("config", "run_hooks", "expected"),
        [
            (HOOKS, True, ["task_created", "comment_added", "status_changed"]),
            (HOOKS, False, []),  # a server: config for its rules, no hooks
            (None, True, []),  # nothing to read hooks from; config is not loaded
            (None, False, []),
        ],
    )
    def test_mutate_task(
        self,
        ld: Path,
        monkeypatch: pytest.MonkeyPatch,
        config: dict | None,
        run_hooks: bool,
        expected: list[str],
    ) -> None:
        fired = self._observe(monkeypatch)
        mutate_task_events(
            ld,
            TASK,
            _batch(),
            config,
            source="absent",
            may_emit_lifecycle=True,
            run_hooks=run_hooks,
        )
        assert fired == expected

    @pytest.mark.parametrize(("run_hooks", "expected"), [(True, 1), (False, 0)])
    def test_write_resource_event(
        self, ld: Path, monkeypatch: pytest.MonkeyPatch, run_hooks: bool, expected: int
    ) -> None:
        fired: list[str] = []
        monkeypatch.setattr(
            "lattice.storage.hooks.execute_resource_hooks",
            lambda _c, _ld, _rid, _name, event: fired.append(event["type"]),
        )
        rid = "res_01J9ZABCDEFGHJKMNPQRSTVWXY"
        event = create_resource_event("resource_created", rid, "agent:t", {"name": "db"})
        write_resource_event(ld, rid, "db", [event], {"id": rid}, HOOKS, run_hooks=run_hooks)
        assert len(fired) == expected

    def test_run_hooks_is_required(self, ld: Path) -> None:
        with pytest.raises(TypeError, match="run_hooks"):
            mutate_task_events(ld, TASK, [_created()], source="absent")  # type: ignore[call-arg]


class TestConflicts:
    def _seed(self, ld: Path) -> dict:
        mutate_task_events(
            ld, TASK, [_created()], source="absent", may_emit_lifecycle=True, run_hooks=False
        )
        authority = read_task_authority(ld, TASK)
        assert authority is not None
        return authority.snapshot

    def test_expect_last_event_id_matches(self, ld: Path) -> None:
        snapshot = self._seed(ld)
        event = create_event("comment_added", TASK, "human:t", {"body": "ok"})
        result = mutate_task(
            ld,
            TASK,
            lambda _c: TaskMutationDecision(events=[event]),
            run_hooks=False,
            expect_last_event_id=snapshot["last_event_id"],
        )
        assert result.snapshot["last_event_id"] == event["id"]

    def test_expect_last_event_id_mismatch_conflicts_before_the_callback(self, ld: Path) -> None:
        snapshot = self._seed(ld)
        before = (ld / "events" / f"{TASK}.jsonl").read_bytes()
        called = []
        with pytest.raises(StateConflict) as exc:
            mutate_task(
                ld,
                TASK,
                lambda _c: called.append(1) or TaskMutationDecision(),
                run_hooks=False,
                expect_last_event_id="ev_01J9ZABCDEFGHJKMNPQRSTVWXZ",
            )
        assert called == []
        assert exc.value.code == "CONFLICT"
        assert exc.value.details["snapshot"]["last_event_id"] == snapshot["last_event_id"]
        assert exc.value.details["snapshot"]["status"] == "backlog"
        assert (ld / "events" / f"{TASK}.jsonl").read_bytes() == before

    def test_expectation_on_an_absent_task_conflicts(self, ld: Path) -> None:
        with pytest.raises(StateConflict) as exc:
            mutate_task(
                ld,
                TASK,
                lambda _c: TaskMutationDecision(events=[_created()]),
                source="absent",
                may_emit_lifecycle=True,
                run_hooks=False,
                expect_last_event_id="ev_01J9ZABCDEFGHJKMNPQRSTVWXZ",
            )
        assert exc.value.details["snapshot"] is None

    @pytest.mark.parametrize(
        "event",
        [
            create_event("status_changed", TASK, "h:t", {"from": "review", "to": "done"}),
            create_event("assignment_changed", TASK, "h:t", {"from": "agent:x", "to": "agent:y"}),
            create_event(
                "field_updated", TASK, "h:t", {"field": "title", "from": "stale", "to": "new"}
            ),
        ],
    )
    def test_from_mismatch_is_a_conflict_and_still_a_value_error(
        self, ld: Path, event: dict
    ) -> None:
        self._seed(ld)
        before = (ld / "events" / f"{TASK}.jsonl").read_bytes()
        with pytest.raises(StateConflict) as exc:
            mutate_task_events(ld, TASK, [event], run_hooks=False)
        assert isinstance(exc.value, ValueError) and isinstance(exc.value, OpError)
        assert exc.value.code == "CONFLICT"
        assert "from value does not match authoritative state" in str(exc.value)
        assert exc.value.message == str(exc.value)
        assert exc.value.details["snapshot"]["id"] == TASK
        assert (ld / "events" / f"{TASK}.jsonl").read_bytes() == before
