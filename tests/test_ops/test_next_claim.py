"""``board.next_claim``: selection and claim in one call under the board lock."""

from __future__ import annotations

import threading
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
    """A backlog task with a real plan, so the plan gate lets it through."""
    task_id = _run(board, "task.create", {"title": title, **fields}).value["id"]
    (board.lattice_dir / "plans" / f"{task_id}.md").write_text(f"# {title}\n\nDo the thing.\n")
    return task_id


class TestNextClaim:
    def test_claims_the_top_task(self, board: LocalBoard) -> None:
        _ready(board, "low", priority="low")
        top = _ready(board, "high", priority="high")
        result = _run(board, "board.next_claim", {}, actor="agent:a")
        assert result.value["id"] == top
        assert result.value["assigned_to"] == "agent:a"
        assert result.value["status"] == "in_progress"
        types = [(e["type"], e["data"].get("to")) for e in result.events]
        assert types[0] == ("assignment_changed", "agent:a")
        assert types[-1] == ("status_changed", "in_progress")
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
        with pytest.raises(OpError) as exc:
            _run(board, "board.next_claim", {}, actor="agent:a")
        assert exc.value.code == "PLAN_REQUIRED"
        assert exc.value.details["snapshot"]["id"] == task_id

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
