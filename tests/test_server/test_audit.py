"""Audit history (AC-26, SPEC §8.10): each project directory is a git repository
holding exactly the board's durable data, committed on a short debounce.

Hermetic: every repository and remote lives under ``tmp_path``; debounces are
fractions of a second.
"""

from __future__ import annotations

import json
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest
from click.testing import CliRunner

from lattice.cli.main import cli
from lattice.ops.base import CommonParams, OpContext, OpResult, operation
from lattice.server import admin, audit
from lattice.server.config import AuditConfig, ServerConfigError, parse_config
from lattice.server.testing import make_root, running_server, wait_for
from lattice.storage.fs import atomic_write
from tests.test_server.audit_helpers import (
    FAST,
    MESSAGE_RE,
    bare_remote,
    close,
    commits,
    direct_project,
    durable_files,
    git_out,
    head_tree,
    install_git_shim,
    last_committed_seq,
    log_lines,
    remote_head,
)
from tests.test_server.conftest import create_task, mint
from tests.test_server.faults import request, run

pytestmark = [
    pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed"),
    # Servers, git subprocesses, and waits: generous for slow CI runners.
    pytest.mark.timeout(60),
]


# ---------------------------------------------------------------------------
# A transaction that pauses between its two writes
# ---------------------------------------------------------------------------

PAUSED = threading.Event()
RESUME = threading.Event()


@dataclass(frozen=True, kw_only=True)
class PairParams(CommonParams):
    name: str = "pair"


@operation("xtest.audit_pair")
class WritePair:
    """Writes ``notes/<name>-a.md``, waits for :data:`RESUME`, then ``notes/<name>-b.md``."""

    Params = PairParams

    def run(self, ctx: OpContext, p: PairParams) -> OpResult:
        atomic_write(ctx.lattice_dir / "notes" / f"{p.name}-a.md", "a\n")
        PAUSED.set()
        assert RESUME.wait(10), "the test never resumed the transaction"
        atomic_write(ctx.lattice_dir / "notes" / f"{p.name}-b.md", "b\n")
        return OpResult(value={"name": p.name})


# ---------------------------------------------------------------------------
# The allowlist
# ---------------------------------------------------------------------------


def test_allowlist_admits_exactly_the_durable_and_workspace_paths(tmp_path: Path) -> None:
    directory = tmp_path / "proj"
    board = directory / ".lattice"
    files = {
        # durable and workspace: recorded
        "tasks/task_1.json": True,
        "events/_lifecycle.jsonl": True,
        "archive/events/task_2.jsonl": True,
        "plans/task_1.md": True,
        "plans/review-pack.md": True,
        "notes/task_1.md": True,
        "artifacts/meta/art_1.json": True,
        "resources/r/x.json": True,
        "sessions/archive/s.json": True,
        "templates/review.md": True,
        "issues/events/iss_01J9ZABCDEFGHJKMNPQRSTVWXY.jsonl": True,
        "issues/iss_01J9ZABCDEFGHJKMNPQRSTVWXY.json": True,
        "issues/media/iss_01J9ZABCDEFGHJKMNPQRSTVWXY/med.jpg": False,
        "issues/media/iss_01J9ZABCDEFGHJKMNPQRSTVWXY/.frames/med/1000.jpg": False,
        "orchestration/run/state.md": True,
        "config.json": True,
        "ids.json": True,
        "context.md": True,
        ".gitignore": True,
        # never recorded
        "hosted/journal.jsonl": False,
        "hosted/undo/x.jsonl": False,
        "hosted/audit.json": False,
        "locks/task_1.lock": False,
        "review_state/task_1.json": False,
        "tmp-prompts/p.md": False,
        ".daemon/log": False,
        "cache/state.json": False,
        "reviews/r.md": False,
        "runner.log": False,
        "tasks/.tmp.abc123": False,
    }
    for rel in files:
        path = board / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rel)
    (board / ".gitignore").write_text("locks/\n")  # the scaffold's style of ignore file
    (directory / "stray.txt").write_text("not board data")
    assert audit.init_repo(directory) is True
    tracked = set(git_out(directory, "ls-files").splitlines())
    assert tracked == {f".lattice/{rel}" for rel, keep in files.items() if keep}
    allowlist = audit.gitignore_text()
    assert "!/.lattice/issues/" in allowlist
    assert allowlist.index("!/.lattice/issues/") < allowlist.index("/.lattice/issues/media/")
    # The allowlist itself is not history: a commit's tree is the board alone.
    assert ".gitignore" not in tracked
    assert commits(directory) == ["audit: project created"]
    assert audit.init_repo(directory) is False  # idempotent


