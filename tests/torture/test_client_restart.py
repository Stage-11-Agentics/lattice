"""AC-46 (H-22): kill the server right after a commit and restart it within
10 s; the CLI call that was waiting on it succeeds, and the write is applied
exactly once (SPEC §8.6 "Client retries", §8.7).

A real ``serve`` process pauses right after the journal fsync (the launcher's
``LATTICE_TEST_PAUSE_AFTER_COMMIT`` seam) and is killed with SIGKILL there:
the operation is committed on disk, its undo log and in-memory finish are
not done, and the client has no answer. The restarted server's startup
recovery settles the undo log and rebuilds the idempotency index from the
receipt, so the client's retry with the same ``op_id`` is replayed.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from lattice.remote import acked, session
from lattice.server import tokens
from lattice.server.testing import make_root, wait_for
from tests.test_remote.hosted import REMOTE, TOKEN_ENV, make_repo, run_cli
from tests.torture.processes import start_server, stop

pytestmark = pytest.mark.torture

PROJECT = "demo"


def _journal(board: Path) -> list[dict]:
    raw = (board / "hosted" / "journal.jsonl").read_text()
    return [json.loads(line) for line in raw.splitlines()]


@pytest.mark.timeout(90)
def test_a_server_killed_after_a_commit_and_restarted_applies_the_write_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_root(tmp_path, projects={PROJECT: {"code": "DEM"}})
    board = root / "projects" / PROJECT / ".lattice"
    minted = tokens.create_token(root, user="human:alice", machine="laptop", projects=[PROJECT])
    monkeypatch.setenv(TOKEN_ENV, minted["token"])
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    pause = tmp_path / "pause-after-commit"
    first, port = start_server(root, env={"LATTICE_TEST_PAUSE_AFTER_COMMIT": str(pause)})
    remotes = tmp_path / "config" / "lattice" / "remotes.json"
    remotes.parent.mkdir(parents=True)
    entry = {"url": f"http://127.0.0.1:{port}", "token": {"env": TOKEN_ENV}, "retry_seconds": 30}
    remotes.write_text(json.dumps({"remotes": {REMOTE: entry}}))
    remotes.chmod(0o600)
    session.reset_process_state()
    second = None
    try:
        repo = make_repo(tmp_path / "repo")
        assert run_cli(repo, "remote", "attach", REMOTE, PROJECT).exit_code == 0
        assert run_cli(repo, "create", "Warm up", "--actor", "agent:dev").exit_code == 0

        pause.touch()  # the next write stops right after its commit point
        outcome: dict = {}

        def write() -> None:
            outcome["result"] = run_cli(
                repo, "create", "Survives", "--actor", "agent:dev", "--json"
            )

        writer = threading.Thread(target=write)
        writer.start()
        assert wait_for(lambda: len(_journal(board)) == 2, timeout=15)  # committed on disk
        assert list((board / "hosted" / "undo").glob("*.jsonl"))  # finish never ran
        first.kill()  # SIGKILL at the commit point
        first.communicate(timeout=10)
        killed_at = time.monotonic()
        pause.unlink()
        second, _ = start_server(root, port)
        assert time.monotonic() - killed_at < 10.0  # restarted within 10 s
        writer.join(timeout=60)
        assert not writer.is_alive()
    finally:
        stop(first)
        if second is not None:
            stop(second)
        session.reset_process_state()

    result = outcome["result"]
    assert result.exit_code == 0, result.output
    task = json.loads(result.stdout)["data"]
    assert task["title"] == "Survives" and task["short_id"] == "DEM-2"
    lines = _journal(board)
    creates = [x for x in lines if x["op"] == "task.create"]
    assert len(creates) == 2  # the warm-up and this one: applied exactly once
    assert not list((board / "hosted" / "undo").glob("*.jsonl"))
    acks = acked.read(repo / ".lattice" / "cache")
    assert acks[-1]["op_id"] == creates[-1]["op_id"]
