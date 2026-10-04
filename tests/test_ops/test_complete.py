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
from lattice.ops.task_complete import normalize_via
from lattice.storage.fs import LATTICE_DIR
from lattice.storage.operations import read_task_authority


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


def test_via_task_from_backlog_records_canonical_bundle_and_force_reason(
    board: LocalBoard,
) -> None:
    target_id = _task(board)
    task_id = _task(board)
    target = read_task_authority(board.lattice_dir, target_id)
    assert target is not None
    target_via = {
        "kind": "task",
        "id": target_id,
        "short_id": target.snapshot.get("short_id"),
    }

    result = board.execute(
        "task.complete",
        {
            "task": task_id,
            "review": "Reviewed in the bundle.",
            "via": target_id,
            "reason": "landed",
        },
        Caller(actor="agent:t"),
    )

    assert [event["type"] for event in result.events] == [
        "comment_added",
        "status_changed",
        "artifact_attached",
        "status_changed",
    ]
    initial_move = result.events[1]
    assert initial_move["data"]["from"] == "backlog"
    assert initial_move["data"]["to"] == "review"
    assert initial_move["data"]["force"] is True
    expected_target = target_via["short_id"] or target_id
    assert expected_target in initial_move["data"]["reason"]
    assert initial_move["provenance"]["reason"] == "landed"
    assert result.events[-1]["data"]["via"] == target_via
    assert result.task is not None and result.task["status"] == "done"


def test_via_pr_reference_is_normalized_in_completion_event(board: LocalBoard) -> None:
    task_id = _task(board, "in_progress")

    result = board.execute(
        "task.complete",
        {
            "task": task_id,
            "review": "Reviewed.",
            "via": "HTTPS://GitHub.Example/Org/Repo/PR/7",
        },
        Caller(actor="agent:t"),
    )

    assert result.events[-1]["data"]["via"] == {
        "kind": "pull_request",
        "reference": "https://github.example/Org/Repo/PR/7",
    }
    assert "force" not in result.events[1]["data"]


def test_via_does_not_satisfy_reachable_gate_without_own_branch_link(board: LocalBoard) -> None:
    _set_done_policy(board, {"require_reachable_review_commit": True})
    task_id = _task(board)
    primary_id = _task(board)

    err = _refuse(
        board,
        {"task": task_id, "review": "Reviewed in the bundle.", "via": primary_id},
        {
            "review_head": "c" * 40,
            "reachable_review_commits": [
                {"sha": "c" * 40, "branch": None, "exists": True, "reachable": False}
            ],
        },
    )

    assert err.code == "COMPLETION_BLOCKED"
    assert "linked branch" in err.message


def test_via_accepts_ascii_pull_request_number(board: LocalBoard) -> None:
    task_id = _task(board, "in_progress")

    result = board.execute(
        "task.complete",
        {"task": task_id, "review": "Reviewed.", "via": "#123"},
        Caller(actor="agent:t"),
    )

    assert result.events[-1]["data"]["via"] == {
        "kind": "pull_request",
        "reference": "#123",
    }


def test_via_task_target_may_be_archived(board: LocalBoard) -> None:
    target_id = _task(board)
    board.execute("task.archive", {"task": target_id}, Caller(actor="agent:t"))
    task_id = _task(board, "review")

    result = board.execute(
        "task.complete",
        {"task": task_id, "review": "Reviewed.", "via": target_id},
        Caller(actor="agent:t"),
    )

    assert result.events[-1]["data"]["via"]["id"] == target_id


def test_via_review_status_records_only_three_events(board: LocalBoard) -> None:
    task_id = _task(board, "review")

    result = board.execute(
        "task.complete",
        {"task": task_id, "review": "Reviewed.", "via": "#123"},
        Caller(actor="agent:t"),
    )

    assert [event["type"] for event in result.events] == [
        "comment_added",
        "artifact_attached",
        "status_changed",
    ]
    assert result.events[-1]["data"]["via"] == {
        "kind": "pull_request",
        "reference": "#123",
    }


def test_via_allowed_graph_edge_does_not_set_force(board: LocalBoard) -> None:
    task_id = _task(board, "planned")
    config_path = board.lattice_dir / "config.json"
    config = json.loads(config_path.read_text())
    config["workflow"]["transitions"]["planned"].append("review")
    config_path.write_text(json.dumps(config, sort_keys=True, indent=2) + "\n")

    result = board.execute(
        "task.complete",
        {"task": task_id, "review": "Reviewed.", "via": "#123"},
        Caller(actor="agent:t"),
    )

    assert result.events[1]["data"]["from"] == "planned"
    assert result.events[1]["data"]["to"] == "review"
    assert "force" not in result.events[1]["data"]


@pytest.mark.parametrize(
    "via",
    [
        "#٠",
        "https://user@example.com/pull/1",
        "http://example.com/path?token=x",
        "http://example.com/path#frag",
        "http://example.com/path with-space",
        "http:///path",
        "http://example.com/path\x01control",
        "https://exämple.com/pull/1",
        "https://example.com/\x85",
        "https://example.com/" + ("x" * 256),
    ],
)
def test_invalid_via_forms_are_rejected_without_writes(board: LocalBoard, via: str) -> None:
    task_id = _task(board)
    error = _refuse(board, {"task": task_id, "review": "Reviewed.", "via": via})

    assert error.code == "VALIDATION_ERROR"
    assert "--via" in error.message
    assert "<task ID>" in error.message
    assert "#<N>" in error.message
    assert "http(s)://<host>/" in error.message


