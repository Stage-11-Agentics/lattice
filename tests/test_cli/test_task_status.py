"""Tests for completion policy gating and review cycle limits in `lattice status`."""

from __future__ import annotations

import json
import re
from pathlib import Path

from lattice.storage.fs import LATTICE_DIR

from tests.conftest import _add_policies_to_config


_ACTOR = "human:test"


def _set_review_cycle_limit(lattice_root: Path, limit: int) -> None:
    """Set the review_cycle_limit in the workflow config."""
    config_path = lattice_root / LATTICE_DIR / "config.json"
    config = json.loads(config_path.read_text())
    config["workflow"]["review_cycle_limit"] = limit
    config_path.write_text(json.dumps(config, sort_keys=True, indent=2) + "\n")


class TestCompletionPolicyGating:
    """Status transitions blocked by completion policies.

    Tests that use the standard policy (done: require_roles: [review])
    use the shared ``invoke_with_policies`` / ``fill_plan_with_policies``
    fixtures. Tests with custom policies inject them inline.
    """

    def test_blocked_without_required_role(
        self,
        invoke_with_policies,
        initialized_root_with_policies,
        fill_plan_with_policies,
    ) -> None:
        r = invoke_with_policies("create", "Test task", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]
        invoke_with_policies("status", task_id, "in_planning", "--actor", _ACTOR)
        fill_plan_with_policies(task_id, "Test task")
        invoke_with_policies("status", task_id, "planned", "--actor", _ACTOR)
        invoke_with_policies("status", task_id, "in_progress", "--actor", _ACTOR)
        invoke_with_policies("status", task_id, "review", "--actor", _ACTOR)

        r = invoke_with_policies("status", task_id, "done", "--actor", _ACTOR, "--json")
        assert r.exit_code != 0
        parsed = json.loads(r.output)
        assert parsed["ok"] is False
        assert parsed["error"]["code"] == "COMPLETION_BLOCKED"
        assert "review" in parsed["error"]["message"]

    def test_passes_with_required_role(
        self,
        invoke_with_policies,
        initialized_root_with_policies,
        tmp_path,
        fill_plan_with_policies,
    ) -> None:
        r = invoke_with_policies("create", "Test task", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]
        invoke_with_policies("status", task_id, "in_planning", "--actor", _ACTOR)
        fill_plan_with_policies(task_id, "Test task")
        invoke_with_policies("status", task_id, "planned", "--actor", _ACTOR)
        invoke_with_policies("status", task_id, "in_progress", "--actor", _ACTOR)
        invoke_with_policies("status", task_id, "review", "--actor", _ACTOR)

        src_file = tmp_path / "review.md"
        src_file.write_text("# Code Review\nLGTM")
        invoke_with_policies(
            "attach",
            task_id,
            str(src_file),
            "--role",
            "review",
            "--actor",
            _ACTOR,
        )

        r = invoke_with_policies("status", task_id, "done", "--actor", _ACTOR, "--json")
        assert r.exit_code == 0
        parsed = json.loads(r.output)
        assert parsed["ok"] is True

    def test_force_override_requires_reason(
        self,
        invoke_with_policies,
        initialized_root_with_policies,
        fill_plan_with_policies,
    ) -> None:
        r = invoke_with_policies("create", "Test task", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]
        invoke_with_policies("status", task_id, "in_planning", "--actor", _ACTOR)
        fill_plan_with_policies(task_id, "Test task")
        invoke_with_policies("status", task_id, "planned", "--actor", _ACTOR)
        invoke_with_policies("status", task_id, "in_progress", "--actor", _ACTOR)
        invoke_with_policies("status", task_id, "review", "--actor", _ACTOR)

        r = invoke_with_policies(
            "status",
            task_id,
            "done",
            "--force",
            "--actor",
            _ACTOR,
            "--json",
        )
        assert r.exit_code != 0
        parsed = json.loads(r.output)
        assert parsed["error"]["code"] == "VALIDATION_ERROR"

    def test_force_with_reason_overrides(
        self,
        invoke_with_policies,
        initialized_root_with_policies,
        fill_plan_with_policies,
    ) -> None:
        r = invoke_with_policies("create", "Test task", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]
        invoke_with_policies("status", task_id, "in_planning", "--actor", _ACTOR)
        fill_plan_with_policies(task_id, "Test task")
        invoke_with_policies("status", task_id, "planned", "--actor", _ACTOR)
        invoke_with_policies("status", task_id, "in_progress", "--actor", _ACTOR)
        invoke_with_policies("status", task_id, "review", "--actor", _ACTOR)

        r = invoke_with_policies(
            "status",
            task_id,
            "done",
            "--force",
            "--reason",
            "Reviewed offline",
            "--actor",
            _ACTOR,
            "--json",
        )
        assert r.exit_code == 0
        parsed = json.loads(r.output)
        assert parsed["ok"] is True

    def test_universal_target_bypasses_policy(self, invoke, initialized_root, fill_plan) -> None:
        """Universal targets bypass policies — even with a policy on needs_human."""
        _add_policies_to_config(
            initialized_root,
            {
                "done": {"require_roles": ["review"]},
                "needs_human": {"require_roles": ["review"]},
            },
        )

        r = invoke("create", "Test task", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]
        invoke("status", task_id, "in_planning", "--actor", _ACTOR)
        fill_plan(task_id, "Test task")
        invoke("status", task_id, "planned", "--actor", _ACTOR)
        invoke("status", task_id, "in_progress", "--actor", _ACTOR)

        r = invoke("status", task_id, "needs_human", "--actor", _ACTOR, "--json")
        assert r.exit_code == 0

    def test_no_policy_no_gating(self, invoke, initialized_root, fill_plan) -> None:
        """Without completion_policies, transitions work normally."""
        r = invoke("create", "Test task", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]
        invoke("status", task_id, "in_planning", "--actor", _ACTOR)
        fill_plan(task_id, "Test task")
        invoke("status", task_id, "planned", "--actor", _ACTOR)
        invoke("status", task_id, "in_progress", "--actor", _ACTOR)
        invoke("status", task_id, "review", "--actor", _ACTOR)

        r = invoke("status", task_id, "done", "--actor", _ACTOR, "--json")
        assert r.exit_code == 0

    def test_require_assigned_blocks(self, invoke, initialized_root, fill_plan) -> None:
        _add_policies_to_config(initialized_root, {"done": {"require_assigned": True}})

        r = invoke("create", "Test task", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]
        invoke("status", task_id, "in_planning", "--actor", _ACTOR)
        fill_plan(task_id, "Test task")
        invoke("status", task_id, "planned", "--actor", _ACTOR)
        invoke("status", task_id, "in_progress", "--actor", _ACTOR)
        invoke("status", task_id, "review", "--actor", _ACTOR)

        r = invoke("status", task_id, "done", "--actor", _ACTOR, "--json")
        assert r.exit_code != 0
        parsed = json.loads(r.output)
        assert parsed["error"]["code"] == "COMPLETION_BLOCKED"
        assert "assigned" in parsed["error"]["message"].lower()

    def test_require_assigned_passes_when_assigned(
        self,
        invoke,
        initialized_root,
        fill_plan,
    ) -> None:
        _add_policies_to_config(initialized_root, {"done": {"require_assigned": True}})

        r = invoke(
            "create",
            "Test task",
            "--assigned-to",
            "agent:claude",
            "--actor",
            _ACTOR,
            "--json",
        )
        task_id = json.loads(r.output)["data"]["id"]
        invoke("status", task_id, "in_planning", "--actor", _ACTOR)
        fill_plan(task_id, "Test task")
        invoke("status", task_id, "planned", "--actor", _ACTOR)
        invoke("status", task_id, "in_progress", "--actor", _ACTOR)
        invoke("status", task_id, "review", "--actor", _ACTOR)

        r = invoke("status", task_id, "done", "--actor", _ACTOR, "--json")
        assert r.exit_code == 0

    def test_passes_with_review_comment_role(
        self,
        invoke_with_policies,
        initialized_root_with_policies,
    ) -> None:
        """A comment with --role review satisfies the require_roles policy."""
        r = invoke_with_policies("create", "Test task", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]
        invoke_with_policies(
            "status",
            task_id,
            "in_progress",
            "--actor",
            _ACTOR,
            "--force",
            "--reason",
            "skip",
        )
        invoke_with_policies("status", task_id, "review", "--actor", _ACTOR)

        r = invoke_with_policies(
            "comment",
            task_id,
            "LGTM — no issues found",
            "--role",
            "review",
            "--actor",
            _ACTOR,
        )
        assert r.exit_code == 0, r.output

        r = invoke_with_policies("status", task_id, "done", "--actor", _ACTOR, "--json")
        assert r.exit_code == 0, r.output
        assert json.loads(r.output)["ok"] is True

    def test_blocked_when_only_non_role_comment(
        self,
        invoke_with_policies,
        initialized_root_with_policies,
    ) -> None:
        """A comment without a role does NOT satisfy the require_roles policy."""
        r = invoke_with_policies("create", "Test task", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]
        invoke_with_policies(
            "status",
            task_id,
            "in_progress",
            "--actor",
            _ACTOR,
            "--force",
            "--reason",
            "skip",
        )
        invoke_with_policies("status", task_id, "review", "--actor", _ACTOR)

        invoke_with_policies("comment", task_id, "Just a regular comment", "--actor", _ACTOR)

        r = invoke_with_policies("status", task_id, "done", "--actor", _ACTOR, "--json")
        assert r.exit_code != 0
        assert json.loads(r.output)["error"]["code"] == "COMPLETION_BLOCKED"

    def test_passes_with_inline_attach_review_role(
        self,
        invoke_with_policies,
        initialized_root_with_policies,
    ) -> None:
        """Inline artifact with --role review satisfies the require_roles policy."""
        r = invoke_with_policies("create", "Test task", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]
        invoke_with_policies(
            "status",
            task_id,
            "in_progress",
            "--actor",
            _ACTOR,
            "--force",
            "--reason",
            "skip",
        )
        invoke_with_policies("status", task_id, "review", "--actor", _ACTOR)

        r = invoke_with_policies(
            "attach",
            task_id,
            "--inline",
            "Reviewed thoroughly. LGTM.",
            "--role",
            "review",
            "--actor",
            _ACTOR,
        )
        assert r.exit_code == 0, r.output

        r = invoke_with_policies("status", task_id, "done", "--actor", _ACTOR, "--json")
        assert r.exit_code == 0, r.output
        assert json.loads(r.output)["ok"] is True

    def test_done_remains_after_review_comment_deleted(
        self,
        invoke_with_policies,
        initialized_root_with_policies,
    ) -> None:
        """Deleting review evidence after done must not reopen the task."""
        r = invoke_with_policies("create", "Done remains terminal", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]
        invoke_with_policies(
            "status",
            task_id,
            "in_progress",
            "--actor",
            _ACTOR,
            "--force",
            "--reason",
            "skip",
        )
        invoke_with_policies("status", task_id, "review", "--actor", _ACTOR)

        r = invoke_with_policies(
            "comment",
            task_id,
            "Final review",
            "--role",
            "review",
            "--actor",
            _ACTOR,
            "--json",
        )
        assert r.exit_code == 0, r.output
        comment_id = json.loads(r.output)["data"]["last_event_id"]

        done = invoke_with_policies("status", task_id, "done", "--actor", _ACTOR, "--json")
        assert done.exit_code == 0, done.output
        assert json.loads(done.output)["data"]["status"] == "done"

        deleted = invoke_with_policies(
            "comment-delete",
            task_id,
            comment_id,
            "--actor",
            _ACTOR,
            "--json",
        )
        assert deleted.exit_code == 0, deleted.output
        snapshot = json.loads(deleted.output)["data"]
        assert snapshot["status"] == "done"
        comment_refs = [
            ref for ref in snapshot.get("evidence_refs", []) if ref.get("source_type") == "comment"
        ]
        assert comment_refs == []


