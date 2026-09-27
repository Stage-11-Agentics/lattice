"""AC-1 (H-15 row): 8 processes with 8 distinct actors race ``next --claim`` over
8 ready tasks. Each claims a distinct task; none is claimed twice.

Hosted: 8 CLI processes in one bound checkout against a server subprocess.
Local: the same race on a local board, where the board's lock serializes them.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from lattice.server.testing import make_root
from tests.torture.harness import (
    CLI_SHIM,
    PROJECT,
    Client,
    ServerProcess,
    base_env,
    board_events,
    bound_checkout,
    chmod_tree_writable,
    lattice,
    make_ready,
)

pytestmark = [pytest.mark.torture, pytest.mark.timeout(240)]

RACERS = 8


def _race(env: dict[str, str], cwd: Path) -> list[dict]:
    """Start every racer before waiting on any; return each one's claimed task."""
    procs = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                CLI_SHIM,
                "next",
                "--claim",
                "--actor",
                f"agent:racer-{n}",
                "--json",
            ],
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for n in range(RACERS)
    ]
    claimed = []
    for n, proc in enumerate(procs):
        out, err = proc.communicate(timeout=180)
        assert proc.returncode == 0, f"racer {n}: {out}{err}"
        data = json.loads(out)["data"]
        assert data is not None, f"racer {n} found nothing to claim"
        claimed.append(data)
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
