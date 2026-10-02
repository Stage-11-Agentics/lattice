"""Tests for code-review, plan-review, and review-status CLI commands."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from lattice.cli.main import cli
from lattice.core.config import default_config, serialize_config
from lattice.storage.fs import LATTICE_DIR, ensure_lattice_dirs, atomic_write

# These commands resolve the caller's git worktree from cwd.
pytestmark = pytest.mark.usefixtures("caller_git_worktree")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolution(
    diff: str = "diff --git a/foo.py b/foo.py\n+print('hello')",
    *,
    success: bool = True,
    error: str | None = None,
    error_code: str | None = None,
    base_ref: str = "origin/main",
    base_selection_rule: str = "inferred_nearest_merge_base",
    head_ref: str = "feat/branch",
    head_sha: str | None = "b" * 40,
):
    """Build a DiffResolution for tests that mock out git entirely."""
    from lattice.core.review import DiffResolution

    return DiffResolution(
        success=success,
        diff=diff,
        error=error,
        error_code=error_code,
        base_ref=base_ref,
        head_ref=head_ref,
        base_sha="a" * 40,
        head_sha=head_sha,
        worktree=Path.cwd().resolve(),
        source="linked_branch",
        base_selection_rule=base_selection_rule,
    )


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

    def test_an_attached_review_artifact_is_reported(self, tmp_path):
        """Artifact metadata lives under artifacts/meta/, not per task: the check
        reads the task's artifact_attached events (it used to find none, ever)."""
        root = _make_board(tmp_path)
        runner = CliRunner()
        task_id = _create_task(runner, root)
        env = {"LATTICE_ROOT": str(root)}
        attach = ["attach", task_id, "--inline", "LGTM", "--role", "review"]
        assert runner.invoke(cli, [*attach, "--actor", "agent:test"], env=env).exit_code == 0

        plain = runner.invoke(cli, ["review-status", task_id], env=env, catch_exceptions=False)
        assert "Review artifacts exist" in plain.output
        as_json = runner.invoke(
            cli, ["review-status", task_id, "--json"], env=env, catch_exceptions=False
        )
        assert "note" in json.loads(as_json.output)["data"]

    def test_reports_latest_code_and_plan_review_artifacts_by_role(self, tmp_path):
        root = _make_board(tmp_path)
        runner = CliRunner()
        task_id = _create_task(runner, root)
        env = {"LATTICE_ROOT": str(root)}

        for body, role in (
            ("old code review", "review"),
            ("latest code review", "review"),
            ("latest plan review", "plan-review"),
        ):
            attached = runner.invoke(
                cli,
                ["attach", task_id, "--inline", body, "--role", role, "--actor", "agent:test"],
                env=env,
                catch_exceptions=False,
            )
            assert attached.exit_code == 0, attached.output

        events_path = root / LATTICE_DIR / "events" / f"{task_id}.jsonl"
        attached_events = [
            event
            for event in map(json.loads, events_path.read_text(encoding="utf-8").splitlines())
            if event.get("type") == "artifact_attached"
        ]
        old_id, latest_code_id, plan_id = [
            event["data"]["artifact_id"] for event in attached_events
        ]

        plain = runner.invoke(cli, ["review-status", task_id], env=env, catch_exceptions=False)
        assert plain.exit_code == 0
        assert "code-review (role=review)" in plain.output
        assert "plan-review" in plain.output
        assert latest_code_id in plain.output
        assert plan_id in plain.output
        assert old_id not in plain.output

        as_json = runner.invoke(
            cli, ["review-status", task_id, "--json"], env=env, catch_exceptions=False
        )
        data = json.loads(as_json.output)["data"]
        assert data["artifacts"]["review"]["review_type"] == "code-review"
        assert data["artifacts"]["review"]["artifact_id"] == latest_code_id
        assert data["artifacts"]["plan-review"]["review_type"] == "plan-review"
        assert data["artifacts"]["plan-review"]["artifact_id"] == plan_id

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

    def test_shows_failed_review_state(self, tmp_path):
        """LAT-243: a durable 'failed' review_state surfaces loudly, not as 'no review'."""
        root = _make_board(tmp_path)
        runner = CliRunner()
        task_id = _create_task(runner, root)

        from lattice.core.review import write_review_state

        lattice_dir = root / LATTICE_DIR
        write_review_state(
            lattice_dir,
            {
                "task_id": task_id,
                "mode": "single",
                "review_type": "code-review",
                "status": "failed",
                "started_at": "2026-06-23T00:00:00Z",
                "finished_at": "2026-06-23T00:05:00Z",
                "error": "produced no output",
                "detail": {"returncode": 1, "duration_seconds": 300.0, "stderr_tail": "boom"},
                "agents": [{"name": "claude", "status": "failed", "artifact_id": None}],
            },
        )

        result = runner.invoke(
            cli,
            ["review-status", task_id],
            env={"LATTICE_ROOT": str(root)},
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        assert "FAILED" in result.output
        assert "produced no output" in result.output
        # Must NOT masquerade as "nothing ran".
        assert "No in-flight review found" not in result.output

    def test_falls_back_to_failures_jsonl(self, tmp_path):
        """LAT-243: with no review_state, a recorded failure still surfaces (not 'no review')."""
        root = _make_board(tmp_path)
        runner = CliRunner()
        task_id = _create_task(runner, root)

        from lattice.core.review import record_agent_failure

        lattice_dir = root / LATTICE_DIR
        record_agent_failure(
            lattice_dir,
            "claude",
            task_id,
            detail={"error": "exited with code 1", "returncode": 1, "duration_seconds": 312.0},
        )

        result = runner.invoke(
            cli,
            ["review-status", task_id],
            env={"LATTICE_ROOT": str(root)},
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        assert "FAILED" in result.output
        assert "exited with code 1" in result.output

        # JSON surfaces the failure too.
        result_json = runner.invoke(
            cli,
            ["review-status", task_id, "--json"],
            env={"LATTICE_ROOT": str(root)},
            catch_exceptions=False,
        )
        data = json.loads(result_json.output)
        assert data["data"]["status"] == "failed"
        assert data["data"]["last_failure"]["task_id"] == task_id


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
            return_value=_resolution(
                success=False,
                diff="",
                error="Not inside a git repository.",
                error_code="NO_GIT_REPO",
            ),
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
            return_value=_resolution(""),
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
            patch("lattice.cli.review_cmds.resolve_diff", return_value=_resolution(fake_diff)),
            patch(
                "lattice.cli.review_cmds.run_single_review",
                return_value=(True, "Review complete.", fake_review),
            ) as run_single,
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
                ],
                env={"LATTICE_ROOT": str(root)},
                catch_exceptions=False,
            )
        # Should not error (may warn if lattice attach subprocess fails in test env)
        # The key assertion: no unhandled exception and diff was attempted
        assert (
            result.exit_code == 0 or "Review stored" in result.output or "failed" in result.output
        )
        # Manual command boundary carries a normalized source checkout into
        # the reviewer; it is not inferred from LATTICE_ROOT.
        assert run_single.call_args.kwargs["worktree"] == Path.cwd().resolve()

    def test_single_mode_agent_failure_reports_error(self, tmp_path):
        root = _make_board(tmp_path, {"review_mode": "single"})
        runner = CliRunner()
        task_id = _create_task(runner, root)

        fake_diff = "diff --git a/foo.py b/foo.py\n+print('hello')"

        with (
            patch("lattice.cli.review_cmds.resolve_diff", return_value=_resolution(fake_diff)),
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
        # Agent failure is a failed command: non-zero exit AND a stated reason.
        # (The `or` this assertion used to allow is exactly how an exit-0
        # failure path stayed green for two days of silent review failures.)
        assert result.exit_code != 0
        assert "failed" in result.output


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

    @pytest.mark.parametrize("output_args", [("--json",), ("--quiet",)])
    def test_human_plan_approval_keeps_result_stdout_clean(self, tmp_path, output_args):
        from types import SimpleNamespace

        root = _make_board(tmp_path, {"plan_review_mode": "single", "plan_approval": "human"})
        runner = CliRunner()
        task_id = _create_task(runner, root)
        _write_plan(root, task_id, "## Plan\nRefactor the auth module.")
        art_id = "art_fake-plan-review"

        with (
            patch(
                "lattice.cli.review_cmds.run_single_review",
                return_value=(True, "ok", "### 1. Verdict\n**PASS**"),
            ),
            patch("lattice.cli.review_cmds._attach_review_artifact", return_value=art_id),
            patch(
                "lattice.cli.review_cmds.subprocess.run",
                return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
            ),
        ):
            result = runner.invoke(
                cli,
                [
                    "plan-review",
                    task_id,
                    "--mode",
                    "single",
                    "--actor",
                    "agent:test",
                    *output_args,
                ],
                env={"LATTICE_ROOT": str(root)},
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        if "--json" in output_args:
            payload = json.loads(result.stdout)
            assert payload == {"ok": True, "data": {"artifact_id": art_id, "role": "plan-review"}}
        else:
            assert result.stdout == f"{art_id}\n"
        assert "Running plan-review" not in result.stdout
        assert "Running plan-review" in result.stderr
        assert "timeout 600s" in result.stderr
        assert f"lattice review-status {task_id}" in result.stderr
        assert "needs_human flag set" in result.stderr

    def test_completion_names_artifact_show_command(self, tmp_path):
        root = _make_board(tmp_path, {"plan_review_mode": "single"})
        runner = CliRunner()
        task_id = _create_task(runner, root)
        _write_plan(root, task_id, "## Plan\nRefactor the auth module.")

        with (
            patch(
                "lattice.cli.review_cmds.run_single_review",
                return_value=(True, "ok", "PASS"),
            ),
            patch("lattice.cli.review_cmds._attach_review_artifact", return_value="art_fake"),
            patch("lattice.cli.review_cmds.program_name", return_value="lattice-alt"),
        ):
            result = runner.invoke(
                cli,
                ["plan-review", task_id, "--mode", "single", "--actor", "agent:test"],
                env={"LATTICE_ROOT": str(root)},
                catch_exceptions=False,
            )
        assert result.exit_code == 0, result.output
        assert "lattice-alt artifact show art_fake" in result.stdout
        assert "Running plan-review" in result.stderr
        assert f"lattice-alt review-status {task_id}" in result.stderr

    def test_heartbeat_stops_before_artifact_attachment(self, tmp_path):
        import click

        root = _make_board(tmp_path, {"plan_review_mode": "single"})
        runner = CliRunner()
        task_id = _create_task(runner, root)
        _write_plan(root, task_id, "## Plan\nRefactor the auth module.")
        real_echo = click.echo
        ticks: list[str] = []
        tick_count_at_attach: list[int] = []

        def recording_echo(*args, **kwargs):
            message = str(args[0]) if args else str(kwargs.get("message", ""))
            if "still running" in message:
                ticks.append(message)
            return real_echo(*args, **kwargs)

        def delayed_review(**_kwargs):
            time.sleep(0.055)
            return True, "ok", "PASS"

        def delayed_attach(**_kwargs):
            tick_count_at_attach.append(len(ticks))
            time.sleep(0.045)
            return "art_fake"

        with (
            patch("lattice.cli.review_cmds._REVIEW_HEARTBEAT_INTERVAL_SECONDS", 0.01),
            patch("lattice.cli.review_cmds.run_single_review", side_effect=delayed_review),
            patch("lattice.cli.review_cmds._attach_review_artifact", side_effect=delayed_attach),
            patch("lattice.cli.review_cmds.click.echo", side_effect=recording_echo),
        ):
            result = runner.invoke(
                cli,
                ["plan-review", task_id, "--mode", "single", "--actor", "agent:test"],
                env={"LATTICE_ROOT": str(root)},
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        assert len(ticks) >= 2
        assert tick_count_at_attach == [len(ticks)]
        assert all("elapsed; timeout 600s" in tick for tick in ticks)

    def test_heartbeat_stops_quietly_when_stderr_is_closed(self, tmp_path):
        import click

        root = _make_board(tmp_path, {"plan_review_mode": "single"})
        runner = CliRunner()
        task_id = _create_task(runner, root)
        _write_plan(root, task_id, "## Plan\nRefactor the auth module.")
        real_echo = click.echo

        def broken_progress(*args, **kwargs):
            message = str(args[0]) if args else str(kwargs.get("message", ""))
            if "still running" in message:
                raise BrokenPipeError("closed stderr")
            return real_echo(*args, **kwargs)

        with (
            patch("lattice.cli.review_cmds._REVIEW_HEARTBEAT_INTERVAL_SECONDS", 0.001),
            patch(
                "lattice.cli.review_cmds.run_single_review",
                side_effect=lambda **_kwargs: (time.sleep(0.01) or True, "ok", "PASS"),
            ),
            patch("lattice.cli.review_cmds._attach_review_artifact", return_value="art_fake"),
            patch("lattice.cli.review_cmds.click.echo", side_effect=broken_progress),
        ):
            result = runner.invoke(
                cli,
                ["plan-review", task_id, "--mode", "single", "--actor", "agent:test"],
                env={"LATTICE_ROOT": str(root)},
                catch_exceptions=False,
            )
        assert result.exit_code == 0, result.output


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
            patch("lattice.cli.review_cmds.resolve_diff", return_value=_resolution(fake_diff)),
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
        assert kwargs["worktree"] == Path.cwd().resolve()

    def test_triple_mode_hands_the_pane_the_resolved_range(self, tmp_path):
        """The pane's cwd is the caller's checkout, whose HEAD is usually not the
        branch under review. Base alone leaves it diffing the wrong tree."""
        root = _make_board(tmp_path, {"review_mode": "triple"})
        runner = CliRunner()
        task_id = _create_task(runner, root)

        with (
            patch("lattice.cli.review_cmds.resolve_diff", return_value=_resolution()),
            patch(
                "lattice.cli.review_cmds.run_triple_review",
                return_value=(True, "Triple review running in surface:99."),
            ) as mock_run,
        ):
            runner.invoke(
                cli,
                ["code-review", task_id, "--mode", "triple", "--actor", "agent:test"],
                env={"LATTICE_ROOT": str(root)},
                catch_exceptions=False,
            )

        kwargs = mock_run.call_args.kwargs
        assert kwargs["base"] == "origin/main"
        assert kwargs["head"] == "feat/branch"
        assert kwargs["head_sha"] == "b" * 40

    def test_triple_mode_outside_c11_errors(self, tmp_path):
        """Triple mode outside c11 must fail cleanly with a non-zero exit and
        release the in-flight claim so retries aren't blocked."""
        root = _make_board(tmp_path, {"review_mode": "triple"})
        runner = CliRunner()
        task_id = _create_task(runner, root)

        fake_diff = "diff --git a/foo.py b/foo.py\n+print('hello')"

        with (
            patch("lattice.cli.review_cmds.resolve_diff", return_value=_resolution(fake_diff)),
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
        import os

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
                "started_by_pid": os.getpid(),
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
        assert f"started_by_pid {os.getpid()}" in result.output

    def test_review_status_json_round_trips_new_fields(self, tmp_path: Path) -> None:
        import os

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
                "started_by_pid": os.getpid(),
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
        assert data["started_by_pid"] == os.getpid()

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
                return_value=_resolution("diff --git a/x.py b/x.py\n"),
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
        with (
            patch("lattice.core.review._git_diff", return_value="some diff content"),
            patch("lattice.core.review._find_git_root", return_value=tmp_path),
            patch("lattice.core.review._ref_exists", return_value=True),
            patch("lattice.core.review._merge_base", return_value="a" * 40),
            patch("lattice.core.review._rev_parse", return_value="b" * 40),
        ):
            res = resolve_diff(
                tmp_path / ".lattice",
                "task_01ABC",
                {},
                base="main",
            )
        assert res.success is True
        assert res.diff == "some diff content"
        assert res.base_ref == "main"

    def test_no_git_root_fails(self, tmp_path):
        from lattice.core.review import resolve_diff

        with patch("lattice.core.review._find_git_root", return_value=None):
            res = resolve_diff(
                tmp_path / ".lattice",
                "task_01ABC",
                {},
            )
        assert res.success is False
        assert "git repository" in (res.error or "")

    def test_branch_link_used(self, tmp_path):
        from lattice.core.review import resolve_diff

        snapshot = {"branch_links": [{"branch": "feat/my-feature"}]}
        with (
            patch("lattice.core.review._find_git_root", return_value=tmp_path),
            patch("lattice.core.review._ref_exists", return_value=True),
            patch(
                "lattice.core.review._resolve_base_ref",
                return_value=("origin/main", "a" * 40, None, "inferred_nearest_merge_base"),
            ),
            patch("lattice.core.review._rev_parse", return_value="b" * 40),
            patch("lattice.core.review._git_diff", return_value="branch diff"),
        ):
            res = resolve_diff(tmp_path / ".lattice", "task_01ABC", snapshot)
        assert res.success is True
        assert res.diff == "branch diff"
        assert res.head_ref == "feat/my-feature"
        assert res.source == "linked_branch"

    def test_unresolvable_linked_branch_fails_loudly(self, tmp_path):
        """A branch link that does not resolve is an error, never a fall-through
        to some other tree."""
        from lattice.core.review import resolve_diff

        snapshot = {"branch_links": [{"branch": "feat/gone"}], "short_id": "LAT-9"}
        with (
            patch("lattice.core.review._find_git_root", return_value=tmp_path),
            patch("lattice.core.review._ref_exists", return_value=False),
        ):
            res = resolve_diff(tmp_path / ".lattice", "task_01ABC", snapshot)
        assert res.success is False
        assert res.error_code == "HEAD_REF_UNRESOLVABLE"
        assert "feat/gone" in (res.error or "")
        assert "--head" in (res.error or "")
        assert res.diff == ""

    def test_fallback_to_error_when_diff_command_fails(self, tmp_path):
        from lattice.core.review import resolve_diff

        with (
            patch("lattice.core.review._find_git_root", return_value=tmp_path),
            patch(
                "lattice.core.review._resolve_base_ref",
                return_value=("origin/main", None, None, "inferred_nearest_merge_base"),
            ),
            patch("lattice.core.review._rev_parse", return_value=None),
            patch("lattice.core.review._git_diff", return_value=None),
        ):
            res = resolve_diff(tmp_path / ".lattice", "task_01ABC", {})
        assert res.success is False
        assert "--base" in (res.error or "")


# ---------------------------------------------------------------------------
# Tests: a failed review has to be visible (LAT-267)
# ---------------------------------------------------------------------------

#: A pid above macOS/Linux pid_max — os.kill() on it always raises
#: ProcessLookupError, so it is a deterministically dead holder.
DEAD_PID = 4_000_000


def _snapshot(root: Path, task_id: str) -> dict:
    return json.loads((root / LATTICE_DIR / "tasks" / f"{task_id}.json").read_text())


def _comment_bodies(root: Path, task_id: str) -> list[str]:
    path = root / LATTICE_DIR / "events" / f"{task_id}.jsonl"
    bodies = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if event.get("type") == "comment_added":
            bodies.append(event.get("data", {}).get("body", ""))
    return bodies


def _run_failing_code_review(
    runner: CliRunner,
    root: Path,
    task_id: str,
    *extra: str,
    failure_message: str = "Agent 'claude' timed out after 600s",
):
    from lattice.core.agent_spawn import SpawnResult

    with (
        patch(
            "lattice.cli.review_cmds.resolve_diff",
            return_value=_resolution(),
        ),
        patch(
            "lattice.core.review.spawn_one",
            return_value=SpawnResult(
                agent="claude",
                success=False,
                output_text="",
                error=failure_message,
                backend="headless",
                duration_seconds=720.0,
                returncode=-1,
                stderr_tail="mocked timeout",
            ),
        ),
    ):
        return runner.invoke(
            cli,
            ["code-review", task_id, "--mode", "single", "--actor", "agent:test", *extra],
            env={"LATTICE_ROOT": str(root)},
            catch_exceptions=False,
        )


class TestFailedReviewIsVisible:
    def test_failure_exits_non_zero(self, tmp_path):
        root = _make_board(tmp_path, {"review_mode": "single"})
        runner = CliRunner()
        task_id = _create_task(runner, root)

        result = _run_failing_code_review(runner, root, task_id)

        assert result.exit_code != 0, result.output
        assert "timed out after 600s" in result.output

    def test_timeout_reports_board_budgets_and_next_step(self, tmp_path):
        settings = {
            "review_timeout_seconds": 720,
            "review_max_diff_lines": 1234,
            "review_max_diff_chars": 45678,
        }
        root = _make_board(tmp_path, {"review_mode": "single", **settings})
        runner = CliRunner()
        task_id = _create_task(runner, root)

        result = _run_failing_code_review(
            runner,
            root,
            task_id,
            failure_message="Agent 'claude' timed out after 720s",
        )

        assert result.exit_code != 0
        assert "review_timeout_seconds=720" in result.output
        assert "review_max_diff_lines=1234" in result.output
        assert "review_max_diff_chars=45678" in result.output
        assert "Narrow the review diff" in result.output
        assert "in .lattice/config.json before retrying" in result.output

        status = runner.invoke(
            cli,
            ["review-status", task_id],
            env={"LATTICE_ROOT": str(root)},
            catch_exceptions=False,
        )
        assert "configured limits" in status.output
        assert "review_timeout_seconds=720" in status.output

        status_json = runner.invoke(
            cli,
            ["review-status", task_id, "--json"],
            env={"LATTICE_ROOT": str(root)},
            catch_exceptions=False,
        )
        guidance = json.loads(status_json.output)["data"]["timeout_guidance"]
        assert guidance["review_timeout_seconds"] == 720
        assert guidance["review_max_diff_lines"] == 1234
        assert guidance["review_max_diff_chars"] == 45678
        assert "Narrow the review diff" in guidance["next_step"]

    def test_hosted_remediation_uses_server_project_config_command(self, tmp_path):
        from lattice.cli.review_cmds import _review_base_config_remedy, _timeout_guidance

        root = tmp_path / "hosted-cache"
        lattice_dir = root / ".lattice"
        (lattice_dir / "cache").mkdir(parents=True)
        identity = {"remote": "stage11", "project": "lattice"}
        (root / ".lattice-remote.json").write_text(json.dumps(identity))
        (lattice_dir / "cache" / "state.json").write_text(json.dumps(identity))

        base_remedy = _review_base_config_remedy(lattice_dir)
        timeout_remedy = _timeout_guidance({}, lattice_dir)["next_step"]

        assert "server project config lattice --set review_base_branch=<branch>" in base_remedy
        assert "on the server host" in base_remedy
        assert "server project config lattice" in timeout_remedy
        assert "--set review_timeout_seconds=<seconds>" in timeout_remedy
        assert ".lattice/config.json" not in timeout_remedy

    def test_success_still_exits_zero(self, tmp_path):
        """Positive pair for the exit-code assertion above."""
        root = _make_board(tmp_path, {"review_mode": "single"})
        runner = CliRunner()
        task_id = _create_task(runner, root)

        with (
            patch(
                "lattice.cli.review_cmds.resolve_diff",
                return_value=_resolution(),
            ),
            patch(
                "lattice.cli.review_cmds.run_single_review",
                return_value=(True, "Review complete.", "### 1. Verdict\n**PASS**"),
            ),
            patch("lattice.cli.review_cmds._attach_review_artifact", return_value="art_fake"),
        ):
            result = runner.invoke(
                cli,
                ["code-review", task_id, "--mode", "single", "--actor", "agent:test"],
                env={"LATTICE_ROOT": str(root)},
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        assert not any("Automated review failed" in b for b in _comment_bodies(root, task_id))

    def test_failure_comments_on_the_task(self, tmp_path):
        root = _make_board(tmp_path, {"review_mode": "single"})
        runner = CliRunner()
        task_id = _create_task(runner, root)

        _run_failing_code_review(runner, root, task_id)

        bodies = [b for b in _comment_bodies(root, task_id) if "Automated review failed" in b]
        assert bodies, (
            f"no failure comment recorded; comments were {_comment_bodies(root, task_id)}"
        )
        assert "timed out after 600s" in bodies[0]
        assert "has NOT been reviewed" in bodies[0]

    def test_manual_failure_does_not_flag_needs_human(self, tmp_path):
        """A human/agent running the command sees the non-zero exit directly."""
        root = _make_board(tmp_path, {"review_mode": "single"})
        runner = CliRunner()
        task_id = _create_task(runner, root)

        _run_failing_code_review(runner, root, task_id)

        assert not _snapshot(root, task_id).get("needs_human")

    def test_auto_fired_failure_flags_needs_human(self, tmp_path):
        """Nobody is reading an auto-fired review's exit code, so raise the flag."""
        root = _make_board(tmp_path, {"review_mode": "single"})
        runner = CliRunner()
        task_id = _create_task(runner, root)

        _run_failing_code_review(runner, root, task_id, "--triggered-by", "ev_fake")

        snapshot = _snapshot(root, task_id)
        flag = snapshot.get("needs_human")
        assert flag, "auto-fired review failure left no needs_human flag"
        assert "unreviewed" in flag["reason"]

    def test_plan_review_failure_exits_non_zero_and_comments(self, tmp_path):
        root = _make_board(tmp_path, {"plan_review_mode": "single"})
        runner = CliRunner()
        task_id = _create_task(runner, root)
        _write_plan(root, task_id, "# Plan\n\nApproach: implement it.\n")

        with patch(
            "lattice.cli.review_cmds.run_single_review",
            return_value=(False, "Agent 'claude' timed out after 600s", None),
        ):
            result = runner.invoke(
                cli,
                ["plan-review", task_id, "--mode", "single", "--actor", "agent:test"],
                env={"LATTICE_ROOT": str(root)},
                catch_exceptions=False,
            )

        assert result.exit_code != 0, result.output
        assert any("Automated review failed" in b for b in _comment_bodies(root, task_id))

    # -- resolution failures are failures too (LAT-271) ---------------------
    #
    # A review that dies at diff resolution never reaches the agent, so it
    # takes none of the paths above unless it is wired to. Left unwired it
    # vanishes: `review-status` reports "No in-flight review found ... No
    # review artifacts found either" while the task sits in `review` looking
    # reviewed. These assert the same visibility an agent failure gets.

    def _run_failing_resolution(self, runner, root, task_id, *extra: str):
        with patch(
            "lattice.cli.review_cmds.resolve_diff",
            return_value=_resolution(
                success=False,
                diff="",
                error="Task has a linked branch 'feat/gone' but it does not resolve.",
                error_code="HEAD_REF_UNRESOLVABLE",
            ),
        ):
            return runner.invoke(
                cli,
                ["code-review", task_id, "--mode", "single", "--actor", "agent:test", *extra],
                env={"LATTICE_ROOT": str(root)},
                catch_exceptions=False,
            )

    def _review_status(self, runner, root, task_id, *extra: str):
        return runner.invoke(
            cli,
            ["review-status", task_id, *extra],
            env={"LATTICE_ROOT": str(root)},
            catch_exceptions=False,
        )

    def test_resolution_failure_still_shows_in_review_status(self, tmp_path):
        root = _make_board(tmp_path, {"review_mode": "single"})
        runner = CliRunner()
        task_id = _create_task(runner, root)

        result = self._run_failing_resolution(runner, root, task_id)
        assert result.exit_code != 0, result.output

        status = self._review_status(runner, root, task_id)
        assert "Review FAILED" in status.output, status.output
        assert "does not resolve" in status.output
        assert "No in-flight review found" not in status.output

    def test_resolution_failure_review_status_json_says_failed(self, tmp_path):
        root = _make_board(tmp_path, {"review_mode": "single"})
        runner = CliRunner()
        task_id = _create_task(runner, root)

        self._run_failing_resolution(runner, root, task_id)

        payload = json.loads(self._review_status(runner, root, task_id, "--json").output)
        assert payload["ok"] is True
        assert payload["data"]["status"] == "failed"
        assert payload["data"]["detail"]["error_code"] == "HEAD_REF_UNRESOLVABLE"

    def test_resolution_failure_comments_on_the_task(self, tmp_path):
        root = _make_board(tmp_path, {"review_mode": "single"})
        runner = CliRunner()
        task_id = _create_task(runner, root)

        self._run_failing_resolution(runner, root, task_id)

        bodies = [b for b in _comment_bodies(root, task_id) if "Automated review failed" in b]
        assert bodies, f"no failure comment; comments were {_comment_bodies(root, task_id)}"
        assert "does not resolve" in bodies[0]
        assert not _snapshot(root, task_id).get("needs_human"), (
            "a manual run reads its own exit code — no flag"
        )

    def test_auto_fired_resolution_failure_flags_needs_human(self, tmp_path):
        root = _make_board(tmp_path, {"review_mode": "single"})
        runner = CliRunner()
        task_id = _create_task(runner, root)

        self._run_failing_resolution(runner, root, task_id, "--triggered-by", "ev_fake")

        flag = _snapshot(root, task_id).get("needs_human")
        assert flag, "auto-fired resolution failure left no needs_human flag"
        assert "unreviewed" in flag["reason"]
        assert "Review FAILED" in self._review_status(runner, root, task_id).output

    def test_dry_run_resolution_failure_writes_nothing(self, tmp_path):
        """A dry run claims nothing, so it must not leave a failure record either."""
        root = _make_board(tmp_path, {"review_mode": "single"})
        runner = CliRunner()
        task_id = _create_task(runner, root)

        result = self._run_failing_resolution(runner, root, task_id, "--dry-run")

        assert result.exit_code != 0
        assert not any("Automated review failed" in b for b in _comment_bodies(root, task_id))
        assert "No in-flight review found" in self._review_status(runner, root, task_id).output

    def test_unknown_head_sha_fails_before_writing_an_artifact(self, tmp_path):
        """An empty Lattice-Reviewed-Commit silently re-vacuates the completion gate."""
        root = _make_board(tmp_path, {"review_mode": "single"})
        runner = CliRunner()
        task_id = _create_task(runner, root)

        with (
            patch(
                "lattice.cli.review_cmds.resolve_diff",
                return_value=_resolution(head_sha=None),
            ),
            patch("lattice.cli.review_cmds.run_single_review") as run_single,
        ):
            result = runner.invoke(
                cli,
                ["code-review", task_id, "--mode", "single", "--actor", "agent:test"],
                env={"LATTICE_ROOT": str(root)},
                catch_exceptions=False,
            )

        assert result.exit_code != 0, result.output
        assert "head SHA is unknown" in result.output
        run_single.assert_not_called()
        assert "Review FAILED" in self._review_status(runner, root, task_id).output

    def test_evidence_header_refuses_an_unknown_head_sha(self, tmp_path):
        import pytest

        from lattice.cli.review_cmds import _evidence_header

        with pytest.raises(ValueError, match="head SHA is unknown"):
            _evidence_header(_resolution(head_sha=None))

    def test_stored_artifact_header_carries_the_resolved_head(self, tmp_path):
        """The plan's criterion: prompt and stored artifact carry the same values."""
        root = _make_board(tmp_path, {"review_mode": "single"})
        runner = CliRunner()
        task_id = _create_task(runner, root)

        with (
            patch("lattice.cli.review_cmds.resolve_diff", return_value=_resolution()),
            patch(
                "lattice.cli.review_cmds.run_single_review",
                return_value=(True, "Review complete.", "### 1. Verdict\n**PASS**"),
            ),
        ):
            result = runner.invoke(
                cli,
                ["code-review", task_id, "--mode", "single", "--actor", "agent:test"],
                env={"LATTICE_ROOT": str(root)},
                catch_exceptions=False,
            )
        assert result.exit_code == 0, result.output

        payloads = list((root / LATTICE_DIR / "artifacts" / "payload").glob("*.md"))
        assert payloads, "no review artifact stored"
        stored = payloads[0].read_text(encoding="utf-8")
        assert stored.startswith(f"Lattice-Reviewed-Commit: {'b' * 40}\n")
        assert f"Lattice-Reviewed-Head: feat/branch ({'b' * 40})" in stored
        assert f"Lattice-Reviewed-Base: origin/main ({'a' * 40})" in stored

    def test_failure_json_mode_is_an_error_envelope(self, tmp_path):
        root = _make_board(tmp_path, {"review_mode": "single"})
        runner = CliRunner()
        task_id = _create_task(runner, root)

        result = _run_failing_code_review(runner, root, task_id, "--json")

        assert result.exit_code != 0
        payload = json.loads(result.stdout)
        assert payload["ok"] is False
        assert payload["error"]["code"] == "REVIEW_FAILED"


class TestAbandonedReviewIsVisible:
    def _seed(self, root: Path, task_id: str, pid: int) -> None:
        from lattice.core.review import write_review_state

        write_review_state(
            root / LATTICE_DIR,
            {
                "task_id": task_id,
                "mode": "single",
                "review_type": "code-review",
                "started_at": "2026-05-06T00:00:00Z",
                "started_by_pid": pid,
                "auto_fired": True,
                "agents": [
                    {"name": "claude", "status": "running", "started_at": "2026-05-06T00:00:00Z"}
                ],
            },
        )

    def test_dead_holder_reports_abandoned(self, tmp_path):
        root = _make_board(tmp_path)
        runner = CliRunner()
        task_id = _create_task(runner, root)
        self._seed(root, task_id, DEAD_PID)

        result = runner.invoke(
            cli,
            ["review-status", task_id],
            env={"LATTICE_ROOT": str(root)},
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        assert "abandoned" in result.output
        assert "running" not in result.output

    def test_live_holder_still_reports_running(self, tmp_path):
        """Positive pair: the same seed with a live pid is still in flight."""
        import os

        root = _make_board(tmp_path)
        runner = CliRunner()
        task_id = _create_task(runner, root)
        self._seed(root, task_id, os.getpid())

        result = runner.invoke(
            cli,
            ["review-status", task_id],
            env={"LATTICE_ROOT": str(root)},
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        assert "running" in result.output
        assert "abandoned" not in result.output

    def test_dead_holder_reports_abandoned_json(self, tmp_path):
        root = _make_board(tmp_path)
        runner = CliRunner()
        task_id = _create_task(runner, root)
        self._seed(root, task_id, DEAD_PID)

        result = runner.invoke(
            cli,
            ["review-status", task_id, "--json"],
            env={"LATTICE_ROOT": str(root)},
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        assert json.loads(result.output)["data"]["status"] == "abandoned"


class TestAutoFiredProvenanceSurvivesTheHandoff:
    def test_triggered_by_child_claims_as_auto_fired(self, tmp_path):
        """The detached child usually outlives its parent, so adoption can't carry this."""
        from lattice.core.review import read_review_state

        root = _make_board(tmp_path, {"review_mode": "single"})
        runner = CliRunner()
        task_id = _create_task(runner, root)

        with (
            patch(
                "lattice.cli.review_cmds.resolve_diff",
                return_value=_resolution("diff --git a/x.py b/x.py\n"),
            ),
            patch("lattice.cli.review_cmds.run_single_review", return_value=(True, "ok", "PASS")),
            patch("lattice.cli.review_cmds._attach_review_artifact", return_value="art_fake"),
        ):
            runner.invoke(
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

        # run_single_review is mocked, so the claim record it would have
        # rewritten (and cleared) is still exactly as the CLI body wrote it.
        state = read_review_state(root / LATTICE_DIR, task_id)
        assert state is not None
        assert state["auto_fired"] is True

    def test_manual_child_claims_as_not_auto_fired(self, tmp_path):
        from lattice.core.review import read_review_state

        root = _make_board(tmp_path, {"review_mode": "single"})
        runner = CliRunner()
        task_id = _create_task(runner, root)

        with (
            patch(
                "lattice.cli.review_cmds.resolve_diff",
                return_value=_resolution("diff --git a/x.py b/x.py\n"),
            ),
            patch("lattice.cli.review_cmds.run_single_review", return_value=(True, "ok", "PASS")),
            patch("lattice.cli.review_cmds._attach_review_artifact", return_value="art_fake"),
        ):
            runner.invoke(
                cli,
                ["code-review", task_id, "--mode", "single", "--actor", "agent:test"],
                env={"LATTICE_ROOT": str(root)},
                catch_exceptions=False,
            )

        state = read_review_state(root / LATTICE_DIR, task_id)
        assert state is not None
        assert state["auto_fired"] is False


class TestDiffCharCap:
    def test_prompt_is_bounded_by_char_cap(self, tmp_path):
        """5000 lines of a wide diff is still a quarter-million-character prompt."""
        root = _make_board(tmp_path, {"review_mode": "single", "review_max_diff_chars": 40_000})
        runner = CliRunner()
        task_id = _create_task(runner, root)

        wide_diff = "\n".join(f"+{'x' * 500}" for _ in range(200))  # 100k chars, 200 lines
        with (
            patch("lattice.cli.review_cmds.resolve_diff", return_value=_resolution(wide_diff)),
            patch(
                "lattice.cli.review_cmds.run_single_review", return_value=(True, "ok", "PASS")
            ) as run_single,
            patch("lattice.cli.review_cmds._attach_review_artifact", return_value="art_fake"),
        ):
            result = runner.invoke(
                cli,
                ["code-review", task_id, "--mode", "single", "--actor", "agent:test"],
                env={"LATTICE_ROOT": str(root)},
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        prompt = run_single.call_args.kwargs["prompt_content"]
        # The line cap (5000) never fires here — only the char cap can bound this.
        assert len(prompt) < 45_000, f"prompt was {len(prompt)} chars"
        assert "diff truncated by Lattice" in prompt

    def test_small_diff_is_not_truncated(self, tmp_path):
        root = _make_board(tmp_path, {"review_mode": "single"})
        runner = CliRunner()
        task_id = _create_task(runner, root)

        diff = "diff --git a/foo.py b/foo.py\n+print('hello')"
        with (
            patch("lattice.cli.review_cmds.resolve_diff", return_value=_resolution(diff)),
            patch(
                "lattice.cli.review_cmds.run_single_review", return_value=(True, "ok", "PASS")
            ) as run_single,
            patch("lattice.cli.review_cmds._attach_review_artifact", return_value="art_fake"),
        ):
            runner.invoke(
                cli,
                ["code-review", task_id, "--mode", "single", "--actor", "agent:test"],
                env={"LATTICE_ROOT": str(root)},
                catch_exceptions=False,
            )

        prompt = run_single.call_args.kwargs["prompt_content"]
        assert "diff truncated by Lattice" not in prompt
        assert "print('hello')" in prompt

    def test_below_three_x_threshold_truncates_and_preserves_marker(self, tmp_path):
        from lattice.core.review import read_review_state

        root = _make_board(
            tmp_path,
            {
                "review_mode": "single",
                "review_base_branch": "v2",
                "review_max_diff_lines": 2,
                "review_max_diff_chars": 0,
            },
        )
        runner = CliRunner()
        task_id = _create_task(runner, root)
        diff = "\n".join(f"+line {index}" for index in range(6))
        stored: list[str] = []

        with (
            patch(
                "lattice.cli.review_cmds.resolve_diff",
                return_value=_resolution(
                    diff,
                    base_ref="origin/v2",
                    base_selection_rule="board_config",
                ),
            ) as resolve,
            patch(
                "lattice.cli.review_cmds.run_single_review", return_value=(True, "ok", "PASS")
            ) as run_single,
            patch(
                "lattice.cli.review_cmds._attach_review_artifact",
                side_effect=lambda **kwargs: stored.append(kwargs["content"]) or "art_fake",
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
                    "evt_auto",
                ],
                env={"LATTICE_ROOT": str(root)},
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        assert resolve.call_args.kwargs["review_base_branch"] == "v2"
        assert "Review base: origin/v2 (selection rule: board_config)." in result.output
        assert run_single.call_count == 1
        prompt = run_single.call_args.kwargs["prompt_content"]
        assert "diff truncated by Lattice: showing first 2 of 6 lines" in prompt
        assert "Lattice-Reviewed-Base-Selection: board_config" in stored[0]
        assert "Lattice-Reviewed-Diff: raw-lines=6," in stored[0]
        assert "truncated=true" in stored[0]
        state = read_review_state(root / LATTICE_DIR, task_id)
        assert state is not None
        assert state["auto_fired"] is True

    @pytest.mark.parametrize("mode", ["single", "triple"])
    def test_more_than_three_x_cap_fails_before_reviewer_or_artifact(self, tmp_path, mode):
        from lattice.core.review import read_review_state

        root = _make_board(
            tmp_path,
            {
                "review_mode": mode,
                "review_base_branch": "v2",
                "review_max_diff_lines": 2,
                "review_max_diff_chars": 0,
            },
        )
        runner = CliRunner()
        task_id = _create_task(runner, root)
        diff = "\n".join(f"+line {index}" for index in range(7))

        with (
            patch(
                "lattice.cli.review_cmds.resolve_diff",
                return_value=_resolution(
                    diff,
                    base_ref="origin/v2",
                    base_selection_rule="board_config",
                ),
            ),
            patch("lattice.cli.review_cmds.run_single_review") as run_single,
            patch("lattice.core.review.run_triple_review") as run_triple,
            patch("lattice.cli.review_cmds._attach_review_artifact") as attach,
        ):
            result = runner.invoke(
                cli,
                [
                    "code-review",
                    task_id,
                    "--mode",
                    mode,
                    "--actor",
                    "agent:test",
                    "--triggered-by",
                    "evt_auto",
                ],
                env={"LATTICE_ROOT": str(root)},
                catch_exceptions=False,
            )

        assert result.exit_code != 0
        assert "origin/v2" in result.output
        assert "board_config" in result.output
        assert "7 lines and" in result.output
        assert "review_max_diff_lines=2" in result.output
        assert "review_max_diff_chars=0" in result.output
        assert "--base <ref>" in result.output
        run_single.assert_not_called()
        run_triple.assert_not_called()
        attach.assert_not_called()

        state = read_review_state(root / LATTICE_DIR, task_id)
        assert state is not None
        assert state["status"] == "failed"
        assert state["auto_fired"] is True
        assert state["detail"]["error_code"] == "DIFF_TRUNCATION_UNSAFE"
        assert state["detail"]["base_selection_rule"] == "board_config"
        assert state["detail"]["raw_diff_lines"] == 7
        assert not list((root / LATTICE_DIR / "artifacts" / "meta").glob("*.json"))

        status = runner.invoke(
            cli,
            ["review-status", task_id],
            env={"LATTICE_ROOT": str(root)},
            catch_exceptions=False,
        )
        assert status.exit_code == 0
        assert "Refusing to review the resolved diff" in status.output
        assert "review_base_branch" in status.output
        assert any(
            "Refusing to review the resolved diff" in body
            for body in _comment_bodies(root, task_id)
        )
        assert _snapshot(root, task_id).get("needs_human")

    def test_more_than_three_x_character_cap_fails_closed(self, tmp_path):
        from lattice.core.review import read_review_state

        root = _make_board(
            tmp_path,
            {
                "review_mode": "single",
                "review_base_branch": "v2",
                "review_max_diff_lines": 0,
                "review_max_diff_chars": 2,
            },
        )
        runner = CliRunner()
        task_id = _create_task(runner, root)

        with (
            patch(
                "lattice.cli.review_cmds.resolve_diff",
                return_value=_resolution(
                    "+123456",
                    base_ref="origin/v2",
                    base_selection_rule="board_config",
                ),
            ),
            patch("lattice.cli.review_cmds.run_single_review") as run_single,
            patch("lattice.cli.review_cmds._attach_review_artifact") as attach,
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
                    "evt_auto",
                ],
                env={"LATTICE_ROOT": str(root)},
                catch_exceptions=False,
            )

        assert result.exit_code != 0
        assert "origin/v2" in result.output
        assert "board_config" in result.output
        assert "1 lines and 7 characters" in result.output
        assert "review_max_diff_lines=0" in result.output
        assert "review_max_diff_chars=2" in result.output
        run_single.assert_not_called()
        attach.assert_not_called()
        state = read_review_state(root / LATTICE_DIR, task_id)
        assert state is not None
        assert state["detail"]["raw_diff_chars"] == 7
        assert state["detail"]["review_max_diff_chars"] == 2
        assert not list((root / LATTICE_DIR / "artifacts" / "meta").glob("*.json"))


class TestFailureReportNamesTheRightCommand:
    def test_plan_review_failure_suggests_plan_review_rerun(self, tmp_path):
        from lattice.core.review import write_review_state

        root = _make_board(tmp_path)
        runner = CliRunner()
        task_id = _create_task(runner, root)
        write_review_state(
            root / LATTICE_DIR,
            {
                "task_id": task_id,
                "mode": "single",
                "review_type": "plan-review",
                "started_at": "2026-05-06T00:00:00Z",
                "status": "failed",
                "error": "timed out after 600s",
                "agents": [],
            },
        )

        result = runner.invoke(
            cli,
            ["review-status", task_id],
            env={"LATTICE_ROOT": str(root)},
            catch_exceptions=False,
        )

        assert f"lattice plan-review {task_id}" in result.output
        assert "lattice code-review" not in result.output


# ---------------------------------------------------------------------------
# Tests: evidence headers and --dry-run against a real git topology (LAT-271)
# ---------------------------------------------------------------------------


class TestReviewEvidenceHeaders:
    """The headers must describe the tree that was diffed, not the caller's cwd.

    Sixteen review artifacts on one production board carried the identical
    ``Lattice-Reviewed-Commit`` — the board checkout's HEAD — including reviews
    run with an explicit ``--base``/``--head``. That also defeats the
    ``require_reachable_review_commit`` gate, since a stale local ``main`` is an
    ancestor of every branch cut from it.
    """

    def _board(self, worktree_repo, **config):
        runner = CliRunner()
        root = _make_board(worktree_repo.main, config or None)
        task_id = _create_task(runner, root)
        _write_plan(root, task_id, "# Plan\n\nApproach: implement.\n")
        result = runner.invoke(
            cli,
            ["branch-link", task_id, worktree_repo.branch, "--actor", "agent:test"],
            env={"LATTICE_ROOT": str(root)},
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        return runner, root, task_id

    def _dry_run(self, runner, root, task_id, worktree_repo, *extra):
        return runner.invoke(
            cli,
            [
                "code-review",
                task_id,
                "--dry-run",
                "--worktree",
                str(worktree_repo.main),
                *extra,
            ],
            env={"LATTICE_ROOT": str(root)},
            catch_exceptions=False,
        )

    def test_reviewed_commit_header_is_the_diffed_head(self, worktree_repo):
        from tests.conftest import git

        runner, root, task_id = self._board(worktree_repo)
        result = self._dry_run(runner, root, task_id, worktree_repo, "--json")
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        branch_tip = git(worktree_repo.main, "rev-parse", worktree_repo.branch).strip()
        checkout_head = git(worktree_repo.main, "rev-parse", "HEAD").strip()
        assert branch_tip != checkout_head  # the trap this test exists for
        first_line = data["prompt"].splitlines()[0]
        assert first_line == f"Lattice-Reviewed-Commit: {branch_tip}"
        assert data["head_sha"] == branch_tip

    def test_reviewed_base_and_head_headers_present(self, worktree_repo):
        import re

        from lattice.core.config import _REVIEW_MARKER

        runner, root, task_id = self._board(worktree_repo)
        result = self._dry_run(runner, root, task_id, worktree_repo, "--json")
        prompt = json.loads(result.output)["data"]["prompt"]
        assert _REVIEW_MARKER.match(prompt) is not None
        assert re.search(r"^Lattice-Reviewed-Base: origin/main \([0-9a-f]{40}\)$", prompt, re.M)
        assert re.search(
            rf"^Lattice-Reviewed-Head: {re.escape(worktree_repo.branch)} \([0-9a-f]{{40}}\)$",
            prompt,
            re.M,
        )
        assert f"Lattice-Reviewed-Worktree: {worktree_repo.main}" in prompt

    def test_reachable_review_commit_gate_still_matches(self, worktree_repo):
        """The generated header must still satisfy the completion gate that
        parses it — verified, not assumed."""
        from lattice.core.config import _has_reachable_review_commit

        runner, root, task_id = self._board(worktree_repo)
        result = self._dry_run(runner, root, task_id, worktree_repo, "--json")
        prompt = json.loads(result.output)["data"]["prompt"]
        snapshot = {"branch_links": [{"branch": worktree_repo.branch}], "evidence_refs": []}
        assert (
            _has_reachable_review_commit(
                snapshot, root / LATTICE_DIR, worktree_repo.main, [prompt]
            )
            is True
        )

    def test_dry_run_does_not_claim_or_spawn(self, worktree_repo):
        from lattice.core.review import read_review_state

        runner, root, task_id = self._board(worktree_repo)
        with patch("lattice.cli.review_cmds.run_single_review") as run_single:
            result = self._dry_run(runner, root, task_id, worktree_repo)
        assert result.exit_code == 0, result.output
        assert run_single.call_count == 0
        assert read_review_state(root / LATTICE_DIR, task_id) is None
        assert not list((root / LATTICE_DIR / "artifacts" / "meta").glob("*.json"))
        assert "--- prompt ---" in result.output
        assert f"head:     {worktree_repo.branch}" in result.output

    def test_dry_run_json_shape(self, worktree_repo):
        runner, root, task_id = self._board(worktree_repo)
        result = self._dry_run(runner, root, task_id, worktree_repo, "--json")
        payload = json.loads(result.output)
        assert payload["ok"] is True
        data = payload["data"]
        assert set(data) >= {
            "base_ref",
            "head_ref",
            "base_sha",
            "head_sha",
            "worktree",
            "source",
            "diff_lines",
            "diff_chars",
            "truncated",
            "prompt",
        }
        assert data["base_ref"] == "origin/main"
        assert data["base_selection_rule"] == "inferred_nearest_merge_base"
        assert data["head_ref"] == worktree_repo.branch
        assert data["source"] == "linked_branch"
        assert data["truncated"] is False

    def test_dry_run_unresolvable_branch_fails_loudly(self, worktree_repo):
        runner, root, task_id = self._board(worktree_repo)
        result = runner.invoke(
            cli,
            [
                "code-review",
                task_id,
                "--dry-run",
                "--json",
                "--worktree",
                str(worktree_repo.main),
                "--head",
                "feat/does-not-exist",
            ],
            env={"LATTICE_ROOT": str(root)},
            catch_exceptions=False,
        )
        assert result.exit_code != 0
        assert "feat/does-not-exist" in result.output

    def test_truncation_note_names_the_range(self, worktree_repo):
        runner, root, task_id = self._board(worktree_repo, review_max_diff_lines=3)
        # --quiet: the truncation note goes to stderr, which CliRunner merges
        # into stdout and would otherwise break the JSON parse.
        result = self._dry_run(runner, root, task_id, worktree_repo, "--json", "--quiet")
        data = json.loads(result.output)["data"]
        assert data["truncated"] is True
        assert f"range: origin/main...{worktree_repo.branch}" in data["prompt"]


class TestIdentityArgs:
    """Nested lattice subprocesses must pass a session as --name, not --actor."""

    def test_legacy_actor_string_uses_actor_flag(self):
        from lattice.cli.review_cmds import _identity_args

        assert _identity_args("agent:test") == ["--actor", "agent:test"]

    def test_session_dict_uses_name_flag(self):
        from lattice.cli.review_cmds import _identity_args

        actor = {"name": "C11-238-Delegator-1", "base_name": "C11-238-Delegator", "serial": 1}
        assert _identity_args(actor) == ["--name", "C11-238-Delegator-1"]

    def test_empty_dict_is_none(self):
        from lattice.cli.review_cmds import _identity_args

        assert _identity_args({}) is None
