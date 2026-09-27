"""AC-38 (CLI): ``lattice show`` prints ``actor · user@machine · worktree (branch)``
under each event that has an origin, and nothing extra for older events."""

from __future__ import annotations

import json
import socket
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from lattice.cli.main import cli
from lattice.core.origin import format_origin_line
from tests.test_remote.hosted import HostedEnv
from tests.test_remote.hosted import hosted_env as hosted_env  # noqa: F401 - fixture

ACTOR = "agent:o"


def _event(origin: dict | None, actor: object = ACTOR) -> dict:
    event = {"type": "comment_added", "actor": actor, "data": {}}
    if origin is not None:
        event["origin"] = origin
    return event


@pytest.mark.parametrize(
    ("origin", "expected"),
    [
        (
            {"reported": {"os_user": "alice", "host": "lap", "worktree": "/w", "branch": "b"}},
            "agent:o · alice@lap · /w (b)",
        ),
        (
            {
                "reported": {"os_user": "alice", "host": "lap", "worktree": "/w", "branch": "b"},
                "authenticated": {"user": "human:alice", "machine": "alice-laptop"},
            },
            "agent:o · human:alice@alice-laptop · /w (b)",
        ),
        (
            {"reported": {"os_user": "alice", "host": "lap", "source": "browser"}},
            "agent:o · alice@lap · browser",
        ),
        (
            {"reported": {"os_user": "alice", "host": "lap", "worktree": "/w"}},
            "agent:o · alice@lap · /w",
        ),
        ({"reported": {"host": "lap"}}, "agent:o · lap"),
        ({"op": "task.x"}, "agent:o"),
    ],
)
def test_format(origin: dict, expected: str) -> None:
    assert format_origin_line(_event(origin)) == expected


def test_structured_actor_shows_its_name() -> None:
    origin = {"reported": {"os_user": "u", "host": "h"}}
    assert format_origin_line(_event(origin, {"name": "Argus-3", "session": "s"})) == (
        "Argus-3 · u@h"
    )


def test_legacy_event_has_no_line() -> None:
    assert format_origin_line(_event(None)) is None


def test_show_prints_the_line_for_operation_events_only(
    initialized_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "feat/x"], cwd=repo, check=True)
    monkeypatch.chdir(repo)
    env = {"LATTICE_ROOT": str(initialized_root)}
    runner = CliRunner()
    created = runner.invoke(cli, ["create", "Shown", "--actor", ACTOR, "--json"], env=env)
    task_id = json.loads(created.output)["data"]["id"]

    # An event written before origins existed (appended directly).
    log = initialized_root / ".lattice" / "events" / f"{task_id}.jsonl"
    from lattice.core.events import create_event
    from lattice.storage.operations import mutate_task_events

    legacy = create_event("comment_added", task_id, "human:old", {"body": "legacy"})
    mutate_task_events(initialized_root / ".lattice", task_id, [legacy], run_hooks=False)
    assert "origin" not in json.loads(log.read_text().splitlines()[-1])

    shown = runner.invoke(cli, ["show", task_id], env=env)
    assert shown.exit_code == 0, shown.output
    lines = shown.output.splitlines()
    import getpass

    origin_line = (
        f"    {ACTOR} · {getpass.getuser()}@{socket.gethostname()} · {repo.resolve()} (feat/x)"
    )
    created_at = next(i for i, line in enumerate(lines) if "task_created" in line)
    assert lines[created_at + 1] == origin_line
    legacy_at = next(i for i, line in enumerate(lines) if "by human:old" in line)
    assert lines[legacy_at + 1].startswith("  2")  # next event line, no origin line
    assert lines.count(origin_line) == 1

    as_json = json.loads(runner.invoke(cli, ["show", task_id, "--json"], env=env).output)
    events = as_json["data"]["events"]
    assert events[0]["origin"]["reported"]["branch"] == "feat/x"
    assert "origin" not in events[1]


def test_hosted_plain_output_replaces_control_characters(
    hosted_env: HostedEnv,  # noqa: F811 - the fixture imported above
    tmp_path: Path,
) -> None:
    """On a hosted checkout, another user's terminal escape shows as U+FFFD in
    plain output (SPEC §4); ``--json`` and local output are unchanged."""
    from tests.test_remote.hosted import make_repo, run_cli

    repo = make_repo(tmp_path / "repo")
    assert run_cli(repo, "remote", "attach", "team", "demo").exit_code == 0
    hostile = "red \x1b[31malert\x1b]0;pwned\x07 end\x9b2J\tkept\nline two"
    hosted_env.server_op(
        "task.create",
        {"title": "Escape \x1b[2J title", "description": hostile},
        actor="human:alice",
    )

    # color=True: click keeps escapes as it does for a terminal, so only the
    # hosted scrubbing stands between them and the screen.
    plain = run_cli(repo, "show", "DEM-1", color=True)
    assert plain.exit_code == 0, plain.output
    assert "\x1b" not in plain.stdout and "\x07" not in plain.stdout and "\x9b" not in plain.stdout
    assert "red �[31malert�]0;pwned� end�2J\tkept" in plain.stdout
    assert "line two" in plain.stdout
    assert "Escape �[2J title" in plain.stdout

    as_json = run_cli(repo, "show", "DEM-1", "--json")
    data = json.loads(as_json.stdout)["data"]
    assert data["title"] == "Escape \x1b[2J title"


def test_local_plain_output_keeps_control_characters(tmp_path: Path) -> None:
    """Local mode is unchanged (G-6): no scrubbing without a binding."""
    from lattice.storage.board_init import create_board
    from tests.test_remote.hosted import run_cli

    create_board(tmp_path, project_code="LOC", actor="human:a")
    assert (
        run_cli(tmp_path, "create", "Local \x1b]0;t\x07 title", "--actor", "human:a").exit_code
        == 0
    )
    plain = run_cli(tmp_path, "show", "LOC-1", color=True)
    assert "Local \x1b]0;t\x07 title" in plain.stdout
