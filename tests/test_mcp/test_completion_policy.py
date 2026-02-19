"""Tests for MCP completion-policy enforcement parity with CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lattice.mcp.tools import lattice_comment, lattice_create, lattice_status
from lattice.storage.fs import LATTICE_DIR

_ACTOR = "human:test"


def _config_with_policy(lattice_env: Path, policy: dict | None = None) -> None:
    """Write a config with completion policies to the lattice env."""
    lattice_dir = lattice_env / LATTICE_DIR
    config_path = lattice_dir / "config.json"
    config = json.loads(config_path.read_text())
    if policy is not None:
        config["workflow"]["completion_policies"] = policy
    config_path.write_text(json.dumps(config, sort_keys=True, indent=2) + "\n")


def _create_task_at_review(lattice_env: Path, *, assigned_to: str | None = None) -> str:
    """Create a task and advance it to review status. Returns the task ID."""
    kwargs: dict = {"title": "Test task", "actor": _ACTOR}
    if assigned_to is not None:
        kwargs["assigned_to"] = assigned_to
    snapshot = lattice_create(**kwargs)
    task_id = snapshot["id"]
    lattice_status(
        task_id=task_id, new_status="in_progress", actor=_ACTOR, force=True, reason="skip for test"
    )
    lattice_status(task_id=task_id, new_status="review", actor=_ACTOR)
    return task_id


class TestMCPCompletionPolicy:
    """MCP status transitions blocked by completion policies — parity with CLI."""

    def test_blocked_without_required_role(self, lattice_env: Path) -> None:
        _config_with_policy(lattice_env, {"done": {"require_roles": ["review"]}})
        task_id = _create_task_at_review(lattice_env)

        with pytest.raises(ValueError, match="Completion policy not satisfied"):
            lattice_status(task_id=task_id, new_status="done", actor=_ACTOR)

    def test_passes_with_review_comment_role(self, lattice_env: Path) -> None:
        _config_with_policy(lattice_env, {"done": {"require_roles": ["review"]}})
        task_id = _create_task_at_review(lattice_env)

        lattice_comment(
            task_id=task_id, text="LGTM — no issues found", actor=_ACTOR, role="review"
        )
        snapshot = lattice_status(task_id=task_id, new_status="done", actor=_ACTOR)
        assert snapshot["status"] == "done"

    def test_force_override_requires_reason(self, lattice_env: Path) -> None:
        _config_with_policy(lattice_env, {"done": {"require_roles": ["review"]}})
        task_id = _create_task_at_review(lattice_env)

        with pytest.raises(ValueError, match="reason is required"):
            lattice_status(task_id=task_id, new_status="done", actor=_ACTOR, force=True)

    def test_force_with_reason_overrides(self, lattice_env: Path) -> None:
        _config_with_policy(lattice_env, {"done": {"require_roles": ["review"]}})
        task_id = _create_task_at_review(lattice_env)

        snapshot = lattice_status(
            task_id=task_id, new_status="done", actor=_ACTOR, force=True, reason="Reviewed offline"
        )
        assert snapshot["status"] == "done"

    def test_universal_target_bypasses_policy(self, lattice_env: Path) -> None:
        _config_with_policy(
            lattice_env,
            {
                "done": {"require_roles": ["review"]},
                "needs_human": {"require_roles": ["review"]},
            },
        )
        task_id = _create_task_at_review(lattice_env)

        # needs_human is a universal target — should bypass policy
        snapshot = lattice_status(task_id=task_id, new_status="needs_human", actor=_ACTOR)
        assert snapshot["status"] == "needs_human"

    def test_no_policy_no_gating(self, lattice_env: Path) -> None:
        """Without completion_policies, transitions work normally."""
        task_id = _create_task_at_review(lattice_env)

        snapshot = lattice_status(task_id=task_id, new_status="done", actor=_ACTOR)
        assert snapshot["status"] == "done"

    def test_require_assigned_blocks(self, lattice_env: Path) -> None:
        _config_with_policy(lattice_env, {"done": {"require_assigned": True}})
        task_id = _create_task_at_review(lattice_env)

        with pytest.raises(ValueError, match="Completion policy not satisfied"):
            lattice_status(task_id=task_id, new_status="done", actor=_ACTOR)

    def test_require_assigned_passes_when_assigned(self, lattice_env: Path) -> None:
        _config_with_policy(lattice_env, {"done": {"require_assigned": True}})
        task_id = _create_task_at_review(lattice_env, assigned_to="agent:claude")

        snapshot = lattice_status(task_id=task_id, new_status="done", actor=_ACTOR)
        assert snapshot["status"] == "done"
