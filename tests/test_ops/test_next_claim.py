"""``board.next_claim``: selection and claim in one call under the board lock."""

from __future__ import annotations

import os
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from lattice.boards import LocalBoard, resolve_board
from lattice.ops import Caller, OpError


@pytest.fixture()
def board(initialized_root: Path, monkeypatch: pytest.MonkeyPatch) -> LocalBoard:
    monkeypatch.delenv("LATTICE_ROOT", raising=False)
    return resolve_board(initialized_root)


def _run(board: LocalBoard, op: str, params: dict, actor: str = "agent:t"):  # noqa: ANN202
    return board.execute(op, params, Caller(actor=actor))


def _ready(board: LocalBoard, title: str = "T", **fields) -> str:  # noqa: ANN003
    """A backlog task with a substantive plan."""
    task_id = _run(board, "task.create", {"title": title, **fields}).value["id"]
    (board.lattice_dir / "plans" / f"{task_id}.md").write_text(f"# {title}\n\nDo the thing.\n")
    return task_id


def _planned_unassigned(board: LocalBoard, title: str = "planned") -> str:
    task_id = _ready(board, title)
    _run(board, "task.status", {"task": task_id, "new_status": "in_planning"})
    _run(board, "task.status", {"task": task_id, "new_status": "planned"})
    _run(board, "task.assign", {"task": task_id, "actor_id": "none"})
    return task_id


def _record_remote_plan_review(
    board: LocalBoard, task_id: str, *, spawned_at: str | None = None
) -> None:
    board.execute(
        "task.record_auto_review",
        {
            "task": task_id,
            "review_type": "plan-review",
            "mode": "single",
            "log_path": ".lattice/.daemon/auto-plan-review-test.log",
            "spawned_at": spawned_at or datetime.now(timezone.utc).isoformat(),
            "pid": 12345,
            "trigger_status_event_id": "ev_planned",
        },
        Caller(actor="agent:lattice-auto-review", origin={"reported": {"host": "review-host"}}),
    )