def test_project_create_makes_the_audit_repo(tmp_path: Path) -> None:
    root = make_root(tmp_path, projects={"alpha": {"code": "ALP"}})
    directory = root / "projects" / "alpha"
    assert (directory / ".git").is_dir()
    assert (directory / ".gitignore").read_text() == audit.gitignore_text()
    assert head_tree(directory) == durable_files(directory / ".lattice")
    author = git_out(directory, "log", "-1", "--format=%an <%ae>|%cn <%ce>").strip()
    signature = "Lattice Hosted <lattice-hosted@localhost>"
    assert author == f"{signature}|{signature}"
    assert not any(p.name.startswith(".creating-") for p in (root / "projects").iterdir())


# ---------------------------------------------------------------------------
# AC-26
# ---------------------------------------------------------------------------


def test_twenty_writes_commit_exactly_the_board_durable_paths(tmp_path: Path) -> None:
    root = make_root(tmp_path, projects={"alpha": {"code": "ALP"}}, config=FAST)
    directory = root / "projects" / "alpha"
    board = directory / ".lattice"
    (board / "reviews").mkdir()
    (board / "reviews" / "unmanaged.md").write_text("never in the history")
    token = mint(root)
    with running_server(root) as server:
        task = create_task(server, token, title="first")
        for i in range(19):
            status, _, body = server.op(
                "alpha",
                "task.comment",
                {"task": task["id"], "text": f"comment {i}"},
                token=token,
                actor="human:alice",
            )
            assert status == 200, body
        head = server.project("alpha").journal.head_seq
        assert head == 20
        assert wait_for(lambda: last_committed_seq(directory) == head, timeout=4)
        subjects = commits(directory)[1:]
        assert subjects and all(MESSAGE_RE.match(s) for s in subjects)
        assert sum(int(MESSAGE_RE.match(s).group(3)) for s in subjects) == 20
        # Quiescent (no write in flight): the tree is the board's durable files, byte for byte.
        tree = head_tree(directory)
        assert tree == durable_files(board)
        assert not any(k.startswith(("hosted/", "locks/", "reviews/")) for k in tree)
        assert any(k.startswith("events/task_") for k in tree)
        assert all(line["event"] != "audit_failed" for line in server.log_lines), server.log_lines


def test_staging_waits_for_a_paused_transaction(tmp_path: Path) -> None:
    """Staging that falls due mid-transaction waits for the work lock; no commit
    ever holds the transaction's first write without its second."""
    root = make_root(tmp_path, projects={"alpha": {"code": "ALP"}})
    directory = root / "projects" / "alpha"
    # Nothing falls due on its own: the test makes the commit due once the
    # transaction is paused.
    project, _ = direct_project(
        root, "alpha", AuditConfig(debounce_seconds=30, max_interval_seconds=30)
    )
    PAUSED.clear()
    RESUME.clear()
    try:
        run(project, request("task.create", {"title": "before"}))  # seq 1: a commit falls due
        outcome: dict = {}
        worker = threading.Thread(
            target=lambda: outcome.setdefault("r", run(project, request("xtest.audit_pair"))),
        )
        worker.start()
        assert PAUSED.wait(5)
        # Seq 1's commit falls due now: the committer wants to stage, but the paused
        # transaction holds the work lock.
        committer = project.committer
        with committer._cond:
            committer.config = AuditConfig(debounce_seconds=0.01, max_interval_seconds=0.01)
            committer._cond.notify_all()
        assert wait_for(lambda: project.committer.waiting_for_lock, timeout=4)
        assert commits(directory) == ["audit: project created"]
        assert git_out(directory, "ls-files", ".lattice/notes").strip() == ""
        RESUME.set()
        worker.join(10)
        assert outcome["r"].seq == 2
        assert wait_for(lambda: project.committer.commits >= 1, timeout=4)
        assert commits(directory)[1] == "audit: seq 1-2 (2 ops)"
        for rev in git_out(directory, "rev-list", "HEAD").split():
            tree = head_tree(directory, rev)
            assert ("notes/pair-a.md" in tree) == ("notes/pair-b.md" in tree), rev
        assert "notes/pair-b.md" in head_tree(directory)
    finally:
        RESUME.set()
        close(project)


def test_gc_auto_runs_after_each_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every commit, the project's first included, is followed by ``git gc --auto``."""
    shim = install_git_shim(tmp_path, monkeypatch)
    root = make_root(tmp_path, projects={"alpha": {"code": "ALP"}})
    directory = root / "projects" / "alpha"
    project, _ = direct_project(
        root, "alpha", AuditConfig(debounce_seconds=0.05, max_interval_seconds=1)
    )
    try:
        for n in (1, 2, 3):
            run(project, request("task.create", {"title": f"t{n}"}))
            assert wait_for(lambda n=n: project.committer.maintenance.gc_runs == n, timeout=4)
    finally:
        close(project)
    assert len(commits(directory)) == 4  # created + three audit commits
    # A commit is its update-ref; the sequence of commits and gc runs alternates,
    # starting with project create's first commit.
    events = [
        words[0] for words in shim.subcommands() if words and words[0] in ("update-ref", "gc")
    ]
    assert events == ["update-ref", "gc"] * 4, events
    gcs = [words for words in shim.subcommands() if words and words[0] == "gc"]
    assert all(words[:2] == ["gc", "--auto"] for words in gcs)


