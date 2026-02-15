"""Tests for the `lattice init` CLI command."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from lattice.cli.main import cli
from lattice.core.config import default_config, serialize_config


class TestInitDirectoryStructure:
    """lattice init creates the full .lattice/ directory tree."""

    def test_creates_all_expected_directories(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["init", "--path", str(tmp_path)], input="\n\n")
        assert result.exit_code == 0

        lattice = tmp_path / ".lattice"
        expected_dirs = [
            "tasks",
            "events",
            "artifacts/meta",
            "artifacts/payload",
            "notes",
            "archive/tasks",
            "archive/events",
            "archive/notes",
            "locks",
        ]
        for d in expected_dirs:
            assert (lattice / d).is_dir(), f"Missing directory: {d}"

    def test_creates_empty_lifecycle_jsonl(self, tmp_path: Path) -> None:
        runner = CliRunner()
        runner.invoke(cli, ["init", "--path", str(tmp_path)], input="\n\n")

        lifecycle_log = tmp_path / ".lattice" / "events" / "_lifecycle.jsonl"
        assert lifecycle_log.is_file()
        assert lifecycle_log.read_text() == ""

    def test_init_with_custom_path(self, tmp_path: Path) -> None:
        target = tmp_path / "myproject"
        target.mkdir()

        runner = CliRunner()
        result = runner.invoke(cli, ["init", "--path", str(target)], input="\n\n")
        assert result.exit_code == 0
        assert (target / ".lattice" / "config.json").is_file()

    def test_prints_success_message(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["init", "--path", str(tmp_path)], input="\n\n")
        assert result.exit_code == 0
        assert "Initialized empty Lattice in .lattice/" in result.output


class TestInitConfig:
    """lattice init writes a valid, deterministic config.json."""

    def test_writes_valid_json(self, tmp_path: Path) -> None:
        runner = CliRunner()
        runner.invoke(cli, ["init", "--path", str(tmp_path)], input="\n\n")

        config_path = tmp_path / ".lattice" / "config.json"
        config = json.loads(config_path.read_text())
        assert isinstance(config, dict)

    def test_config_has_schema_version_1(self, tmp_path: Path) -> None:
        runner = CliRunner()
        runner.invoke(cli, ["init", "--path", str(tmp_path)], input="\n\n")

        config = json.loads((tmp_path / ".lattice" / "config.json").read_text())
        assert config["schema_version"] == 1

    def test_config_is_byte_identical_to_canonical(self, tmp_path: Path) -> None:
        """Config on disk must be byte-identical to json.dumps(default_config(), sort_keys=True, indent=2) + '\\n'."""
        runner = CliRunner()
        runner.invoke(cli, ["init", "--path", str(tmp_path)], input="\n\n")

        actual = (tmp_path / ".lattice" / "config.json").read_text()
        expected = serialize_config(default_config())
        assert actual == expected

    def test_config_has_trailing_newline(self, tmp_path: Path) -> None:
        runner = CliRunner()
        runner.invoke(cli, ["init", "--path", str(tmp_path)], input="\n\n")

        raw = (tmp_path / ".lattice" / "config.json").read_bytes()
        assert raw.endswith(b"\n")
        # Exactly one trailing newline, not two
        assert not raw.endswith(b"\n\n")


class TestInitIdempotency:
    """Running init twice must not clobber existing data."""

    def test_second_init_does_not_clobber_config(self, tmp_path: Path) -> None:
        runner = CliRunner()
        runner.invoke(cli, ["init", "--path", str(tmp_path)], input="\n\n")

        # Record config content after first init
        config_path = tmp_path / ".lattice" / "config.json"
        original = config_path.read_text()

        # Run init again (no input needed — idempotency check returns before prompt)
        result = runner.invoke(cli, ["init", "--path", str(tmp_path)])
        assert result.exit_code == 0
        assert "already initialized" in result.output

        # Config unchanged
        assert config_path.read_text() == original

    def test_modified_config_survives_second_init(self, tmp_path: Path) -> None:
        runner = CliRunner()
        runner.invoke(cli, ["init", "--path", str(tmp_path)], input="\n\n")

        # Modify config between runs
        config_path = tmp_path / ".lattice" / "config.json"
        config = json.loads(config_path.read_text())
        config["custom_key"] = "user_value"
        config_path.write_text(json.dumps(config, sort_keys=True, indent=2) + "\n")

        # Run init again (no input needed — idempotency check returns before prompt)
        runner.invoke(cli, ["init", "--path", str(tmp_path)])

        # Modified config is preserved
        reloaded = json.loads(config_path.read_text())
        assert reloaded["custom_key"] == "user_value"

    def test_second_init_prints_already_initialized(self, tmp_path: Path) -> None:
        runner = CliRunner()
        runner.invoke(cli, ["init", "--path", str(tmp_path)], input="\n\n")

        result = runner.invoke(cli, ["init", "--path", str(tmp_path)])
        assert result.exit_code == 0
        assert "already initialized" in result.output

    def test_existing_tasks_survive_second_init(self, tmp_path: Path) -> None:
        runner = CliRunner()
        runner.invoke(cli, ["init", "--path", str(tmp_path)], input="\n\n")

        # Create a fake task file
        task_file = tmp_path / ".lattice" / "tasks" / "task_fake.json"
        task_file.write_text('{"id": "task_fake"}\n')

        # Run init again (no input needed — idempotency check returns before prompt)
        runner.invoke(cli, ["init", "--path", str(tmp_path)])

        # Task file still exists
        assert task_file.is_file()
        assert json.loads(task_file.read_text())["id"] == "task_fake"


class TestInitActorConfig:
    """lattice init --actor flag and interactive actor prompt."""

    def test_init_with_actor_flag_sets_config_default(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            cli, ["init", "--path", str(tmp_path), "--actor", "human:atin"], input="\n"
        )
        assert result.exit_code == 0

        config = json.loads((tmp_path / ".lattice" / "config.json").read_text())
        assert config["default_actor"] == "human:atin"

    def test_init_prompts_for_actor_when_flag_omitted(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["init", "--path", str(tmp_path)], input="human:atin\n\n")
        assert result.exit_code == 0

        config = json.loads((tmp_path / ".lattice" / "config.json").read_text())
        assert config["default_actor"] == "human:atin"
        assert "Default actor: human:atin" in result.output

    def test_init_empty_actor_input_skips_default(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["init", "--path", str(tmp_path)], input="\n\n")
        assert result.exit_code == 0

        config = json.loads((tmp_path / ".lattice" / "config.json").read_text())
        assert "default_actor" not in config

    def test_init_invalid_actor_format_errors(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["init", "--path", str(tmp_path), "--actor", "badformat"])
        assert result.exit_code != 0
        assert "Invalid actor format" in result.output


class TestInitErrorHandling:
    """lattice init handles filesystem errors gracefully."""

    def test_file_collision_shows_error(self, tmp_path: Path) -> None:
        """If .lattice exists as a file, init fails with a clear message."""
        (tmp_path / ".lattice").write_text("not a directory")

        runner = CliRunner()
        result = runner.invoke(cli, ["init", "--path", str(tmp_path)])
        assert result.exit_code != 0
        assert "not a directory" in result.output

    def test_file_collision_does_not_traceback(self, tmp_path: Path) -> None:
        """File collision produces a Click error, not a Python traceback."""
        (tmp_path / ".lattice").write_text("not a directory")

        runner = CliRunner()
        result = runner.invoke(cli, ["init", "--path", str(tmp_path)])
        assert "Traceback" not in result.output

    def test_permission_error_shows_message(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PermissionError is caught and reported cleanly."""
        from lattice.cli import main as cli_module

        def raise_permission_error(root: Path) -> None:
            raise PermissionError("Operation not permitted")

        monkeypatch.setattr(cli_module, "ensure_lattice_dirs", raise_permission_error)

        runner = CliRunner()
        result = runner.invoke(cli, ["init", "--path", str(tmp_path)], input="\n\n")
        assert result.exit_code != 0
        assert "Permission denied" in result.output
        assert "Traceback" not in result.output

    def test_oserror_shows_message(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Generic OSError is caught and reported cleanly."""
        from lattice.cli import main as cli_module

        def raise_os_error(root: Path) -> None:
            raise OSError("No space left on device")

        monkeypatch.setattr(cli_module, "ensure_lattice_dirs", raise_os_error)

        runner = CliRunner()
        result = runner.invoke(cli, ["init", "--path", str(tmp_path)], input="\n\n")
        assert result.exit_code != 0
        assert "Failed to initialize" in result.output
        assert "Traceback" not in result.output