def test_non_string_via_operation_param_is_a_validation_error(board: LocalBoard) -> None:
    task_id = _task(board)

    error = _refuse(board, {"task": task_id, "review": "Reviewed.", "via": 123})

    assert error.code == "VALIDATION_ERROR"
    assert "parameter 'via'" in error.message
    assert "got int" in error.message


def test_via_normalizer_rejects_non_string_values_with_accepted_forms() -> None:
    with pytest.raises(OpError) as exc:
        normalize_via(123)

    assert exc.value.code == "VALIDATION_ERROR"
    assert "--via" in exc.value.message
    assert "<task ID>" in exc.value.message
    assert "#<N>" in exc.value.message
    assert "http(s)://<host>/" in exc.value.message


def test_via_normalizer_rejects_control_characters_in_task_reference() -> None:
    with pytest.raises(OpError) as exc:
        normalize_via("task\x85id")

    assert exc.value.code == "VALIDATION_ERROR"
    assert "control characters" in exc.value.message


def test_via_missing_self_or_erased_task_is_refused_without_writes(board: LocalBoard) -> None:
    task_id = _task(board)
    for via in ("task_01JBBBBBBBBBBBBBBBBBBBBBBB", task_id):
        error = _refuse(board, {"task": task_id, "review": "Reviewed.", "via": via})
        assert error.code == "VALIDATION_ERROR"
        assert "--via" in error.message

    target_id = _task(board)
    board.execute(
        "task.erase",
        {"task": target_id, "reason": "test"},
        Caller(actor="agent:t"),
    )
    error = _refuse(board, {"task": task_id, "review": "Reviewed.", "via": target_id})
    assert error.code == "VALIDATION_ERROR"
    assert "--via" in error.message


@pytest.mark.parametrize("status", ["done", "cancelled", "foreign"])
def test_via_completion_refuses_terminal_or_unknown_current_status(
    board: LocalBoard, status: str
) -> None:
    current_status = "backlog" if status == "foreign" else status
    task_id = _task(board, current_status)
    if status == "foreign":
        config_path = board.lattice_dir / "config.json"
        config = json.loads(config_path.read_text())
        config["workflow"]["statuses"].remove("backlog")
        config_path.write_text(json.dumps(config, sort_keys=True, indent=2) + "\n")

    error = _refuse(
        board,
        {"task": task_id, "review": "Reviewed.", "via": "#123"},
    )
    assert error.code == "INVALID_TRANSITION"
    assert current_status in error.message


def test_via_refuses_done_even_if_custom_workflow_reopens_it(board: LocalBoard) -> None:
    task_id = _task(board, "done")
    config_path = board.lattice_dir / "config.json"
    config = json.loads(config_path.read_text())
    config["workflow"]["transitions"]["done"] = ["review"]
    config_path.write_text(json.dumps(config, sort_keys=True, indent=2) + "\n")

    error = _refuse(board, {"task": task_id, "review": "Reviewed.", "via": "#123"})

    assert error.code == "INVALID_TRANSITION"
    assert "done" in error.message


def test_via_does_not_bypass_completion_policy(board: LocalBoard) -> None:
    _set_done_policy(board, {"require_assigned": True})
    task_id = _task(board)

    error = _refuse(
        board,
        {"task": task_id, "review": "Reviewed.", "via": "#123"},
    )

    assert error.code == "COMPLETION_BLOCKED"


def test_pre_rework_review_artifact_does_not_satisfy_current_cycle(board: LocalBoard) -> None:
    task_id = _task(board, "review")
    caller = Caller(actor="agent:t")
    board.execute(
        "task.attach",
        {"task": task_id, "inline": "FAIL (implementation-level)", "role": "review"},
        caller,
    )
    board.execute(
        "task.status",
        {
            "task": task_id,
            "new_status": "in_progress",
            "force": True,
            "reason": "begin rework",
        },
        caller,
    )
    board.execute(
        "task.status",
        {"task": task_id, "new_status": "review", "force": True, "reason": "re-enter review"},
        caller,
    )

    with pytest.raises(OpError) as exc:
        board.execute("task.status", {"task": task_id, "new_status": "done"}, caller)

    assert exc.value.code == "COMPLETION_BLOCKED"
    assert "current-cycle review evidence" in exc.value.message

    board.execute(
        "task.attach",
        {"task": task_id, "inline": "PASS", "role": "review"},
        caller,
    )
    result = board.execute("task.status", {"task": task_id, "new_status": "done"}, caller)
    assert result.value["status"] == "done"


def test_missing_and_archived_tasks_are_not_found(board: LocalBoard) -> None:
    missing = "task_01AAAAAAAAAAAAAAAAAAAAAAAA"
    err = _refuse(board, {"task": missing, "review": "ok"})
    assert (err.code, err.message) == ("NOT_FOUND", f"Task {missing} not found.")
    task_id = _task(board, "review")
    board.execute("task.archive", {"task": task_id}, Caller(actor="agent:t"))
    err = _refuse(board, {"task": task_id, "review": "ok"})
    assert (err.code, err.message) == ("NOT_FOUND", f"Task {task_id} is archived.")
