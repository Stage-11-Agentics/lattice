"""MCP tools call operations (AC-5's MCP row, SPEC §12, §14 G-6).

MCP status changes now apply the CLI's rules: the plan gate, the review-cycle
limit, and the completion policies. Every write lands through a named
operation (its events carry ``origin.op``), each call's ``lattice_root`` picks
its board, and the tools also work on a checkout bound to a server.
"""

from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path

import pytest

import lattice.boards
from lattice.core.config import default_config, serialize_config
from lattice.mcp import tools
from lattice.mcp.tools import (
    LatticeToolError,
    lattice_comment,
    lattice_create,
    lattice_list,
    lattice_show,
    lattice_status,
    lattice_update,
)
from lattice.storage.fs import LATTICE_DIR, atomic_write, ensure_lattice_dirs

ACTOR = "agent:mcp-test"


def _events(lattice_dir: Path, task_id: str) -> list[dict]:
    path = lattice_dir / "events" / f"{task_id}.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def _set_config(lattice_dir: Path, **workflow: object) -> None:
    config = json.loads((lattice_dir / "config.json").read_text())
    config["workflow"].update(workflow)
    atomic_write(lattice_dir / "config.json", serialize_config(config))


def _to_planned(task_id: str) -> None:
    lattice_status(task_id=task_id, new_status="in_planning", actor=ACTOR)
    lattice_status(task_id=task_id, new_status="planned", actor=ACTOR)


class TestPlanGate:
    def test_status_to_in_progress_with_scaffold_plan_is_refused(
        self, lattice_env: Path, lattice_dir: Path
    ) -> None:
        task = lattice_create(title="Gate me", actor=ACTOR)
        _to_planned(task["id"])
        before = _events(lattice_dir, task["id"])

        with pytest.raises(LatticeToolError) as info:
            lattice_status(task_id=task["id"], new_status="in_progress", actor=ACTOR)

        assert info.value.code == "PLAN_REQUIRED"
        assert isinstance(info.value, ValueError)
        assert info.value.details["snapshot"]["status"] == "planned"
        assert _events(lattice_dir, task["id"]) == before

    def test_written_plan_passes_the_gate(self, lattice_env: Path, lattice_dir: Path) -> None:
        task = lattice_create(title="Planned", actor=ACTOR)
        _to_planned(task["id"])
        plan = lattice_dir / "plans" / f"{task['id']}.md"
        plan.write_text("# Plan\n\nDo the thing, then check it.\n")

        snapshot = lattice_status(task_id=task["id"], new_status="in_progress", actor=ACTOR)

        assert snapshot["status"] == "in_progress"

    def test_force_with_reason_overrides_the_gate(self, lattice_env: Path) -> None:
        task = lattice_create(title="Forced", actor=ACTOR)
        _to_planned(task["id"])

        snapshot = lattice_status(
            task_id=task["id"],
            new_status="in_progress",
            actor=ACTOR,
            force=True,
            reason="spike",
        )

        assert snapshot["status"] == "in_progress"


