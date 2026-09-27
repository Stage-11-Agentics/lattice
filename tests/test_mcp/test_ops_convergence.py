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

    def test_update_values_are_text_like_the_cli(
        self, lattice_env: Path, lattice_dir: Path
    ) -> None:
        task = lattice_create(title="Fields", actor=ACTOR)

        snapshot = lattice_update(
            task_id=task["id"],
            actor=ACTOR,
            fields={"tags": ["a", "b"], "custom_fields.points": 3},
        )

        assert snapshot["tags"] == ["a", "b"]
        assert snapshot["custom_fields"]["points"] == "3"
        with pytest.raises(LatticeToolError) as info:
            lattice_update(task_id=task["id"], actor=ACTOR, fields={"custom_fields.x": {"a": 1}})
        assert info.value.code == "VALIDATION_ERROR"
        with pytest.raises(LatticeToolError, match="Invalid field name"):
            lattice_update(task_id=task["id"], actor=ACTOR, fields={"custom_fields.a=b": "c"})

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
# Bound checkout (H-11): runs once ``resolve_board`` routes a bound checkout
# to a ``HostedBoard`` (SPEC §9.2, §9.3, §9.5). Until then it is skipped.
# ---------------------------------------------------------------------------

requires_binding = pytest.mark.skipif(
    not hasattr(lattice.boards, "HostedBoard"),
    reason="H-11 (client binding) has not landed: resolve_board has no HostedBoard yet",
)


@requires_binding
def test_mcp_tools_work_on_a_bound_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lattice.server import tokens
    from lattice.server.testing import make_root, running_server

    root = make_root(tmp_path / "server", projects={"alpha": {"code": "ALP"}})
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

        # The write happened on the server's board, not in the checkout.
        server_events = (
            root / "projects" / "alpha" / LATTICE_DIR / "events" / f"{created['id']}.jsonl"
        )
        types = [json.loads(line)["type"] for line in server_events.read_text().splitlines()]
        assert types.count("status_changed") == 2
        assert types[-1] == "comment_added"