class TestNextClaim:
    def test_claims_the_top_task(self, board: LocalBoard) -> None:
        _ready(board, "low", priority="low")
        top = _ready(board, "high", priority="high")
        result = _run(board, "board.next_claim", {}, actor="agent:a")
        assert result.value["id"] == top
        assert result.value["assigned_to"] == "agent:a"
        assert result.value["status"] == "in_planning"
        types = [(e["type"], e["data"].get("to")) for e in result.events]
        assert types[0] == ("assignment_changed", "agent:a")
        assert types[-1] == ("status_changed", "in_planning")
        assert all(e["actor"] == "agent:a" for e in result.events)
        assert all(e["origin"]["op"] == "board.next_claim" for e in result.events)

    def test_resumes_own_work_without_events(self, board: LocalBoard) -> None:
        first = _run(board, "board.next_claim", {"status": None}, actor="agent:a")
        assert first.value is None and first.idempotent
        task_id = _ready(board)
        _run(board, "board.next_claim", {}, actor="agent:a")
        again = _run(board, "board.next_claim", {}, actor="agent:a")
        assert again.value["id"] == task_id
        assert again.events == [] and again.idempotent

    def test_status_filter(self, board: LocalBoard) -> None:
        _ready(board)
        result = _run(board, "board.next_claim", {"status": " nowhere , "}, actor="agent:a")
        assert result.value is None

    def test_plan_gate(self, board: LocalBoard) -> None:
        task_id = _run(board, "task.create", {"title": "T"}).value["id"]
        _run(board, "task.status", {"task": task_id, "new_status": "in_planning"})
        _run(board, "task.status", {"task": task_id, "new_status": "planned"})
        _run(board, "task.assign", {"task": task_id, "actor_id": "none"})
        with pytest.raises(OpError) as exc:
            _run(board, "board.next_claim", {"status": "planned"}, actor="agent:a")
        assert exc.value.code == "PLAN_REQUIRED"
        assert exc.value.details["snapshot"]["id"] == task_id
        assert exc.value.message.endswith("No assignment or status change was made.")

    def test_live_plan_review_returns_no_claim_without_falling_through(
        self, board: LocalBoard
    ) -> None:
        selected = _ready(board, "reviewing", priority="high")
        fallback = _ready(board, "next", priority="low")
        for task_id in (selected, fallback):
            _run(board, "task.status", {"task": task_id, "new_status": "in_planning"})
            _run(board, "task.status", {"task": task_id, "new_status": "planned"})
            _run(board, "task.assign", {"task": task_id, "actor_id": "none"})

        state_dir = board.lattice_dir / "review_state"
        state_dir.mkdir(exist_ok=True)
        (state_dir / f"{selected}.json").write_text(
            json.dumps(
                {
                    "task_id": selected,
                    "review_type": "plan-review",
                    "status": "running",
                    "started_by_pid": os.getpid(),
                }
            )
        )
        selected_events = (board.lattice_dir / "events" / f"{selected}.jsonl").read_bytes()
        fallback_events = (board.lattice_dir / "events" / f"{fallback}.jsonl").read_bytes()

        result = _run(board, "board.next_claim", {"status": "planned"}, actor="agent:a")

        selected_snapshot = json.loads(
            (board.lattice_dir / "tasks" / f"{selected}.json").read_text()
        )
        fallback_snapshot = json.loads(
            (board.lattice_dir / "tasks" / f"{fallback}.json").read_text()
        )
        assert result.value == {
            "task": selected_snapshot,
            "claimed": False,
            "reason": "PLAN_REVIEW_IN_FLIGHT",
        }
        assert result.task["id"] == selected
        assert result.events == []
        assert selected_snapshot["status"] == "planned"
        assert selected_snapshot["assigned_to"] is None
        assert (board.lattice_dir / "events" / f"{selected}.jsonl").read_bytes() == selected_events
        assert fallback_snapshot["status"] == "planned"
        assert fallback_snapshot["assigned_to"] is None
        assert (board.lattice_dir / "events" / f"{fallback}.jsonl").read_bytes() == fallback_events

    @pytest.mark.parametrize(
        ("status", "owner"),
        [
            ("done", "live"),
            ("failed", "live"),
            ("abandoned", "live"),
            ("running", "dead"),
        ],
    )
    def test_terminal_or_abandoned_local_review_does_not_block_claim(
        self, board: LocalBoard, status: str, owner: str
    ) -> None:
        task_id = _planned_unassigned(board)
        state_dir = board.lattice_dir / "review_state"
        state_dir.mkdir(exist_ok=True)
        (state_dir / f"{task_id}.json").write_text(
            json.dumps(
                {
                    "task_id": task_id,
                    "review_type": "plan-review",
                    "status": status,
                    "started_by_pid": os.getpid() if owner == "live" else 2_000_000_000,
                }
            )
        )

        result = _run(board, "board.next_claim", {"status": "planned"}, actor="agent:claimer")

        assert result.value["id"] == task_id
        assert result.value["status"] == "in_progress"
        assert result.value["assigned_to"] == "agent:claimer"
        assert [event["type"] for event in result.events] == [
            "assignment_changed",
            "status_changed",
        ]

    def test_remote_live_plan_review_has_the_same_no_claim_result(self, board: LocalBoard) -> None:
        task_id = _planned_unassigned(board)
        _record_remote_plan_review(board, task_id)

        result = _run(board, "board.next_claim", {"status": "planned"}, actor="agent:claimer")

        assert result.value["task"]["id"] == task_id
        assert result.value["claimed"] is False
        assert result.value["reason"] == "PLAN_REVIEW_IN_FLIGHT"
        assert result.task["status"] == "planned"
        assert result.task["assigned_to"] is None
        assert result.events == []

    @pytest.mark.parametrize("completion", ["expired", "artifact"])
    def test_remote_failed_or_completed_plan_review_does_not_block_claim(
        self, board: LocalBoard, completion: str
    ) -> None:
        task_id = _planned_unassigned(board)
        spawned_at = (
            (datetime.now(timezone.utc) - timedelta(seconds=601)).isoformat()
            if completion == "expired"
            else None
        )
        _record_remote_plan_review(board, task_id, spawned_at=spawned_at)
        if completion == "artifact":
            board.execute(
                "task.attach",
                {"task": task_id, "inline": "Review complete", "role": "plan-review"},
                Caller(actor="agent:reviewer"),
            )

        result = _run(board, "board.next_claim", {"status": "planned"}, actor="agent:claimer")

        assert result.value["id"] == task_id
        assert result.value["status"] == "in_progress"
        assert result.value["assigned_to"] == "agent:claimer"

    def test_missing_plan_backlog_claim_stops_in_planning(self, board: LocalBoard) -> None:
        task_id = _run(board, "task.create", {"title": "Unplanned"}).value["id"]
        (board.lattice_dir / "plans" / f"{task_id}.md").unlink()
        result = _run(board, "board.next_claim", {}, actor="agent:a")
        assert result.value["status"] == "in_planning"
        assert [event["type"] for event in result.events] == [
            "assignment_changed",
            "status_changed",
        ]
        assert result.events[-1]["data"] == {"from": "backlog", "to": "in_planning"}

    @pytest.mark.parametrize("write_plan", [False, True], ids=["missing-plan", "existing-plan"])
    def test_in_planning_pickup_assigns_without_status_event(
        self, board: LocalBoard, write_plan: bool
    ) -> None:
        task_id = (
            _ready(board) if write_plan else _run(board, "task.create", {"title": "T"}).value["id"]
        )
        if not write_plan:
            (board.lattice_dir / "plans" / f"{task_id}.md").unlink()
        _run(board, "task.status", {"task": task_id, "new_status": "in_planning"})
        _run(board, "task.assign", {"task": task_id, "actor_id": "none"})
        result = _run(board, "board.next_claim", {"status": "in_planning"}, actor="agent:a")
        assert result.value["status"] == "in_planning"
        assert result.value["assigned_to"] == "agent:a"
        assert len(result.events) == 1
        assert result.events[0]["type"] == "assignment_changed"

    def test_no_path_to_in_progress(self, board: LocalBoard) -> None:
        task_id = _ready(board)
        config = board.load_config()
        config["workflow"]["transitions"]["backlog"] = ["cancelled"]
        with pytest.raises(OpError) as exc:
            board.execute("board.next_claim", {}, Caller(actor="agent:a"), config=config)
        assert exc.value.code == "INVALID_TRANSITION"
        assert exc.value.message == "No valid transition path from backlog to in_progress."
        # The rejected claim wrote nothing, not even the assignment.
        assert exc.value.details["snapshot"]["assigned_to"] is None
        assert board.execute(
            "task.update", {"task": task_id, "pairs": ["title=T"]}, Caller(actor="agent:a")
        ).idempotent

    @pytest.mark.parametrize("missing", ["status", "backlog-edge", "planning-edge"])
    def test_incomplete_plan_route_keeps_in_progress_target(
        self, board: LocalBoard, missing: str
    ) -> None:
        task_id = _ready(board)
        config = board.load_config()
        workflow = config["workflow"]
        if missing == "status":
            workflow["statuses"].remove("in_planning")
        elif missing == "backlog-edge":
            workflow["transitions"]["backlog"].remove("in_planning")
        else:
            workflow["transitions"]["in_planning"].remove("planned")
        result = board.execute("board.next_claim", {}, Caller(actor="agent:a"), config=config)
        assert result.value["id"] == task_id
        assert result.value["status"] == "in_progress"

    def test_divergent_plan_files_refuse_before_any_claim_write(self, board: LocalBoard) -> None:
        task_id = _run(board, "task.create", {"title": "Divergent"}).value["id"]
        archived_plan = board.lattice_dir / "archive" / "plans" / f"{task_id}.md"
        archived_plan.parent.mkdir(parents=True, exist_ok=True)
        archived_plan.write_text("# Different plan\n")
        event_path = board.lattice_dir / "events" / f"{task_id}.jsonl"
        before = event_path.read_bytes()

        with pytest.raises(OpError) as exc:
            _run(board, "board.next_claim", {}, actor="agent:a")
        assert exc.value.code == "INTEGRITY_ERROR"
        assert str(archived_plan) in exc.value.message
        assert exc.value.message.endswith(
            "active and archived plan files diverge; manual recovery required"
        )
        assert event_path.read_bytes() == before

    def test_unreadable_plan_keeps_existing_nonblocking_behavior(
        self, board: LocalBoard, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        task_id = _run(board, "task.create", {"title": "Unreadable"}).value["id"]
        _run(board, "task.status", {"task": task_id, "new_status": "in_planning"})
        _run(board, "task.status", {"task": task_id, "new_status": "planned"})
        _run(board, "task.assign", {"task": task_id, "actor_id": "none"})
        plan_path = board.lattice_dir / "plans" / f"{task_id}.md"
        original_read_text = Path.read_text

        def unreadable(path: Path, *args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            if path == plan_path:
                raise OSError("permission denied")
            return original_read_text(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", unreadable)
        result = _run(board, "board.next_claim", {"status": "planned"}, actor="agent:a")
        assert result.value["status"] == "in_progress"
        assert result.value["assigned_to"] == "agent:a"

    def test_plan_required_precedes_already_claimed_after_stale_selection(
        self, board: LocalBoard, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        task_id = _run(board, "task.create", {"title": "Stale selection"}).value["id"]
        import lattice.ops.board_next_claim as claim_module

        original_select = claim_module.select_next
        changed_event_ids: list[str] = []

        def select_then_change(snapshots, **kwargs):  # noqa: ANN003, ANN202
            selected = original_select(snapshots, **kwargs)
            if selected is not None:
                _run(board, "task.status", {"task": task_id, "new_status": "in_planning"})
                _run(board, "task.status", {"task": task_id, "new_status": "planned"})
                _run(
                    board,
                    "task.assign",
                    {"task": task_id, "actor_id": "agent:other"},
                    actor="agent:other",
                )
                changed_event_ids.extend(event["id"] for event in _events(board, task_id))
            return selected

        monkeypatch.setattr(claim_module, "select_next", select_then_change)
        with pytest.raises(OpError) as exc:
            _run(board, "board.next_claim", {}, actor="agent:a")
        assert exc.value.code == "PLAN_REQUIRED"
        assert exc.value.details["snapshot"]["status"] == "planned"
        assert exc.value.details["snapshot"]["assigned_to"] == "agent:other"
        assert [event["id"] for event in _events(board, task_id)] == changed_event_ids

    def test_requires_an_actor(self, board: LocalBoard) -> None:
        with pytest.raises(OpError) as exc:
            board.execute("board.next_claim", {}, Caller())
        assert exc.value.code == "MISSING_ACTOR"

    def test_unknown_param(self, board: LocalBoard) -> None:
        with pytest.raises(OpError) as exc:
            _run(board, "board.next_claim", {"claim": True})
        assert exc.value.details["reason"] == "UNKNOWN_PARAM"


class TestConcurrentClaims:
    """Two callers in one process against one board never receive the same task."""

    def _race(self, board: LocalBoard, actors: list[str]) -> list:
        barrier = threading.Barrier(len(actors))
        results: list = [None] * len(actors)
        errors: list[BaseException] = []

        def claim(i: int, actor: str) -> None:
            try:
                barrier.wait()
                results[i] = _run(board, "board.next_claim", {}, actor=actor).value
            except BaseException as exc:  # noqa: BLE001 - surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=claim, args=(i, a)) for i, a in enumerate(actors)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert not errors, errors
        return results

    def test_two_callers_two_tasks(self, board: LocalBoard) -> None:
        tasks = {_ready(board, f"t{i}") for i in range(2)}
        for _ in range(10):
            results = self._race(board, ["agent:a", "agent:b"])
            claimed = [r["id"] for r in results]
            assert len(set(claimed)) == 2 and set(claimed) == tasks
            assert {r["assigned_to"] for r in results} == {"agent:a", "agent:b"}
            for task_id in tasks:  # back to the pool for the next round
                _run(board, "task.assign", {"task": task_id, "actor_id": "none"})
                _run(
                    board,
                    "task.status",
                    {"task": task_id, "new_status": "backlog", "force": True, "reason": "r"},
                )

    def test_two_callers_one_task(self, board: LocalBoard) -> None:
        task_id = _ready(board)
        results = self._race(board, ["agent:a", "agent:b"])
        winners = [r for r in results if r is not None]
        assert [w["id"] for w in winners] == [task_id]
        assert winners[0]["status"] == "in_planning"
        events = _events(board, task_id)
        assert [event["type"] for event in events].count("assignment_changed") == 1
        assert [event["data"] for event in events if event["type"] == "status_changed"] == [
            {"from": "backlog", "to": "in_planning"}
        ]


def _events(board: LocalBoard, task_id: str) -> list[dict]:
    path = board.lattice_dir / "events" / f"{task_id}.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]