class TestOtherStatusRules:
    def test_review_cycle_limit(self, lattice_env: Path, lattice_dir: Path) -> None:
        _set_config(lattice_dir, review_cycle_limit=1)
        task = lattice_create(title="Cycles", actor=ACTOR)
        tid = task["id"]
        (lattice_dir / "plans" / f"{tid}.md").write_text("# Plan\n\nOne line.\n")
        lattice_status(task_id=tid, new_status="in_progress", actor=ACTOR, force=True, reason="x")
        lattice_status(task_id=tid, new_status="review", actor=ACTOR)
        lattice_status(task_id=tid, new_status="in_progress", actor=ACTOR)
        lattice_status(task_id=tid, new_status="review", actor=ACTOR)

        with pytest.raises(LatticeToolError) as info:
            lattice_status(task_id=tid, new_status="in_progress", actor=ACTOR)

        assert info.value.code == "REVIEW_CYCLE_LIMIT"

    def test_invalid_transition_uses_the_cli_code(self, lattice_env: Path) -> None:
        task = lattice_create(title="Jump", actor=ACTOR)

        with pytest.raises(LatticeToolError, match="Valid transitions from backlog") as info:
            lattice_status(task_id=task["id"], new_status="review", actor=ACTOR)

        assert info.value.code == "INVALID_TRANSITION"

    def test_completion_policy_uses_the_cli_code(
        self, lattice_env: Path, lattice_dir: Path
    ) -> None:
        config = json.loads((lattice_dir / "config.json").read_text())
        config["workflow"]["completion_policies"] = {"done": {"require_roles": ["review"]}}
        atomic_write(lattice_dir / "config.json", serialize_config(config))
        task = lattice_create(title="Policy", actor=ACTOR)
        tid = task["id"]
        lattice_status(task_id=tid, new_status="in_progress", actor=ACTOR, force=True, reason="x")
        lattice_status(task_id=tid, new_status="review", actor=ACTOR)

        with pytest.raises(LatticeToolError) as info:
            lattice_status(task_id=tid, new_status="done", actor=ACTOR)

        assert info.value.code == "COMPLETION_BLOCKED"

    def test_entering_active_work_auto_assigns_like_the_cli(
        self, lattice_env: Path, lattice_dir: Path
    ) -> None:
        task = lattice_create(title="Claim", actor=ACTOR)

        snapshot = lattice_status(task_id=task["id"], new_status="in_planning", actor=ACTOR)

        assert snapshot["assigned_to"] == ACTOR
        types = [e["type"] for e in _events(lattice_dir, task["id"])]
        assert types[-2:] == ["assignment_changed", "status_changed"]

    def test_backward_move_appends_the_plan_reset_heading(
        self, lattice_env: Path, lattice_dir: Path
    ) -> None:
        task = lattice_create(title="Reset", actor=ACTOR)
        tid = task["id"]
        plan = lattice_dir / "plans" / f"{tid}.md"
        plan.write_text("# Plan\n\nOne line.\n")
        lattice_status(task_id=tid, new_status="in_progress", actor=ACTOR, force=True, reason="x")
        lattice_status(task_id=tid, new_status="review", actor=ACTOR)

        lattice_status(task_id=tid, new_status="in_planning", actor=ACTOR)

        assert "## Reset " in plan.read_text()
        assert f"by {ACTOR}" in plan.read_text()


