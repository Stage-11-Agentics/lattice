"""AC-36 (local): every event an operation writes carries its origin.

``worktree`` and ``branch`` come from the operation's starting directory,
read from git's files; no ``git`` subprocess runs in a normal checkout.
"""

from __future__ import annotations

import json
import socket
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

import lattice
from lattice.boards import git_branch, reported_origin, resolve_board
from lattice.cli.main import cli
from lattice.ops import Caller
from lattice.ops.base import OP_ID_RE


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "feat/PAR-12-auth")
    _git(
        path,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@e",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "i",
    )
    (path / "sub" / "dir").mkdir(parents=True)
    return path


@pytest.fixture()
def no_subprocess(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Fail any subprocess the code under test starts; record its argv."""
    calls: list[list[str]] = []

    class Refused:
        def __init__(self, args, *a, **kw):  # noqa: ANN001, ANN002, ANN003
            calls.append(list(args))
            raise AssertionError(f"unexpected subprocess: {args}")

    monkeypatch.setattr(subprocess, "Popen", Refused)
    return calls


def _events(board: Path) -> list[dict]:
    events = []
    for log in sorted((board / ".lattice" / "events").glob("task_*.jsonl")):
        events += [json.loads(line) for line in log.read_text().splitlines()]
    return events


def _run(cwd: Path, board: Path, *args: str, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN202
    monkeypatch.chdir(cwd)
    result = CliRunner().invoke(
        cli, [*args, "--actor", "agent:o"], env={"LATTICE_ROOT": str(board)}
    )
    assert result.exit_code == 0, result.output
    return result


def test_cli_operations_stamp_full_origin_without_git_subprocess(
    repo: Path,
    initialized_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_subprocess: list,
) -> None:
    start = repo / "sub" / "dir"
    _run(start, initialized_root, "create", "Origin task", monkeypatch=monkeypatch)
    task_id = _events(initialized_root)[0]["task_id"]
    _run(start, initialized_root, "status", task_id, "in_planning", monkeypatch=monkeypatch)
    _run(start, initialized_root, "comment", task_id, "hi", monkeypatch=monkeypatch)

    events = _events(initialized_root)
    assert [e["type"] for e in events] == [
        "task_created",
        "assignment_changed",
        "status_changed",
        "comment_added",
    ]
    expected = {
        "host": socket.gethostname(),
        "worktree": str(repo.resolve()),
        "branch": "feat/PAR-12-auth",
        "client_version": lattice.__version__,
    }
    for event in events:
        origin = event["origin"]
        assert OP_ID_RE.fullmatch(origin["op_id"])
        assert origin["reported"]["os_user"]
        assert {k: origin["reported"][k] for k in expected} == expected
        assert "authenticated" not in origin
    assert [e["origin"]["op"] for e in events] == [
        "task.create",
        "task.status",
        "task.status",
        "task.comment",
    ]
    # One op_id per operation call; the two events of one status share it.
    op_ids = [e["origin"]["op_id"] for e in events]
    assert op_ids[1] == op_ids[2]
    assert len({op_ids[0], op_ids[1], op_ids[3]}) == 3
    # The lifecycle log carries the same stamped task_created.
    lifecycle = (initialized_root / ".lattice" / "events" / "_lifecycle.jsonl").read_text()
    assert json.loads(lifecycle.splitlines()[0])["origin"] == events[0]["origin"]
    assert no_subprocess == []


def test_each_operation_reads_its_own_checkout(
    repo: Path, initialized_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q", "-b", "main")
    monkeypatch.setenv("LATTICE_ROOT", str(initialized_root))
    first = resolve_board(repo).execute("task.create", {"title": "a"}, Caller(actor="agent:o"))
    second = resolve_board(other).execute("task.create", {"title": "b"}, Caller(actor="agent:o"))
    _git(repo, "checkout", "-q", "-b", "feat/next")
    third = resolve_board(repo).execute("task.create", {"title": "c"}, Caller(actor="agent:o"))

    reported = [r.events[0]["origin"]["reported"] for r in (first, second, third)]
    assert [(r["worktree"], r["branch"]) for r in reported] == [
        (str(repo.resolve()), "feat/PAR-12-auth"),
        (str(other.resolve()), "main"),
        (str(repo.resolve()), "feat/next"),
    ]


def test_linked_worktree_reports_its_own_path_and_branch(
    repo: Path, tmp_path: Path, no_subprocess: list
) -> None:
    # The layout `git worktree add` creates, built by hand (subprocesses are refused).
    gitdir = repo / ".git" / "worktrees" / "linked"
    gitdir.mkdir(parents=True)
    (gitdir / "HEAD").write_text("ref: refs/heads/feat/linked\n")
    linked = tmp_path / "linked"
    (linked / "deep").mkdir(parents=True)
    (linked / ".git").write_text(f"gitdir: {gitdir}\n")

    fields = reported_origin(linked / "deep")
    assert fields["worktree"] == str(linked.resolve())
    assert fields["branch"] == "feat/linked"
    assert no_subprocess == []


def test_relative_gitdir(tmp_path: Path, no_subprocess: list) -> None:
    wt = tmp_path / "wt"
    (tmp_path / "main" / ".git" / "worktrees" / "wt").mkdir(parents=True)
    (tmp_path / "main" / ".git" / "worktrees" / "wt" / "HEAD").write_text("ref: refs/heads/rel\n")
    wt.mkdir()
    (wt / ".git").write_text("gitdir: ../main/.git/worktrees/wt\n")
    assert git_branch(wt) == "rel"
    assert no_subprocess == []


def test_detached_head_omits_branch(repo: Path, no_subprocess: list) -> None:
    sha = (repo / ".git" / "refs" / "heads" / "feat" / "PAR-12-auth").read_text().strip()
    (repo / ".git" / "HEAD").write_text(sha + "\n")
    fields = reported_origin(repo)
    assert fields["worktree"] == str(repo.resolve())
    assert "branch" not in fields
    assert no_subprocess == []


@pytest.mark.parametrize("head", ["ref: refs/heads/.invalid\n", "garbage\n", None])
def test_unreadable_head_falls_back_to_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, head: str | None
) -> None:
    wt = tmp_path / "wt"
    (wt / ".git").mkdir(parents=True)
    if head is not None:
        (wt / ".git" / "HEAD").write_text(head)
    calls: list = []

    def fake_run(args, **kwargs):  # noqa: ANN001, ANN003, ANN202
        calls.append((args, kwargs.get("cwd")))
        return subprocess.CompletedProcess(args, 0, stdout="from-git\n", stderr="")

    monkeypatch.setattr("lattice.boards.subprocess.run", fake_run)
    assert git_branch(wt) == "from-git"
    assert calls == [(["git", "rev-parse", "--abbrev-ref", "HEAD"], wt)]


def test_outside_git_omits_worktree_and_branch(tmp_path: Path, no_subprocess: list) -> None:
    fields = reported_origin(tmp_path)
    assert "worktree" not in fields and "branch" not in fields
    assert fields["host"] == socket.gethostname()


def test_resource_events_are_stamped(initialized_root: Path) -> None:
    from lattice.core.events import create_resource_event
    from lattice.core.origin import origin_scope
    from lattice.storage.operations import write_resource_event

    ld = initialized_root / ".lattice"
    event = create_resource_event(
        "resource_created", "res_01J9ZABCDEFGHJKMNPQRSTVWXY", "agent:o", {}
    )
    origin = {"op": "resource.create", "op_id": "op_01J9ZABCDEFGHJKMNPQRSTVWXY", "reported": {}}
    with origin_scope(origin):
        write_resource_event(
            ld, "res_01J9ZABCDEFGHJKMNPQRSTVWXY", "db", [event], {"id": "x"}, run_hooks=False
        )
    line = (ld / "events" / "res_01J9ZABCDEFGHJKMNPQRSTVWXY.jsonl").read_text()
    assert json.loads(line)["origin"] == origin


def test_writes_outside_an_operation_carry_no_origin(initialized_root: Path) -> None:
    from lattice.core.events import create_event
    from lattice.storage.operations import mutate_task_events

    ld = initialized_root / ".lattice"
    event = create_event(
        "task_created", "task_01J9ZABCDEFGHJKMNPQRSTVWXY", "agent:o", {"title": "t"}
    )
    mutate_task_events(
        ld,
        "task_01J9ZABCDEFGHJKMNPQRSTVWXY",
        [event],
        source="absent",
        may_emit_lifecycle=True,
        run_hooks=False,
    )
    line = (ld / "events" / "task_01J9ZABCDEFGHJKMNPQRSTVWXY.jsonl").read_text()
    assert "origin" not in json.loads(line)
