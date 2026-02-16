"""Integration tests for the plugin system in CLI context."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import click
from click.testing import CliRunner

from lattice.cli.main import cli


class TestPluginsCommand:
    """lattice plugins diagnostic command."""

    @patch("lattice.plugins.entry_points", return_value=[])
    def test_no_plugins_installed(self, mock_ep: MagicMock) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["plugins"])
        assert result.exit_code == 0
        assert "No plugins installed" in result.output

    @patch("lattice.plugins.entry_points", return_value=[])
    def test_no_plugins_json(self, mock_ep: MagicMock) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["plugins", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ok"] is True
        assert data["data"]["cli_plugins"] == []
        assert data["data"]["template_blocks"] == []

    @patch("lattice.plugins.entry_points")
    def test_shows_cli_plugins(self, mock_ep: MagicMock) -> None:
        fake_ep = MagicMock()
        fake_ep.name = "fractal"
        fake_ep.value = "lattice_fractal:register_cli"
        fake_ep.load.return_value = lambda group: None  # no-op register
        mock_ep.return_value = [fake_ep]

        runner = CliRunner()
        result = runner.invoke(cli, ["plugins"])
        assert result.exit_code == 0
        assert "fractal" in result.output

    @patch("lattice.plugins.entry_points")
    def test_shows_template_blocks(self, mock_ep: MagicMock) -> None:
        def side_effect(group=None):
            if group == "lattice.template_blocks":
                ep = MagicMock()
                ep.name = "fractal"
                ep.load.return_value = lambda: [
                    {
                        "marker": "## Lattice -- Fractal",
                        "content": "Fractal content",
                        "position": "after_base",
                    }
                ]
                return [ep]
            return []

        mock_ep.side_effect = side_effect

        runner = CliRunner()
        result = runner.invoke(cli, ["plugins"])
        assert result.exit_code == 0
        assert "Lattice -- Fractal" in result.output


class TestPluginRegisteredCommand:
    """Plugin-registered Click commands are invocable through the CLI."""

    @patch("lattice.plugins.discover_cli_plugins")
    def test_plugin_command_is_invocable(self, mock_discover: MagicMock) -> None:
        """Simulate a plugin that registers a 'hello' command."""
        # We can't easily inject into the already-loaded cli group,
        # so we test the mechanism directly: create a group, register via plugin
        test_group = click.Group("test")

        @click.command()
        def hello():
            click.echo("hello from plugin")

        def register(group):
            group.add_command(hello)

        fake_ep = MagicMock()
        fake_ep.name = "test"
        fake_ep.load.return_value = register

        from lattice.plugins import load_cli_plugins

        mock_discover.return_value = [fake_ep]
        load_cli_plugins(test_group)

        runner = CliRunner()
        result = runner.invoke(test_group, ["hello"])
        assert result.exit_code == 0
        assert "hello from plugin" in result.output


class TestBrokenPluginDoesNotBreakBuiltins:
    """A broken plugin must not prevent built-in commands from working."""

    @patch("lattice.plugins.discover_cli_plugins")
    def test_builtin_commands_work_with_broken_plugin(self, mock_discover: MagicMock) -> None:
        fake_ep = MagicMock()
        fake_ep.name = "broken"
        fake_ep.load.side_effect = ImportError("module not found")
        mock_discover.return_value = [fake_ep]

        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "Lattice" in result.output


class TestSetupClaudeWithPlugins:
    """setup-claude composes base + plugin template blocks."""

    @patch("lattice.plugins.discover_template_blocks")
    def test_setup_claude_composes_plugin_blocks(
        self, mock_discover: MagicMock, tmp_path: Path
    ) -> None:
        """setup-claude should include plugin template blocks in the output."""
        mock_discover.return_value = [
            {
                "marker": "## Lattice -- Fractal Workflow",
                "content": "## Lattice -- Fractal Workflow\n\nFractal-specific content.\n",
                "position": "after_base",
            }
        ]

        runner = CliRunner()
        result = runner.invoke(cli, ["setup-claude", "--path", str(tmp_path)])
        assert result.exit_code == 0

        content = (tmp_path / "CLAUDE.md").read_text()
        assert "## Lattice" in content
        assert "The First Act" in content
        assert "Fractal-specific content" in content

    @patch("lattice.plugins.discover_template_blocks")
    def test_setup_claude_force_strips_plugin_blocks(
        self, mock_discover: MagicMock, tmp_path: Path
    ) -> None:
        """setup-claude --force should strip plugin sections too."""
        mock_discover.return_value = [
            {
                "marker": "## Lattice -- Fractal Workflow",
                "content": "## Lattice -- Fractal Workflow\n\nFractal-specific content.\n",
                "position": "after_base",
            }
        ]

        # Write a CLAUDE.md with both base and plugin blocks
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text(
            "# My Project\n\n## Setup\n\nSetup info.\n\n"
            "## Lattice\n\nOld lattice info.\n\n"
            "## Lattice -- Fractal Workflow\n\nOld fractal info.\n\n"
            "## Other Section\n\nOther content.\n"
        )

        runner = CliRunner()
        result = runner.invoke(cli, ["setup-claude", "--path", str(tmp_path), "--force"])
        assert result.exit_code == 0

        content = claude_md.read_text()
        # Non-Lattice sections preserved
        assert "## Setup" in content
        assert "Setup info" in content
        assert "## Other Section" in content
        assert "Other content" in content
        # Old content removed
        assert "Old lattice info" not in content
        assert "Old fractal info" not in content
        # New content present
        assert "The First Act" in content
        assert "Fractal-specific content" in content

    @patch("lattice.plugins.discover_template_blocks", return_value=[])
    def test_setup_claude_no_plugins_produces_base_only(
        self, mock_discover: MagicMock, tmp_path: Path
    ) -> None:
        """Without plugins, setup-claude produces the standard base template."""
        runner = CliRunner()
        result = runner.invoke(cli, ["setup-claude", "--path", str(tmp_path)])
        assert result.exit_code == 0

        content = (tmp_path / "CLAUDE.md").read_text()
        assert "## Lattice\n" in content
        assert "The First Act" in content
        # Only one H2 Lattice section (base only)
        assert content.count("## Lattice\n") == 1
