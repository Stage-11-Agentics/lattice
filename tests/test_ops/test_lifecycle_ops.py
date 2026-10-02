"""H-2 operations called directly (no CLI): task.update, task.edit_description,
task.assign, task.needs_human, task.claim, task.unclaim, task.archive,
task.unarchive, task.event. Rules, rejections with their codes (and the task
snapshot where the rejection is about the task's state), and idempotent no-ops.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lattice.boards import LocalBoard, resolve_board
from lattice.ops import Caller, OpError

EV_ID = "ev_01J9ZABCDEFGHJKMNPQRSTVWXY"


@pytest.fixture()
def board(initialized_root: Path, monkeypatch: pytest.MonkeyPatch) -> LocalBoard:
    monkeypatch.delenv("LATTICE_ROOT", raising=False)
    return resolve_board(initialized_root)


def _run(board: LocalBoard, op: str, params: dict, actor: str = "agent:t"):  # noqa: ANN202
    return board.execute(op, params, Caller(actor=actor))


def _reject(board: LocalBoard, op: str, params: dict) -> OpError:
    with pytest.raises(OpError) as exc:
        _run(board, op, params)
    return exc.value


def _task(board: LocalBoard, **fields) -> str:  # noqa: ANN003
    return _run(board, "task.create", {"title": "T", **fields}).value["id"]


def _log(board: LocalBoard, task_id: str) -> bytes:
    for base in (board.lattice_dir, board.lattice_dir / "archive"):
        path = base / "events" / f"{task_id}.jsonl"
        if path.exists():
            return path.read_bytes()
    raise AssertionError(f"no log for {task_id}")


class TestUpdate:
    def test_changed_fields_only_one_timestamp(self, board: LocalBoard) -> None:
        task_id = _task(board, priority="medium")
        result = _run(
            board,
            "task.update",
            {
                "task": task_id,
                "pairs": ["priority=medium", "title=New", "tags= a, b ,", "custom_fields.k=v"],
            },
        )
        assert [e["data"]["field"] for e in result.events] == [
            "title",
            "tags",
            "custom_fields.k",
        ]
        assert len({e["ts"] for e in result.events}) == 1
        assert result.value["title"] == "New"
        assert result.value["tags"] == ["a", "b"]
        assert result.value["custom_fields"] == {"k": "v"}
        assert result.events[0]["origin"]["op"] == "task.update"

    def test_no_change_is_idempotent(self, board: LocalBoard) -> None:
        task_id = _task(board, priority="low")
        before = _log(board, task_id)
        result = _run(board, "task.update", {"task": task_id, "pairs": ["priority=low"]})
        assert result.idempotent and result.events == []
        assert result.value == {"message": "No changes"}
        assert _log(board, task_id) == before

    @pytest.mark.parametrize(
        ("pairs", "message"),
        [
            ([], "No field=value pairs provided."),
            (["noequals"], "Invalid field=value pair: 'noequals'. Expected format: field=value."),
            (["status=done"], "Use 'lattice status' to change status."),
            (["assigned_to=agent:x"], "Use 'lattice assign' to change assignment."),
            (["custom_fields.=x"], "Invalid custom field: 'custom_fields.' requires a key name."),
            (["priority=urgent"], "Invalid priority: 'urgent'."),
            (["urgency=soon"], "Invalid urgency: 'soon'."),
            (["complexity=huge"], "Invalid complexity: 'huge'."),
            (["type=epic"], "Invalid task type: 'epic'."),
            (["nonsense=1"], "Unknown or non-updatable field: 'nonsense'."),
        ],
    )
    def test_rejections(self, board: LocalBoard, pairs: list[str], message: str) -> None:
        task_id = _task(board)
        before = _log(board, task_id)
        exc = _reject(board, "task.update", {"task": task_id, "pairs": pairs})
        assert exc.code == "VALIDATION_ERROR"
        assert exc.message.startswith(message)
        assert _log(board, task_id) == before

    def test_task_resolved_before_pairs(self, board: LocalBoard) -> None:
        # Today's order: an unknown task wins over missing pairs.
        assert _reject(board, "task.update", {"task": "NOPE-9", "pairs": []}).code == "NOT_FOUND"

    def test_archived_task_is_not_found(self, board: LocalBoard) -> None:
        task_id = _task(board)
        _run(board, "task.archive", {"task": task_id})
        exc = _reject(board, "task.update", {"task": task_id, "pairs": ["title=x"]})
        assert exc.code == "NOT_FOUND"
        assert exc.message == f"Task {task_id} is archived."


class TestTaskTypes:
    def test_create_and_update_share_actionable_invalid_type_message(
        self, board: LocalBoard
    ) -> None:
        expected = (
            "Invalid task type: 'epic'. Valid types: task, bug, chore. "
            "On a local board, add the type to `.lattice/config.json` `task_types`."
        )
        with pytest.raises(OpError) as create_error:
            _run(board, "task.create", {"title": "Bad", "type": "epic"})
        task_id = _task(board)
        with pytest.raises(OpError) as update_error:
            _run(board, "task.update", {"task": task_id, "pairs": ["type=epic"]})

        for error in (create_error.value, update_error.value):
            assert error.code == "VALIDATION_ERROR"
            assert error.message == expected
            assert error.message.startswith("Invalid task type: 'epic'.")
            assert "host" not in error.message.lower()

    def test_custom_type_succeeds_after_board_config_adds_it(self, board: LocalBoard) -> None:
        config_path = board.lattice_dir / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["task_types"].append("research")
        config_path.write_text(json.dumps(config), encoding="utf-8")

        task_id = _task(board)
        updated = _run(board, "task.update", {"task": task_id, "pairs": ["type=research"]})
        assert updated.value["type"] == "research"
        created = _run(board, "task.create", {"title": "Research", "type": "research"})
        assert created.value["type"] == "research"


class TestEditDescription:
    def test_edit_then_no_op(self, board: LocalBoard) -> None:
        task_id = _task(board, description="old")
        result = _run(board, "task.edit_description", {"task": task_id, "description": "new"})
        assert result.events[0]["data"] == {"field": "description", "from": "old", "to": "new"}
        assert result.value["description"] == "new"
        again = _run(board, "task.edit_description", {"task": task_id, "description": "new"})
        assert again.idempotent and again.events == []
        assert again.value == {"message": "No changes"}

    def test_missing_description_param(self, board: LocalBoard) -> None:
        exc = _reject(board, "task.edit_description", {"task": _task(board)})
        assert exc.code == "VALIDATION_ERROR"
        assert exc.details["reason"] == "MISSING_PARAM"


class TestAssign:
    def test_assign_reassign_unassign(self, board: LocalBoard) -> None:
        task_id = _task(board)
        first = _run(board, "task.assign", {"task": task_id, "actor_id": "agent:a"})
        assert first.events[0]["data"] == {"from": None, "to": "agent:a"}
        assert first.value["assigned_to"] == "agent:a"
        same = _run(board, "task.assign", {"task": task_id, "actor_id": "agent:a"})
        assert same.idempotent and same.value == {"message": "Already assigned to agent:a"}
        for sentinel in ("none", "UNASSIGNED"):
            result = _run(board, "task.assign", {"task": task_id, "actor_id": sentinel})
        assert result.idempotent and result.value == {"message": "Already unassigned"}
        assert _run(board, "task.assign", {"task": task_id, "actor_id": "-"}).idempotent

    def test_invalid_assignee(self, board: LocalBoard) -> None:
        exc = _reject(board, "task.assign", {"task": _task(board), "actor_id": "nocolon"})
        assert exc.code == "INVALID_ACTOR"
        assert "Use 'none', 'unassigned', or '-' to unassign." in exc.message

    def test_unknown_task(self, board: LocalBoard) -> None:
        exc = _reject(board, "task.assign", {"task": "NOPE-99", "actor_id": "agent:a"})
        assert (exc.code, exc.message) == ("NOT_FOUND", "Short ID 'NOPE-99' not found.")


class TestNeedsHuman:
    def test_set_and_clear(self, board: LocalBoard) -> None:
        task_id = _task(board)
        flagged = _run(board, "task.needs_human", {"task": task_id, "flag_reason": "  Need X  "})
        assert flagged.events[0]["type"] == "needs_human_flagged"
        assert flagged.events[0]["data"] == {"reason": "Need X"}
        assert flagged.value["needs_human"]["reason"] == "Need X"
        assert flagged.value["status"] == "backlog"
        cleared = _run(board, "task.needs_human", {"task": task_id, "clear": True, "note": "ok"})
        assert cleared.events[0]["data"] == {"note": "ok"}
        assert not cleared.value.get("needs_human")

    def test_reason_from_file_text(self, board: LocalBoard) -> None:
        task_id = _task(board)
        result = _run(board, "task.needs_human", {"task": task_id, "file": "From a file\n"})
        assert result.events[0]["data"]["reason"] == "From a file"

    def test_provenance_reason_is_separate(self, board: LocalBoard) -> None:
        task_id = _task(board)
        result = _run(
            board, "task.needs_human", {"task": task_id, "flag_reason": "Need", "reason": "why"}
        )
        assert result.events[0]["data"] == {"reason": "Need"}
        assert result.events[0]["provenance"] == {"reason": "why"}

    def test_flag_conflicts_carry_the_snapshot(self, board: LocalBoard) -> None:
        task_id = _task(board)
        exc = _reject(board, "task.needs_human", {"task": task_id, "clear": True})
        assert exc.code == "FLAG_NOT_SET"
        assert exc.details["snapshot"]["id"] == task_id
        _run(board, "task.needs_human", {"task": task_id, "flag_reason": "first"})
        exc = _reject(board, "task.needs_human", {"task": task_id, "flag_reason": "again"})
        assert exc.code == "FLAG_ALREADY_SET"
        assert "(by agent:t since " in exc.message and ": first). Clear it" in exc.message
        assert exc.details["snapshot"]["last_event_id"].startswith("ev_")

    @pytest.mark.parametrize(
        ("params", "message"),
        [
            ({}, "REASON is required"),
            ({"flag_reason": "   "}, "REASON is required"),
            ({"flag_reason": "a", "file": "b"}, "Provide either REASON or --file, not both."),
            ({"flag_reason": "a", "clear": True}, "REASON / --file is only for setting"),
            ({"flag_reason": "a", "note": "n"}, "--note is only for clearing"),
        ],
    )
    def test_argument_rejections(self, board: LocalBoard, params: dict, message: str) -> None:
        exc = _reject(board, "task.needs_human", {"task": _task(board), **params})
        assert exc.code == "VALIDATION_ERROR"
        assert exc.message.startswith(message)

    def test_task_checked_before_arguments(self, board: LocalBoard) -> None:
        assert _reject(board, "task.needs_human", {"task": "NOPE-3"}).code == "NOT_FOUND"


class TestClaim:
    def test_claim_is_not_exclusive(self, board: LocalBoard) -> None:
        task_id = _task(board)
        first = _run(board, "task.claim", {"task": task_id, "surface": "surface:7"})
        assert first.value == {
            "task_id": task_id,
            "short_id": task_id,
            "surface": "surface:7",
            "workspace": None,
        }
        assert first.events[0]["data"] == {"surface": "surface:7"}
        second = _run(
            board,
            "task.claim",
            {"task": task_id, "surface": "surface:8", "workspace": "workspace:1"},
            actor="agent:other",
        )
        assert second.events[0]["data"] == {"surface": "surface:8", "workspace": "workspace:1"}
        assert second.task["c11_surface"] == "surface:8"

    def test_missing_surface_after_task(self, board: LocalBoard) -> None:
        assert _reject(board, "task.claim", {"task": "NOPE-1"}).code == "NOT_FOUND"
        exc = _reject(board, "task.claim", {"task": _task(board)})
        assert exc.code == "MISSING_SURFACE"

    def test_unclaim_reports_the_old_surface(self, board: LocalBoard) -> None:
        task_id = _task(board)
        _run(board, "task.claim", {"task": task_id, "surface": "surface:7"})
        result = _run(board, "task.unclaim", {"task": task_id})
        assert result.value["surface"] == "surface:7"
        assert result.task.get("c11_surface") is None
        # Unbound already: still recorded, as today.
        again = _run(board, "task.unclaim", {"task": task_id})
        assert again.value["surface"] is None and len(again.events) == 1


class TestArchive:
    def test_archive_unarchive_round_trip(self, board: LocalBoard) -> None:
        task_id = _task(board)
        archived = _run(board, "task.archive", {"task": task_id})
        assert archived.value["type"] == "task_archived" and not archived.idempotent
        assert (board.lattice_dir / "archive" / "events" / f"{task_id}.jsonl").exists()
        assert not (board.lattice_dir / "events" / f"{task_id}.jsonl").exists()
        lifecycle = (board.lattice_dir / "events" / "_lifecycle.jsonl").read_text()
        assert archived.value["id"] in lifecycle

        again = _run(board, "task.archive", {"task": task_id})
        assert again.idempotent and again.events == []
        assert again.value == archived.value

        restored = _run(board, "task.unarchive", {"task": task_id})
        assert restored.value["type"] == "task_unarchived"
        assert (board.lattice_dir / "events" / f"{task_id}.jsonl").exists()
        noop = _run(board, "task.unarchive", {"task": task_id})
        assert noop.idempotent and noop.value == restored.value

    def test_unarchive_never_archived_returns_creation(self, board: LocalBoard) -> None:
        task_id = _task(board)
        result = _run(board, "task.unarchive", {"task": task_id})
        assert result.idempotent and result.value["type"] == "task_created"

    def test_unknown_ids_are_marked_unresolved(self, board: LocalBoard) -> None:
        for raw in ("NOPE-4", "junk!"):
            exc = _reject(board, "task.archive", {"task": raw})
            assert exc.code in {"NOT_FOUND", "INVALID_ID"}
            assert exc.details == {"reason": "UNRESOLVED_TASK"}

    def test_absent_ulid_is_not_found(self, board: LocalBoard) -> None:
        exc = _reject(board, "task.archive", {"task": "task_00000000000000000000000099"})
        assert exc.code == "NOT_FOUND"
        assert exc.message.endswith("no authoritative event log exists")
        assert "reason" not in exc.details


class TestEvent:
    def test_custom_event_and_idempotent_retry(self, board: LocalBoard) -> None:
        task_id = _task(board)
        params = {"task": task_id, "event_type": "x_deploy", "data": '{"env": "s"}', "id": EV_ID}
        first = _run(board, "task.event", params)
        assert first.value["id"] == EV_ID and first.value["data"] == {"env": "s"}
        assert first.events == [first.value]
        again = _run(board, "task.event", params)
        assert again.idempotent and again.events == []
        assert again.value["id"] == EV_ID
        exc = _reject(board, "task.event", {**params, "data": '{"env": "p"}'})
        assert (exc.code, exc.message) == (
            "CONFLICT",
            f"Conflict: event {EV_ID} exists with different data.",
        )

    @pytest.mark.parametrize(
        ("params", "code", "message"),
        [
            ({"event_type": "status_changed"}, "VALIDATION_ERROR", "Event type 'status_changed'"),
            ({"event_type": "deploy"}, "VALIDATION_ERROR", "Invalid custom event type"),
            ({"event_type": "x_a", "data": "not json"}, "VALIDATION_ERROR", "Invalid JSON"),
            ({"event_type": "x_a", "data": "[1]"}, "VALIDATION_ERROR", "--data must be"),
            ({"event_type": "x_a", "id": "ev_bad"}, "INVALID_ID", "Invalid event ID format"),
        ],
    )
    def test_rejections(self, board: LocalBoard, params: dict, code: str, message: str) -> None:
        task_id = _task(board)
        before = _log(board, task_id)
        exc = _reject(board, "task.event", {"task": task_id, **params})
        assert exc.code == code and exc.message.startswith(message)
        assert _log(board, task_id) == before
