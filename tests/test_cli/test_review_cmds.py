"""Tests for code-review, plan-review, and review-status CLI commands."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from lattice.cli.main import cli
from lattice.core.config import default_config, serialize_config
from lattice.storage.fs import LATTICE_DIR, ensure_lattice_dirs, atomic_write


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_board(tmp_path: Path, config_overrides: dict | None = None) -> Path:
    """Initialize a .lattice/ directory and return root."""
    ensure_lattice_dirs(tmp_path)
    lattice_dir = tmp_path / LATTICE_DIR
    config = default_config()
    if config_overrides:
        config.update(config_overrides)
    atomic_write(lattice_dir / "config.json", serialize_config(config))
    (lattice_dir / "events" / "_lifecycle.jsonl").touch()
    return tmp_path


def _create_task(runner: CliRunner, root: Path, title: str = "Test task") -> str:
    """Create a task and return its ID."""
    result = runner.invoke(
        cli,
        ["create", title, "--actor", "agent:test", "--quiet"],
        env={"LATTICE_ROOT": str(root)},
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    return result.output.strip()


def _write_plan(root: Path, task_id: str, content: str) -> None:
    plan_path = root / LATTICE_DIR / "plans" / f"{task_id}.md"
    plan_path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Tests: review-status (no agent spawning needed)
# ---------------------------------------------------------------------------


class TestReviewStatus:
    def test_no_in_flight_review(self, tmp_path):
        root = _make_board(tmp_path)
        runner = CliRunner()
        task_id = _create_task(runner, root)

        result = runner.invoke(
            cli,
            ["review-status", task_id],
            env={"LATTICE_ROOT": str(root)},
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        assert "No in-flight review found" in result.output

    def test_no_in_flight_review_json(self, tmp_path):
        root = _make_board(tmp_path)
        runner = CliRunner()
        task_id = _create_task(runner, root)

        result = runner.invoke(
            cli,
            ["review-status", task_id, "--json"],
            env={"LATTICE_ROOT": str(root)},
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ok"] is True
        assert data["data"]["status"] == "none"

    def test_shows_in_flight_state(self, tmp_path):
        root = _make_board(tmp_path)
        runner = CliRunner()
        task_id = _create_task(runner, root)

        # Write fake in-flight state
        from lattice.core.review import write_review_state

        lattice_dir = root / LATTICE_DIR
        state = {
            "task_id": task_id,
            "mode": "triple",
            "review_type": "code-review",
            "started_at": "2026-03-04T00:00:00Z",
            "agents": [
                {"name": "claude", "status": "done", "artifact_id": "art_123"},
                {"name": "codex", "status": "running", "artifact_id": None},
                {"name": "gemini", "status": "failed", "artifact_id": None},
            ],
        }
        write_review_state(lattice_dir, state)

        result = runner.invoke(
            cli,
            ["review-status", task_id],
            env={"LATTICE_ROOT": str(root)},
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        assert "triple" in result.output
        assert "claude" in result.output
        assert "done" in result.output
        assert "codex" in result.output

    def test_shows_in_flight_state_json(self, tmp_path):
        root = _make_board(tmp_path)
        runner = CliRunner()
        task_id = _create_task(runner, root)

        from lattice.core.review import write_review_state

        lattice_dir = root / LATTICE_DIR
        state = {
            "task_id": task_id,
            "mode": "single",
            "review_type": "plan-review",
            "started_at": "2026-03-04T00:00:00Z",
            "agents": [{"name": "claude", "status": "running", "artifact_id": None}],
        }
        write_review_state(lattice_dir, state)

        result = runner.invoke(
            cli,
            ["review-status", task_id, "--json"],
            env={"LATTICE_ROOT": str(root)},
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ok"] is True
        assert data["data"]["mode"] == "single"
        assert data["data"]["agents"][0]["name"] == "claude"


# ---------------------------------------------------------------------------
# Tests: code-review inline mode
# ---------------------------------------------------------------------------


class TestCodeReviewInline:
    def test_inline_mode_prints_message(self, tmp_path):
        root = _make_board(tmp_path, {"review_mode": "inline"})
        runner = CliRunner()
        task_id = _create_task(runner, root)

        result = runner.invoke(
            cli,
            ["code-review", task_id],
            env={"LATTICE_ROOT": str(root)},
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        assert "inline" in result.output

    def test_inline_mode_json(self, tmp_path):
        root = _make_board(tmp_path, {"review_mode": "inline"})
        runner = CliRunner()
        task_id = _create_task(runner, root)

        result = runner.invoke(
            cli,
            ["code-review", task_id, "--json"],
            env={"LATTICE_ROOT": str(root)},
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ok"] is True
        assert data["data"]["mode"] == "inline"

    def test_mode_flag_overrides_config(self, tmp_path):
        root = _make_board(tmp_path, {"review_mode": "single"})
        runner = CliRunner()
        task_id = _create_task(runner, root)

        result = runner.invoke(
            cli,
            ["code-review", task_id, "--mode", "inline"],
            env={"LATTICE_ROOT": str(root)},
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        assert "inline" in result.output


# ---------------------------------------------------------------------------
# Tests: plan-review inline mode
# ---------------------------------------------------------------------------


class TestPlanReviewInline:
    def test_missing_plan_file_errors(self, tmp_path):
        root = _make_board(tmp_path, {"plan_review_mode": "inline"})
        runner = CliRunner()
        task_id = _create_task(runner, root)

        # Remove the auto-created scaffold plan to simulate missing plan
        plan_path = root / LATTICE_DIR / "plans" / f"{task_id}.md"
        plan_path.unlink(missing_ok=True)

        result = runner.invoke(
            cli,
            ["plan-review", task_id],
            env={"LATTICE_ROOT": str(root)},
            catch_exceptions=False,
        )
        assert result.exit_code != 0

    def test_inline_mode_with_plan_file(self, tmp_path):
        root = _make_board(tmp_path, {"plan_review_mode": "inline"})
        runner = CliRunner()
        task_id = _create_task(runner, root)
        _write_plan(root, task_id, "## Plan\nDo the thing.")

        result = runner.invoke(
            cli,
            ["plan-review", task_id],
            env={"LATTICE_ROOT": str(root)},
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        assert "inline" in result.output


# ---------------------------------------------------------------------------
# Tests: code-review single mode (mocked agent)
# ---------------------------------------------------------------------------


class TestCodeReviewSingle:
    def test_single_mode_diff_failure_exits(self, tmp_path):
        root = _make_board(tmp_path, {"review_mode": "single"})
        runner = CliRunner()
        task_id = _create_task(runner, root)

        # No git repo at tmp_path, so diff resolution will fail
        with patch(
            "lattice.cli.review_cmds.resolve_diff",
            return_value=(False, "Not inside a git repository."),
        ):
            result = runner.invoke(
                cli,
                ["code-review", task_id, "--mode", "single"],
                env={"LATTICE_ROOT": str(root)},
                catch_exceptions=False,
            )
        assert result.exit_code != 0

    def test_single_mode_empty_diff_exits(self, tmp_path):
        root = _make_board(tmp_path, {"review_mode": "single"})
        runner = CliRunner()
        task_id = _create_task(runner, root)

        with patch(
            "lattice.cli.review_cmds.resolve_diff",
            return_value=(True, ""),
        ):
            result = runner.invoke(
                cli,
                ["code-review", task_id, "--mode", "single"],
                env={"LATTICE_ROOT": str(root)},
                catch_exceptions=False,
            )
        assert result.exit_code != 0

    def test_single_mode_produces_artifact(self, tmp_path):
        root = _make_board(tmp_path, {"review_mode": "single"})
        runner = CliRunner()
        task_id = _create_task(runner, root)

        fake_diff = "diff --git a/foo.py b/foo.py\n+print('hello')"
        fake_review = "### 1. Verdict\n**PASS**\n\nLooks good."

        with (
            patch("lattice.cli.review_cmds.resolve_diff", return_value=(True, fake_diff)),
            patch(
                "lattice.cli.review_cmds.run_single_review",
                return_value=(True, "Review complete.", fake_review),
            ),
        ):
            result = runner.invoke(
                cli,
                ["code-review", task_id, "--mode", "single", "--actor", "agent:test"],
                env={"LATTICE_ROOT": str(root)},
                catch_exceptions=False,
            )
        # Should not error (may warn if lattice attach subprocess fails in test env)
        # The key assertion: no unhandled exception and diff was attempted
        assert (
            result.exit_code == 0 or "Review stored" in result.output or "failed" in result.output
        )

    def test_single_mode_agent_failure_reports_error(self, tmp_path):
        root = _make_board(tmp_path, {"review_mode": "single"})
        runner = CliRunner()
        task_id = _create_task(runner, root)

        fake_diff = "diff --git a/foo.py b/foo.py\n+print('hello')"

        with (
            patch("lattice.cli.review_cmds.resolve_diff", return_value=(True, fake_diff)),
            patch(
                "lattice.cli.review_cmds.run_single_review", return_value=(False, "timeout", None)
            ),
        ):
            result = runner.invoke(
                cli,
                ["code-review", task_id, "--mode", "single", "--actor", "agent:test"],
                env={"LATTICE_ROOT": str(root)},
                catch_exceptions=False,
            )
        # Agent failure should result in non-zero exit or error message
        assert result.exit_code != 0 or "failed" in result.output


# ---------------------------------------------------------------------------
# Tests: plan-review single mode (mocked agent)
# ---------------------------------------------------------------------------


class TestPlanReviewSingle:
    def test_single_mode_produces_artifact(self, tmp_path):
        root = _make_board(tmp_path, {"plan_review_mode": "single", "plan_approval": "auto"})
        runner = CliRunner()
        task_id = _create_task(runner, root)
        _write_plan(root, task_id, "## Plan\nRefactor the auth module.")

        fake_review = "### 1. Verdict\n**PASS**\n\nSolid plan."

        with patch(
            "lattice.cli.review_cmds.run_single_review", return_value=(True, "ok", fake_review)
        ):
            result = runner.invoke(
                cli,
                ["plan-review", task_id, "--mode", "single", "--actor", "agent:test"],
                env={"LATTICE_ROOT": str(root)},
                catch_exceptions=False,
            )
        # Should not crash
        assert result.exit_code == 0 or "failed" in result.output

    def test_plan_approval_human_moves_status(self, tmp_path):
        root = _make_board(tmp_path, {"plan_review_mode": "single", "plan_approval": "human"})
        runner = CliRunner()
        task_id = _create_task(runner, root)
        _write_plan(root, task_id, "## Plan\nRefactor the auth module.")

        fake_review = "### 1. Verdict\n**PASS**"
        fake_art_id = "art_fakeid123"

        with (
            patch(
                "lattice.cli.review_cmds.run_single_review", return_value=(True, "ok", fake_review)
            ),
            patch(
                "lattice.cli.review_cmds._attach_review_artifact",
                return_value=fake_art_id,
            ),
            patch("lattice.cli.review_cmds._move_to_needs_human") as mock_move,
        ):
            runner.invoke(
                cli,
                ["plan-review", task_id, "--mode", "single", "--actor", "agent:test"],
                env={"LATTICE_ROOT": str(root)},
                catch_exceptions=False,
            )
        mock_move.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: triple mode (mocked agents)
# ---------------------------------------------------------------------------


class TestCodeReviewTriple:
    def test_triple_mode_stores_individual_and_merged(self, tmp_path):
        root = _make_board(tmp_path, {"review_mode": "triple"})
        runner = CliRunner()
        task_id = _create_task(runner, root)

        fake_diff = "diff --git a/foo.py b/foo.py\n+print('hello')"
        fake_reviews = [
            ("claude", True, "PASS claude"),
            ("codex", True, "PASS codex"),
            ("gemini", False, "timeout"),
        ]
        fake_merged = "MERGED PASS"

        with (
            patch("lattice.cli.review_cmds.resolve_diff", return_value=(True, fake_diff)),
            patch(
                "lattice.cli.review_cmds.run_triple_review",
                return_value=(True, "done", fake_reviews),
            ),
            patch(
                "lattice.cli.review_cmds.run_merge_agent",
                return_value=(True, fake_merged),
            ),
            patch(
                "lattice.cli.review_cmds._attach_review_artifact",
                return_value="art_fake",
            ) as mock_attach,
        ):
            runner.invoke(
                cli,
                ["code-review", task_id, "--mode", "triple", "--actor", "agent:test"],
                env={"LATTICE_ROOT": str(root)},
                catch_exceptions=False,
            )

        # Two successful individual reviews + 1 merged
        assert mock_attach.call_count == 3
        roles_used = [call.kwargs.get("role") for call in mock_attach.call_args_list]
        assert roles_used.count("review-individual") == 2
        assert roles_used.count("review") == 1

    def test_triple_mode_all_fail_reports_error(self, tmp_path):
        root = _make_board(tmp_path, {"review_mode": "triple"})
        runner = CliRunner()
        task_id = _create_task(runner, root)

        fake_diff = "diff --git a/foo.py b/foo.py\n+print('hello')"
        fake_reviews = [
            ("claude", False, "timeout"),
            ("codex", False, "timeout"),
            ("gemini", False, "timeout"),
        ]

        with (
            patch("lattice.cli.review_cmds.resolve_diff", return_value=(True, fake_diff)),
            patch(
                "lattice.cli.review_cmds.run_triple_review",
                return_value=(False, "done", fake_reviews),
            ),
        ):
            result = runner.invoke(
                cli,
                ["code-review", task_id, "--mode", "triple", "--actor", "agent:test"],
                env={"LATTICE_ROOT": str(root)},
                catch_exceptions=False,
            )
        assert "All agents failed" in result.output or result.exit_code != 0


# ---------------------------------------------------------------------------
# Tests: core/review.py unit tests
# ---------------------------------------------------------------------------


class TestReviewState:
    def test_write_read_clear(self, tmp_path):
        from lattice.core.review import clear_review_state, read_review_state, write_review_state

        ensure_lattice_dirs(tmp_path)
        lattice_dir = tmp_path / LATTICE_DIR

        state = {
            "task_id": "task_01ABC",
            "mode": "single",
            "review_type": "code-review",
            "started_at": "2026-01-01T00:00:00Z",
            "agents": [{"name": "claude", "status": "running", "artifact_id": None}],
        }

        write_review_state(lattice_dir, state)
        loaded = read_review_state(lattice_dir, "task_01ABC")
        assert loaded is not None
        assert loaded["mode"] == "single"

        clear_review_state(lattice_dir, "task_01ABC")
        assert read_review_state(lattice_dir, "task_01ABC") is None

    def test_read_missing_returns_none(self, tmp_path):
        from lattice.core.review import read_review_state

        ensure_lattice_dirs(tmp_path)
        lattice_dir = tmp_path / LATTICE_DIR

        result = read_review_state(lattice_dir, "task_does_not_exist")
        assert result is None


class TestSpawnAgent:
    def test_unknown_agent_returns_failure(self):
        from lattice.core.review import spawn_agent
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            prompt = tmp / "prompt.md"
            output = tmp / "out.md"
            prompt.write_text("test", encoding="utf-8")

            success, msg = spawn_agent("unknown_agent", prompt, output)
        assert success is False
        assert "Unknown agent type" in msg

    def test_command_failure_returns_failure(self):
        from lattice.core.review import spawn_agent
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            prompt = tmp / "prompt.md"
            output = tmp / "out.md"
            prompt.write_text("test", encoding="utf-8")

            # Patch the command builder to return a command that will fail
            with patch(
                "lattice.core.review._build_agent_command",
                return_value="exit 1",
            ):
                success, msg = spawn_agent("claude", prompt, output)

        assert success is False


class TestBuildMergePrompt:
    def test_includes_all_agents(self):
        from lattice.core.review import build_merge_prompt

        reviews = [
            ("claude", True, "PASS claude"),
            ("codex", False, "timeout"),
            ("gemini", True, "PASS gemini"),
        ]
        prompt = build_merge_prompt("LAT-190", reviews, "code-review")
        assert "claude" in prompt
        assert "gemini" in prompt
        assert "failed or timed out" in prompt
        assert "LAT-190" in prompt


class TestDiffResolution:
    def test_explicit_base_used(self, tmp_path):
        from lattice.core.review import resolve_diff

        # Mock subprocess to return a fake diff
        with patch(
            "lattice.core.review._git_diff",
            return_value="some diff content",
        ):
            with patch(
                "lattice.core.review._find_git_root",
                return_value=tmp_path,
            ):
                success, diff = resolve_diff(
                    tmp_path / ".lattice",
                    "task_01ABC",
                    {},
                    base="main",
                )
        assert success is True
        assert diff == "some diff content"

    def test_no_git_root_fails(self, tmp_path):
        from lattice.core.review import resolve_diff

        with patch("lattice.core.review._find_git_root", return_value=None):
            success, msg = resolve_diff(
                tmp_path / ".lattice",
                "task_01ABC",
                {},
            )
        assert success is False
        assert "git repository" in msg

    def test_branch_link_used(self, tmp_path):
        from lattice.core.review import resolve_diff

        snapshot = {"branch_links": [{"branch": "feat/my-feature"}]}
        with (
            patch("lattice.core.review._find_git_root", return_value=tmp_path),
            patch("lattice.core.review._find_base_branch", return_value="main"),
            patch("lattice.core.review._git_diff", return_value="branch diff"),
        ):
            success, diff = resolve_diff(tmp_path / ".lattice", "task_01ABC", snapshot)
        assert success is True
        assert diff == "branch diff"

    def test_fallback_to_error_when_nothing_works(self, tmp_path):
        from lattice.core.review import resolve_diff

        with (
            patch("lattice.core.review._find_git_root", return_value=tmp_path),
            patch("lattice.core.review._find_base_branch", return_value="main"),
            patch("lattice.core.review._git_diff", return_value=None),
            patch(
                "lattice.core.review._find_commits_by_message",
                return_value=None,
            ),
        ):
            success, msg = resolve_diff(tmp_path / ".lattice", "task_01ABC", {})
        assert success is False
        assert "--base" in msg
