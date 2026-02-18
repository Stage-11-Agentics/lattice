"""Tests for completion policy gating in `lattice status`."""

from __future__ import annotations

import json

from lattice.storage.fs import LATTICE_DIR


_ACTOR = "human:test"


def _config_with_policy(initialized_root, policy: dict | None = None) -> None:
    """Write a config with completion policies to the initialized root."""
    lattice_dir = initialized_root / LATTICE_DIR
    config_path = lattice_dir / "config.json"
    config = json.loads(config_path.read_text())
    if policy is not None:
        config["workflow"]["completion_policies"] = policy
    config_path.write_text(json.dumps(config, sort_keys=True, indent=2) + "\n")


class TestCompletionPolicyGating:
    """Status transitions blocked by completion policies."""

    def test_blocked_without_required_role(self, invoke, initialized_root) -> None:
        _config_with_policy(initialized_root, {"done": {"require_roles": ["review"]}})

        # Create a task and move to review
        r = invoke("create", "Test task", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]
        invoke("status", task_id, "in_planning", "--actor", _ACTOR)
        invoke("status", task_id, "planned", "--actor", _ACTOR)
        invoke("status", task_id, "in_progress", "--actor", _ACTOR)
        invoke("status", task_id, "review", "--actor", _ACTOR)

        # Try to move to done — should be blocked
        r = invoke("status", task_id, "done", "--actor", _ACTOR, "--json")
        assert r.exit_code != 0
        parsed = json.loads(r.output)
        assert parsed["ok"] is False
        assert parsed["error"]["code"] == "COMPLETION_BLOCKED"
        assert "review" in parsed["error"]["message"]

    def test_passes_with_required_role(self, invoke, initialized_root, tmp_path) -> None:
        _config_with_policy(initialized_root, {"done": {"require_roles": ["review"]}})

        r = invoke("create", "Test task", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]
        invoke("status", task_id, "in_planning", "--actor", _ACTOR)
        invoke("status", task_id, "planned", "--actor", _ACTOR)
        invoke("status", task_id, "in_progress", "--actor", _ACTOR)
        invoke("status", task_id, "review", "--actor", _ACTOR)

        # Attach artifact with role=review
        src_file = tmp_path / "review.md"
        src_file.write_text("# Code Review\nLGTM")
        invoke(
            "attach", task_id, str(src_file),
            "--role", "review",
            "--actor", _ACTOR,
        )

        # Now move to done — should succeed
        r = invoke("status", task_id, "done", "--actor", _ACTOR, "--json")
        assert r.exit_code == 0
        parsed = json.loads(r.output)
        assert parsed["ok"] is True

    def test_force_override_requires_reason(self, invoke, initialized_root) -> None:
        _config_with_policy(initialized_root, {"done": {"require_roles": ["review"]}})

        r = invoke("create", "Test task", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]
        invoke("status", task_id, "in_planning", "--actor", _ACTOR)
        invoke("status", task_id, "planned", "--actor", _ACTOR)
        invoke("status", task_id, "in_progress", "--actor", _ACTOR)
        invoke("status", task_id, "review", "--actor", _ACTOR)

        # --force without --reason should fail
        r = invoke("status", task_id, "done", "--force", "--actor", _ACTOR, "--json")
        assert r.exit_code != 0
        parsed = json.loads(r.output)
        assert parsed["error"]["code"] == "VALIDATION_ERROR"

    def test_force_with_reason_overrides(self, invoke, initialized_root) -> None:
        _config_with_policy(initialized_root, {"done": {"require_roles": ["review"]}})

        r = invoke("create", "Test task", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]
        invoke("status", task_id, "in_planning", "--actor", _ACTOR)
        invoke("status", task_id, "planned", "--actor", _ACTOR)
        invoke("status", task_id, "in_progress", "--actor", _ACTOR)
        invoke("status", task_id, "review", "--actor", _ACTOR)

        # --force --reason should succeed
        r = invoke(
            "status", task_id, "done",
            "--force", "--reason", "Reviewed offline",
            "--actor", _ACTOR, "--json",
        )
        assert r.exit_code == 0
        parsed = json.loads(r.output)
        assert parsed["ok"] is True

    def test_universal_target_bypasses_policy(self, invoke, initialized_root) -> None:
        # Even with a policy on needs_human, it should bypass
        _config_with_policy(initialized_root, {
            "done": {"require_roles": ["review"]},
            "needs_human": {"require_roles": ["review"]},
        })

        r = invoke("create", "Test task", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]
        invoke("status", task_id, "in_planning", "--actor", _ACTOR)
        invoke("status", task_id, "planned", "--actor", _ACTOR)
        invoke("status", task_id, "in_progress", "--actor", _ACTOR)

        # needs_human is a universal target — should bypass
        r = invoke("status", task_id, "needs_human", "--actor", _ACTOR, "--json")
        assert r.exit_code == 0

    def test_no_policy_no_gating(self, invoke, initialized_root) -> None:
        """Without completion_policies, transitions work normally."""
        r = invoke("create", "Test task", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]
        invoke("status", task_id, "in_planning", "--actor", _ACTOR)
        invoke("status", task_id, "planned", "--actor", _ACTOR)
        invoke("status", task_id, "in_progress", "--actor", _ACTOR)
        invoke("status", task_id, "review", "--actor", _ACTOR)

        r = invoke("status", task_id, "done", "--actor", _ACTOR, "--json")
        assert r.exit_code == 0

    def test_require_assigned_blocks(self, invoke, initialized_root) -> None:
        _config_with_policy(initialized_root, {"done": {"require_assigned": True}})

        r = invoke("create", "Test task", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]
        invoke("status", task_id, "in_planning", "--actor", _ACTOR)
        invoke("status", task_id, "planned", "--actor", _ACTOR)
        invoke("status", task_id, "in_progress", "--actor", _ACTOR)
        invoke("status", task_id, "review", "--actor", _ACTOR)

        r = invoke("status", task_id, "done", "--actor", _ACTOR, "--json")
        assert r.exit_code != 0
        parsed = json.loads(r.output)
        assert parsed["error"]["code"] == "COMPLETION_BLOCKED"
        assert "assigned" in parsed["error"]["message"].lower()

    def test_require_assigned_passes_when_assigned(self, invoke, initialized_root) -> None:
        _config_with_policy(initialized_root, {"done": {"require_assigned": True}})

        r = invoke(
            "create", "Test task",
            "--assigned-to", "agent:claude",
            "--actor", _ACTOR, "--json",
        )
        task_id = json.loads(r.output)["data"]["id"]
        invoke("status", task_id, "in_planning", "--actor", _ACTOR)
        invoke("status", task_id, "planned", "--actor", _ACTOR)
        invoke("status", task_id, "in_progress", "--actor", _ACTOR)
        invoke("status", task_id, "review", "--actor", _ACTOR)

        r = invoke("status", task_id, "done", "--actor", _ACTOR, "--json")
        assert r.exit_code == 0

    def test_passes_with_review_comment_role(self, invoke, initialized_root) -> None:
        """A comment with --role review satisfies the require_roles policy."""
        _config_with_policy(initialized_root, {"done": {"require_roles": ["review"]}})

        r = invoke("create", "Test task", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]
        invoke("status", task_id, "in_progress", "--actor", _ACTOR, "--force", "--reason", "skip")
        invoke("status", task_id, "review", "--actor", _ACTOR)

        r = invoke("comment", task_id, "LGTM — no issues found", "--role", "review", "--actor", _ACTOR)
        assert r.exit_code == 0, r.output

        r = invoke("status", task_id, "done", "--actor", _ACTOR, "--json")
        assert r.exit_code == 0, r.output
        assert json.loads(r.output)["ok"] is True

    def test_blocked_when_only_non_role_comment(self, invoke, initialized_root) -> None:
        """A comment without a role does NOT satisfy the require_roles policy."""
        _config_with_policy(initialized_root, {"done": {"require_roles": ["review"]}})

        r = invoke("create", "Test task", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]
        invoke("status", task_id, "in_progress", "--actor", _ACTOR, "--force", "--reason", "skip")
        invoke("status", task_id, "review", "--actor", _ACTOR)

        invoke("comment", task_id, "Just a regular comment", "--actor", _ACTOR)

        r = invoke("status", task_id, "done", "--actor", _ACTOR, "--json")
        assert r.exit_code != 0
        assert json.loads(r.output)["error"]["code"] == "COMPLETION_BLOCKED"

    def test_passes_with_inline_attach_review_role(self, invoke, initialized_root) -> None:
        """Inline artifact with --role review satisfies the require_roles policy."""
        _config_with_policy(initialized_root, {"done": {"require_roles": ["review"]}})

        r = invoke("create", "Test task", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]
        invoke("status", task_id, "in_progress", "--actor", _ACTOR, "--force", "--reason", "skip")
        invoke("status", task_id, "review", "--actor", _ACTOR)

        r = invoke(
            "attach", task_id,
            "--inline", "Reviewed thoroughly. LGTM.",
            "--role", "review",
            "--actor", _ACTOR,
        )
        assert r.exit_code == 0, r.output

        r = invoke("status", task_id, "done", "--actor", _ACTOR, "--json")
        assert r.exit_code == 0, r.output
        assert json.loads(r.output)["ok"] is True
