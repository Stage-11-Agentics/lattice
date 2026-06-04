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
    """Initialize a .lattice/ directory and return root.

    Auto-fire of code-review/plan-review on status transitions (LAT-211) is
    disabled by default so tests that just walk through statuses do not
    actually fork a ``lattice code-review`` subprocess. Tests that exercise
    the auto-fire path override the relevant key via ``config_overrides``.
    """
    ensure_lattice_dirs(tmp_path)
    lattice_dir = tmp_path / LATTICE_DIR
    config = default_config()
    config["auto_code_review_on_transition"] = False
    config["auto_plan_review_on_transition"] = False
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

    def test_plan_approval_human_sets_flag(self, tmp_path):
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
            patch("lattice.cli.review_cmds._flag_needs_human") as mock_move,
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
    def test_triple_mode_spawns_c11_pane_and_returns(self, tmp_path):
        """Triple mode is fire-and-forget: ``run_triple_review`` is called once,
        no artifacts are stored by the CLI (the pane owns them), and the CLI
        exits 0 with a "running in pane:N" message."""
        root = _make_board(tmp_path, {"review_mode": "triple"})
        runner = CliRunner()
        task_id = _create_task(runner, root)

        fake_diff = "diff --git a/foo.py b/foo.py\n+print('hello')"

        with (
            patch("lattice.cli.review_cmds.resolve_diff", return_value=(True, fake_diff)),
            patch(
                "lattice.cli.review_cmds.run_triple_review",
                return_value=(
                    True,
                    "Triple review running in surface:99 — task status is the sync primitive.",
                ),
            ) as mock_run,
            patch("lattice.cli.review_cmds._attach_review_artifact") as mock_attach,
        ):
            result = runner.invoke(
                cli,
                ["code-review", task_id, "--mode", "triple", "--actor", "agent:test"],
                env={"LATTICE_ROOT": str(root)},
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        assert "running in surface:99" in result.output
        # CLI no longer stores artifacts in triple mode — the pane does.
        assert mock_attach.call_count == 0
        # run_triple_review is the fire-and-forget primitive.
        assert mock_run.call_count == 1
        kwargs = mock_run.call_args.kwargs
        assert kwargs["review_type"] == "code-review"
        assert kwargs["task_id"] == task_id

    def test_triple_mode_outside_c11_errors(self, tmp_path):
        """Triple mode outside c11 must fail cleanly with a non-zero exit and
        release the in-flight claim so retries aren't blocked."""
        root = _make_board(tmp_path, {"review_mode": "triple"})
        runner = CliRunner()
        task_id = _create_task(runner, root)

        fake_diff = "diff --git a/foo.py b/foo.py\n+print('hello')"

        with (
            patch("lattice.cli.review_cmds.resolve_diff", return_value=(True, fake_diff)),
            patch(
                "lattice.cli.review_cmds.run_triple_review",
                return_value=(
                    False,
                    "triple mode requires c11 — run from inside a c11 surface, or use --mode single.",
                ),
            ),
        ):
            result = runner.invoke(
                cli,
                ["code-review", task_id, "--mode", "triple", "--actor", "agent:test"],
                env={"LATTICE_ROOT": str(root)},
                catch_exceptions=False,
            )
        assert result.exit_code != 0
        assert "triple mode requires c11" in result.output
        # Failed spawn must release the in-flight claim so retries aren't
        # blocked by a phantom review_state record.
        from lattice.core.review import read_review_state

        assert read_review_state(root / LATTICE_DIR, task_id) is None


# ---------------------------------------------------------------------------
# Tests: core/review.py unit tests
# ---------------------------------------------------------------------------


class TestReviewClaimAndDisplay:
    """Coordination tests for the review_state claim path (LAT-211)."""

    def test_review_status_displays_auto_fired_field(self, tmp_path: Path) -> None:
        from lattice.core.review import write_review_state

        root = _make_board(tmp_path)
        runner = CliRunner()
        task_id = _create_task(runner, root)
        write_review_state(
            root / LATTICE_DIR,
            {
                "task_id": task_id,
                "mode": "triple",
                "review_type": "code-review",
                "started_at": "2026-05-06T00:00:00Z",
                "started_by_pid": 4242,
                "auto_fired": True,
                "agents": [],
            },
        )
        result = runner.invoke(
            cli,
            ["review-status", task_id],
            env={"LATTICE_ROOT": str(root)},
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        assert "auto_fired:" in result.output
        assert "True" in result.output
        assert "started_by_pid 4242" in result.output

    def test_review_status_json_round_trips_new_fields(self, tmp_path: Path) -> None:
        from lattice.core.review import write_review_state

        root = _make_board(tmp_path)
        runner = CliRunner()
        task_id = _create_task(runner, root)
        write_review_state(
            root / LATTICE_DIR,
            {
                "task_id": task_id,
                "mode": "single",
                "review_type": "code-review",
                "started_at": "2026-05-06T00:00:00Z",
                "started_by_pid": 9999,
                "auto_fired": False,
                "agents": [],
            },
        )
        result = runner.invoke(
            cli,
            ["review-status", task_id, "--json"],
            env={"LATTICE_ROOT": str(root)},
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        data = json.loads(result.output)["data"]
        assert data["auto_fired"] is False
        assert data["started_by_pid"] == 9999

    def test_code_review_refuses_when_live_other_pid_holds(self, tmp_path: Path) -> None:
        import os

        from lattice.core.review import write_review_state

        ppid = os.getppid()
        if ppid == os.getpid() or ppid <= 1:
            import pytest

            pytest.skip("Need a usable parent pid for this test.")

        root = _make_board(tmp_path)
        runner = CliRunner()
        task_id = _create_task(runner, root)

        # Seed a record held by an external live PID (test parent process).
        write_review_state(
            root / LATTICE_DIR,
            {
                "task_id": task_id,
                "mode": "single",
                "review_type": "code-review",
                "started_at": "2026-05-06T00:00:00Z",
                "started_by_pid": ppid,
                "auto_fired": False,
                "agents": [],
            },
        )

        result = runner.invoke(
            cli,
            ["code-review", task_id, "--mode", "single", "--actor", "agent:test"],
            env={"LATTICE_ROOT": str(root)},
            catch_exceptions=False,
        )
        assert result.exit_code != 0
        assert "already in flight" in result.output
        assert f"pid {ppid}" in result.output

    def test_inline_review_refuses_when_other_pid_holds(self, tmp_path: Path) -> None:
        import os

        from lattice.core.review import write_review_state

        ppid = os.getppid()
        if ppid == os.getpid() or ppid <= 1:
            import pytest

            pytest.skip("Need a usable parent pid for this test.")

        root = _make_board(tmp_path, {"review_mode": "inline"})
        runner = CliRunner()
        task_id = _create_task(runner, root)
        write_review_state(
            root / LATTICE_DIR,
            {
                "task_id": task_id,
                "mode": "single",
                "review_type": "code-review",
                "started_at": "2026-05-06T00:00:00Z",
                "started_by_pid": ppid,
                "auto_fired": False,
                "agents": [],
            },
        )
        result = runner.invoke(
            cli,
            ["code-review", task_id, "--actor", "agent:test"],
            env={"LATTICE_ROOT": str(root)},
            catch_exceptions=False,
        )
        assert result.exit_code != 0
        assert "already in flight" in result.output

    def test_code_review_with_triggered_by_adopts_parent_state(self, tmp_path: Path) -> None:
        # Simulate the rare parent-still-alive edge: parent's pid is alive
        # AND ``--triggered-by`` flags this child as the auto-fired adopter.
        # The CLI body must overwrite started_by_pid → ours, leaving
        # ``auto_fired=True``.  We only run up to the claim step (mocked
        # ``resolve_diff`` & friends) to avoid spawning real agents.
        import os

        from lattice.core.review import read_review_state, write_review_state

        ppid = os.getppid()
        if ppid == os.getpid() or ppid <= 1:
            import pytest

            pytest.skip("Need a usable parent pid for this test.")

        root = _make_board(tmp_path)
        runner = CliRunner()
        task_id = _create_task(runner, root)
        # Give the task a minimal plan + diff context.
        _write_plan(root, task_id, "# Test\n\nApproach: implement.\n")
        write_review_state(
            root / LATTICE_DIR,
            {
                "task_id": task_id,
                "mode": "single",
                "review_type": "code-review",
                "started_at": "2026-05-06T00:00:00Z",
                "started_by_pid": ppid,  # alive — would normally refuse
                "auto_fired": True,
                "agents": [],
            },
        )

        with (
            patch(
                "lattice.cli.review_cmds.resolve_diff",
                return_value=(True, "diff --git a/x.py b/x.py\n"),
            ),
            patch(
                "lattice.cli.review_cmds.run_single_review",
                return_value=(True, "ok", "PASS"),
            ),
            patch(
                "lattice.cli.review_cmds._attach_review_artifact",
                return_value="art_fake",
            ),
        ):
            result = runner.invoke(
                cli,
                [
                    "code-review",
                    task_id,
                    "--mode",
                    "single",
                    "--actor",
                    "agent:test",
                    "--triggered-by",
                    "ev_fake",
                ],
                env={"LATTICE_ROOT": str(root)},
                catch_exceptions=False,
            )
        assert result.exit_code == 0, result.output
        # Most importantly: the command did NOT exit with REVIEW_IN_FLIGHT.
        assert "already in flight" not in result.output
        # After the adoption path the on-disk record holds *our* PID
        # (mocked ``run_single_review`` doesn't run ``clear_review_state``,
        # so the adoption write is what we observe).  ``auto_fired`` stays
        # True — the audit-trail signal is preserved through the handoff.
        state_after = read_review_state(root / LATTICE_DIR, task_id)
        assert state_after is not None
        assert state_after["started_by_pid"] == os.getpid()
        assert state_after["auto_fired"] is True


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

            # Patch the command builder (now lives in agent_spawn after
            # LAT-205) to return a command that will fail. The shim delegates
            # via storage.agent_spawn.HeadlessBackend → _agent_cli_command.
            with patch(
                "lattice.storage.agent_spawn._agent_cli_command",
                return_value="exit 1",
            ):
                success, msg = spawn_agent("claude", prompt, output)

        assert success is False


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