class TestWritesAreOperations:
    def test_events_name_their_operation(self, lattice_env: Path, lattice_dir: Path) -> None:
        task = lattice_create(title="Origin", actor=ACTOR)
        lattice_comment(task_id=task["id"], text="hello", actor=ACTOR)
        lattice_update(task_id=task["id"], actor=ACTOR, fields={"priority": "high"})

        ops = [e["origin"]["op"] for e in _events(lattice_dir, task["id"])]

        assert ops == ["task.create", "task.comment", "task.update"]
        op_ids = [e["origin"]["op_id"] for e in _events(lattice_dir, task["id"])]
        assert len(set(op_ids)) == 3

    def test_invalid_actor_is_the_operations_refusal(self, lattice_env: Path) -> None:
        with pytest.raises(LatticeToolError) as info:
            lattice_create(title="Who", actor="nobody")

        assert info.value.code == "INVALID_ACTOR"

    def test_update_keeps_json_values(self, lattice_env: Path, lattice_dir: Path) -> None:
        """``custom_fields`` is an open object: MCP values round-trip with their types."""
        task = lattice_create(title="Fields", actor=ACTOR)
        values = {
            "custom_fields.points": 3,
            "custom_fields.ratio": 0.5,
            "custom_fields.done": True,
            "custom_fields.off": False,
            "custom_fields.meta": {"a": 1, "nested": {"b": [1, "two", None]}},
            "custom_fields.list": [1, {"c": 2}],
            "custom_fields.a=b": "eq",
            "custom_fields.x=": {"k": "v"},
            "custom_fields.text": "3",
        }

        snapshot = lattice_update(task_id=task["id"], actor=ACTOR, fields=values)

        expected = {name[len("custom_fields.") :]: value for name, value in values.items()}
        assert snapshot["custom_fields"] == expected
        # Stored that way too: the event data and the replayed snapshot on disk.
        updates = [e for e in _events(lattice_dir, task["id"]) if e["type"] == "field_updated"]
        assert {e["data"]["field"]: e["data"]["to"] for e in updates} == values
        assert lattice_show(task_id=task["id"], include_events=False)["custom_fields"] == expected

        cleared = lattice_update(
            task_id=task["id"], actor=ACTOR, fields={"custom_fields.points": None}
        )
        assert cleared["custom_fields"]["points"] is None

    def test_update_tags_as_list_or_text(self, lattice_env: Path) -> None:
        task = lattice_create(title="Tags", actor=ACTOR)

        listed = lattice_update(task_id=task["id"], actor=ACTOR, fields={"tags": ["a", "b"]})
        assert listed["tags"] == ["a", "b"]
        texted = lattice_update(task_id=task["id"], actor=ACTOR, fields={"tags": "c, d"})
        assert texted["tags"] == ["c", "d"]
        same = lattice_update(task_id=task["id"], actor=ACTOR, fields={"tags": ["c", "d"]})
        assert same["message"] == "No changes"

    def test_update_rules_still_apply_to_typed_values(self, lattice_env: Path) -> None:
        task = lattice_create(title="Rules", actor=ACTOR)

        for fields, code in (
            ({}, "VALIDATION_ERROR"),
            ({"priority": 3}, "VALIDATION_ERROR"),
            ({"priority": "urgent"}, "VALIDATION_ERROR"),
            ({"status": "done"}, "VALIDATION_ERROR"),
            ({"nope": 1}, "VALIDATION_ERROR"),
            ({"custom_fields.": 1}, "VALIDATION_ERROR"),
        ):
            with pytest.raises(LatticeToolError) as info:
                lattice_update(task_id=task["id"], actor=ACTOR, fields=fields)
            assert info.value.code == code, fields

    def test_cli_update_values_stay_text(self, lattice_env: Path) -> None:
        """The CLI keeps parsing ``field=value`` text exactly as before."""
        from click.testing import CliRunner

        from lattice.cli.main import cli

        task = lattice_create(title="CLI", actor=ACTOR)
        result = CliRunner().invoke(
            cli,
            [
                "update",
                task["id"],
                "custom_fields.points=3",
                "custom_fields.a=b=c",
                "tags=x,y",
                "--actor",
                ACTOR,
                "--json",
            ],
        )

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        assert data["custom_fields"] == {"points": "3", "a": "b=c"}
        assert data["tags"] == ["x", "y"]

    def test_update_refuses_pairs_and_fields_together(self, lattice_dir: Path) -> None:
        from lattice.ops import OpError, get_operation, parse_params

        params_cls = get_operation("task.update").Params
        with pytest.raises(OpError, match="not both") as info:
            parse_params(params_cls, {"task": "T-1", "pairs": ["a=b"], "fields": {"a": 1}})
        assert info.value.code == "VALIDATION_ERROR"

    def test_no_private_rules_or_storage_writes_in_mcp(self) -> None:
        """The tools module reaches the board only through operations and reads."""
        forbidden = {
            "mutate_task",
            "atomic_write",
            "jsonl_append",
            "scaffold_plan",
            "validate_transition",
            "validate_completion_policy",
            "create_event",
        }
        for module in ("tools.py", "resources.py"):
            source = (Path(tools.__file__).parent / module).read_text()
            names = {
                node.id if isinstance(node, ast.Name) else node.attr
                for node in ast.walk(ast.parse(source))
                if isinstance(node, ast.Name | ast.Attribute)
            }
            imported = {
                alias.name
                for node in ast.walk(ast.parse(source))
                if isinstance(node, ast.ImportFrom)
                for alias in node.names
            }
            assert not (names | imported) & forbidden, module


