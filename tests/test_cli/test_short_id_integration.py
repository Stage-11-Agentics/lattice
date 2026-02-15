"""Integration tests for short ID feature across CLI commands."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from lattice.cli.main import cli
from lattice.core.config import default_config, serialize_config
from lattice.storage.fs import LATTICE_DIR, atomic_write, ensure_lattice_dirs
from lattice.storage.short_ids import save_id_index


def _init_with_project_code(root: Path, code: str = "LAT") -> Path:
    """Initialize a .lattice directory with a project code."""
    ensure_lattice_dirs(root)
    lattice_dir = root / LATTICE_DIR
    config = dict(default_config())
    config["project_code"] = code
    atomic_write(lattice_dir / "config.json", serialize_config(config))
    save_id_index(lattice_dir, {"schema_version": 1, "next_seq": 1, "map": {}})
    (lattice_dir / "events" / "_lifecycle.jsonl").touch()
    return lattice_dir


class TestCreateWithShortId:
    def test_create_assigns_short_id(self, tmp_path: Path) -> None:
        _init_with_project_code(tmp_path)
        runner = CliRunner()
        env = {"LATTICE_ROOT": str(tmp_path)}
        result = runner.invoke(
            cli, ["create", "My task", "--actor", "human:test", "--json"], env=env
        )
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["ok"] is True
        assert parsed["data"]["short_id"] == "LAT-1"

    def test_create_sequential_ids(self, tmp_path: Path) -> None:
        _init_with_project_code(tmp_path)
        runner = CliRunner()
        env = {"LATTICE_ROOT": str(tmp_path)}

        r1 = runner.invoke(
            cli, ["create", "Task 1", "--actor", "human:test", "--json"], env=env
        )
        r2 = runner.invoke(
            cli, ["create", "Task 2", "--actor", "human:test", "--json"], env=env
        )
        snap1 = json.loads(r1.output)["data"]
        snap2 = json.loads(r2.output)["data"]
        assert snap1["short_id"] == "LAT-1"
        assert snap2["short_id"] == "LAT-2"

    def test_create_quiet_outputs_short_id(self, tmp_path: Path) -> None:
        _init_with_project_code(tmp_path)
        runner = CliRunner()
        env = {"LATTICE_ROOT": str(tmp_path)}
        result = runner.invoke(
            cli, ["create", "My task", "--actor", "human:test", "--quiet"], env=env
        )
        assert result.exit_code == 0
        assert result.output.strip() == "LAT-1"

    def test_create_without_project_code_no_short_id(self, tmp_path: Path) -> None:
        """Without project code, tasks get no short_id."""
        ensure_lattice_dirs(tmp_path)
        lattice_dir = tmp_path / LATTICE_DIR
        atomic_write(lattice_dir / "config.json", serialize_config(default_config()))
        (lattice_dir / "events" / "_lifecycle.jsonl").touch()

        runner = CliRunner()
        env = {"LATTICE_ROOT": str(tmp_path)}
        result = runner.invoke(
            cli, ["create", "My task", "--actor", "human:test", "--json"], env=env
        )
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert "short_id" not in parsed["data"]


class TestCommandsWithShortId:
    """Test that various commands accept short IDs."""

    def _setup(self, tmp_path: Path) -> tuple[CliRunner, dict, str, str]:
        """Create a project with a short-ID task, return (runner, env, ulid, short_id)."""
        _init_with_project_code(tmp_path)
        runner = CliRunner()
        env = {"LATTICE_ROOT": str(tmp_path)}
        result = runner.invoke(
            cli, ["create", "Test task", "--actor", "human:test", "--json"], env=env
        )
        snap = json.loads(result.output)["data"]
        return runner, env, snap["id"], snap["short_id"]

    def test_show_with_short_id(self, tmp_path: Path) -> None:
        runner, env, ulid, short_id = self._setup(tmp_path)
        result = runner.invoke(cli, ["show", short_id, "--json"], env=env)
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["ok"] is True
        assert parsed["data"]["id"] == ulid

    def test_status_with_short_id(self, tmp_path: Path) -> None:
        runner, env, ulid, short_id = self._setup(tmp_path)
        result = runner.invoke(
            cli,
            ["status", short_id, "in_planning", "--actor", "human:test"],
            env=env,
        )
        assert result.exit_code == 0

    def test_update_with_short_id(self, tmp_path: Path) -> None:
        runner, env, ulid, short_id = self._setup(tmp_path)
        result = runner.invoke(
            cli,
            ["update", short_id, "title=New title", "--actor", "human:test"],
            env=env,
        )
        assert result.exit_code == 0

    def test_assign_with_short_id(self, tmp_path: Path) -> None:
        runner, env, ulid, short_id = self._setup(tmp_path)
        result = runner.invoke(
            cli,
            ["assign", short_id, "human:alice", "--actor", "human:test"],
            env=env,
        )
        assert result.exit_code == 0

    def test_comment_with_short_id(self, tmp_path: Path) -> None:
        runner, env, ulid, short_id = self._setup(tmp_path)
        result = runner.invoke(
            cli,
            ["comment", short_id, "A comment", "--actor", "human:test"],
            env=env,
        )
        assert result.exit_code == 0

    def test_archive_with_short_id(self, tmp_path: Path) -> None:
        runner, env, ulid, short_id = self._setup(tmp_path)
        result = runner.invoke(
            cli,
            ["archive", short_id, "--actor", "human:test"],
            env=env,
        )
        assert result.exit_code == 0

    def test_case_insensitive_resolution(self, tmp_path: Path) -> None:
        runner, env, ulid, short_id = self._setup(tmp_path)
        lower = short_id.lower()
        result = runner.invoke(cli, ["show", lower, "--json"], env=env)
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["data"]["id"] == ulid


class TestListWithShortIds:
    def test_list_shows_short_id(self, tmp_path: Path) -> None:
        _init_with_project_code(tmp_path)
        runner = CliRunner()
        env = {"LATTICE_ROOT": str(tmp_path)}
        runner.invoke(
            cli, ["create", "My task", "--actor", "human:test"], env=env
        )
        result = runner.invoke(cli, ["list"], env=env)
        assert result.exit_code == 0
        assert "LAT-1" in result.output

    def test_list_quiet_shows_short_id(self, tmp_path: Path) -> None:
        _init_with_project_code(tmp_path)
        runner = CliRunner()
        env = {"LATTICE_ROOT": str(tmp_path)}
        runner.invoke(
            cli, ["create", "My task", "--actor", "human:test"], env=env
        )
        result = runner.invoke(cli, ["list", "--quiet"], env=env)
        assert result.exit_code == 0
        assert result.output.strip() == "LAT-1"

    def test_list_json_includes_short_id(self, tmp_path: Path) -> None:
        _init_with_project_code(tmp_path)
        runner = CliRunner()
        env = {"LATTICE_ROOT": str(tmp_path)}
        runner.invoke(
            cli, ["create", "My task", "--actor", "human:test"], env=env
        )
        result = runner.invoke(cli, ["list", "--json", "--compact"], env=env)
        parsed = json.loads(result.output)
        assert parsed["data"][0]["short_id"] == "LAT-1"


class TestBackfillIds:
    def test_backfill_assigns_to_existing_tasks(self, tmp_path: Path) -> None:
        """Create tasks without project code, then backfill."""
        ensure_lattice_dirs(tmp_path)
        lattice_dir = tmp_path / LATTICE_DIR
        atomic_write(lattice_dir / "config.json", serialize_config(default_config()))
        (lattice_dir / "events" / "_lifecycle.jsonl").touch()

        runner = CliRunner()
        env = {"LATTICE_ROOT": str(tmp_path)}

        # Create 3 tasks without short IDs
        for i in range(3):
            runner.invoke(
                cli, ["create", f"Task {i}", "--actor", "human:test"], env=env
            )

        # Backfill with project code
        result = runner.invoke(
            cli, ["backfill-ids", "--code", "TST", "--json"], env=env
        )
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["ok"] is True
        assert parsed["data"]["assigned"] == 3
        assert parsed["data"]["first"] == "TST-1"
        assert parsed["data"]["last"] == "TST-3"

        # Verify tasks now have short IDs
        result = runner.invoke(
            cli, ["list", "--json", "--compact"], env=env
        )
        parsed = json.loads(result.output)
        short_ids = sorted(t["short_id"] for t in parsed["data"])
        assert short_ids == ["TST-1", "TST-2", "TST-3"]

    def test_backfill_idempotent(self, tmp_path: Path) -> None:
        """Running backfill twice is safe."""
        ensure_lattice_dirs(tmp_path)
        lattice_dir = tmp_path / LATTICE_DIR
        atomic_write(lattice_dir / "config.json", serialize_config(default_config()))
        (lattice_dir / "events" / "_lifecycle.jsonl").touch()

        runner = CliRunner()
        env = {"LATTICE_ROOT": str(tmp_path)}

        runner.invoke(
            cli, ["create", "Task A", "--actor", "human:test"], env=env
        )

        # First backfill
        runner.invoke(
            cli, ["backfill-ids", "--code", "TST"], env=env
        )

        # Second backfill — should report 0 assigned
        result = runner.invoke(
            cli, ["backfill-ids", "--json"], env=env
        )
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["data"]["assigned"] == 0

    def test_backfill_no_project_code_errors(self, tmp_path: Path) -> None:
        ensure_lattice_dirs(tmp_path)
        lattice_dir = tmp_path / LATTICE_DIR
        atomic_write(lattice_dir / "config.json", serialize_config(default_config()))
        (lattice_dir / "events" / "_lifecycle.jsonl").touch()

        runner = CliRunner()
        env = {"LATTICE_ROOT": str(tmp_path)}
        result = runner.invoke(cli, ["backfill-ids"], env=env)
        assert result.exit_code != 0


class TestSetProjectCode:
    def test_sets_code(self, tmp_path: Path) -> None:
        ensure_lattice_dirs(tmp_path)
        lattice_dir = tmp_path / LATTICE_DIR
        atomic_write(lattice_dir / "config.json", serialize_config(default_config()))
        (lattice_dir / "events" / "_lifecycle.jsonl").touch()

        runner = CliRunner()
        env = {"LATTICE_ROOT": str(tmp_path)}
        result = runner.invoke(cli, ["set-project-code", "PRJ"], env=env)
        assert result.exit_code == 0

        config = json.loads((lattice_dir / "config.json").read_text())
        assert config["project_code"] == "PRJ"

    def test_rejects_change_without_force(self, tmp_path: Path) -> None:
        _init_with_project_code(tmp_path, "OLD")
        runner = CliRunner()
        env = {"LATTICE_ROOT": str(tmp_path)}
        result = runner.invoke(cli, ["set-project-code", "NEW"], env=env)
        assert result.exit_code != 0

    def test_allows_change_with_force(self, tmp_path: Path) -> None:
        _init_with_project_code(tmp_path, "OLD")
        runner = CliRunner()
        env = {"LATTICE_ROOT": str(tmp_path)}
        result = runner.invoke(cli, ["set-project-code", "NEW", "--force"], env=env)
        assert result.exit_code == 0


class TestRebuildRegeneratesIndex:
    def test_rebuild_all_regenerates_ids_json(self, tmp_path: Path) -> None:
        _init_with_project_code(tmp_path)
        runner = CliRunner()
        env = {"LATTICE_ROOT": str(tmp_path)}

        # Create tasks with short IDs
        runner.invoke(
            cli, ["create", "Task 1", "--actor", "human:test"], env=env
        )
        runner.invoke(
            cli, ["create", "Task 2", "--actor", "human:test"], env=env
        )

        # Delete ids.json
        lattice_dir = tmp_path / LATTICE_DIR
        (lattice_dir / "ids.json").unlink()

        # Rebuild
        result = runner.invoke(cli, ["rebuild", "--all"], env=env)
        assert result.exit_code == 0

        # ids.json should be regenerated
        assert (lattice_dir / "ids.json").exists()
        index = json.loads((lattice_dir / "ids.json").read_text())
        assert "LAT-1" in index["map"]
        assert "LAT-2" in index["map"]
        assert index["next_seq"] == 3


class TestDoctorChecksAliases:
    def test_doctor_passes_clean_project(self, tmp_path: Path) -> None:
        _init_with_project_code(tmp_path)
        runner = CliRunner()
        env = {"LATTICE_ROOT": str(tmp_path)}
        runner.invoke(
            cli, ["create", "Task 1", "--actor", "human:test"], env=env
        )
        result = runner.invoke(cli, ["doctor"], env=env)
        assert result.exit_code == 0
        assert "Short ID aliases consistent" in result.output


class TestProtectedShortIdField:
    def test_cannot_update_short_id(self, tmp_path: Path) -> None:
        _init_with_project_code(tmp_path)
        runner = CliRunner()
        env = {"LATTICE_ROOT": str(tmp_path)}
        r = runner.invoke(
            cli, ["create", "Task", "--actor", "human:test", "--json"], env=env
        )
        short_id = json.loads(r.output)["data"]["short_id"]
        result = runner.invoke(
            cli,
            ["update", short_id, "short_id=HACKED-99", "--actor", "human:test"],
            env=env,
        )
        assert result.exit_code != 0


class TestInitWithProjectCode:
    def test_init_with_project_code_flag(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["init", "--path", str(tmp_path), "--actor", "human:test", "--project-code", "LAT"],
        )
        assert result.exit_code == 0
        assert "Project code: LAT" in result.output

        config = json.loads((tmp_path / ".lattice" / "config.json").read_text())
        assert config["project_code"] == "LAT"

        # ids.json should exist
        assert (tmp_path / ".lattice" / "ids.json").exists()

    def test_init_invalid_project_code(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["init", "--path", str(tmp_path), "--actor", "human:test", "--project-code", "123"],
        )
        assert result.exit_code != 0
        assert "Invalid project code" in result.output
