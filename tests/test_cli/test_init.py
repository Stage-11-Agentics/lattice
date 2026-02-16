"""Tests for the `lattice init` CLI command."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from lattice.cli.main import cli
from lattice.core.config import default_config


class TestInitDirectoryStructure:
    """lattice init creates the full .lattice/ directory tree."""

    def test_creates_all_expected_directories(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["init", "--path", str(tmp_path)], input="\n\nn\n")
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
        runner.invoke(cli, ["init", "--path", str(tmp_path)], input="\n\nn\n")

        lifecycle_log = tmp_path / ".lattice" / "events" / "_lifecycle.jsonl"
        assert lifecycle_log.is_file()
        assert lifecycle_log.read_text() == ""

    def test_init_with_custom_path(self, tmp_path: Path) -> None:
        target = tmp_path / "myproject"
        target.mkdir()

        runner = CliRunner()
        result = runner.invoke(cli, ["init", "--path", str(target)], input="\n\nn\n")
        assert result.exit_code == 0
        assert (target / ".lattice" / "config.json").is_file()

    def test_prints_success_message(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["init", "--path", str(tmp_path)], input="\n\nn\n")
        assert result.exit_code == 0
        assert "Lattice initialized in .lattice/" in result.output


class TestInitConfig:
    """lattice init writes a valid, deterministic config.json."""

    def test_writes_valid_json(self, tmp_path: Path) -> None:
        runner = CliRunner()
        runner.invoke(cli, ["init", "--path", str(tmp_path)], input="\n\nn\n")

        config_path = tmp_path / ".lattice" / "config.json"
        config = json.loads(config_path.read_text())
        assert isinstance(config, dict)

    def test_config_has_schema_version_1(self, tmp_path: Path) -> None:
        runner = CliRunner()
        runner.invoke(cli, ["init", "--path", str(tmp_path)], input="\n\nn\n")

        config = json.loads((tmp_path / ".lattice" / "config.json").read_text())
        assert config["schema_version"] == 1

    def test_config_contains_default_fields(self, tmp_path: Path) -> None:
        """Config on disk must contain all default config fields plus instance_id."""
        runner = CliRunner()
        runner.invoke(cli, ["init", "--path", str(tmp_path)], input="\n\nn\n")

        config = json.loads((tmp_path / ".lattice" / "config.json").read_text())
        expected = default_config()
        for key in expected:
            assert key in config, f"Missing default config key: {key}"
            assert config[key] == expected[key]
        # Init always generates an instance_id
        assert "instance_id" in config
        assert config["instance_id"].startswith("inst_")

    def test_config_has_trailing_newline(self, tmp_path: Path) -> None:
        runner = CliRunner()
        runner.invoke(cli, ["init", "--path", str(tmp_path)], input="\n\nn\n")

        raw = (tmp_path / ".lattice" / "config.json").read_bytes()
        assert raw.endswith(b"\n")
        # Exactly one trailing newline, not two
        assert not raw.endswith(b"\n\n")


class TestInitIdempotency:
    """Running init twice must not clobber existing data."""

    def test_second_init_does_not_clobber_config(self, tmp_path: Path) -> None:
        runner = CliRunner()
        runner.invoke(cli, ["init", "--path", str(tmp_path)], input="\n\nn\n")

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
        runner.invoke(cli, ["init", "--path", str(tmp_path)], input="\n\nn\n")

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
        runner.invoke(cli, ["init", "--path", str(tmp_path)], input="\n\nn\n")

        result = runner.invoke(cli, ["init", "--path", str(tmp_path)])
        assert result.exit_code == 0
        assert "already initialized" in result.output

    def test_existing_tasks_survive_second_init(self, tmp_path: Path) -> None:
        runner = CliRunner()
        runner.invoke(cli, ["init", "--path", str(tmp_path)], input="\n\nn\n")

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
            cli, ["init", "--path", str(tmp_path), "--actor", "human:atin"], input="\nn\n"
        )
        assert result.exit_code == 0

        config = json.loads((tmp_path / ".lattice" / "config.json").read_text())
        assert config["default_actor"] == "human:atin"

    def test_init_prompts_for_actor_when_flag_omitted(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["init", "--path", str(tmp_path)], input="human:atin\n\nn\n")
        assert result.exit_code == 0

        config = json.loads((tmp_path / ".lattice" / "config.json").read_text())
        assert config["default_actor"] == "human:atin"
        assert "Default actor: human:atin" in result.output

    def test_init_empty_actor_input_skips_default(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["init", "--path", str(tmp_path)], input="\n\nn\n")
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


class TestInitInstanceIdentity:
    """lattice init generates instance_id and supports --instance-name."""

    def test_always_generates_instance_id(self, tmp_path: Path) -> None:
        runner = CliRunner()
        runner.invoke(cli, ["init", "--path", str(tmp_path)], input="\n\nn\n")

        config = json.loads((tmp_path / ".lattice" / "config.json").read_text())
        assert "instance_id" in config
        assert config["instance_id"].startswith("inst_")
        assert len(config["instance_id"]) == 31  # "inst_" + 26 char ULID

    def test_instance_name_flag(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["init", "--path", str(tmp_path), "--instance-name", "Frontend"],
            input="\n\nn\n",
        )
        assert result.exit_code == 0
        assert "Instance name: Frontend" in result.output

        config = json.loads((tmp_path / ".lattice" / "config.json").read_text())
        assert config["instance_name"] == "Frontend"

    def test_no_instance_name_by_default(self, tmp_path: Path) -> None:
        runner = CliRunner()
        runner.invoke(cli, ["init", "--path", str(tmp_path)], input="\n\nn\n")

        config = json.loads((tmp_path / ".lattice" / "config.json").read_text())
        assert "instance_name" not in config


class TestInitContextMd:
    """lattice init creates .lattice/context.md template."""

    def test_creates_context_md(self, tmp_path: Path) -> None:
        runner = CliRunner()
        runner.invoke(cli, ["init", "--path", str(tmp_path)], input="\n\nn\n")

        context_path = tmp_path / ".lattice" / "context.md"
        assert context_path.is_file()

    def test_context_md_has_expected_sections(self, tmp_path: Path) -> None:
        runner = CliRunner()
        runner.invoke(cli, ["init", "--path", str(tmp_path)], input="\n\nn\n")

        content = (tmp_path / ".lattice" / "context.md").read_text()
        assert "# Instance Context" in content
        assert "## Purpose" in content
        assert "## Related Instances" in content
        assert "## Conventions" in content


class TestInitSubprojectCode:
    """lattice init --subproject-code flag."""

    def test_init_with_subproject_code(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "init",
                "--path",
                str(tmp_path),
                "--actor",
                "human:test",
                "--project-code",
                "AUT",
                "--subproject-code",
                "F",
            ],
            input="n\n",
        )
        assert result.exit_code == 0
        assert "Project code: AUT" in result.output
        assert "Subproject code: F" in result.output

        config = json.loads((tmp_path / ".lattice" / "config.json").read_text())
        assert config["project_code"] == "AUT"
        assert config["subproject_code"] == "F"

    def test_subproject_without_project_code_errors(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "init",
                "--path",
                str(tmp_path),
                "--actor",
                "human:test",
                "--subproject-code",
                "F",
            ],
            input="\n",
        )
        assert result.exit_code != 0
        assert "Cannot set --subproject-code without --project-code" in result.output

    def test_invalid_subproject_code_errors(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "init",
                "--path",
                str(tmp_path),
                "--actor",
                "human:test",
                "--project-code",
                "AUT",
                "--subproject-code",
                "123",
            ],
        )
        assert result.exit_code != 0
        assert "Invalid subproject code" in result.output


class TestInitClaudeMd:
    """lattice init CLAUDE.md integration."""

    def test_init_offers_claude_md_append(self, tmp_path: Path) -> None:
        """Init with existing CLAUDE.md, accept prompt -> block appended."""
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text("# My Project\n\nExisting content.\n")

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["init", "--path", str(tmp_path), "--actor", "human:test", "--project-code", "TST"],
            input="y\n",  # yes to CLAUDE.md integration
        )
        assert result.exit_code == 0
        assert "Added Lattice integration to CLAUDE.md" in result.output

        content = claude_md.read_text()
        assert "## Lattice" in content
        assert "Existing content" in content  # original content preserved
        assert "The First Act" in content

    def test_init_offers_claude_md_create(self, tmp_path: Path) -> None:
        """Init without CLAUDE.md, accept prompt -> file created."""
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["init", "--path", str(tmp_path), "--actor", "human:test", "--project-code", "TST"],
            input="y\n",  # yes to create CLAUDE.md
        )
        assert result.exit_code == 0
        assert "Created CLAUDE.md with Lattice integration" in result.output

        claude_md = tmp_path / "CLAUDE.md"
        assert claude_md.exists()
        content = claude_md.read_text()
        assert content.startswith(f"# {tmp_path.name}\n")
        assert "## Lattice" in content
        assert "The First Act" in content

    def test_init_claude_md_decline(self, tmp_path: Path) -> None:
        """Decline CLAUDE.md prompt -> file not created."""
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["init", "--path", str(tmp_path), "--actor", "human:test", "--project-code", "TST"],
            input="n\n",  # no to CLAUDE.md
        )
        assert result.exit_code == 0

        claude_md = tmp_path / "CLAUDE.md"
        assert not claude_md.exists()

    def test_init_claude_md_decline_preserves_existing(self, tmp_path: Path) -> None:
        """Decline CLAUDE.md prompt with existing file -> file not modified."""
        claude_md = tmp_path / "CLAUDE.md"
        original_content = "# My Project\n\nExisting content.\n"
        claude_md.write_text(original_content)

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["init", "--path", str(tmp_path), "--actor", "human:test", "--project-code", "TST"],
            input="n\n",  # no to CLAUDE.md
        )
        assert result.exit_code == 0
        assert claude_md.read_text() == original_content

    def test_init_claude_md_already_has_lattice(self, tmp_path: Path) -> None:
        """CLAUDE.md already contains Lattice block -> no duplicate, no prompt."""
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text("# My Project\n\n## Lattice\n\nAlready integrated.\n")

        runner = CliRunner()
        # No input needed — should detect existing block and skip prompt
        result = runner.invoke(
            cli,
            ["init", "--path", str(tmp_path), "--actor", "human:test", "--project-code", "TST"],
        )
        assert result.exit_code == 0
        assert "already has Lattice integration" in result.output

        # Content unchanged (no duplicate block)
        content = claude_md.read_text()
        assert content.count("## Lattice") == 1


class TestSetupClaude:
    """lattice setup-claude standalone command."""

    def test_setup_claude_creates_new(self, tmp_path: Path) -> None:
        """setup-claude with no CLAUDE.md -> creates file."""
        runner = CliRunner()
        result = runner.invoke(cli, ["setup-claude", "--path", str(tmp_path)])
        assert result.exit_code == 0
        assert "Created CLAUDE.md with Lattice integration" in result.output

        claude_md = tmp_path / "CLAUDE.md"
        assert claude_md.exists()
        content = claude_md.read_text()
        assert content.startswith(f"# {tmp_path.name}\n")
        assert "## Lattice" in content
        assert "The First Act" in content

    def test_setup_claude_appends(self, tmp_path: Path) -> None:
        """setup-claude with existing CLAUDE.md without Lattice block -> appends."""
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text("# My Project\n\nExisting content.\n")

        runner = CliRunner()
        result = runner.invoke(cli, ["setup-claude", "--path", str(tmp_path)])
        assert result.exit_code == 0
        assert "Added Lattice integration to CLAUDE.md" in result.output

        content = claude_md.read_text()
        assert "Existing content" in content
        assert "## Lattice" in content
        assert "The First Act" in content

    def test_setup_claude_already_present(self, tmp_path: Path) -> None:
        """setup-claude when block already exists (no --force) -> message, no change."""
        claude_md = tmp_path / "CLAUDE.md"
        original = "# My Project\n\n## Lattice\n\nCustom content.\n"
        claude_md.write_text(original)

        runner = CliRunner()
        result = runner.invoke(cli, ["setup-claude", "--path", str(tmp_path)])
        assert result.exit_code == 0
        assert "already has Lattice integration" in result.output
        assert "Use --force to replace" in result.output

        assert claude_md.read_text() == original

    def test_setup_claude_force_replaces(self, tmp_path: Path) -> None:
        """setup-claude --force replaces existing Lattice block."""
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text("# My Project\n\n## Lattice\n\nOld content.\nMore old content.\n")

        runner = CliRunner()
        result = runner.invoke(cli, ["setup-claude", "--path", str(tmp_path), "--force"])
        assert result.exit_code == 0
        assert "Updated Lattice integration in CLAUDE.md" in result.output

        content = claude_md.read_text()
        assert "# My Project" in content
        assert "Old content" not in content
        assert "The First Act" in content
        # Should have exactly one base Lattice block (plugins may add their own)
        assert content.count("## Lattice\n") == 1

    def test_setup_claude_force_preserves_other_sections(self, tmp_path: Path) -> None:
        """setup-claude --force preserves sections before and after the Lattice block."""
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text(
            "# My Project\n\n## Setup\n\nSetup info.\n\n"
            "## Lattice\n\nOld lattice info.\n\n"
            "## Other Section\n\nOther content.\n"
        )

        runner = CliRunner()
        result = runner.invoke(cli, ["setup-claude", "--path", str(tmp_path), "--force"])
        assert result.exit_code == 0

        content = claude_md.read_text()
        assert "## Setup" in content
        assert "Setup info" in content
        assert "## Other Section" in content
        assert "Other content" in content
        assert "Old lattice info" not in content
        assert "The First Act" in content
