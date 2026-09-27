"""AC-1 (H-15 row): 8 processes with 8 distinct actors race ``next --claim`` over
8 ready tasks. Each claims a distinct task; none is claimed twice.

Hosted: 8 CLI processes in one bound checkout against a server subprocess.
Local: the same race on a local board, where the board's lock serializes them.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from lattice.server.testing import make_root
from tests.torture.harness import (
    PROJECT,
    Client,
    ServerProcess,
    base_env,
    board_events,
    bound_checkout,
    chmod_tree_writable,
    lattice,
    make_ready,
    track,
)

pytestmark = [pytest.mark.torture, pytest.mark.timeout(240)]

RACERS = 8

#: A racer: import everything, say "ready", block on one byte from the shared
#: barrier pipe (stdin), then run ``lattice next --claim``. It reports when it was
#: released and when it finished on stderr, so the test can prove the claims
#: overlapped.
RACER_SHIM = (
    "import atexit, os, sys, time\n"
    "from lattice.cli.main import cli\n"
    "sys.stderr.write('ready\\n'); sys.stderr.flush()\n"
    "assert os.read(0, 1) == b'g'\n"
    "sys.stderr.write(f'go {time.time()}\\n'); sys.stderr.flush()\n"
    "atexit.register(lambda: sys.stderr.write(f'done {time.time()}\\n'))\n"
    "cli(prog_name='lattice')\n"
)


def _race(env: dict[str, str], cwd: Path) -> list[dict]:
    """Start every racer, wait until all eight are ready, release them with one
    write to the barrier pipe, and return each one's claimed task."""
    barrier_r, barrier_w = os.pipe()
    procs: list[subprocess.Popen] = []
    try:
        for n in range(RACERS):
            procs.append(
                track(
                    subprocess.Popen(
                        [
                            sys.executable,
                            "-c",
                            RACER_SHIM,
                            "next",
                            "--claim",
                            "--actor",
                            f"agent:racer-{n}",
                            "--json",
                        ],
                        cwd=cwd,
                        env=env,
                        stdin=barrier_r,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                )
            )
        os.close(barrier_r)
        barrier_r = -1
        for n, proc in enumerate(procs):
            assert proc.stderr is not None
            line = proc.stderr.readline()
            assert line == "ready\n", f"racer {n} never got ready: {line!r}"
        os.write(barrier_w, b"g" * RACERS)  # the barrier: one write releases all eight
        claimed, released, finished = [], [], []
        for n, proc in enumerate(procs):
            out, err = proc.communicate(timeout=180)
            assert proc.returncode == 0, f"racer {n}: {out}{err}"
            stamps = dict(line.split(" ", 1) for line in err.splitlines() if " " in line)
            released.append(float(stamps["go"]))
            finished.append(float(stamps["done"]))
            data = json.loads(out)["data"]
            assert data is not None, f"racer {n} found nothing to claim"
            claimed.append(data)
    finally:
        os.close(barrier_w)
        if barrier_r >= 0:
            os.close(barrier_r)
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
    # Every racer was released before the first one finished: the claims overlapped.
    assert max(released) < min(finished), (released, finished)
    return claimed


def _assert_distinct(claimed: list[dict], events: list[dict]) -> None:
    ids = [task["id"] for task in claimed]
    assert len(set(ids)) == RACERS, ids
    for n, task in enumerate(claimed):
        assert task["assigned_to"] == f"agent:racer-{n}"
        assert task["status"] == "in_progress"
    # On the board: each task assigned once and started once.
    for task_id in ids:
        mine = [e for e in events if e["task_id"] == task_id]
        assigned = [e for e in mine if e["type"] == "assignment_changed"]
        started = [
            e for e in mine if e["type"] == "status_changed" and e["data"]["to"] == "in_progress"
        ]
        assert len(assigned) == 1 and len(started) == 1, mine


def test_next_claim_race_hosted(tmp_path: Path) -> None:
    server = ServerProcess(make_root(tmp_path, projects={PROJECT: {"code": "DEM"}}))
    server.start()
    try:
        client: Client = server.client(tmp_path / "alice")
        repo = bound_checkout(client, tmp_path / "repo")
        for n in range(RACERS):
            lattice(client, repo, "create", f"Ready {n}", "--actor", "agent:setup")
            make_ready(client, repo, f"DEM-{n + 1}")
        claimed = _race(client.env, repo)
        _assert_distinct(claimed, board_events(server.board()))
        # Nothing left to claim, and the cache agrees with the server.
        after = json.loads(lattice(client, repo, "next", "--claim", "--json").stdout)
        assert after["data"] is None
        assert json.loads(lattice(client, repo, "doctor", "--json").stdout)["ok"]
    finally:
        server.stop()
        chmod_tree_writable(tmp_path)


def test_next_claim_race_local(tmp_path: Path) -> None:
    board = tmp_path / "local"
    board.mkdir()
    env = base_env()
    env["HOME"] = str(tmp_path)
    client = Client("local", env, "human:alice", "laptop", None, "")
    lattice(client, board, "init", "--project-code", "LOC", "--actor", "human:alice")
    for n in range(RACERS):
        lattice(client, board, "create", f"Ready {n}", "--actor", "agent:setup")
        make_ready(client, board, f"LOC-{n + 1}")
    claimed = _race(env, board)
    _assert_distinct(claimed, board_events(board / ".lattice"))
