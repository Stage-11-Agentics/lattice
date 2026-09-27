"""CLI command discovery: every ``*_cmds`` / ``*_cmd`` module in ``lattice.cli`` registers.

The list below is frozen on purpose, so a module cannot join (or drop out of)
the CLI silently. A ticket that adds a command module adds one line here, kept
sorted, one module per line, so parallel additions merge as a clean union.
"""

from __future__ import annotations

import sys

from lattice.cli.main import cli, command_module_names

EXPECTED_MODULES = [
    "archive_cmds",
    "artifact_cmds",
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
    "query_cmds",
    "resource_cmds",
    "review_cmds",
    "server_cmds",
    "session_cmds",
    "stats_cmds",
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
    for name in EXPECTED_MODULES:
        assert f"lattice.cli.{name}" in sys.modules


def test_representative_commands_are_registered() -> None:
    """One command per module, so a module that imports but fails to register shows."""
    for command in (
        "archive",
        "attach",
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
        "list",
        "resource",
        "code-review",
        "session",
        "stats",
        "create",
        "wait",
        "watch",
        "weather",
    ):
        assert command in cli.commands, command
