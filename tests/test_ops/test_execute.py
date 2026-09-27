"""``lattice.ops.execute`` and ``LocalBoard``: step order, actors, typed
errors, and the first three operations called directly (no CLI)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lattice.boards import LocalBoard, resolve_board
from lattice.ops import Caller, OpError, execute

OP_ID = "op_01J9ZABCDEFGHJKMNPQRSTVWXY"


@pytest.fixture()
def board(initialized_root: Path, monkeypatch: pytest.MonkeyPatch) -> LocalBoard:
    monkeypatch.delenv("LATTICE_ROOT", raising=False)
    return resolve_board(initialized_root)


def _code(fn) -> str:  # noqa: ANN001
    with pytest.raises(OpError) as exc:
        fn()
    return exc.value.code


def _run(board: LocalBoard, op: str, params: dict, actor: str = "agent:t"):  # noqa: ANN202
    return board.execute(op, params, Caller(actor=actor))


class TestResolveBoard:
    def test_no_board(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("LATTICE_ROOT", raising=False)
        with pytest.raises(OpError) as exc:
            resolve_board(tmp_path)
        assert exc.value.code == "NOT_INITIALIZED"
        assert exc.value.message == (
            "Not a Lattice project (no .lattice/ found). Run 'lattice init' first."
        )

    def test_bad_lattice_root(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LATTICE_ROOT", str(tmp_path / "missing"))
        with pytest.raises(OpError) as exc:
            resolve_board(tmp_path)
        assert exc.value.code == "NOT_INITIALIZED"
        assert "does not exist" in exc.value.message


class TestExecuteSteps:
    def test_unknown_op(self, board: LocalBoard) -> None:
        assert _code(lambda: _run(board, "task.nope", {})) == "UNKNOWN_OP"

    def test_params_checked_before_actor(self, board: LocalBoard) -> None:
        # No actor and a bad param: the param error wins (step 2 before step 3).
        with pytest.raises(OpError) as exc:
            board.execute("task.create", {"titel": "x"}, Caller())
        assert exc.value.code == "VALIDATION_ERROR"
        assert exc.value.details["reason"] == "UNKNOWN_PARAM"

    def test_missing_actor(self, board: LocalBoard) -> None:
        with pytest.raises(OpError) as exc:
            board.execute("task.create", {"title": "x"}, Caller())
        assert (exc.value.code, exc.value.message) == (
            "MISSING_ACTOR",
            "Either --name (session) or --actor (legacy) is required.",
        )

    def test_invalid_actor_and_on_behalf_of(self, board: LocalBoard) -> None:
        assert _code(lambda: _run(board, "task.create", {"title": "x"}, "nocolon")) == (
            "INVALID_ACTOR"
        )
        assert (
            _code(lambda: _run(board, "task.create", {"title": "x", "on_behalf_of": "bad"}))
            == "INVALID_ACTOR"
        )

    def test_session_actor_resolves_and_is_touched(self, board: LocalBoard) -> None:
        from lattice.storage.sessions import create_session, resolve_session

        session = create_session(
            board.lattice_dir, base_name="Argus", model="claude-opus", framework="cc"
        )
        name = session.name
        before = resolve_session(board.lattice_dir, name)["last_active"]
        result = board.execute("task.create", {"title": "x"}, Caller(actor_name=name))
        actor = result.events[0]["actor"]
        assert actor["name"] == name and actor["base_name"] == "Argus"
        assert resolve_session(board.lattice_dir, name)["last_active"] >= before

    def test_unknown_session(self, board: LocalBoard) -> None:
        with pytest.raises(OpError) as exc:
            board.execute("task.create", {"title": "x"}, Caller(actor_name="Nobody-1"))
        assert exc.value.code == "SESSION_NOT_FOUND"
        assert exc.value.message == (
            "No active session named 'Nobody-1'. Start one with 'lattice session start'."
        )

    def test_rejected_operation_writes_nothing(self, board: LocalBoard) -> None:
        ld = board.lattice_dir
        before = {p: p.read_bytes() for p in ld.rglob("*") if p.is_file()}
        assert _code(lambda: _run(board, "task.create", {"title": "x", "status": "zzz"})) == (
            "VALIDATION_ERROR"
        )
        after = {
            p: p.read_bytes() for p in ld.rglob("*") if p.is_file() and "locks" not in p.parts
        }
        assert after == {p: b for p, b in before.items() if "locks" not in p.parts}

    def test_supplied_op_id_is_kept(self, board: LocalBoard) -> None:
        result = board.execute(
            "task.create", {"title": "x"}, Caller(actor="agent:t", origin={"op_id": OP_ID})
        )
        assert result.events[0]["origin"]["op_id"] == OP_ID
        assert result.events[0]["origin"]["op"] == "task.create"

    def test_execute_requires_an_op_id(self, board: LocalBoard) -> None:
        with pytest.raises(OpError) as exc:
            execute(
                board.lattice_dir,
                "task.create",
                {"title": "x"},
                Caller(actor="a:b"),
                run_hooks=False,
            )
        assert exc.value.details["reason"] == "INVALID_OP_ID"

    def test_run_hooks_false_fires_no_hook(
        self, board: LocalBoard, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fired: list[str] = []
        monkeypatch.setattr(
            "lattice.storage.operations.execute_hooks",
            lambda _c, _ld, _t, event: fired.append(event["type"]),
        )
        caller = Caller(actor="agent:t", origin={"op_id": OP_ID})
        execute(board.lattice_dir, "task.create", {"title": "x"}, caller, run_hooks=False)
        assert fired == []
        board.execute("task.create", {"title": "y"}, Caller(actor="agent:t"))
        assert fired == ["task_created"]


class TestStorageMapping:
    def _task(self, board: LocalBoard) -> str:
        return _run(board, "task.create", {"title": "t"}).value["id"]

    def test_expectation_conflict_through_execute(self, board: LocalBoard) -> None:
        task_id = self._task(board)
        with pytest.raises(OpError) as exc:
            board.execute(
                "task.comment",
                {"task": task_id, "text": "x"},
                Caller(actor="agent:t", expect_last_event_id="ev_01J9ZABCDEFGHJKMNPQRSTVWXZ"),
            )
        assert exc.value.code == "CONFLICT"
        assert exc.value.details["snapshot"]["id"] == task_id

    def test_placement_error_is_not_found(
        self, board: LocalBoard, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        task_id = self._task(board)
        # The task is archived between the pre-check and the lock.
        from lattice.ops.base import OpContext

        monkeypatch.setattr(OpContext, "require_active", lambda self, tid: {})
        from lattice.storage.operations import mutate_task_events
        from lattice.core.events import create_event

        mutate_task_events(
            board.lattice_dir,
            task_id,
            [create_event("task_archived", task_id, "agent:t", {})],
            destination="archived",
            may_emit_lifecycle=True,
            run_hooks=False,
        )
        with pytest.raises(OpError) as exc:
            _run(board, "task.comment", {"task": task_id, "text": "x"})
        assert (exc.value.code, exc.value.message) == ("NOT_FOUND", f"Task {task_id} is archived.")

    def test_corrupt_log_is_integrity_error(
        self, board: LocalBoard, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        task_id = self._task(board)
        from lattice.ops.base import OpContext

        monkeypatch.setattr(OpContext, "require_active", lambda self, tid: {})
        log = board.lattice_dir / "events" / f"{task_id}.jsonl"
        log.write_bytes(log.read_bytes() + b"{not json\n")
        with pytest.raises(OpError) as exc:
            _run(board, "task.comment", {"task": task_id, "text": "x"})
        assert exc.value.code == "INTEGRITY_ERROR"
        assert "invalid JSONL record" in exc.value.message


class TestFirstSlice:
    def test_create_value_and_idempotence(self, board: LocalBoard) -> None:
        params = {"title": "T", "id": "task_01J9ZABCDEFGHJKMNPQRSTVWXY", "tag": ["a"]}
        first = _run(board, "task.create", params)
        again = _run(board, "task.create", params)
        assert first.idempotent is False and again.idempotent is True
        assert again.events == []
        assert first.value["tags"] == ["a"] and first.task == first.value
        with pytest.raises(OpError) as exc:
            _run(board, "task.create", {**params, "title": "Other"})
        assert exc.value.code == "CONFLICT"
        plan = board.lattice_dir / "plans" / "task_01J9ZABCDEFGHJKMNPQRSTVWXY.md"
        assert plan.read_text().startswith("# ")

    def test_status_auto_assigns_and_gates_the_plan(self, board: LocalBoard) -> None:
        task_id = _run(board, "task.create", {"title": "T"}).value["id"]
        result = _run(board, "task.status", {"task": task_id, "new_status": "in_planning"})
        assert [e["type"] for e in result.events] == ["assignment_changed", "status_changed"]
        assert result.value["assigned_to"] == "agent:t"
        _run(board, "task.status", {"task": task_id, "new_status": "planned"})
        with pytest.raises(OpError) as exc:
            _run(board, "task.status", {"task": task_id, "new_status": "in_progress"})
        assert exc.value.code == "PLAN_REQUIRED"
        forced = _run(
            board,
            "task.status",
            {"task": task_id, "new_status": "in_progress", "force": True, "reason": "r"},
        )
        assert forced.events[-1]["data"] == {
            "from": "planned",
            "to": "in_progress",
            "force": True,
            "reason": "r",
        }

    def test_backward_move_appends_the_plan_reset(self, board: LocalBoard) -> None:
        task_id = _run(board, "task.create", {"title": "T"}).value["id"]
        for status in ("in_planning", "planned"):
            _run(board, "task.status", {"task": task_id, "new_status": status})
        _run(
            board,
            "task.status",
            {"task": task_id, "new_status": "in_planning", "force": True, "reason": "redo"},
        )
        plan = (board.lattice_dir / "plans" / f"{task_id}.md").read_text()
        assert plan.rstrip().splitlines()[-1].startswith("## Reset ")
        assert plan.rstrip().endswith(" by agent:t")

    def test_comment_rules(self, board: LocalBoard) -> None:
        task_id = _run(board, "task.create", {"title": "T"}).value["id"]
        assert _code(lambda: _run(board, "task.comment", {"task": task_id})) == "VALIDATION_ERROR"
        assert (
            _code(lambda: _run(board, "task.comment", {"task": task_id, "text": "a", "file": "b"}))
            == "VALIDATION_ERROR"
        )
        assert (
            _code(
                lambda: _run(board, "task.comment", {"task": task_id, "text": "a", "role": "zz"})
            )
            == "INVALID_ROLE"
        )
        result = _run(board, "task.comment", {"task": task_id, "file": "from a file\n"})
        assert result.events[0]["data"]["body"].startswith("from a file")

    def test_record_auto_review(self, board: LocalBoard) -> None:
        task_id = _run(board, "task.create", {"title": "T"}).value["id"]
        params = {
            "task": task_id,
            "review_type": "code",
            "mode": "single",
            "log_path": "/x.log",
            "spawned_at": "2026-09-26T00:00:00Z",
            "pid": 42,
            "trigger_status_event_id": "ev_01J9ZABCDEFGHJKMNPQRSTVWXZ",
        }
        result = _run(board, "task.record_auto_review", params, "agent:lattice-auto-review")
        event = result.events[0]
        assert event["type"] == "auto_review_spawned"
        assert event["actor"] == "agent:lattice-auto-review"
        assert "reviewed_worktree" not in event["data"]
        assert event["origin"]["op"] == "task.record_auto_review"

    def test_no_system_exit_escapes_an_operation(self, board: LocalBoard) -> None:
        for op, params in [
            ("task.create", {"title": "x", "priority": "zzz"}),
            ("task.status", {"task": "PAR-999", "new_status": "done"}),
            ("task.comment", {"task": "junk!", "text": "x"}),
        ]:
            try:
                _run(board, op, params)
            except OpError:
                pass
            except BaseException as exc:  # noqa: BLE001
                pytest.fail(f"{op} raised {type(exc).__name__}, not OpError")


def test_json_value_matches_cli_output(
    initialized_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``OpResult.value`` is exactly the ``data`` the CLI prints under ``--json``."""
    from click.testing import CliRunner

    from lattice.cli.main import cli

    monkeypatch.delenv("LATTICE_ROOT", raising=False)
    env = {"LATTICE_ROOT": str(initialized_root)}
    out = CliRunner().invoke(cli, ["create", "J", "--actor", "agent:t", "--json"], env=env)
    data = json.loads(out.output)["data"]
    snapshot = json.loads(
        (initialized_root / ".lattice" / "tasks" / f"{data['id']}.json").read_text()
    )
    assert data == snapshot
