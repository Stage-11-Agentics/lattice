"""``task.complete`` (AC-27): a refused completion writes nothing.

Every rule runs before any file is written, so there is nothing to unlink:
the board's durable files are byte-identical after each refusal, and no
durable mutation is even attempted (the write recorder sees none).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lattice.boards import LocalBoard, resolve_board
from lattice.core.ids import generate_op_id
from lattice.ops import Caller, OpError, execute
from lattice.storage.fs import LATTICE_DIR


@pytest.fixture()
def board(initialized_root: Path, monkeypatch: pytest.MonkeyPatch) -> LocalBoard:
    monkeypatch.delenv("LATTICE_ROOT", raising=False)
    return resolve_board(initialized_root)


def _set_done_policy(board: LocalBoard, policy: dict) -> None:
    path = board.lattice_dir / "config.json"
    config = json.loads(path.read_text())
    config["workflow"]["completion_policies"]["done"] = policy
    path.write_text(json.dumps(config, sort_keys=True, indent=2) + "\n")


def _tree(lattice_dir: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(lattice_dir)): p.read_bytes()
        for p in sorted(lattice_dir.rglob("*"))
        if p.is_file() and p.relative_to(lattice_dir).parts[0] != "locks"
    }


def _refuse(board: LocalBoard, params: dict, attestations: dict | None = None) -> OpError:
    mutations: list[tuple[Path, str]] = []
    before = _tree(board.lattice_dir)
    with pytest.raises(OpError) as exc:
        execute(
            board.lattice_dir,
            "task.complete",
            params,
            Caller(
                actor="agent:t",
                origin={"op_id": generate_op_id()},
                attestations=attestations or {},
            ),
            run_hooks=True,
            on_mutation=lambda path, kind: mutations.append((path, kind)),
        )
    assert mutations == []
    assert _tree(board.lattice_dir) == before
    return exc.value


def _task(board: LocalBoard, status: str | None = None) -> str:
    caller = Caller(actor="agent:t")
    task_id = board.execute("task.create", {"title": "T"}, caller).value["id"]
    if status:
        board.execute(
            "task.status",
            {"task": task_id, "new_status": status, "force": True, "reason": "setup"},
            caller,
        )
    return task_id


class TestRefusedCompletionWritesNothing:
    def test_policy_refusal(self, board: LocalBoard) -> None:
        _set_done_policy(board, {"require_assigned": True})
        task_id = _task(board, "review")
        err = _refuse(board, {"task": task_id, "review": "ok"})
        assert err.code == "COMPLETION_BLOCKED"
        assert err.details["snapshot"]["id"] == task_id

    def test_transition_refusal(self, board: LocalBoard) -> None:
        task_id = _task(board)  # backlog cannot move to review
        err = _refuse(board, {"task": task_id, "review": "ok"})
        assert err.code == "INVALID_TRANSITION"

    def test_stale_attestation_refusal(self, board: LocalBoard) -> None:
        _set_done_policy(board, {"require_reachable_review_commit": True})
        task_id = _task(board, "review")
        err = _refuse(
            board,
            {"task": task_id, "review": "ok"},
            {"review_head": "c" * 40, "reachable_review_commits": []},
        )
        assert err.code == "COMPLETION_BLOCKED"

    def test_body_refusal(self, board: LocalBoard) -> None:
        task_id = _task(board, "review")
        assert _refuse(board, {"task": task_id, "review": "   "}).code == "VALIDATION_ERROR"

    def test_both_bodies_refused_by_params(self, board: LocalBoard) -> None:
        task_id = _task(board, "review")
        err = _refuse(board, {"task": task_id, "review": "a", "review_file": "b"})
        assert err.message == "Provide either --review or --review-file, not both."


def test_accepted_completion_writes_payload_before_events(board: LocalBoard) -> None:
    task_id = _task(board, "review")
    result = board.execute(
        "task.complete", {"task": task_id, "review_file": "From a file."}, Caller(actor="agent:t")
    )
    assert [e["type"] for e in result.events] == [
        "comment_added",
        "artifact_attached",
        "status_changed",
    ]
    art_id = result.events[1]["data"]["artifact_id"]
    lattice_dir = board.root / LATTICE_DIR
    assert (lattice_dir / "artifacts" / "payload" / f"{art_id}.md").read_text() == "From a file."
    meta = json.loads((lattice_dir / "artifacts" / "meta" / f"{art_id}.json").read_text())
    assert meta["created_at"] == result.events[0]["ts"]
    assert len({e["ts"] for e in result.events}) == 1
