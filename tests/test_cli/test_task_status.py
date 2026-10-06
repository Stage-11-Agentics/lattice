"""Tests for completion policy gating, review cycle limits, and next_steps hints in `lattice status`."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from lattice.storage.fs import LATTICE_DIR

from tests.conftest import _add_policies_to_config


_ACTOR = "human:test"


def _set_config_field(lattice_root: Path, field: str, value: object) -> None:
    """Set a top-level field in config.json."""
    config_path = lattice_root / LATTICE_DIR / "config.json"
    config = json.loads(config_path.read_text())
    config[field] = value
    config_path.write_text(json.dumps(config, sort_keys=True, indent=2) + "\n")


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
        """Universal targets bypass policies — even with a policy on cancelled."""
        _add_policies_to_config(
            initialized_root,
            {
                "done": {"require_roles": ["review"]},
                "cancelled": {"require_roles": ["review"]},
            },
        )

        r = invoke("create", "Test task", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]
        invoke("status", task_id, "in_planning", "--actor", _ACTOR)
        fill_plan(task_id, "Test task")
        invoke("status", task_id, "planned", "--actor", _ACTOR)
        invoke("status", task_id, "in_progress", "--actor", _ACTOR)

        r = invoke("status", task_id, "cancelled", "--actor", _ACTOR, "--json")
        assert r.exit_code == 0

    def test_no_policy_no_gating(self, invoke, initialized_root, fill_plan) -> None:
        """Without completion_policies, transitions work normally."""
        _add_policies_to_config(initialized_root, {})
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
        # Unassign to test that require_assigned blocks completion
        invoke("assign", task_id, "none", "--actor", _ACTOR)

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


@pytest.fixture
def lattice_fires_reviews(monkeypatch, tmp_path):
    """Make every transition into review look auto-fired by Lattice.

    The conftest board disables auto-fire; this stands in a successful spawn so
    ``lattice status`` records the ``auto_review_spawned`` event without
    launching a review process.
    """
    from lattice.cli import auto_review, task_cmds

    def fake_auto_fire(lattice_dir, task_id, new_status, **kwargs):  # noqa: ANN001, ANN003, ANN202
        if new_status != "review" or kwargs.get("no_auto_review_flag"):
            return {"fired": False, "reason": "test"}
        return {
            "fired": True,
            "review_type": "code-review",
            "mode": "single",
            "log_path": "auto-code-review.log",
            "spawned_at": "2026-01-01T00:00:00Z",
            "pid": 1,
        }

    monkeypatch.setattr(auto_review, "auto_fire_review", fake_auto_fire)
    monkeypatch.setattr(task_cmds, "_caller_git_worktree", lambda: tmp_path)


def _status_events(root: Path, task_id: str) -> list[dict]:
    path = root / LATTICE_DIR / "events" / f"{task_id}.jsonl"
    events = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [e for e in events if e["type"] == "status_changed"]


class TestReviewCycleLimitGating:
    """Review rework cycles are recorded; the limit hard-stops only Lattice's own review loop."""

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
        invoke("comment", task_id, "LGTM", "--role", "review", "--actor", _ACTOR)
        r = invoke("status", task_id, "done", "--actor", _ACTOR, "--json")
        assert r.exit_code == 0
        assert json.loads(r.output)["ok"] is True

    def test_every_rework_records_its_cycle(
        self,
        invoke,
        initialized_root,
        fill_plan,
    ) -> None:
        """Each rework transition carries its cycle number on the status event."""
        task_id = self._create_and_advance_to_review(invoke, fill_plan)
        self._rework_cycle_impl(invoke, fill_plan, task_id)
        self._rework_cycle_impl(invoke, fill_plan, task_id)

        reworks = [
            e["data"]
            for e in _status_events(initialized_root, task_id)
            if e["data"]["to"] == "in_progress"
        ][1:]
        assert [d["review_cycle"]["cycle"] for d in reworks] == [1, 2]
        assert all(d["review_cycle"]["limit"] == 3 for d in reworks)
        assert not any(d["review_cycle"]["over_limit"] for d in reworks)

    def test_cycle_limit_is_advisory_when_lattice_did_not_fire_the_review(
        self,
        invoke,
        initialized_root,
        fill_plan,
    ) -> None:
        """An orchestrated loop passes the limit: allowed, recorded, warned."""
        task_id = self._create_and_advance_to_review(invoke, fill_plan)
        for _ in range(3):
            self._rework_cycle_impl(invoke, fill_plan, task_id)

        r = invoke("status", task_id, "in_progress", "--actor", _ACTOR, "--json")
        assert r.exit_code == 0, r.output
        cycle = json.loads(r.output)["data"]["review_cycle"]
        assert cycle == {"cycle": 4, "limit": 3, "over_limit": True, "enforced": False}
        assert _status_events(initialized_root, task_id)[-1]["data"]["review_cycle"] == cycle

        invoke("status", task_id, "review", "--actor", _ACTOR)
        r = invoke("status", task_id, "in_progress", "--actor", _ACTOR)
        assert r.exit_code == 0, r.output
        assert "Warning: review cycle 5 passes the limit of 3" in r.output

    def test_cycle_limit_blocks_after_3_reworks_of_auto_fired_reviews(
        self,
        invoke,
        initialized_root,
        fill_plan,
        lattice_fires_reviews,
    ) -> None:
        """After 3 reworks of Lattice-fired reviews, the 4th is blocked."""
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
        assert "auto-fired" in parsed["error"]["message"]

    def test_manual_review_after_auto_fired_ones_is_advisory(
        self,
        invoke,
        initialized_root,
        fill_plan,
        lattice_fires_reviews,
    ) -> None:
        """Only the latest entry into review decides: --no-auto-review hands the budget back."""
        task_id = self._create_and_advance_to_review(invoke, fill_plan)
        for _ in range(3):
            self._rework_cycle_impl(invoke, fill_plan, task_id)
        invoke("status", task_id, "in_progress", "--actor", _ACTOR, "--force", "--reason", "x")
        invoke("status", task_id, "review", "--no-auto-review", "--actor", _ACTOR)

        r = invoke("status", task_id, "in_progress", "--actor", _ACTOR, "--json")
        assert r.exit_code == 0, r.output
        assert json.loads(r.output)["data"]["review_cycle"]["enforced"] is False

    def test_cycle_limit_force_override(
        self,
        invoke,
        initialized_root,
        fill_plan,
        lattice_fires_reviews,
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
        lattice_fires_reviews,
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
        lattice_fires_reviews,
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


# ---------------------------------------------------------------------------
# Next-step hints on status transitions (LAT-197)
# ---------------------------------------------------------------------------


class TestNextStepsHints:
    """Status transitions produce next-step hints in human and JSON output."""

    def test_in_planning_human_hint(self, invoke, initialized_root) -> None:
        r = invoke("create", "Hint test", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]

        r = invoke("status", task_id, "in_planning", "--actor", _ACTOR)
        assert r.exit_code == 0
        assert "Next: write the plan in plans/" in r.output
        assert "then move to planned" in r.output

    def test_in_planning_json_next_steps(self, invoke, initialized_root) -> None:
        r = invoke("create", "Hint test json", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]

        r = invoke("status", task_id, "in_planning", "--actor", _ACTOR, "--json")
        assert r.exit_code == 0
        data = json.loads(r.output)["data"]
        assert "next_steps" in data
        assert data["next_steps"]["action"] == "write_plan"
        assert data["next_steps"]["then"] == "planned"
        assert task_id in data["next_steps"]["plan_path"]

    def test_review_human_hint(self, invoke, initialized_root, fill_plan) -> None:
        r = invoke("create", "Review hint", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]
        invoke("status", task_id, "in_planning", "--actor", _ACTOR)
        fill_plan(task_id, "Review hint")
        invoke("status", task_id, "planned", "--actor", _ACTOR)
        invoke("status", task_id, "in_progress", "--actor", _ACTOR)

        r = invoke("status", task_id, "review", "--actor", _ACTOR)
        assert r.exit_code == 0
        assert "lattice code-review" in r.output
        assert "review_mode: single" in r.output

    def test_review_json_next_steps(self, invoke, initialized_root, fill_plan) -> None:
        r = invoke("create", "Review json hint", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]
        invoke("status", task_id, "in_planning", "--actor", _ACTOR)
        fill_plan(task_id, "Review json hint")
        invoke("status", task_id, "planned", "--actor", _ACTOR)
        invoke("status", task_id, "in_progress", "--actor", _ACTOR)

        r = invoke("status", task_id, "review", "--actor", _ACTOR, "--json")
        assert r.exit_code == 0
        data = json.loads(r.output)["data"]
        ns = data["next_steps"]
        assert ns["action"] == "code_review"
        assert ns["review_mode"] == "single"
        assert "lattice code-review" in ns["command"]
        assert ns["then"] == "in_validation"

    def test_in_progress_human_hint(self, invoke, initialized_root, fill_plan) -> None:
        r = invoke("create", "Impl hint", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]
        invoke("status", task_id, "in_planning", "--actor", _ACTOR)
        fill_plan(task_id, "Impl hint")
        invoke("status", task_id, "planned", "--actor", _ACTOR)

        r = invoke("status", task_id, "in_progress", "--actor", _ACTOR)
        assert r.exit_code == 0
        assert "implement the plan" in r.output
        assert "move to review" in r.output

    def test_needs_human_status_rejected_with_flag_hint(self, invoke, initialized_root) -> None:
        """needs_human is not a status in the default config — the error points at the flag."""
        r = invoke("create", "Needs human hint", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]
        invoke("status", task_id, "in_planning", "--actor", _ACTOR)

        r = invoke("status", task_id, "needs_human", "--actor", _ACTOR)
        assert r.exit_code != 0
        assert "needs_human is a flag, not a status" in r.output
        assert "lattice needs-human" in r.output

    def test_needs_human_status_rejected_json(self, invoke, initialized_root) -> None:
        r = invoke("create", "NH json", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]
        invoke("status", task_id, "in_planning", "--actor", _ACTOR)

        r = invoke("status", task_id, "needs_human", "--actor", _ACTOR, "--json")
        assert r.exit_code != 0
        parsed = json.loads(r.output)
        assert parsed["ok"] is False
        assert parsed["error"]["code"] == "VALIDATION_ERROR"
        assert "lattice needs-human" in parsed["error"]["message"]

    def test_planned_no_hint_when_inline(self, invoke, initialized_root, fill_plan) -> None:
        """When plan_review_mode is inline, planned produces no hint."""
        _set_config_field(initialized_root, "plan_review_mode", "inline")

        r = invoke("create", "Planned inline", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]
        invoke("status", task_id, "in_planning", "--actor", _ACTOR)
        fill_plan(task_id, "Planned inline")

        r = invoke("status", task_id, "planned", "--actor", _ACTOR, "--json")
        assert r.exit_code == 0
        data = json.loads(r.output)["data"]
        assert "next_steps" not in data

    def test_planned_hint_when_single_review(self, invoke, initialized_root, fill_plan) -> None:
        """When plan_review_mode is single, planned produces a plan-review hint."""
        _set_config_field(initialized_root, "plan_review_mode", "single")

        r = invoke("create", "Planned single", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]
        invoke("status", task_id, "in_planning", "--actor", _ACTOR)
        fill_plan(task_id, "Planned single")

        r = invoke("status", task_id, "planned", "--actor", _ACTOR)
        assert r.exit_code == 0
        assert "lattice plan-review" in r.output
        assert "plan_review_mode: single" in r.output

    def test_quiet_mode_suppresses_hint(self, invoke, initialized_root) -> None:
        """Quiet mode should only output 'ok', no hints."""
        r = invoke("create", "Quiet hint test", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]

        r = invoke("status", task_id, "in_planning", "--actor", _ACTOR, "--quiet")
        assert r.exit_code == 0
        assert r.output.strip() == "ok"

    def test_done_no_hint(self, invoke, initialized_root, fill_plan) -> None:
        """Moving to done produces no next_steps."""
        _add_policies_to_config(initialized_root, {})

        r = invoke("create", "Done no hint", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]
        invoke("status", task_id, "in_planning", "--actor", _ACTOR)
        fill_plan(task_id, "Done no hint")
        invoke("status", task_id, "planned", "--actor", _ACTOR)
        invoke("status", task_id, "in_progress", "--actor", _ACTOR)
        invoke("status", task_id, "review", "--actor", _ACTOR)

        r = invoke("status", task_id, "done", "--actor", _ACTOR, "--json")
        assert r.exit_code == 0
        data = json.loads(r.output)["data"]
        assert "next_steps" not in data


# ---------------------------------------------------------------------------
# Validation gate (LAT-233)
# ---------------------------------------------------------------------------


class TestValidationGate:
    """in_validation swimlane: transitions, hints, evidence gating, rework."""

    def _advance_to_review(self, invoke, fill_plan, title="Validation test") -> str:
        r = invoke("create", title, "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]
        invoke("status", task_id, "in_planning", "--actor", _ACTOR)
        fill_plan(task_id, title)
        invoke("status", task_id, "planned", "--actor", _ACTOR)
        invoke("status", task_id, "in_progress", "--actor", _ACTOR)
        invoke("status", task_id, "review", "--actor", _ACTOR)
        return task_id

    def test_review_to_in_validation(self, invoke, initialized_root, fill_plan) -> None:
        task_id = self._advance_to_review(invoke, fill_plan)
        r = invoke("status", task_id, "in_validation", "--actor", _ACTOR, "--json")
        assert r.exit_code == 0
        assert json.loads(r.output)["ok"] is True

    def test_in_validation_hint_carries_e2e_culture(
        self, invoke, initialized_root, fill_plan
    ) -> None:
        task_id = self._advance_to_review(invoke, fill_plan)
        r = invoke("status", task_id, "in_validation", "--actor", _ACTOR)
        assert r.exit_code == 0
        assert "I saw it work" in r.output
        assert "--role validation" in r.output

    def test_in_validation_json_next_steps(self, invoke, initialized_root, fill_plan) -> None:
        task_id = self._advance_to_review(invoke, fill_plan)
        r = invoke("status", task_id, "in_validation", "--actor", _ACTOR, "--json")
        ns = json.loads(r.output)["data"]["next_steps"]
        assert ns["action"] == "validate_e2e"
        assert ns["then"] == "pr_open"
        assert ns["or"] == "done"
        assert "--role validation" in ns["evidence"]

    def test_in_validation_hint_names_both_routes(
        self, invoke, initialized_root, fill_plan
    ) -> None:
        task_id = self._advance_to_review(invoke, fill_plan)
        r = invoke("status", task_id, "in_validation", "--actor", _ACTOR)
        assert (
            "On pass move to pr_open, or straight to done when the PR already merged" in r.output
        )

    def test_in_validation_hint_omits_done_without_the_edge(
        self, invoke, initialized_root, fill_plan
    ) -> None:
        config_path = initialized_root / LATTICE_DIR / "config.json"
        config = json.loads(config_path.read_text())
        config["workflow"]["transitions"]["in_validation"].remove("done")
        config_path.write_text(json.dumps(config, sort_keys=True, indent=2) + "\n")
        task_id = self._advance_to_review(invoke, fill_plan)
        r = invoke("status", task_id, "in_validation", "--actor", _ACTOR, "--json")
        assert "or" not in json.loads(r.output)["data"]["next_steps"]

    def test_pr_open_blocked_without_validation_evidence(
        self, invoke, initialized_root, fill_plan
    ) -> None:
        task_id = self._advance_to_review(invoke, fill_plan)
        invoke("status", task_id, "in_validation", "--actor", _ACTOR)
        r = invoke("status", task_id, "pr_open", "--actor", _ACTOR, "--json")
        assert r.exit_code != 0
        parsed = json.loads(r.output)
        assert parsed["ok"] is False
        assert parsed["error"]["code"] == "COMPLETION_BLOCKED"
        assert "validation" in parsed["error"]["message"]

    def test_pr_open_passes_with_validation_evidence(
        self, invoke, initialized_root, fill_plan
    ) -> None:
        task_id = self._advance_to_review(invoke, fill_plan)
        invoke("status", task_id, "in_validation", "--actor", _ACTOR)
        invoke(
            "comment",
            task_id,
            "Validated: exercised the login flow in the browser, saw it work.",
            "--role",
            "validation",
            "--actor",
            _ACTOR,
        )
        r = invoke("status", task_id, "pr_open", "--actor", _ACTOR, "--json")
        assert r.exit_code == 0
        assert json.loads(r.output)["ok"] is True

    def test_validation_failure_counts_toward_cycle_limit(
        self, invoke, initialized_root, fill_plan, lattice_fires_reviews
    ) -> None:
        """in_validation -> in_progress reworks trip the 3-cycle valve."""
        task_id = self._advance_to_review(invoke, fill_plan)
        for _ in range(3):
            invoke("status", task_id, "in_validation", "--actor", _ACTOR)
            r = invoke("status", task_id, "in_progress", "--actor", _ACTOR, "--json")
            assert r.exit_code == 0, r.output
            invoke("status", task_id, "review", "--actor", _ACTOR)
        invoke("status", task_id, "in_validation", "--actor", _ACTOR)
        r = invoke("status", task_id, "in_progress", "--actor", _ACTOR, "--json")
        assert r.exit_code != 0
        parsed = json.loads(r.output)
        assert parsed["error"]["code"] == "REVIEW_CYCLE_LIMIT"

    def test_in_validation_to_done_with_review_evidence(
        self, invoke, initialized_root, fill_plan
    ) -> None:
        """Merge-first flow: review -> (PR merged) -> in_validation -> done, no pr_open detour."""
        task_id = self._advance_to_review(invoke, fill_plan)
        invoke("comment", task_id, "Reviewed: PASS", "--role", "review", "--actor", _ACTOR)
        invoke("status", task_id, "in_validation", "--actor", _ACTOR)
        r = invoke("status", task_id, "done", "--actor", _ACTOR, "--json")
        assert r.exit_code == 0, r.output
        assert json.loads(r.output)["data"]["status"] == "done"

    def test_in_validation_to_done_still_needs_completion_evidence(
        self, invoke, initialized_root, fill_plan
    ) -> None:
        task_id = self._advance_to_review(invoke, fill_plan)
        invoke("status", task_id, "in_validation", "--actor", _ACTOR)
        r = invoke("status", task_id, "done", "--actor", _ACTOR, "--json")
        assert r.exit_code != 0
        assert json.loads(r.output)["error"]["code"] == "COMPLETION_BLOCKED"

    def test_complete_from_in_validation_skips_the_review_hop(
        self, invoke, initialized_root, fill_plan
    ) -> None:
        task_id = self._advance_to_review(invoke, fill_plan)
        invoke("status", task_id, "in_validation", "--actor", _ACTOR)
        r = invoke("complete", task_id, "--review", "Validated after merge.", "--actor", _ACTOR)
        assert r.exit_code == 0, r.output
        assert "(in_validation -> done)" in r.output
        last = _status_events(initialized_root, task_id)[-1]["data"]
        assert (last["from"], last["to"]) == ("in_validation", "done")

    def test_in_validation_to_review_rejected(self, invoke, initialized_root, fill_plan) -> None:
        """No backflow into review — rework re-enters via in_progress."""
        task_id = self._advance_to_review(invoke, fill_plan)
        invoke("status", task_id, "in_validation", "--actor", _ACTOR)
        r = invoke("status", task_id, "review", "--actor", _ACTOR, "--json")
        assert r.exit_code != 0
        assert json.loads(r.output)["ok"] is False