class TestLatticeRoot:
    def _board(self, path: Path, code: str) -> Path:
        ensure_lattice_dirs(path)
        config = default_config()
        config["project_code"] = code
        atomic_write(path / LATTICE_DIR / "config.json", serialize_config(config))
        return path

    def test_lattice_root_wins_over_the_process_environment(
        self, lattice_env: Path, tmp_path: Path
    ) -> None:
        other = self._board(tmp_path / "other", "OTH")

        created = lattice_create(title="Elsewhere", actor=ACTOR, lattice_root=str(other))

        assert created["short_id"] == "OTH-1"
        assert [t["id"] for t in lattice_list(lattice_root=str(other))] == [created["id"]]
        assert lattice_list() == []

    def test_lattice_root_is_a_starting_directory(self, lattice_env: Path, tmp_path: Path) -> None:
        board = self._board(tmp_path / "proj", "PRJ")
        nested = board / "src" / "pkg"
        nested.mkdir(parents=True)

        created = lattice_create(title="From below", actor=ACTOR, lattice_root=str(nested))

        assert created["short_id"] == "PRJ-1"
        assert lattice_show(task_id="PRJ-1", lattice_root=str(nested))["id"] == created["id"]

    def test_no_board_is_not_initialized(
        self, lattice_env: Path, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        empty = tmp_path_factory.mktemp("no-board")

        with pytest.raises(LatticeToolError) as info:
            lattice_create(title="Nowhere", actor=ACTOR, lattice_root=str(empty))

        assert info.value.code == "NOT_INITIALIZED"


# ---------------------------------------------------------------------------
# Bound checkout (H-11): the same tools on a checkout routed to a real
# in-process server (SPEC §9.2, §9.3, §9.5).
# ---------------------------------------------------------------------------


def _read_lock_free(checkout: Path) -> bool:
    """Whether no process holds the cache's read lock (an exclusive try succeeds)."""
    import fcntl
    import os

    fd = os.open(checkout / LATTICE_DIR / "locks" / "cache_rw.lock", os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    finally:
        os.close(fd)


def test_mcp_tools_work_on_a_bound_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lattice.server import tokens
    from lattice.server.testing import make_root, running_server

    root = make_root(
        tmp_path / "server",
        projects={"alpha": {"code": "ALP"}},
        config={"audit": {"enabled": False}},
    )
    token = tokens.create_token(root, user="human:alice", machine="laptop", all_projects=True)[
        "token"
    ]
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    (checkout / ".lattice-remote.json").write_text(
        json.dumps({"remote": "team", "project": "alpha"}) + "\n"
    )

    with running_server(root) as server:
        monkeypatch.setenv("LATTICE_REMOTE_TEAM_URL", server.url)
        monkeypatch.setenv("LATTICE_REMOTE_TEAM_TOKEN", token)
        lroot = str(checkout)
        assert isinstance(lattice.boards.resolve_board(checkout), lattice.boards.HostedBoard)

        created = lattice_create(title="Hosted", actor=ACTOR, lattice_root=lroot)
        assert created["short_id"] == "ALP-1"
        lattice_status(task_id="ALP-1", new_status="in_planning", actor=ACTOR, lattice_root=lroot)
        lattice_status(task_id="ALP-1", new_status="planned", actor=ACTOR, lattice_root=lroot)
        with pytest.raises(LatticeToolError) as info:
            lattice_status(
                task_id="ALP-1", new_status="in_progress", actor=ACTOR, lattice_root=lroot
            )
        assert info.value.code == "PLAN_REQUIRED"
        lattice_comment(task_id="ALP-1", text="from mcp", actor=ACTOR, lattice_root=lroot)

        # Reads come from the checkout's cache, caught up after each write.
        shown = lattice_show(task_id="ALP-1", lattice_root=lroot)
        assert shown["status"] == "planned"
        assert shown["events"][-1]["type"] == "comment_added"
        assert [t["short_id"] for t in lattice_list(lattice_root=lroot)] == ["ALP-1"]
        # No call leaves the cache's read lock held: a sync could not apply.
        assert _read_lock_free(checkout)

        # Each call catches up, even in this long-lived process: a write another
        # client makes on the server is visible to the next MCP read.
        status, _, body = server.op(
            "alpha",
            "task.comment",
            {"task": "ALP-1", "text": "elsewhere"},
            token=token,
            actor="human:alice",
        )
        assert status == 200, body
        shown = lattice_show(task_id="ALP-1", lattice_root=lroot)
        assert [e["data"].get("body") for e in shown["events"][-2:]] == ["from mcp", "elsewhere"]
        assert _read_lock_free(checkout)

        # The write happened on the server's board, not in the checkout.
        server_events = (
            root / "projects" / "alpha" / LATTICE_DIR / "events" / f"{created['id']}.jsonl"
        )
        types = [json.loads(line)["type"] for line in server_events.read_text().splitlines()]
        assert types.count("status_changed") == 2
        assert types[-1] == "comment_added"
