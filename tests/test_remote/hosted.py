"""End-to-end fixtures for hosted checkouts: a real server, a bound git clone,
and the CLI run in-process from any directory.

``hosted_env`` starts one in-process server (``127.0.0.1:0``) holding project
``demo`` (code ``DEM``), mints a token for ``human:alice``, and configures
remote ``team`` in a per-test ``remotes.json`` whose token comes from
``LATTICE_TOKEN_TEAM``. ``retry_seconds`` is 1 so a stopped server never
stalls the suite (EVALUATION §1).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result

from lattice.server import tokens
from lattice.server.testing import ServerHandle, make_root, running_server

REMOTE = "team"
PROJECT = "demo"
TOKEN_ENV = "LATTICE_TOKEN_TEAM"
FAKE_LATTICE = "/nonexistent/fake-lattice"


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True, timeout=30
    )
    return proc.stdout.strip()


def make_repo(path: Path) -> Path:
    """A git repository with one commit on ``main``."""
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("repo\n")
    git(path, "add", "README.md")
    git(path, "commit", "-q", "-m", "init")
    return path


def add_worktree(repo: Path, path: Path, branch: str, *, relative: bool = False) -> Path:
    """A linked worktree of *repo* on a new *branch*; with *relative*, its ``.git``
    file names the gitdir with a relative path (``git worktree add --relative-paths``
    where available, else rewritten)."""
    git(repo, "worktree", "add", "-q", "-b", branch, str(path))
    if relative:
        dotgit = path / ".git"
        gitdir = Path(dotgit.read_text().split(":", 1)[1].strip())
        dotgit.write_text(f"gitdir: {os.path.relpath(gitdir, path)}\n")
    return path


def run_cli(cwd: Path, *args: str, input: str | None = None, color: bool = False) -> Result:
    """``lattice <args>`` in-process, as if started in *cwd*. With *color*, click
    keeps escape sequences as it would for a terminal."""
    from lattice.cli.main import cli

    previous = Path.cwd()
    os.chdir(cwd)
    try:
        return CliRunner().invoke(
            cli, list(args), input=input, catch_exceptions=False, color=color
        )
    finally:
        os.chdir(previous)


def tree_hash(root: Path) -> dict[str, str]:
    """Every file under *root* (names, modes, bytes), for "nothing changed" checks."""
    found: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            path = Path(dirpath) / name
            rel = path.relative_to(root).as_posix()
            if rel.startswith(".git/"):
                continue
            found[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return found


@dataclass
class HostedEnv:
    tmp: Path
    server_root: Path
    token: str
    monkeypatch: pytest.MonkeyPatch
    handle: ServerHandle | None = None
    _stack: ExitStack = field(default_factory=ExitStack)
    settings: dict[str, Any] = field(default_factory=dict)

    @property
    def url(self) -> str:
        assert self.handle is not None
        return self.handle.url

    @property
    def board(self) -> Path:
        return self.server_root / "projects" / PROJECT / ".lattice"

    def start(self) -> None:
        self.handle = self._stack.enter_context(running_server(self.server_root))
        self.write_remote()

    def stop(self) -> None:
        self._stack.close()
        self._stack = ExitStack()

    @contextmanager
    def stopped(self) -> Iterator[None]:
        """Stop the server for the block, then start it again on a new port."""
        self.stop()
        try:
            yield
        finally:
            self.start()

    def write_remote(self, **settings: Any) -> Path:
        """(Re)write ``remotes.json`` for ``team`` with *settings* merged in."""
        self.settings.update(settings)
        path = self.tmp / "config" / "lattice" / "remotes.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"url": self.url, "token": {"env": TOKEN_ENV}, "retry_seconds": 1}
        entry.update(self.settings)
        path.write_text(json.dumps({"remotes": {REMOTE: entry}}, indent=2) + "\n")
        path.chmod(0o600)
        return path

    def server_op(self, op: str, params: dict | None = None, **envelope: Any) -> dict:
        assert self.handle is not None
        status, _, body = self.handle.op(PROJECT, op, params, token=self.token, **envelope)
        assert status == 200, body
        return body["data"]

    def bind(self, repo: Path) -> Path:
        """Commit-free binding of *repo* (as a teammate's clone after a pull)."""
        (repo / ".lattice-remote.json").write_text(
            json.dumps({"project": PROJECT, "remote": REMOTE}) + "\n"
        )
        return repo


@pytest.fixture()
def hosted_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[HostedEnv]:
    from lattice.remote import session

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    root = make_root(tmp_path, projects={PROJECT: {"code": "DEM"}})
    minted = tokens.create_token(root, user="human:alice", machine="laptop", projects=[PROJECT])
    monkeypatch.setenv(TOKEN_ENV, minted["token"])
    env = HostedEnv(tmp=tmp_path, server_root=root, token=minted["token"], monkeypatch=monkeypatch)
    env.start()
    session.reset_process_state()
    try:
        yield env
    finally:
        session.reset_process_state()
        env.stop()


def chmod_writable(root: Path) -> None:
    """Undo a cache's read-only modes so pytest can delete ``tmp_path``."""
    for dirpath, dirnames, _ in os.walk(root):
        for name in dirnames:
            with_path = Path(dirpath) / name
            if not with_path.is_symlink():
                with_path.chmod(0o700)


@dataclass
class SpawnRecorder:
    """Stands in for the detached ``lattice code-review`` / ``plan-review`` spawn."""

    real_popen: Any = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, cmd: list[str], **kwargs: Any) -> Any:
        # ``auto_review.subprocess`` is the subprocess module itself: pass
        # everything but the review spawn through.
        if not (isinstance(cmd, list) and cmd and cmd[0] == FAKE_LATTICE):
            return self.real_popen(cmd, **kwargs)
        self.calls.append({"cmd": list(cmd), **kwargs})

        class _Proc:
            pid = 40000 + len(self.calls)

        return _Proc()

    @property
    def review_types(self) -> list[str]:
        return [c["cmd"][1] for c in self.calls]


@pytest.fixture()
def spawns(monkeypatch: pytest.MonkeyPatch) -> SpawnRecorder:
    from lattice.cli import auto_review

    recorder = SpawnRecorder(real_popen=auto_review.subprocess.Popen)
    monkeypatch.setattr(auto_review.subprocess, "Popen", recorder)
    monkeypatch.setattr(auto_review, "find_lattice_executable", lambda: FAKE_LATTICE)
    return recorder


def walk_to(cwd: Path, task: str, *statuses: str, actor: str = "agent:dev") -> None:
    """Move *task* through *statuses*, writing a plan before ``planned``."""
    for status in statuses:
        if status == "planned":
            plan = cwd / f".plan-{task}.md"
            plan.write_text(f"# {task}\n\n## Approach\n\n- Do the work.\n")
            result = run_cli(cwd, "plan", "write", task, "--file", str(plan), "--actor", actor)
            assert result.exit_code == 0, result.output
            plan.unlink()
        result = run_cli(cwd, "status", task, status, "--actor", actor, "--json")
        assert result.exit_code == 0, result.output


def events_of(env: HostedEnv, short_id: str) -> list[dict]:
    """The server board's events for *short_id* (read from the server's files)."""
    ids = json.loads((env.board / "ids.json").read_text())
    task_id = ids["map"][short_id]
    lines = (env.board / "events" / f"{task_id}.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]
