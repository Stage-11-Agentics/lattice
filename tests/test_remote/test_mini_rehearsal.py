"""AC-40 mini rehearsal (default suite): one in-process server, a bound repo with
two linked worktrees, two scripted writers alternating 50 writes. Every write
is visible from the other worktree on its next command, and doctor is clean on
the server and on the cache. H-15 grows this into Scenario W (``test_w``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_remote.hosted import HostedEnv, add_worktree, git, make_repo, run_cli

WRITES = 50
TASKS = 10


def _show(cwd: Path, short_id: str) -> dict:
    result = run_cli(cwd, "show", short_id, "--json")
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)["data"]


def test_two_worktrees_alternate_fifty_writes(
    hosted_env: HostedEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(tmp_path / "repo")
    assert run_cli(repo, "remote", "attach", "team", "demo").exit_code == 0
    git(repo, "add", ".lattice-remote.json", ".gitignore")
    git(repo, "commit", "-q", "-m", "bind the board")
    writers = [
        (add_worktree(repo, tmp_path / "wt-a", "feat-a"), "agent:writer-a"),
        (add_worktree(repo, tmp_path / "wt-b", "feat-b", relative=True), "agent:writer-b"),
    ]
    comments: dict[str, int] = {}
    for n in range(WRITES):
        (cwd, actor), (other, _) = writers[n % 2], writers[(n + 1) % 2]
        if n < TASKS:
            short_id = f"DEM-{n + 1}"
            result = run_cli(cwd, "create", f"Task {n}", "--actor", actor)
            assert result.exit_code == 0, result.output
            assert _show(other, short_id)["title"] == f"Task {n}"
        elif n % 5 == 0:
            short_id = f"DEM-{n // 5 % TASKS + 1}"  # a different task each time
            assert _show(cwd, short_id)["status"] == "backlog"
            result = run_cli(cwd, "status", short_id, "in_planning", "--actor", actor)
            assert result.exit_code == 0, result.output
            assert _show(other, short_id)["status"] == "in_planning"
        else:
            short_id = f"DEM-{n % TASKS + 1}"
            result = run_cli(cwd, "comment", short_id, f"write {n}", "--actor", actor)
            assert result.exit_code == 0, result.output
            comments[short_id] = comments.get(short_id, 0) + 1
            assert _show(other, short_id)["comment_count"] == comments[short_id]

    # One cache, in the primary checkout; nothing about the board in git.
    for cwd, _ in writers:
        assert not (cwd / ".lattice").exists()
        assert git(cwd, "status", "--porcelain") == ""
    listed = json.loads(run_cli(writers[1][0], "list", "--json").stdout)["data"]
    assert len(listed) == TASKS

    doctor = run_cli(writers[0][0], "doctor", "--json")
    assert doctor.exit_code == 0, doctor.output
    cache_report = json.loads(doctor.stdout)["data"]
    assert not [f for f in cache_report["findings"] if f["level"] == "error"], cache_report

    hosted_env.stop()
    server_project = hosted_env.board.parent
    monkeypatch.setenv("LATTICE_ROOT", str(server_project))
    server_doctor = run_cli(tmp_path, "doctor", "--json")
    assert server_doctor.exit_code == 0, server_doctor.output
    server_report = json.loads(server_doctor.stdout)["data"]
    assert not [f for f in server_report["findings"] if f["level"] == "error"], server_report
