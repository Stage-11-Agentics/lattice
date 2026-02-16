"""Unit tests for lattice.plugins module."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from lattice.plugins import (
    CLI_PLUGIN_GROUP,
    TEMPLATE_BLOCK_GROUP,
    discover_cli_plugins,
    discover_template_blocks,
    load_cli_plugins,
)


class TestDiscoverCliPlugins:
    """discover_cli_plugins() returns entry points from the correct group."""

    def test_returns_empty_when_no_plugins_installed(self) -> None:
        # With no third-party plugins installed, should return empty list
        # (or whatever is actually installed — at minimum, doesn't crash)
        result = discover_cli_plugins()
        assert isinstance(result, list)

    def test_uses_correct_group_name(self) -> None:
        assert CLI_PLUGIN_GROUP == "lattice.cli_plugins"

    @patch("lattice.plugins.entry_points")
    def test_returns_entry_points_from_group(self, mock_ep: MagicMock) -> None:
        fake_ep = MagicMock()
        fake_ep.name = "test"
        mock_ep.return_value = [fake_ep]

        result = discover_cli_plugins()
        mock_ep.assert_called_once_with(group=CLI_PLUGIN_GROUP)
        assert result == [fake_ep]


class TestLoadCliPlugins:
    """load_cli_plugins() calls register functions and handles failures."""

    @patch("lattice.plugins.discover_cli_plugins")
    def test_calls_register_with_group(self, mock_discover: MagicMock) -> None:
        register_fn = MagicMock()
        fake_ep = MagicMock()
        fake_ep.load.return_value = register_fn
        fake_ep.name = "test"
        mock_discover.return_value = [fake_ep]

        cli_group = MagicMock()
        load_cli_plugins(cli_group)

        fake_ep.load.assert_called_once()
        register_fn.assert_called_once_with(cli_group)

    @patch("lattice.plugins.discover_cli_plugins")
    def test_load_failure_logged_to_stderr(
        self, mock_discover: MagicMock, capsys: pytest.CaptureFixture
    ) -> None:
        fake_ep = MagicMock()
        fake_ep.name = "broken"
        fake_ep.load.side_effect = ImportError("no such module")
        mock_discover.return_value = [fake_ep]

        cli_group = MagicMock()
        load_cli_plugins(cli_group)

        captured = capsys.readouterr()
        assert "broken" in captured.err
        assert "no such module" in captured.err

    @patch("lattice.plugins.discover_cli_plugins")
    def test_load_failure_never_crashes(self, mock_discover: MagicMock) -> None:
        fake_ep = MagicMock()
        fake_ep.name = "broken"
        fake_ep.load.side_effect = RuntimeError("boom")
        mock_discover.return_value = [fake_ep]

        cli_group = MagicMock()
        # Must not raise
        load_cli_plugins(cli_group)

    @patch("lattice.plugins.discover_cli_plugins")
    def test_broken_plugin_does_not_prevent_others(self, mock_discover: MagicMock) -> None:
        broken_ep = MagicMock()
        broken_ep.name = "broken"
        broken_ep.load.side_effect = ImportError("nope")

        good_register = MagicMock()
        good_ep = MagicMock()
        good_ep.name = "good"
        good_ep.load.return_value = good_register

        mock_discover.return_value = [broken_ep, good_ep]

        cli_group = MagicMock()
        load_cli_plugins(cli_group)

        good_register.assert_called_once_with(cli_group)

    @patch.dict("os.environ", {"LATTICE_DEBUG": "1"})
    @patch("lattice.plugins.discover_cli_plugins")
    def test_debug_env_prints_traceback(
        self, mock_discover: MagicMock, capsys: pytest.CaptureFixture
    ) -> None:
        fake_ep = MagicMock()
        fake_ep.name = "broken"
        fake_ep.load.side_effect = ImportError("debug test")
        mock_discover.return_value = [fake_ep]

        load_cli_plugins(MagicMock())

        captured = capsys.readouterr()
        assert "Traceback" in captured.err


class TestDiscoverTemplateBlocks:
    """discover_template_blocks() validates and returns plugin template blocks."""

    def test_returns_empty_when_no_plugins(self) -> None:
        result = discover_template_blocks()
        assert isinstance(result, list)

    def test_uses_correct_group_name(self) -> None:
        assert TEMPLATE_BLOCK_GROUP == "lattice.template_blocks"

    @patch("lattice.plugins.entry_points")
    def test_returns_valid_blocks(self, mock_ep: MagicMock) -> None:
        block = {"marker": "## Lattice -- Test", "content": "Test content\n"}
        fake_ep = MagicMock()
        fake_ep.name = "test"
        fake_ep.load.return_value = lambda: [block]
        mock_ep.return_value = [fake_ep]

        result = discover_template_blocks()
        assert len(result) == 1
        assert result[0]["marker"] == "## Lattice -- Test"
        assert result[0]["content"] == "Test content\n"

    @patch("lattice.plugins.entry_points")
    def test_rejects_replace_base(self, mock_ep: MagicMock, capsys: pytest.CaptureFixture) -> None:
        block = {
            "marker": "## Lattice -- Evil",
            "content": "Replace it all",
            "position": "replace_base",
        }
        fake_ep = MagicMock()
        fake_ep.name = "evil"
        fake_ep.load.return_value = lambda: [block]
        mock_ep.return_value = [fake_ep]

        result = discover_template_blocks()
        assert len(result) == 0
        captured = capsys.readouterr()
        assert "replace_base" in captured.err

    @patch("lattice.plugins.entry_points")
    def test_rejects_missing_keys(self, mock_ep: MagicMock, capsys: pytest.CaptureFixture) -> None:
        block = {"marker": "## Test"}  # missing 'content'
        fake_ep = MagicMock()
        fake_ep.name = "incomplete"
        fake_ep.load.return_value = lambda: [block]
        mock_ep.return_value = [fake_ep]

        result = discover_template_blocks()
        assert len(result) == 0
        captured = capsys.readouterr()
        assert "content" in captured.err

    @patch("lattice.plugins.entry_points")
    def test_load_failure_logged_not_crashed(
        self, mock_ep: MagicMock, capsys: pytest.CaptureFixture
    ) -> None:
        fake_ep = MagicMock()
        fake_ep.name = "broken"
        fake_ep.load.side_effect = ImportError("no module")
        mock_ep.return_value = [fake_ep]

        result = discover_template_blocks()
        assert result == []
        captured = capsys.readouterr()
        assert "broken" in captured.err

    @patch("lattice.plugins.entry_points")
    def test_non_list_return_skipped(
        self, mock_ep: MagicMock, capsys: pytest.CaptureFixture
    ) -> None:
        fake_ep = MagicMock()
        fake_ep.name = "wrong_type"
        fake_ep.load.return_value = lambda: "not a list"
        mock_ep.return_value = [fake_ep]

        result = discover_template_blocks()
        assert result == []
        captured = capsys.readouterr()
        assert "expected list[dict]" in captured.err

    @patch("lattice.plugins.entry_points")
    def test_non_dict_block_skipped(
        self, mock_ep: MagicMock, capsys: pytest.CaptureFixture
    ) -> None:
        fake_ep = MagicMock()
        fake_ep.name = "bad_block"
        fake_ep.load.return_value = lambda: ["not a dict"]
        mock_ep.return_value = [fake_ep]

        result = discover_template_blocks()
        assert result == []
        captured = capsys.readouterr()
        assert "not a dict" in captured.err
