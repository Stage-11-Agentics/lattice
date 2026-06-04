"""Tests for the config-driven CLAUDE.md block renderer (LAT-234).

The stage11 default render is locked byte-for-byte against the pre-change
static block (tests/fixtures/claude_md_block_stage11_snapshot.md).  Other
presets must only describe statuses that exist in the instance.
"""

from __future__ import annotations

from pathlib import Path

from lattice.core.config import compose_workflow, default_config
from lattice.templates.claude_md_block import CLAUDE_MD_BLOCK, render_claude_md_block

_SNAPSHOT = Path(__file__).parent.parent / "fixtures" / "claude_md_block_stage11_snapshot.md"


def _custom_config(*, review: bool, validation: bool, pr_open: bool) -> dict:
    config = dict(default_config(status_preset="custom"))
    config["workflow"] = compose_workflow(
        include_review=review, include_validation=validation, include_pr_open=pr_open
    )
    return config


class TestStage11Snapshot:
    def test_render_default_is_byte_identical_to_pre_change_block(self) -> None:
        snapshot = _SNAPSHOT.read_text(encoding="utf-8")
        assert render_claude_md_block(None) == snapshot
        assert render_claude_md_block(dict(default_config())) == snapshot

    def test_module_constant_equals_default_render(self) -> None:
        assert CLAUDE_MD_BLOCK == render_claude_md_block(None)

    def test_empty_config_renders_stage11(self) -> None:
        assert render_claude_md_block({}) == CLAUDE_MD_BLOCK


class TestLinearBlock:
    def test_mentions_only_existing_statuses(self) -> None:
        block = render_claude_md_block(dict(default_config(status_preset="linear")))
        for absent in ("in_planning", "in_validation", "pr_open", "`planned`", "`review`"):
            assert absent not in block, f"linear block leaks {absent!r}"
        for present in ("`todo`", "`in_review`", "backlog", "in_progress"):
            assert present in block

    def test_gate_sections_absent(self) -> None:
        block = render_claude_md_block(dict(default_config(status_preset="linear")))
        for section in (
            "### Sub-Agent Execution Model",
            "### The Planning Gate",
            "### Plan Review Triage",
            "### The Review Gate",
            "### Review Verdict Routing",
            "### Review Rework Loop",
            "### The Validation Gate",
            "### Review Config Reference",
            "### Auto-fire Conventions",
        ):
            assert section not in block

    def test_always_on_sections_present(self) -> None:
        block = render_claude_md_block(dict(default_config(status_preset="linear")))
        for section in (
            "## Lattice",
            "### Creating Tasks (Non-Negotiable)",
            "### Status Transitions",
            "### When You're Stuck",
            "### Actor Attribution",
            "### Quick Reference",
        ):
            assert section in block

    def test_lifecycle_diagram(self) -> None:
        block = render_claude_md_block(dict(default_config(status_preset="linear")))
        assert "backlog → todo → in_progress → in_review → done" in block
        assert "↕" not in block  # no blocked status, no connector


class TestCustomBlocks:
    def test_review_only(self) -> None:
        block = render_claude_md_block(
            _custom_config(review=True, validation=False, pr_open=False)
        )
        assert "### The Review Gate" in block
        assert "### The Validation Gate" not in block
        assert "in_validation" not in block
        assert "pr_open" not in block
        # Review pass routes straight to done.
        assert "move to `done` on pass" in block

    def test_validation_only(self) -> None:
        block = render_claude_md_block(
            _custom_config(review=False, validation=True, pr_open=False)
        )
        assert "### The Validation Gate" in block
        assert "### The Review Gate" not in block
        assert "pass → `done`" in block
        assert "pr_open" not in block

    def test_review_and_pr_open_without_validation(self) -> None:
        all_on = render_claude_md_block(_custom_config(review=True, validation=True, pr_open=True))
        no_validation = render_claude_md_block(
            _custom_config(review=True, validation=False, pr_open=True)
        )
        # With validation declined, pr_open carries no evidence requirement.
        assert "Requires recorded validation evidence" in all_on
        assert "Requires recorded validation evidence" not in no_validation
        assert "in_validation" not in no_validation

    def test_all_gates_on_includes_everything(self) -> None:
        block = render_claude_md_block(_custom_config(review=True, validation=True, pr_open=True))
        for section in (
            "### The Planning Gate",
            "### The Review Gate",
            "### The Validation Gate",
            "### Auto-fire Conventions",
        ):
            assert section in block

    def test_needs_human_never_a_status(self) -> None:
        # The flag is referenced (it exists in every preset); the *status* is
        # never drawn in the lifecycle diagram.
        for combo in (
            dict(review=True, validation=True, pr_open=True),
            dict(review=False, validation=False, pr_open=False),
        ):
            block = render_claude_md_block(_custom_config(**combo))
            assert "→ needs_human" not in block
            assert "needs_human →" not in block