def test_push_to_a_local_bare_repo_and_an_unwritable_remote(tmp_path: Path) -> None:
    config = {"audit": {**FAST["audit"], "push": {"remote": "backup", "branch": "audit"}}}
    root = make_root(tmp_path, projects={"alpha": {"code": "ALP"}}, config=config)
    directory = root / "projects" / "alpha"
    bare = bare_remote(tmp_path, directory)
    token = mint(root)

    def local_head() -> str:
        return git_out(directory, "rev-parse", "HEAD").strip()

    with running_server(root) as server:
        create_task(server, token, title="one")
        assert wait_for(lambda: last_committed_seq(directory) == 1, timeout=4)
        assert wait_for(lambda: remote_head(bare) == local_head(), timeout=4)

        # The remote becomes unwritable: writes still succeed, a warning is logged.
        moved = tmp_path / "remote-away.git"
        bare.rename(moved)
        bare.write_text("not a repository")
        create_task(server, token, title="two")
        assert wait_for(
            lambda: any(line["event"] == "audit_push_failed" for line in server.log_lines),
            timeout=4,
        )
        warning = next(line for line in server.log_lines if line["event"] == "audit_push_failed")
        assert warning["level"] == "warning"
        assert warning["remote"] == "backup" and warning["branch"] == "audit"
        assert warning["error"].startswith("fatal:"), warning
        assert last_committed_seq(directory) == 2
        create_task(server, token, title="three")  # still accepted while the push fails

        # Back again: the next commit's push carries everything.
        bare.unlink()
        moved.rename(bare)
        create_task(server, token, title="four")
        assert wait_for(lambda: last_committed_seq(directory) == 4, timeout=4)
        assert wait_for(lambda: remote_head(bare) == local_head(), timeout=4)
        assert wait_for(
            lambda: any(line["event"] == "audit_push_recovered" for line in server.log_lines),
            timeout=4,
        )


def test_git_missing_disables_audit_with_one_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    root = make_root(tmp_path, projects={"alpha": {"code": "ALP"}}, config=FAST)
    directory = root / "projects" / "alpha"
    assert not (directory / ".git").exists()
    token = mint(root)
    with running_server(root) as server:
        _, _, body = server.request("GET", "/v1/info", token=token)
        assert body["data"]["audit"] == {
            "configured": True,
            "active": False,
            "reason": "git is not on PATH",
        }
        create_task(server, token)
        create_task(server, token)
        disabled = [line for line in server.log_lines if line["event"] == "audit_disabled"]
        assert len(disabled) == 1 and disabled[0]["level"] == "warning"
        assert server.project("alpha").committer is None
    assert not (directory / ".git").exists()


def test_audit_disabled_in_config(tmp_path: Path) -> None:
    root = make_root(tmp_path, projects={"alpha": {}}, config={"audit": {"enabled": False}})
    assert not (root / "projects" / "alpha" / ".git").exists()
    token = mint(root)
    with running_server(root) as server:
        _, _, body = server.request("GET", "/v1/info", token=token)
        assert body["data"]["audit"]["active"] is False
        assert body["data"]["audit"]["configured"] is False
        assert not any(
            line["event"] == "audit_disabled" and line["level"] == "warning"
            for line in server.log_lines
        )


def test_shutdown_makes_a_final_commit(tmp_path: Path) -> None:
    config = {"audit": {"debounce_seconds": 60, "max_interval_seconds": 60}}
    root = make_root(tmp_path, projects={"alpha": {"code": "ALP"}}, config=config)
    directory = root / "projects" / "alpha"
    token = mint(root)
    with running_server(root) as server:
        create_task(server, token)
        create_task(server, token)
        assert commits(directory) == ["audit: project created"]
    assert commits(directory)[-1] == "audit: seq 1-2 (2 ops)"
    final = [line for line in server.log_lines if line["event"] == "audit_commit"]
    assert [line["final"] for line in final] == [True]
    assert head_tree(directory) == durable_files(directory / ".lattice")


def test_load_creates_a_missing_repo(tmp_path: Path) -> None:
    root = make_root(tmp_path, projects={"alpha": {}}, config={"audit": {"enabled": False}})
    directory = root / "projects" / "alpha"
    assert not (directory / ".git").exists()
    project, stream = direct_project(root, "alpha", AuditConfig(debounce_seconds=0.05))
    try:
        assert (directory / ".git").is_dir()
        assert any(line["event"] == "audit_repo_created" for line in log_lines(stream))
    finally:
        close(project)


