"""CLI command discovery: every ``*_cmds`` / ``*_cmd`` module in ``lattice.cli`` registers.

The list below is frozen on purpose, so a module cannot join (or drop out of)
the CLI silently. A ticket that adds a command module adds one line here, kept
sorted, one module per line, so parallel additions merge as a clean union.

The root group imports a command's module only when that command runs
(LAT-359); ``COMMAND_MODULES`` in ``lattice.cli.main`` is the index it uses.
"""

from __future__ import annotations

import subprocess
import sys

import click
from click.testing import CliRunner

from lattice.cli.main import COMMAND_MODULES, cli, command_module_names, load_all_commands

EXPECTED_MODULES = [
    "archive_cmds",
    "artifact_cmds",
    "cache_cmds",
    "claim_cmd",
    "criterion_cmds",
    "dashboard_cmd",
    "demo_cmd",
    "erase_cmds",
    "file_cmds",
    "flag_cmds",
    "integrity_cmds",
    "link_cmds",
    "migration_cmds",
    "prose_cmds",
    "query_cmds",
    "remote_cmds",
    "resource_cmds",
    "review_cmds",
    "server_cmds",
    "session_cmds",
    "stats_cmds",
    "sync_cmd",
    "task_cmds",
    "wait_cmd",
    "watch_cmd",
    "weather_cmds",
]


def test_expected_list_is_sorted_and_unique() -> None:
    assert EXPECTED_MODULES == sorted(set(EXPECTED_MODULES))


def test_discovered_modules_match_the_frozen_list() -> None:
    assert command_module_names() == EXPECTED_MODULES


def test_every_discovered_module_is_imported() -> None:
    load_all_commands()
    for name in EXPECTED_MODULES:
        assert f"lattice.cli.{name}" in sys.modules


def test_representative_commands_are_registered() -> None:
    """One command per module, so a module that imports but fails to register shows."""
    load_all_commands()
    for command in (
        "archive",
        "attach",
        "cache",
        "claim",
        "criterion",
        "dashboard",
        "demo",
        "erase",
        "file-link",
        "needs-human",
        "doctor",
        "link",
        "migrate",
        "board",
        "list",
        "resource",
        "code-review",
        "session",
        "stats",
        "sync",
        "create",
        "wait",
        "watch",
        "weather",
    ):
        assert command in cli.commands, command


def test_command_index_matches_the_registrations() -> None:
    """Every built-in command outside ``main`` is indexed under the module that
    registers it, and the index names no command that does not exist."""
    load_all_commands()
    registered = {
        name: command.callback.__module__.rsplit(".", 1)[1]
        for name, command in cli.commands.items()
        if command.callback is not None and command.callback.__module__ != "lattice.cli.main"
    }
    assert COMMAND_MODULES == registered


def test_start_up_imports_only_the_invoked_command_module() -> None:
    """``lattice status`` imports ``task_cmds`` and no other command module."""
    code = (
        "import sys\n"
        "from lattice.cli.main import cli\n"
        "try:\n"
        "    cli(['status', '--help'])\n"
        "except SystemExit:\n"
        "    pass\n"
        "loaded = sorted(m for m in sys.modules if m.endswith(('_cmds', '_cmd')))\n"
        "print(loaded)\n"
        "assert loaded == ['lattice.cli.task_cmds'], loaded\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_unindexed_command_still_resolves(monkeypatch) -> None:  # noqa: ANN001
    """A command missing from the index (a new module that forgot its line)
    still runs: an unknown name imports every module before giving up."""
    import lattice.cli.main as main

    load_all_commands()
    monkeypatch.delitem(cli.commands, "weather")
    monkeypatch.delitem(COMMAND_MODULES, "weather")
    monkeypatch.setattr(main, "_all_commands_loaded", False)
    monkeypatch.delitem(sys.modules, "lattice.cli.weather_cmds")

    assert isinstance(cli.get_command(click.Context(cli), "weather"), click.Command)
    assert CliRunner().invoke(cli, ["no-such-command"]).exit_code == 2
