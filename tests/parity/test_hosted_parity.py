"""Hosted parity (AC-5, AC-9, G-2; H-12): the golden corpus through a bound checkout.

Every scenario of ``corpus.py`` is replayed, plain and ``--json``, through a
checkout bound to its own project on one in-process server per pytest worker
(``hosted.py`` says how the replay differs from the local run, and which
differences SPEC declares). Each replay must match the local golden: stdout,
stderr, exit codes, and the board. After each one:

- the cache holds exactly the server board's durable paths, byte for byte, and
  read commands print the same on either (AC-9);
- the server's write recorder saw no removal of a durable path except a
  relocation SPEC §7 permits (G-2).

A hosted divergence is a bug in the product, never in the golden.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.parity.corpus import SCENARIOS, Scenario
from tests.parity.hosted import (
    ACTORLESS_COMMANDS,
    FIXTURE_OP,
    Mutation,
    ParityServer,
    comparable,
    declared_differences,
    durable_tree,
    forbidden_removals,
    hosted_target,
    parity_server,
)
from tests.parity.record import MODES, _base_env, _chdir, _process_env, _runner, load_golden

#: Scenarios the hosted replay leaves out, each with the ticket that owns the gap.
NOT_HOSTED = {
    # The settings POST goes through the in-process *local* dashboard, which
    # H-13a converts to board.set_dashboard_config and gives a hosted
    # checkout's dashboard its follower (dashboard/server.py is H-13a's file,
    # BUILDPLAN §4). Until then a bound checkout's dashboard does not write.
    "dashboard_settings": "H-13a",
}

CASES = [(s, m) for s in SCENARIOS if s.name not in NOT_HOSTED for m in MODES]


@pytest.fixture(scope="session")
def server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ParityServer]:
    """One server per worker (each xdist worker is its own session)."""
    with parity_server(tmp_path_factory.mktemp("parity-server")) as handle:
        yield handle


def _read(root: Path, args: list[str], extra_env: dict[str, str]) -> tuple[int, str, str]:
    """``lattice <args>`` read in-process against the board at *root*: exit code,
    stdout, and the type of any exception (a corrupt board crashes some reads,
    locally as well)."""
    from lattice.cli.main import cli

    env = {**_base_env(root), **extra_env}
    with _chdir(root), _process_env(env):
        result = _runner().invoke(cli, args, env=env)
    exc = result.exception
    crash = "" if exc is None or isinstance(exc, SystemExit) else type(exc).__name__
    return result.exit_code, result.stdout.replace(str(root), "<ROOT>"), crash


def _reads(lattice_dir: Path) -> list[list[str]]:
    """Read commands covering every task, active and archived."""
    commands = [["list", "--json"], ["list", "--include-archived", "--json"]]
    ids = json.loads((lattice_dir / "ids.json").read_text())["map"]
    for short in sorted(ids):
        commands.append(["show", short, "--json", "--compact"])
    return commands


def _assert_reads_match(
    checkout: Path, server_project: Path, cache: Path, env: dict[str, str]
) -> None:
    """Read commands print the same through the cache as on the server's own board."""
    for args in _reads(cache):
        on_cache = _read(checkout, args, env)
        on_server = _read(server_project, args, {})
        assert on_cache == on_server, f"lattice {' '.join(args)} differs on cache and server"


@pytest.mark.parametrize(("scenario", "mode"), CASES, ids=[f"{s.name}.{m}" for s, m in CASES])
def test_scenario_matches_golden_through_the_server(
    scenario: Scenario, mode: str, server: ParityServer, tmp_path: Path
) -> None:
    from tests.parity.record import run_scenario

    target = hosted_target(server, scenario)
    checkout = tmp_path / "board"
    capture = run_scenario(scenario, checkout, mode=mode, target=target)

    expected = comparable(load_golden(scenario.name, mode))
    actual = comparable(declared_differences(capture))
    assert actual["steps"] == expected["steps"], f"hosted output drift in {scenario.name}.{mode}"
    assert actual["board"] == expected["board"], f"hosted board drift in {scenario.name}.{mode}"
    assert actual.get("sentinel") == expected.get("sentinel"), "hook sentinel drift"

    # AC-9: the cache is the server board, byte for byte, and reads agree.
    cache = checkout / ".lattice"
    board = server.board(target.slug)
    assert durable_tree(cache) == durable_tree(board)
    _assert_reads_match(checkout, board.parent, cache, target.env(checkout))

    # G-2: no removal of board data except a permitted relocation.
    assert server.mutations is not None
    mutations = server.mutations.of(target.slug)
    assert mutations, "the recorder saw the scenario's writes"
    assert forbidden_removals(mutations, board) == []