def test_quarantine_stops_the_committer_without_committing(tmp_path: Path) -> None:
    root = make_root(tmp_path, projects={"alpha": {}})
    directory = root / "projects" / "alpha"
    project, _ = direct_project(root, "alpha", AuditConfig(debounce_seconds=30))
    committer = project.committer
    with project.work:
        project._journaled(1)
        project._mark_unavailable("test quarantine")
    assert project.committer is None
    committer._thread.join(4)
    assert not committer._thread.is_alive()
    assert commits(directory) == ["audit: project created"]


# ---------------------------------------------------------------------------
# Config and ``lattice server project audit``
# ---------------------------------------------------------------------------


def test_audit_timings_accept_fractions_and_refuse_bad_numbers() -> None:
    config = parse_config({"audit": {"debounce_seconds": 0.25, "max_interval_seconds": 3}})
    assert config.audit.debounce_seconds == 0.25
    assert config.audit.max_interval_seconds == 3.0
    for bad in (-1, True, "5", float("nan"), float("inf")):
        with pytest.raises(ServerConfigError):
            parse_config({"audit": {"debounce_seconds": bad}})


def _cli(*args: str):
    return CliRunner().invoke(cli, list(args), catch_exceptions=False)


def test_project_audit_command_offline(tmp_path: Path) -> None:
    root = make_root(tmp_path, projects={"alpha": {}})
    directory = root / "projects" / "alpha"
    settings = directory / ".lattice" / "hosted" / "audit.json"
    base = ("server", "project", "audit", "alpha", "--root", str(root))

    # Rejections, plain and --json, write nothing.
    result = _cli(*base, "--push-remote", "backup", "--branch", "audit")
    assert result.exit_code != 0
    assert "No git remote 'backup'" in result.output
    result = _cli(*base, "--push-remote", "backup", "--branch", "audit", "--json")
    envelope = json.loads(result.output)
    assert envelope["ok"] is False and envelope["error"]["code"] == "VALIDATION_ERROR"
    result = _cli(*base, "--push-remote", "backup", "--json")
    assert json.loads(result.output)["error"]["code"] == "VALIDATION_ERROR"
    result = _cli(*base, "--no-push", "--branch", "x", "--json")
    assert json.loads(result.output)["error"]["code"] == "VALIDATION_ERROR"
    result = _cli(*base, "--push-remote", "-x", "--branch", "audit", "--json")
    assert json.loads(result.output)["error"]["code"] == "VALIDATION_ERROR"
    assert not settings.exists()
    result = _cli("server", "project", "audit", "nope", "--root", str(root), "--no-push")
    assert result.exit_code != 0 and "No project 'nope'" in result.output

    bare_remote(tmp_path, directory)
    result = _cli(*base, "--push-remote", "backup", "--branch", "audit")
    assert result.exit_code == 0, result.output
    assert "pushes to backup branch audit" in result.output
    assert json.loads(settings.read_text()) == {"push": {"remote": "backup", "branch": "audit"}}
    assert audit.push_target(directory / ".lattice", AuditConfig()) == {
        "remote": "backup",
        "branch": "audit",
    }
    result = _cli(*base, "--no-push", "--json")
    envelope = json.loads(result.output)
    assert envelope == {"ok": True, "data": {"via": "offline", "slug": "alpha", "push": None}}
    # null overrides server.json's audit.push
    assert (
        audit.push_target(directory / ".lattice", AuditConfig(push={"remote": "o", "branch": "b"}))
        is None
    )
    assert "hosted/" not in git_out(directory, "ls-files")


def test_project_audit_command_with_a_server_running(tmp_path: Path) -> None:
    root = make_root(tmp_path, projects={"alpha": {"code": "ALP"}}, config=FAST)
    directory = root / "projects" / "alpha"
    bare = bare_remote(tmp_path, directory)
    token = mint(root)
    base = ("server", "project", "audit", "alpha", "--root", str(root))
    with running_server(root) as server:
        result = _cli(*base, "--push-remote", "nope", "--branch", "audit", "--json")
        assert json.loads(result.output)["error"]["code"] == "VALIDATION_ERROR"
        result = _cli(*base, "--push-remote", "backup", "--branch", "audit", "--json")
        envelope = json.loads(result.output)
        assert envelope["ok"] is True and envelope["data"]["via"] == "server", envelope
        create_task(server, token)
        head = lambda: git_out(directory, "rev-parse", "HEAD").strip()  # noqa: E731
        assert wait_for(lambda: remote_head(bare) == head(), timeout=4)


def test_admin_create_reports_audit_state(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    data = admin.create_project(root, "gamma")
    assert data["audit"] == {"repo": True, "reason": None}