# ---------------------------------------------------------------------------
# Review cycle limit gating (LAT-168)
# ---------------------------------------------------------------------------


class TestReviewCycleLimitGating:
    """Review rework transitions blocked after cycle limit is reached."""

    def _create_and_advance_to_review(self, invoke, fill_plan) -> str:
        """Create a task and advance it to review status. Returns task_id."""
        r = invoke("create", "Cycle test", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]
        invoke("status", task_id, "in_planning", "--actor", _ACTOR)
        fill_plan(task_id, "Cycle test")
        invoke("status", task_id, "planned", "--actor", _ACTOR)
        invoke("status", task_id, "in_progress", "--actor", _ACTOR)
        invoke("status", task_id, "review", "--actor", _ACTOR)
        return task_id

    def _rework_cycle_impl(self, invoke, fill_plan, task_id) -> None:
        """Perform one review -> in_progress -> review cycle."""
        r = invoke("status", task_id, "in_progress", "--actor", _ACTOR, "--json")
        assert r.exit_code == 0, f"Failed review->in_progress: {r.output}"
        r = invoke("status", task_id, "review", "--actor", _ACTOR, "--json")
        assert r.exit_code == 0, f"Failed in_progress->review: {r.output}"

    def test_review_to_in_progress_allowed(
        self,
        invoke,
        initialized_root,
        fill_plan,
    ) -> None:
        """First review -> in_progress transition succeeds."""
        task_id = self._create_and_advance_to_review(invoke, fill_plan)
        r = invoke("status", task_id, "in_progress", "--actor", _ACTOR, "--json")
        assert r.exit_code == 0
        assert json.loads(r.output)["ok"] is True

    def test_review_to_in_planning_allowed(
        self,
        invoke,
        initialized_root,
        fill_plan,
    ) -> None:
        """review -> in_planning transition succeeds (new transition)."""
        task_id = self._create_and_advance_to_review(invoke, fill_plan)
        r = invoke("status", task_id, "in_planning", "--actor", _ACTOR, "--json")
        assert r.exit_code == 0
        assert json.loads(r.output)["ok"] is True

    def test_review_to_done_unaffected(
        self,
        invoke,
        initialized_root,
        fill_plan,
    ) -> None:
        """review -> done still works (not a rework transition)."""
        task_id = self._create_and_advance_to_review(invoke, fill_plan)
        r = invoke("status", task_id, "done", "--actor", _ACTOR, "--json")
        assert r.exit_code == 0
        assert json.loads(r.output)["ok"] is True

    def test_cycle_limit_blocks_after_3_reworks(
        self,
        invoke,
        initialized_root,
        fill_plan,
    ) -> None:
        """After 3 review->rework transitions, the 4th is blocked."""
        task_id = self._create_and_advance_to_review(invoke, fill_plan)

        # Cycle 1: review -> in_progress -> review
        self._rework_cycle_impl(invoke, fill_plan, task_id)
        # Cycle 2: review -> in_progress -> review
        self._rework_cycle_impl(invoke, fill_plan, task_id)
        # Cycle 3: review -> in_progress -> review
        self._rework_cycle_impl(invoke, fill_plan, task_id)

        # Attempt cycle 4: should be blocked
        r = invoke("status", task_id, "in_progress", "--actor", _ACTOR, "--json")
        assert r.exit_code != 0
        parsed = json.loads(r.output)
        assert parsed["ok"] is False
        assert parsed["error"]["code"] == "REVIEW_CYCLE_LIMIT"
        assert "3" in parsed["error"]["message"]

    def test_cycle_limit_force_override(
        self,
        invoke,
        initialized_root,
        fill_plan,
    ) -> None:
        """--force --reason overrides the cycle limit."""
        task_id = self._create_and_advance_to_review(invoke, fill_plan)

        # 3 rework cycles
        self._rework_cycle_impl(invoke, fill_plan, task_id)
        self._rework_cycle_impl(invoke, fill_plan, task_id)
        self._rework_cycle_impl(invoke, fill_plan, task_id)

        # Force override on 4th attempt
        r = invoke(
            "status",
            task_id,
            "in_progress",
            "--force",
            "--reason",
            "Exceptional rework needed",
            "--actor",
            _ACTOR,
            "--json",
        )
        assert r.exit_code == 0
        assert json.loads(r.output)["ok"] is True

    def test_configurable_cycle_limit(
        self,
        invoke,
        initialized_root,
        fill_plan,
    ) -> None:
        """Custom review_cycle_limit of 1 blocks after 1 rework."""
        _set_review_cycle_limit(initialized_root, 1)

        task_id = self._create_and_advance_to_review(invoke, fill_plan)

        # Cycle 1
        self._rework_cycle_impl(invoke, fill_plan, task_id)

        # Attempt cycle 2: should be blocked (limit is 1)
        r = invoke("status", task_id, "in_progress", "--actor", _ACTOR, "--json")
        assert r.exit_code != 0
        parsed = json.loads(r.output)
        assert parsed["error"]["code"] == "REVIEW_CYCLE_LIMIT"
        assert "1/1" in parsed["error"]["message"]

    def test_mixed_rework_types_count_together(
        self,
        invoke,
        initialized_root,
        fill_plan,
    ) -> None:
        """review -> in_progress and review -> in_planning both count toward limit."""
        task_id = self._create_and_advance_to_review(invoke, fill_plan)

        # Cycle 1: review -> in_progress -> review (impl-level rework)
        self._rework_cycle_impl(invoke, fill_plan, task_id)

        # Cycle 2: review -> in_planning -> planned -> in_progress -> review (plan-level rework)
        r = invoke("status", task_id, "in_planning", "--actor", _ACTOR, "--json")
        assert r.exit_code == 0, f"Failed review->in_planning: {r.output}"
        fill_plan(task_id, "Cycle test rework")
        invoke("status", task_id, "planned", "--actor", _ACTOR)
        invoke("status", task_id, "in_progress", "--actor", _ACTOR)
        invoke("status", task_id, "review", "--actor", _ACTOR)

        # Cycle 3: review -> in_progress -> review (impl-level rework)
        self._rework_cycle_impl(invoke, fill_plan, task_id)

        # Attempt cycle 4: should be blocked (3 rework transitions total)
        r = invoke("status", task_id, "in_progress", "--actor", _ACTOR, "--json")
        assert r.exit_code != 0
        parsed = json.loads(r.output)
        assert parsed["error"]["code"] == "REVIEW_CYCLE_LIMIT"