def test_every_scenario_but_the_declared_gaps_runs_hosted() -> None:
    names = {s.name for s in SCENARIOS}
    assert set(NOT_HOSTED) <= names
    assert {s.name for s, _ in CASES} == names - set(NOT_HOSTED)


def test_actorless_commands_are_the_no_actor_operations() -> None:
    """The replay's token choice (``HostedTarget.step_env``) names exactly the
    operations that take no actor."""
    from lattice.ops.base import registered_operations

    no_actor = {
        name
        for name, cls in registered_operations().items()
        if getattr(cls, "no_actor", False) and not name.startswith("xtest.")
    }
    assert set(ACTORLESS_COMMANDS.values()) == no_actor


def test_the_no_delete_check_catches_an_unpermitted_removal(tmp_path: Path) -> None:
    """The G-2 assertion is live: an unlink outside a relocation is reported, a
    copy-first relocation and a test fixture are not."""
    removed = Mutation("p", "task.comment", "plans/task_x.md", "unlink")
    moved = [
        Mutation("p", "task.archive", "archive/tasks/t.json", "create"),
        Mutation("p", "task.archive", "tasks/t.json", "unlink"),
    ]
    half_moved = [Mutation("p", "task.archive", "tasks/u.json", "unlink")]
    fixture = Mutation("p", FIXTURE_OP, "plans/task_y.md", "unlink")
    runtime = Mutation("p", "task.comment", "locks/x.lock", "unlink")
    found = forbidden_removals([removed, *moved, *half_moved, fixture, runtime], tmp_path)
    assert found == [removed, half_moved[0]]


def test_archive_loop_with_concurrent_client_reads(server: ParityServer, tmp_path: Path) -> None:
    """AC-9 (H-12 part): archive and unarchive through the real CLI while another
    thread reads through the same checkout: no read error, and every read sees the
    task in exactly one placement."""
    from lattice.cli.main import cli
    from tests.parity.corpus import Scenario as S

    target = hosted_target(server, S("archive_loop", "", ()))
    checkout = tmp_path / "board"
    checkout.mkdir()
    env = _base_env(checkout)
    env.update(target.env(checkout))
    target.setup(S("archive_loop", "", ()), checkout, None)

    def run(*args: str) -> tuple[int, str, str]:
        with _chdir(checkout), _process_env(env):
            result = _runner().invoke(cli, list(args), env=env, catch_exceptions=False)
        return result.exit_code, result.stdout, result.stderr

    code, out, err = run("create", "Looping", "--actor", "human:parity", "--json")
    assert code == 0, err
    task_id = json.loads(out)["data"]["id"]

    # The CliRunner swaps process-wide streams and cwd, so the concurrent reader
    # is a second process: a real `lattice` subprocess, reading as a user would.
    import subprocess
    import sys

    lattice = str(Path(sys.executable).with_name("lattice"))
    stop = threading.Event()
    failures: list[str] = []
    reads = 0

    def reader() -> None:
        nonlocal reads
        child_env = {k: v for k, v in {**os.environ, **env}.items() if v is not None}
        while not stop.is_set():
            active = subprocess.run(
                [lattice, "list", "--include-archived", "--json"],
                cwd=checkout,
                env=child_env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            archived = subprocess.run(
                [lattice, "show", task_id, "--json"],
                cwd=checkout,
                env=child_env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            reads += 1
            if active.returncode != 0 or archived.returncode != 0:
                failures.append(active.stderr + archived.stderr)
                continue
            listed = [t["id"] for t in json.loads(active.stdout)["data"]]
            shown = json.loads(archived.stdout)["data"]
            if listed.count(task_id) != 1 or shown.get("id") != task_id:
                failures.append(f"inconsistent read: {listed} / {shown.get('id')}")

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        # Keep moving the task until the reader has overlapped several cycles.
        for cycle in range(30):
            assert run("archive", task_id, "--actor", "human:parity")[0] == 0
            assert run("unarchive", task_id, "--actor", "human:parity")[0] == 0
            if cycle >= 3 and reads >= 3:
                break
    finally:
        stop.set()
        thread.join(timeout=60)
    assert reads >= 3
    assert failures == []
    assert durable_tree(checkout / ".lattice") == durable_tree(server.board(target.slug))
