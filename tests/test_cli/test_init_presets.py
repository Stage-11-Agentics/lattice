"""Tests for `lattice init` status presets and the workflow interview (LAT-234)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from lattice.cli.main import cli
from lattice.core.config import default_config, linear_workflow

# Interactive input that skips the pre-interview prompts with defaults:
# name, project-name, project-code (the interview question comes next when
# the TTY guard passes).
_PRE_INTERVIEW = "\n\n\n"
# Post-interview prompts: done-display choice, agents.md confirm.
_POST_INTERVIEW = "1\nn\n"


def _read_config(root: Path) -> dict:
    return json.loads((root / ".lattice" / "config.json").read_text(encoding="utf-8"))


@pytest.fixture
def force_tty(monkeypatch: pytest.MonkeyPatch):
    """Make init believe stdin is a real TTY so the interview fires."""
    monkeypatch.setattr("lattice.cli.main._stdin_is_tty", lambda: True)


class TestPresetFlag:
    def test_preset_linear_non_interactive(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "init",
                "--path",
                str(tmp_path),
                "--actor",
                "human:tester",
                "--project-code",
                "T",
                "--preset",
                "linear",
            ],
        )
        assert result.exit_code == 0, result.output
        config = _read_config(tmp_path)
        assert config["status_preset"] == "linear"
        assert config["workflow"]["statuses"] == linear_workflow()["statuses"]
        assert config["workflow"]["completion_policies"] == {}
        assert config["workflow"]["wip_limits"] == {}
        assert "Workflow statuses: linear (6 statuses)" in result.output

    def test_preset_stage11_explicit(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "init",
                "--path",
                str(tmp_path),
                "--actor",
                "human:tester",
                "--project-code",
                "T",
                "--preset",
                "stage11",
            ],
        )
        assert result.exit_code == 0, result.output
        config = _read_config(tmp_path)
        assert config["status_preset"] == "stage11"
        assert config["workflow"]["statuses"] == default_config()["workflow"]["statuses"]

    def test_no_preset_non_interactive_defaults_to_stage11(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["init", "--path", str(tmp_path), "--actor", "human:tester", "--project-code", "T"],
        )
        assert result.exit_code == 0, result.output
        config = _read_config(tmp_path)
        assert config["status_preset"] == "stage11"
        # Workflow section identical to the canonical default (AC2).
        assert config["workflow"] == json.loads(
            json.dumps(default_config()["workflow"], sort_keys=True)
        )

    def test_preset_is_case_insensitive(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "init",
                "--path",
                str(tmp_path),
                "--actor",
                "human:tester",
                "--project-code",
                "T",
                "--preset",
                "LINEAR",
            ],
        )
        assert result.exit_code == 0, result.output
        assert _read_config(tmp_path)["status_preset"] == "linear"


class TestNoTtySafety:
    def test_interactive_init_without_tty_skips_question_and_defaults(
        self, tmp_path: Path
    ) -> None:
        # CliRunner stdin is not a TTY: the interview question must not fire
        # and init must complete without hanging.
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["init", "--path", str(tmp_path)],
            input=_PRE_INTERVIEW + _POST_INTERVIEW,
        )
        assert result.exit_code == 0, result.output
        assert "how should work flow here?" not in result.output
        assert _read_config(tmp_path)["status_preset"] == "stage11"


class TestInterview:
    def test_choice_default_is_stage11(self, tmp_path: Path, force_tty) -> None:
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["init", "--path", str(tmp_path)],
            input=_PRE_INTERVIEW + "\n" + _POST_INTERVIEW,
        )
        assert result.exit_code == 0, result.output
        assert "how should work flow here?" in result.output
        assert _read_config(tmp_path)["status_preset"] == "stage11"

    def test_choice_2_is_linear(self, tmp_path: Path, force_tty) -> None:
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["init", "--path", str(tmp_path)],
            input=_PRE_INTERVIEW + "2\n" + _POST_INTERVIEW,
        )
        assert result.exit_code == 0, result.output
        config = _read_config(tmp_path)
        assert config["status_preset"] == "linear"
        assert config["workflow"]["statuses"] == linear_workflow()["statuses"]

    def test_choice_3_custom_asks_three_stage_questions(self, tmp_path: Path, force_tty) -> None:
        runner = CliRunner()
        # review=yes, validation=no, pr_open=yes
        result = runner.invoke(
            cli,
            ["init", "--path", str(tmp_path)],
            input=_PRE_INTERVIEW + "3\ny\nn\ny\n" + _POST_INTERVIEW,
        )
        assert result.exit_code == 0, result.output
        assert "code review stage?" in result.output
        assert "e2e validation stage?" in result.output
        assert "PR stage?" in result.output
        config = _read_config(tmp_path)
        assert config["status_preset"] == "custom"
        statuses = config["workflow"]["statuses"]
        assert "review" in statuses
        assert "in_validation" not in statuses
        assert "pr_open" in statuses
        # R3: declining validation drops the pr_open evidence policy.
        assert "pr_open" not in config["workflow"]["completion_policies"]
        assert "pr_open" in config["workflow"]["transitions"]["review"]

    def test_needs_human_never_offered(self, tmp_path: Path, force_tty) -> None:
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["init", "--path", str(tmp_path)],
            input=_PRE_INTERVIEW + "3\ny\ny\ny\n" + _POST_INTERVIEW,
        )
        assert result.exit_code == 0, result.output
        assert "needs_human" not in result.output
        assert "needs_human" not in _read_config(tmp_path)["workflow"]["statuses"]


class TestGeneratedGuidance:
    def test_linear_agents_md_mentions_only_linear_statuses(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "init",
                "--path",
                str(tmp_path),
                "--actor",
                "human:tester",
                "--project-code",
                "T",
                "--preset",
                "linear",
            ],
        )
        assert result.exit_code == 0, result.output
        agents_md = (tmp_path / "agents.md").read_text(encoding="utf-8")
        for absent in ("in_planning", "in_validation", "pr_open", "`planned`", "`review`"):
            assert absent not in agents_md, f"agents.md leaks {absent!r}"
        assert "backlog → todo → in_progress → in_review → done" in agents_md

    def test_stage11_agents_md_matches_static_block(self, tmp_path: Path) -> None:
        from lattice.templates.claude_md_block import CLAUDE_MD_BLOCK

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["init", "--path", str(tmp_path), "--actor", "human:tester", "--project-code", "T"],
        )
        assert result.exit_code == 0, result.output
        agents_md = (tmp_path / "agents.md").read_text(encoding="utf-8")
        assert CLAUDE_MD_BLOCK.lstrip("\n") in agents_md + "\n"


class TestLinearPlanGate:
    def test_todo_to_in_progress_not_blocked_by_plan_scaffold(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "init",
                "--path",
                str(tmp_path),
                "--actor",
                "human:tester",
                "--project-code",
                "T",
                "--preset",
                "linear",
            ],
        )
        assert result.exit_code == 0, result.output

        import os

        cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            result = runner.invoke(cli, ["create", "Linear task", "--actor", "agent:t", "--quiet"])
            assert result.exit_code == 0, result.output
            task_id = result.output.strip()

            result = runner.invoke(cli, ["status", task_id, "todo", "--actor", "agent:t"])
            assert result.exit_code == 0, result.output

            # Plan file is still scaffold — the gate must NOT fire (no
            # planning swimlane in this workflow).
            result = runner.invoke(cli, ["status", task_id, "in_progress", "--actor", "agent:t"])
            assert result.exit_code == 0, result.output

            # And the in_review path works through to done.
            result = runner.invoke(cli, ["status", task_id, "in_review", "--actor", "agent:t"])
            assert result.exit_code == 0, result.output
        finally:
            os.chdir(cwd)

    def test_next_claim_not_blocked_in_linear(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "init",
                "--path",
                str(tmp_path),
                "--actor",
                "human:tester",
                "--project-code",
                "T",
                "--preset",
                "linear",
            ],
        )
        assert result.exit_code == 0, result.output

        import os

        cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            result = runner.invoke(cli, ["create", "Claimable", "--actor", "agent:t", "--quiet"])
            assert result.exit_code == 0, result.output

            result = runner.invoke(cli, ["next", "--actor", "agent:t", "--claim"])
            assert result.exit_code == 0, result.output
            assert "PLAN_REQUIRED" not in result.output
        finally:
            os.chdir(cwd)
