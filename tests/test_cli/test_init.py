"""Tests for the `lattice init` CLI command."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from lattice.cli.main import cli
from lattice.core.config import default_config


# Helper: standard interactive input that skips all prompts with defaults.
# New flow prompts: name, project-name, project-code, agents.md confirm
_SKIP_ALL = "\n\n\nn\n"  # empty name, empty project-name, empty project-code, decline agents.md


class TestInitDirectoryStructure:
    """lattice init creates the full .lattice/ directory tree."""

    def test_creates_all_expected_directories(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["init", "--path", str(tmp_path)], input=_SKIP_ALL)
        assert result.exit_code == 0

        lattice = tmp_path / ".lattice"
        expected_dirs = [
            "tasks",
            "events",
            "artifacts/meta",
            "artifacts/payload",
            "notes",
            "plans",
            "archive/tasks",
            "archive/events",
            "archive/notes",
            "archive/plans",
            "locks",
        ]
        for d in expected_dirs:
            assert (lattice / d).is_dir(), f"Missing directory: {d}"

    def test_creates_empty_lifecycle_jsonl(self, tmp_path: Path) -> None:
        runner = CliRunner()
        runner.invoke(cli, ["init", "--path", str(tmp_path)], input=_SKIP_ALL)

        lifecycle_log = tmp_path / ".lattice" / "events" / "_lifecycle.jsonl"
        assert lifecycle_log.is_file()
        assert lifecycle_log.read_text() == ""

    def test_init_with_custom_path(self, tmp_path: Path) -> None:
        target = tmp_path / "myproject"
        target.mkdir()

        runner = CliRunner()
        result = runner.invoke(cli, ["init", "--path", str(target)], input=_SKIP_ALL)
        assert result.exit_code == 0
        assert (target / ".lattice" / "config.json").is_file()

    def test_prints_success_message(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["init", "--path", str(tmp_path)], input=_SKIP_ALL)
        assert result.exit_code == 0
        assert "a mind now has a place to remember" in result.output


class TestInitConfig:
    """lattice init writes a valid, deterministic config.json."""

    def test_writes_valid_json(self, tmp_path: Path) -> None:
        runner = CliRunner()
        runner.invoke(cli, ["init", "--path", str(tmp_path)], input=_SKIP_ALL)

        config_path = tmp_path / ".lattice" / "config.json"
        config = json.loads(config_path.read_text())
        assert isinstance(config, dict)

    def test_config_has_schema_version_1(self, tmp_path: Path) -> None:
        runner = CliRunner()
        runner.invoke(cli, ["init", "--path", str(tmp_path)], input=_SKIP_ALL)

        config = json.loads((tmp_path / ".lattice" / "config.json").read_text())
        assert config["schema_version"] == 1

    def test_config_contains_default_fields(self, tmp_path: Path) -> None:
        """Config on disk must contain all default config fields plus instance_id."""
        runner = CliRunner()
        runner.invoke(cli, ["init", "--path", str(tmp_path)], input=_SKIP_ALL)

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
        runner.invoke(cli, ["init", "--path", str(tmp_path)], input=_SKIP_ALL)

        raw = (tmp_path / ".lattice" / "config.json").read_bytes()
        assert raw.endswith(b"\n")
        # Exactly one trailing newline, not two
        assert not raw.endswith(b"\n\n")


class TestInitIdempotency:
    """Running init twice must not clobber existing data."""

    def test_second_init_does_not_clobber_config(self, tmp_path: Path) -> None:
        runner = CliRunner()
        runner.invoke(cli, ["init", "--path", str(tmp_path)], input=_SKIP_ALL)

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
        runner.invoke(cli, ["init", "--path", str(tmp_path)], input=_SKIP_ALL)

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
        runner.invoke(cli, ["init", "--path", str(tmp_path)], input=_SKIP_ALL)

        result = runner.invoke(cli, ["init", "--path", str(tmp_path)])
        assert result.exit_code == 0
        assert "already initialized" in result.output

    def test_existing_tasks_survive_second_init(self, tmp_path: Path) -> None:
        runner = CliRunner()
        runner.invoke(cli, ["init", "--path", str(tmp_path)], input=_SKIP_ALL)

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
        # --actor provided, so name prompt skipped; need: project-name, project-code, agents.md
        result = runner.invoke(
            cli, ["init", "--path", str(tmp_path), "--actor", "human:atin"], input="\n\nn\n"
        )
        assert result.exit_code == 0

        config = json.loads((tmp_path / ".lattice" / "config.json").read_text())
        assert config["default_actor"] == "human:atin"

    def test_init_prompts_for_actor_when_flag_omitted(self, tmp_path: Path) -> None:
        runner = CliRunner()
        # Name prompt: "atin" (becomes human:atin), project-name, project-code, agents.md
        result = runner.invoke(
            cli, ["init", "--path", str(tmp_path)], input="atin\n\n\nn\n"
        )
        assert result.exit_code == 0

        config = json.loads((tmp_path / ".lattice" / "config.json").read_text())
        assert config["default_actor"] == "human:atin"
        assert "human:atin" in result.output

    def test_init_full_actor_string_in_name_prompt(self, tmp_path: Path) -> None:
        """Typing a full actor string (with colon) uses it as-is."""
        runner = CliRunner()
        result = runner.invoke(
            cli, ["init", "--path", str(tmp_path)], input="agent:claude\n\n\nn\n"
        )
        assert result.exit_code == 0

        config = json.loads((tmp_path / ".lattice" / "config.json").read_text())
        assert config["default_actor"] == "agent:claude"

    def test_init_empty_actor_input_skips_default(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["init", "--path", str(tmp_path)], input=_SKIP_ALL)
        assert result.exit_code == 0

        config = json.loads((tmp_path / ".lattice" / "config.json").read_text())
        assert "default_actor" not in config

    def test_init_invalid_actor_format_errors(self, tmp_path: Path) -> None:
        runner = CliRunner()
        # Both flags → non-interactive, validation runs immediately
        result = runner.invoke(
            cli, ["init", "--path", str(tmp_path), "--actor", "badformat", "--project-code", "TST"]
        )
        assert result.exit_code != 0
        assert "Invalid actor format" in result.output

    def test_init_di_path_creates_agent_actor(self, tmp_path: Path) -> None:
        """Typing 'di' triggers the digital intelligence path."""
        runner = CliRunner()
        # di, identifier, model, project-name, project-code, agents.md
        result = runner.invoke(
            cli, ["init", "--path", str(tmp_path)],
            input="di\nbuilder\nclaude-opus-4\n\n\nn\n",
        )
        assert result.exit_code == 0

        config = json.loads((tmp_path / ".lattice" / "config.json").read_text())
        assert config["default_actor"] == "agent:builder"
        assert config["model"] == "claude-opus-4"
        assert "a fellow mind in the substrate" in result.output

    def test_init_di_path_no_model(self, tmp_path: Path) -> None:
        """DI path with blank model still works."""
        runner = CliRunner()
        # di, identifier, blank model, project-name, project-code, agents.md
        result = runner.invoke(
            cli, ["init", "--path", str(tmp_path)],
            input="di\nscout\n\n\n\nn\n",
        )
        assert result.exit_code == 0

        config = json.loads((tmp_path / ".lattice" / "config.json").read_text())
        assert config["default_actor"] == "agent:scout"
        assert "model" not in config


class TestInitProjectName:
    """lattice init --project-name flag and interactive prompt."""

    def test_init_with_project_name_flag(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["init", "--path", str(tmp_path), "--actor", "human:test",
             "--project-code", "BC", "--project-name", "Beauty Creator"],
        )
        assert result.exit_code == 0

        config = json.loads((tmp_path / ".lattice" / "config.json").read_text())
        assert config["project_name"] == "Beauty Creator"

    def test_init_prompts_for_project_name(self, tmp_path: Path) -> None:
        runner = CliRunner()
        # name, project-name, project-code, agents.md
        result = runner.invoke(
            cli, ["init", "--path", str(tmp_path)],
            input="sarah\nBeauty Creator\nBC\nn\n",
        )
        assert result.exit_code == 0

        config = json.loads((tmp_path / ".lattice" / "config.json").read_text())
        assert config["project_name"] == "Beauty Creator"

    def test_init_project_name_in_context_md(self, tmp_path: Path) -> None:
        """Project name is used as context.md heading."""
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["init", "--path", str(tmp_path), "--actor", "human:test",
             "--project-code", "BC", "--project-name", "Beauty Creator"],
        )
        assert result.exit_code == 0

        content = (tmp_path / ".lattice" / "context.md").read_text()
        assert "# Beauty Creator" in content

    def test_init_empty_project_name_uses_template(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["init", "--path", str(tmp_path)], input=_SKIP_ALL)
        assert result.exit_code == 0

        content = (tmp_path / ".lattice" / "context.md").read_text()
        assert "# Instance Context" in content


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
        result = runner.invoke(cli, ["init", "--path", str(tmp_path)], input=_SKIP_ALL)
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
        result = runner.invoke(cli, ["init", "--path", str(tmp_path)], input=_SKIP_ALL)
        assert result.exit_code != 0
        assert "Failed to initialize" in result.output
        assert "Traceback" not in result.output


class TestInitInstanceIdentity:
    """lattice init generates instance_id and supports --instance-name."""

    def test_always_generates_instance_id(self, tmp_path: Path) -> None:
        runner = CliRunner()
        runner.invoke(cli, ["init", "--path", str(tmp_path)], input=_SKIP_ALL)

        config = json.loads((tmp_path / ".lattice" / "config.json").read_text())
        assert "instance_id" in config
        assert config["instance_id"].startswith("inst_")
        assert len(config["instance_id"]) == 31  # "inst_" + 26 char ULID

    def test_instance_name_flag(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["init", "--path", str(tmp_path), "--instance-name", "Frontend",
             "--actor", "human:test", "--project-code", "TST"],
        )
        assert result.exit_code == 0
        assert "Instance name: Frontend" in result.output

        config = json.loads((tmp_path / ".lattice" / "config.json").read_text())
        assert config["instance_name"] == "Frontend"

    def test_no_instance_name_by_default(self, tmp_path: Path) -> None:
        runner = CliRunner()
        runner.invoke(cli, ["init", "--path", str(tmp_path)], input=_SKIP_ALL)

        config = json.loads((tmp_path / ".lattice" / "config.json").read_text())
        assert "instance_name" not in config


class TestInitContextMd:
    """lattice init creates .lattice/context.md template."""

    def test_creates_context_md(self, tmp_path: Path) -> None:
        runner = CliRunner()
        runner.invoke(cli, ["init", "--path", str(tmp_path)], input=_SKIP_ALL)

        context_path = tmp_path / ".lattice" / "context.md"
        assert context_path.is_file()

    def test_context_md_has_expected_sections(self, tmp_path: Path) -> None:
        runner = CliRunner()
        runner.invoke(cli, ["init", "--path", str(tmp_path)], input=_SKIP_ALL)

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
        )
        assert result.exit_code == 0
        assert "Project code: AUT" in result.output
        assert "Subproject code: F" in result.output

        config = json.loads((tmp_path / ".lattice" / "config.json").read_text())
        assert config["project_code"] == "AUT"
        assert config["subproject_code"] == "F"

    def test_subproject_without_project_code_errors(self, tmp_path: Path) -> None:
        runner = CliRunner()
        # --actor provided, prompts: project-name + project-code (both empty)
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
            input="\n\n",  # empty project-name, empty project-code
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


class TestInitAgentsMd:
    """lattice init agents.md integration."""

    def test_init_creates_agents_md_on_confirm(self, tmp_path: Path) -> None:
        """Interactive init, confirm agents.md -> file created."""
        runner = CliRunner()
        # name, project-name, project-code, 'y' for agents.md
        result = runner.invoke(
            cli, ["init", "--path", str(tmp_path)], input="\n\n\ny\n"
        )
        assert result.exit_code == 0
        assert "Created agents.md with Lattice integration" in result.output

        agents_md = tmp_path / "agents.md"
        assert agents_md.exists()
        content = agents_md.read_text()
        assert "## Lattice" in content

    def test_init_skips_agents_md_on_decline(self, tmp_path: Path) -> None:
        """Interactive init, decline agents.md -> file not created."""
        runner = CliRunner()
        result = runner.invoke(
            cli, ["init", "--path", str(tmp_path)], input=_SKIP_ALL  # 'n' for agents.md
        )
        assert result.exit_code == 0

        agents_md = tmp_path / "agents.md"
        assert not agents_md.exists()

    def test_init_appends_to_existing_agents_md(self, tmp_path: Path) -> None:
        """Existing agents.md without Lattice block -> appends."""
        agents_md = tmp_path / "agents.md"
        agents_md.write_text("# My Agent Instructions\n\nExisting content.\n")

        runner = CliRunner()
        result = runner.invoke(
            cli, ["init", "--path", str(tmp_path)], input="\n\n\ny\n"
        )
        assert result.exit_code == 0
        assert "Updated agents.md with Lattice integration" in result.output

        content = agents_md.read_text()
        assert "Existing content" in content
        assert "## Lattice" in content

    def test_init_agents_md_already_has_lattice(self, tmp_path: Path) -> None:
        """Existing agents.md with Lattice block -> no duplicate."""
        agents_md = tmp_path / "agents.md"
        agents_md.write_text("# My Agent\n\n## Lattice\n\nAlready here.\n")

        runner = CliRunner()
        result = runner.invoke(
            cli, ["init", "--path", str(tmp_path)], input="\n\n\ny\n"
        )
        assert result.exit_code == 0
        assert "already has Lattice integration" in result.output

        content = agents_md.read_text()
        assert content.count("## Lattice") == 1

    def test_init_noninteractive_auto_creates_agents_md(self, tmp_path: Path) -> None:
        """Non-interactive init auto-creates agents.md."""
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["init", "--path", str(tmp_path), "--actor", "human:test", "--project-code", "TST"],
        )
        assert result.exit_code == 0
        assert "Created agents.md" in result.output

        agents_md = tmp_path / "agents.md"
        assert agents_md.exists()
        assert "## Lattice" in agents_md.read_text()

    def test_init_no_setup_agents_flag(self, tmp_path: Path) -> None:
        """--no-setup-agents prevents agents.md creation."""
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["init", "--path", str(tmp_path), "--actor", "human:test",
             "--project-code", "TST", "--no-setup-agents"],
        )
        assert result.exit_code == 0

        agents_md = tmp_path / "agents.md"
        assert not agents_md.exists()


class TestInitClaudeMd:
    """lattice init CLAUDE.md integration (now secondary to agents.md)."""

    def test_init_updates_existing_claude_md_silently(self, tmp_path: Path) -> None:
        """When agents.md is created and CLAUDE.md exists, it gets updated too."""
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text("# My Project\n\nExisting content.\n")

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["init", "--path", str(tmp_path), "--actor", "human:test", "--project-code", "TST"],
        )
        assert result.exit_code == 0
        assert "Updated CLAUDE.md with Lattice integration" in result.output

        content = claude_md.read_text()
        assert "## Lattice" in content
        assert "Existing content" in content

    def test_init_does_not_create_claude_md_by_default(self, tmp_path: Path) -> None:
        """Init no longer creates CLAUDE.md from scratch (agents.md is primary)."""
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["init", "--path", str(tmp_path), "--actor", "human:test", "--project-code", "TST"],
        )
        assert result.exit_code == 0

        claude_md = tmp_path / "CLAUDE.md"
        assert not claude_md.exists()

    def test_init_setup_claude_flag_creates(self, tmp_path: Path) -> None:
        """--setup-claude explicitly creates CLAUDE.md."""
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["init", "--path", str(tmp_path), "--actor", "human:test",
             "--project-code", "TST", "--setup-claude"],
        )
        assert result.exit_code == 0
        assert "Created CLAUDE.md with Lattice integration" in result.output

        claude_md = tmp_path / "CLAUDE.md"
        assert claude_md.exists()
        assert "## Lattice" in claude_md.read_text()

    def test_init_no_setup_claude_preserves_existing(self, tmp_path: Path) -> None:
        """--no-setup-claude with existing file -> file not modified."""
        claude_md = tmp_path / "CLAUDE.md"
        original_content = "# My Project\n\nExisting content.\n"
        claude_md.write_text(original_content)

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["init", "--path", str(tmp_path), "--actor", "human:test",
             "--project-code", "TST", "--no-setup-claude"],
        )
        assert result.exit_code == 0
        assert claude_md.read_text() == original_content

    def test_init_claude_md_already_has_lattice(self, tmp_path: Path) -> None:
        """CLAUDE.md already contains Lattice block -> no duplicate."""
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text("# My Project\n\n## Lattice\n\nAlready integrated.\n")

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["init", "--path", str(tmp_path), "--actor", "human:test", "--project-code", "TST"],
        )
        assert result.exit_code == 0

        content = claude_md.read_text()
        assert content.count("## Lattice") == 1

    def test_init_heartbeat_stores_config(self, tmp_path: Path) -> None:
        """Init with heartbeat enabled -> config.json has heartbeat section."""
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["init", "--path", str(tmp_path), "--actor", "human:test",
             "--project-code", "TST", "--heartbeat"],
        )
        assert result.exit_code == 0
        assert "Heartbeat: enabled" in result.output

        config = json.loads((tmp_path / ".lattice" / "config.json").read_text())
        assert config["heartbeat"]["enabled"] is True
        assert config["heartbeat"]["max_advances"] == 10

    def test_init_no_heartbeat_no_config(self, tmp_path: Path) -> None:
        """Init with --no-heartbeat -> config.json has no heartbeat section."""
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["init", "--path", str(tmp_path), "--actor", "human:test",
             "--project-code", "TST", "--no-heartbeat"],
        )
        assert result.exit_code == 0
        assert "Heartbeat" not in result.output

        config = json.loads((tmp_path / ".lattice" / "config.json").read_text())
        assert "heartbeat" not in config


class TestInitWelcomeMessage:
    """lattice init shows the new welcome experience in interactive mode."""

    def test_interactive_shows_welcome(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["init", "--path", str(tmp_path)], input=_SKIP_ALL)
        assert result.exit_code == 0
        assert "minds that build" in result.output
        assert "digital intelligence" in result.output

    def test_noninteractive_skips_welcome(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["init", "--path", str(tmp_path), "--actor", "human:test", "--project-code", "TST"],
        )
        assert result.exit_code == 0
        assert "minds that build" not in result.output

    def test_interactive_shows_next_steps(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["init", "--path", str(tmp_path)], input=_SKIP_ALL)
        assert result.exit_code == 0
        assert "from here" in result.output
        assert "lattice advance" in result.output

    def test_interactive_shows_mission_accomplished(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["init", "--path", str(tmp_path)], input=_SKIP_ALL)
        assert result.exit_code == 0
        assert "the lattice exists" in result.output


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
        assert "Creating Tasks (Non-Negotiable)" in content

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
        assert "Creating Tasks (Non-Negotiable)" in content

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
        assert "Creating Tasks (Non-Negotiable)" in content
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
        assert "Creating Tasks (Non-Negotiable)" in content
