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

The replays run in three files of about equal time (``HOSTED_GROUPS``), sharing
one server per worker (``conftest.py``); this one holds group 1 and the checks
on the harness itself. A hosted divergence is a bug in the product, never in
the golden.
"""

from __future__ import annotations

import json
import mimetypes
import os
import subprocess
import sys
from pathlib import Path

import pytest

from lattice.server.testing import wait_for
from tests.parity.corpus import SCENARIOS, Scenario
from tests.parity.hosted import (
    ACTORLESS_COMMANDS,
    FIXTURE_OP,
    HOSTED_GROUPS,
    NOT_HOSTED,
    Mutation,
    ParityServer,
    check_scenario_through_the_server,
    durable_tree,
    forbidden_removals,
    hosted_cases,
    hosted_target,
)
from tests.parity.record import (
    _base_env,
    _chdir,
    _process_env,
    _runner,
    dump,
    load_golden,
    run_scenario,
)


@pytest.mark.parametrize(("scenario", "mode"), hosted_cases(0))
def test_scenario_matches_golden_through_the_server(
    scenario: Scenario, mode: str, server: ParityServer, tmp_path: Path
) -> None:
    check_scenario_through_the_server(scenario, mode, server, tmp_path)


def test_every_scenario_but_the_declared_gaps_runs_hosted_once() -> None:
    names = {s.name for s in SCENARIOS}
    assert set(NOT_HOSTED) <= names
    grouped = [name for group in HOSTED_GROUPS for name in group]
    assert len(grouped) == len(set(grouped))
    assert set(grouped) == names - set(NOT_HOSTED)


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
    copy-first relocation in the same transaction and a test fixture are not, and a
    copy an earlier transaction wrote never excuses a later removal."""
    removed = Mutation("p", "task.comment", "plans/task_x.md", "unlink", 1)
    moved = [
        Mutation("p", "task.archive", "archive/tasks/t.json", "create", 2),
        Mutation("p", "task.archive", "tasks/t.json", "unlink", 2),
    ]
    half_moved = [Mutation("p", "task.archive", "tasks/u.json", "unlink", 3)]
    fixture = Mutation("p", FIXTURE_OP, "plans/task_y.md", "unlink", 4)
    runtime = Mutation("p", "task.comment", "locks/x.lock", "unlink", 5)
    found = forbidden_removals([removed, *moved, *half_moved, fixture, runtime], tmp_path)
    assert found == [removed, half_moved[0]]

    # archive (copy + unlink), unarchive (copy + unlink), then an archive that only
    # unlinks: the first archive's copy is an earlier transaction's.
    cycle = [
        Mutation("p", "task.archive", "archive/tasks/v.json", "create", 10),
        Mutation("p", "task.archive", "tasks/v.json", "unlink", 10),
        Mutation("p", "task.unarchive", "tasks/v.json", "create", 11),
        Mutation("p", "task.unarchive", "archive/tasks/v.json", "unlink", 11),
        Mutation("p", "task.archive", "tasks/v.json", "unlink", 12),
    ]
    assert forbidden_removals(cycle, tmp_path) == [cycle[-1]]


def test_archive_loop_with_concurrent_client_reads(server: ParityServer, tmp_path: Path) -> None:
    """AC-9 (H-12 part): archive and unarchive through the real CLI while another
    process reads through the same checkout: no read error, and every read sees the
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

    # The CliRunner swaps process-wide streams and cwd, so the concurrent reader is
    # a second process: one Python running the real CLI in a loop, one JSON line per
    # read, until the stop file appears.
    stop = tmp_path / "stop"
    reads_file = tmp_path / "reads.jsonl"
    child_env = {k: v for k, v in {**os.environ, **env}.items() if v is not None}
    reader = subprocess.Popen(
        [sys.executable, "-c", READER, task_id, str(stop), str(reads_file)],
        cwd=checkout,
        env=child_env,
    )

    def reads() -> list[dict]:
        if not reads_file.exists():
            return []
        return [json.loads(line) for line in reads_file.read_text().splitlines()[:-1]]

    try:
        assert wait_for(lambda: len(reads()) >= 1 or reader.poll() is not None, 30)
        # Keep moving the task until the reader has overlapped several cycles.
        start = len(reads())
        for cycle in range(30):
            assert run("archive", task_id, "--actor", "human:parity")[0] == 0
            assert run("unarchive", task_id, "--actor", "human:parity")[0] == 0
            if cycle >= 2 and len(reads()) - start >= 3:
                break
    finally:
        stop.touch()
        reader.wait(timeout=60)
    done = reads()
    assert len(done) - start >= 3
    failures = [
        r
        for r in done
        if r["codes"] != [0, 0] or r["listed"].count(task_id) != 1 or r["shown"] != task_id
    ]
    assert failures == []
    assert durable_tree(checkout / ".lattice") == durable_tree(server.board(target.slug))


@pytest.fixture
def foreign_mimetypes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The live ``mimetypes`` tables of another interpreter and host: CPython
    3.12.0's built-in table (no ``.md``, ``.markdown`` or ``.rst``) extended by a host
    file that maps ``.md`` elsewhere. Before LAT-356, attach stored whatever these
    said, so the same attach stored different bytes on different machines."""
    host = tmp_path / "mime.types"
    host.write_text("text/x-host-markdown md markdown\n")
    builtin = {
        k: v
        for k, v in mimetypes._types_map_default.items()  # type: ignore[attr-defined]
        if k not in (".md", ".markdown", ".rst")
    }
    # mimetypes.init() rebinds these module globals; monkeypatch restores them.
    for name in ("types_map", "suffix_map", "encodings_map", "common_types"):
        monkeypatch.setattr(mimetypes, name, getattr(mimetypes, name))
    monkeypatch.setattr(mimetypes, "_types_map_default", builtin)
    monkeypatch.setattr(mimetypes, "knownfiles", [str(host)])
    monkeypatch.setattr(mimetypes, "_db", None)
    monkeypatch.setattr(mimetypes, "inited", False)
    mimetypes.init()
    assert mimetypes.guess_type("report.md")[0] == "text/x-host-markdown"


@pytest.mark.usefixtures("foreign_mimetypes")
def test_attach_stores_the_same_content_type_whatever_the_mimetypes_tables(
    server: ParityServer, tmp_path: Path
) -> None:
    """LAT-356: served output equals local output, and both equal the golden, under
    the tables that made the served corpus drift on a 3.12.0 host."""
    (artifacts,) = [s for s in SCENARIOS if s.name == "artifacts"]
    local = run_scenario(artifacts, tmp_path / "local", mode="plain")
    assert dump(local) == dump(load_golden("artifacts", "plain"))
    check_scenario_through_the_server(artifacts, "plain", server, tmp_path / "hosted")


READER = """
import json, os, sys
from click.testing import CliRunner
from lattice.cli.main import cli

task_id, stop, out = sys.argv[1:4]
runner = CliRunner()
with open(out, "w") as fh:
    while not os.path.exists(stop):
        listed = runner.invoke(cli, ["list", "--include-archived", "--json"])
        shown = runner.invoke(cli, ["show", task_id, "--json"])
        record = {"codes": [listed.exit_code, shown.exit_code], "listed": [], "shown": None}
        try:
            record["listed"] = [t["id"] for t in json.loads(listed.stdout)["data"]]
            record["shown"] = json.loads(shown.stdout)["data"]["id"]
        except (ValueError, KeyError, TypeError):
            record["error"] = listed.stdout[-300:] + shown.stdout[-300:]
        fh.write(json.dumps(record) + "\\n")
        fh.flush()
    fh.write("{}\\n")
"""
