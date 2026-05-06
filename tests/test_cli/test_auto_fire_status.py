"""Tests for auto-fire wiring in ``lattice status`` (LAT-211).

``subprocess.Popen`` is patched everywhere so no actual review subprocess
is spawned.  Each test enables auto-fire explicitly via ``config_overrides``
because the conftest disables it by default.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from lattice.cli import auto_review as cli_auto_review
from lattice.cli.main import cli
from lattice.core.auto_review import AUTO_REVIEW_ACTOR
from lattice.core.config import default_config, serialize_config
from lattice.core.review import write_review_state
from lattice.storage.fs import LATTICE_DIR, atomic_write, ensure_lattice_dirs
from lattice.storage.readers import read_task_events


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_board(tmp_path: Path, **overrides: object) -> Path:
    ensure_lattice_dirs(tmp_path)
    lattice_dir = tmp_path / LATTICE_DIR
    config = default_config()
    # Auto-fire is on by default in production; explicit per-test overrides
    # let us pin a known config without inheriting the conftest's
    # disabled-for-tests defaults.
    config["auto_code_review_on_transition"] = True
    config["auto_plan_review_on_transition"] = True
    for k, v in overrides.items():
        config[k] = v
    atomic_write(lattice_dir / "config.json", serialize_config(config))
    (lattice_dir / "events" / "_lifecycle.jsonl").touch()
    return tmp_path


def _create_task(runner: CliRunner, root: Path, title: str = "Test") -> str:
    res = runner.invoke(
        cli,
        ["create", title, "--actor", "agent:test", "--quiet"],
        env={"LATTICE_ROOT": str(root)},
        catch_exceptions=False,
    )
    assert res.exit_code == 0, res.output
    return res.output.strip()


def _fill_plan(root: Path, task_id: str, title: str = "Test") -> None:
    plan_path = root / LATTICE_DIR / "plans" / f"{task_id}.md"
    plan_path.write_text(f"# {title}\n\n## Approach\n\n- Implement the feature.\n")


def _walk_to_in_progress(runner: CliRunner, root: Path, task_id: str) -> None:
    runner.invoke(
        cli,
        ["status", task_id, "in_planning", "--actor", "agent:test"],
        env={"LATTICE_ROOT": str(root)},
        catch_exceptions=False,
    )
    _fill_plan(root, task_id)
    runner.invoke(
        cli,
        ["status", task_id, "planned", "--actor", "agent:test", "--no-auto-review"],
        env={"LATTICE_ROOT": str(root)},
        catch_exceptions=False,
    )
    runner.invoke(
        cli,
        ["status", task_id, "in_progress", "--actor", "agent:test"],
        env={"LATTICE_ROOT": str(root)},
        catch_exceptions=False,
    )


class _FakeProc:
    def __init__(self, pid: int = 12345) -> None:
        self.pid = pid


def _patch_executable(path: str = "/usr/local/bin/lattice"):
    return patch.object(cli_auto_review, "find_lattice_executable", return_value=path)


# ---------------------------------------------------------------------------
# Auto-fire success paths
# ---------------------------------------------------------------------------


class TestAutoFireSuccessPaths:
    def test_status_to_review_spawns_code_review(self, tmp_path: Path) -> None:
        root = _make_board(tmp_path)
        runner = CliRunner()
        task_id = _create_task(runner, root)
        _walk_to_in_progress(runner, root, task_id)

        with (
            patch.object(
                cli_auto_review.subprocess, "Popen", return_value=_FakeProc(54321)
            ) as popen,
            _patch_executable(),
        ):
            res = runner.invoke(
                cli,
                ["status", task_id, "review", "--actor", "agent:test"],
                env={"LATTICE_ROOT": str(root)},
                catch_exceptions=False,
            )
        assert res.exit_code == 0
        assert "Auto-firing code-review" in res.output
        popen.assert_called_once()
        argv = popen.call_args.args[0]
        # argv ends with the auto-actor + --triggered-by linking back to the
        # status_changed event.
        assert argv[1] == "code-review"
        assert argv[2] == task_id
        assert argv[3:5] == ["--actor", AUTO_REVIEW_ACTOR]
        assert argv[5] == "--triggered-by"
        # event id is a non-empty ULID-shaped string
        assert argv[6].startswith("ev_")

    def test_status_to_planned_spawns_plan_review(self, tmp_path: Path) -> None:
        root = _make_board(tmp_path, plan_review_mode="triple")
        runner = CliRunner()
        task_id = _create_task(runner, root)
        runner.invoke(
            cli,
            ["status", task_id, "in_planning", "--actor", "agent:test"],
            env={"LATTICE_ROOT": str(root)},
            catch_exceptions=False,
        )
        _fill_plan(root, task_id)

        with (
            patch.object(
                cli_auto_review.subprocess, "Popen", return_value=_FakeProc(7777)
            ) as popen,
            _patch_executable(),
        ):
            res = runner.invoke(
                cli,
                ["status", task_id, "planned", "--actor", "agent:test"],
                env={"LATTICE_ROOT": str(root)},
                catch_exceptions=False,
            )
        assert res.exit_code == 0
        assert "Auto-firing plan-review" in res.output
        popen.assert_called_once()
        argv = popen.call_args.args[0]
        assert argv[1] == "plan-review"


# ---------------------------------------------------------------------------
# Auto-fire skip paths
# ---------------------------------------------------------------------------


class TestAutoFireSkipPaths:
    def test_in_progress_does_not_spawn(self, tmp_path: Path) -> None:
        root = _make_board(tmp_path)
        runner = CliRunner()
        task_id = _create_task(runner, root)
        runner.invoke(
            cli,
            ["status", task_id, "in_planning", "--actor", "agent:test"],
            env={"LATTICE_ROOT": str(root)},
            catch_exceptions=False,
        )
        _fill_plan(root, task_id)
        runner.invoke(
            cli,
            ["status", task_id, "planned", "--actor", "agent:test", "--no-auto-review"],
            env={"LATTICE_ROOT": str(root)},
            catch_exceptions=False,
        )

        with (
            patch.object(cli_auto_review.subprocess, "Popen") as popen,
            _patch_executable(),
        ):
            res = runner.invoke(
                cli,
                ["status", task_id, "in_progress", "--actor", "agent:test"],
                env={"LATTICE_ROOT": str(root)},
                catch_exceptions=False,
            )
        assert res.exit_code == 0
        popen.assert_not_called()

    def test_no_auto_review_flag_skips_spawn(self, tmp_path: Path) -> None:
        root = _make_board(tmp_path)
        runner = CliRunner()
        task_id = _create_task(runner, root)
        _walk_to_in_progress(runner, root, task_id)

        with (
            patch.object(cli_auto_review.subprocess, "Popen") as popen,
            _patch_executable(),
        ):
            res = runner.invoke(
                cli,
                ["status", task_id, "review", "--actor", "agent:test", "--no-auto-review"],
                env={"LATTICE_ROOT": str(root)},
                catch_exceptions=False,
            )
        assert res.exit_code == 0
        popen.assert_not_called()
        assert "auto-review skipped (--no-auto-review)" in res.output

    def test_disabled_in_config_skips_spawn(self, tmp_path: Path) -> None:
        root = _make_board(tmp_path, auto_code_review_on_transition=False)
        runner = CliRunner()
        task_id = _create_task(runner, root)
        _walk_to_in_progress(runner, root, task_id)

        with (
            patch.object(cli_auto_review.subprocess, "Popen") as popen,
            _patch_executable(),
        ):
            res = runner.invoke(
                cli,
                ["status", task_id, "review", "--actor", "agent:test"],
                env={"LATTICE_ROOT": str(root)},
                catch_exceptions=False,
            )
        assert res.exit_code == 0
        popen.assert_not_called()
        assert "auto-review skipped (disabled in config)" in res.output

    def test_inline_review_mode_skips_spawn(self, tmp_path: Path) -> None:
        root = _make_board(tmp_path, review_mode="inline")
        runner = CliRunner()
        task_id = _create_task(runner, root)
        _walk_to_in_progress(runner, root, task_id)

        with (
            patch.object(cli_auto_review.subprocess, "Popen") as popen,
            _patch_executable(),
        ):
            res = runner.invoke(
                cli,
                ["status", task_id, "review", "--actor", "agent:test"],
                env={"LATTICE_ROOT": str(root)},
                catch_exceptions=False,
            )
        assert res.exit_code == 0
        popen.assert_not_called()


# ---------------------------------------------------------------------------
# Failure resilience + audit-trail event
# ---------------------------------------------------------------------------


class TestAutoFireResilience:
    def test_spawn_failure_does_not_fail_transition(self, tmp_path: Path) -> None:
        root = _make_board(tmp_path)
        runner = CliRunner()
        task_id = _create_task(runner, root)
        _walk_to_in_progress(runner, root, task_id)

        with (
            _patch_executable(),
            patch.object(
                cli_auto_review.subprocess,
                "Popen",
                side_effect=OSError("no fork"),
            ),
        ):
            res = runner.invoke(
                cli,
                ["status", task_id, "review", "--actor", "agent:test"],
                env={"LATTICE_ROOT": str(root)},
                catch_exceptions=False,
            )
        # Status transition still succeeds.
        assert res.exit_code == 0
        # And the snapshot is at the new status.
        snapshot = json.loads((root / LATTICE_DIR / "tasks" / f"{task_id}.json").read_text())
        assert snapshot["status"] == "review"

    def test_review_in_flight_skips_with_holder_pid(self, tmp_path: Path) -> None:
        import os

        ppid = os.getppid()
        if ppid == os.getpid() or ppid <= 1:
            import pytest

            pytest.skip("Need a usable parent pid to seed a live holder.")

        root = _make_board(tmp_path)
        runner = CliRunner()
        task_id = _create_task(runner, root)
        _walk_to_in_progress(runner, root, task_id)

        # Seed a record held by a live other PID.
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

        with (
            patch.object(cli_auto_review.subprocess, "Popen") as popen,
            _patch_executable(),
        ):
            res = runner.invoke(
                cli,
                ["status", task_id, "review", "--actor", "agent:test"],
                env={"LATTICE_ROOT": str(root)},
                catch_exceptions=False,
            )
        assert res.exit_code == 0
        popen.assert_not_called()
        assert "already in flight" in res.output
        assert f"pid {ppid}" in res.output

    def test_auto_review_spawned_event_appended_on_success(self, tmp_path: Path) -> None:
        root = _make_board(tmp_path)
        runner = CliRunner()
        task_id = _create_task(runner, root)
        _walk_to_in_progress(runner, root, task_id)

        with (
            patch.object(cli_auto_review.subprocess, "Popen", return_value=_FakeProc(31337)),
            _patch_executable(),
        ):
            runner.invoke(
                cli,
                ["status", task_id, "review", "--actor", "agent:test"],
                env={"LATTICE_ROOT": str(root)},
                catch_exceptions=False,
            )

        events = read_task_events(root / LATTICE_DIR, task_id)
        spawn_events = [e for e in events if e["type"] == "auto_review_spawned"]
        assert len(spawn_events) == 1
        ev = spawn_events[0]
        assert ev["actor"] == AUTO_REVIEW_ACTOR
        data = ev["data"]
        assert data["review_type"] == "code-review"
        assert data["mode"] == "single"
        assert data["pid"] == 31337
        assert data["log_path"].endswith("/.daemon/auto-code-review-" + task_id + ".log")
        assert "spawned_at" in data
        assert "trigger_status_event_id" in data
        # No ``fired`` field — the event itself signals fired=true.
        assert "fired" not in data
        # No ``lock_path`` field — coordination lives in review_state, not lockfiles.
        assert "lock_path" not in data

    def test_auto_review_spawned_event_not_appended_on_skip(self, tmp_path: Path) -> None:
        root = _make_board(tmp_path)
        runner = CliRunner()
        task_id = _create_task(runner, root)
        _walk_to_in_progress(runner, root, task_id)

        with (
            patch.object(cli_auto_review.subprocess, "Popen") as popen,
            _patch_executable(),
        ):
            runner.invoke(
                cli,
                ["status", task_id, "review", "--actor", "agent:test", "--no-auto-review"],
                env={"LATTICE_ROOT": str(root)},
                catch_exceptions=False,
            )
        popen.assert_not_called()
        events = read_task_events(root / LATTICE_DIR, task_id)
        assert not [e for e in events if e["type"] == "auto_review_spawned"]


# ---------------------------------------------------------------------------
# JSON output surface
# ---------------------------------------------------------------------------


def test_status_json_output_includes_auto_review_block(tmp_path: Path) -> None:
    root = _make_board(tmp_path)
    runner = CliRunner()
    task_id = _create_task(runner, root)
    _walk_to_in_progress(runner, root, task_id)

    with (
        patch.object(cli_auto_review.subprocess, "Popen", return_value=_FakeProc(99)),
        _patch_executable(),
    ):
        res = runner.invoke(
            cli,
            ["status", task_id, "review", "--actor", "agent:test", "--json"],
            env={"LATTICE_ROOT": str(root)},
            catch_exceptions=False,
        )
    assert res.exit_code == 0
    parsed = json.loads(res.output)["data"]
    assert "auto_review" in parsed
    assert parsed["auto_review"]["fired"] is True
    assert parsed["auto_review"]["pid"] == 99


def test_status_json_skip_payload(tmp_path: Path) -> None:
    root = _make_board(tmp_path)
    runner = CliRunner()
    task_id = _create_task(runner, root)
    _walk_to_in_progress(runner, root, task_id)

    res = runner.invoke(
        cli,
        ["status", task_id, "review", "--actor", "agent:test", "--no-auto-review", "--json"],
        env={"LATTICE_ROOT": str(root)},
        catch_exceptions=False,
    )
    assert res.exit_code == 0
    parsed = json.loads(res.output)["data"]
    assert parsed["auto_review"] == {"fired": False, "reason": "no_auto_review_flag"}