class TestBackwardStatusPlanReset:
    """Backward status transitions append reset breadcrumbs to plan files."""

    def test_backward_transition_appends_reset_section(
        self,
        invoke,
        initialized_root,
        fill_plan,
    ) -> None:
        r = invoke("create", "Reset append task", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]
        plan_path = initialized_root / LATTICE_DIR / "plans" / f"{task_id}.md"

        invoke("status", task_id, "in_planning", "--actor", _ACTOR)
        fill_plan(task_id, "Reset append task")
        invoke("status", task_id, "planned", "--actor", _ACTOR)
        invoke("status", task_id, "in_progress", "--actor", _ACTOR)
        invoke("status", task_id, "review", "--actor", _ACTOR)

        before = plan_path.read_text()
        r = invoke("status", task_id, "in_progress", "--actor", _ACTOR, "--json")
        assert r.exit_code == 0, r.output
        after = plan_path.read_text()

        assert after.startswith(before)
        assert re.search(r"## Reset \d{4}-\d{2}-\d{2} by human:test", after) is not None

    def test_forward_transition_does_not_append_reset_section(
        self,
        invoke,
        initialized_root,
        fill_plan,
    ) -> None:
        r = invoke("create", "No reset append task", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]
        plan_path = initialized_root / LATTICE_DIR / "plans" / f"{task_id}.md"

        invoke("status", task_id, "in_planning", "--actor", _ACTOR)
        fill_plan(task_id, "No reset append task")
        invoke("status", task_id, "planned", "--actor", _ACTOR)

        before = plan_path.read_text()
        r = invoke("status", task_id, "in_progress", "--actor", _ACTOR, "--json")
        assert r.exit_code == 0, r.output
        after = plan_path.read_text()

        assert after == before
