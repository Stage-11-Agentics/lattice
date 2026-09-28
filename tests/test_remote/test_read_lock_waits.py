"""LAT-330, the Architect's condition (SPEC §9.4): a thread holds the cache
read lock only for the reads themselves. It releases it before starting or
waiting on another process and before any open-ended wait, so an apply gets
in while a command waits on a hook, a ``lattice wait`` whose condition only an
apply can deliver finishes, and git runs without the lock."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from lattice.remote import cache
from tests.test_remote.hosted import TOKEN_ENV, HostedEnv, make_repo, run_cli, walk_to

CLI = "from lattice.cli.main import cli; cli(prog_name='lattice')"

#: A board hook: once told to go, it runs ``lattice show`` on the same cache as a
#: child process (with the token the hook environment withholds), and records
#: its exit code, or ``timeout`` if the child is stuck for 15 s.
HOOK = """
import os, subprocess, sys, time
from pathlib import Path
signals = Path(sys.argv[1])
(signals / "started").touch()
deadline = time.monotonic() + 15
while not (signals / "go").exists() and time.monotonic() < deadline:
    time.sleep(0.01)
env = dict(os.environ, {token_env}=(signals / "token").read_text())
try:
    child = subprocess.run(
        [sys.executable, "-c", {cli!r}, "show", "DEM-1", "--json"],
        cwd=os.environ["LATTICE_ROOT"], env=env, capture_output=True, text=True, timeout=15,
    )
    (signals / "result").write_text(str(child.returncode))
except subprocess.TimeoutExpired:
    (signals / "result").write_text("timeout")
"""


class _Thread(threading.Thread):
    def __init__(self, target) -> None:  # noqa: ANN001
        super().__init__(daemon=True)
        self._fn = target
        self.result = None
        self.error: BaseException | None = None

    def run(self) -> None:
        try:
            self.result = self._fn()
        except BaseException as exc:  # noqa: BLE001 - surfaced by the test
            self.error = exc


def _wait_for(predicate, what: str, timeout: float = 30) -> None:  # noqa: ANN001
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.01)


def _attached(env: HostedEnv, tmp_path: Path) -> Path:
    repo = make_repo(tmp_path / "repo")
    assert run_cli(repo, "remote", "attach", "team", "demo").exit_code == 0
    assert run_cli(repo, "create", "Watched", "--actor", "agent:dev").exit_code == 0
    return repo


@pytest.mark.timeout(120)
def test_a_hook_child_reading_the_cache_while_an_apply_waits_finishes(
    hosted_env: HostedEnv, tmp_path: Path
) -> None:
    """The write's parent waits on its hook: an apply gets the cache while the
    hook runs (the parent holds no read lock), and the hook's child then runs
    ``lattice show`` on the same cache."""
    signals = tmp_path / "signals"
    signals.mkdir()
    (signals / "token").write_text(hosted_env.token)
    script = tmp_path / "hook.py"
    script.write_text(HOOK.format(token_env=TOKEN_ENV, cli=CLI))
    config_path = hosted_env.board / "config.json"
    config = json.loads(config_path.read_text())
    config["hooks"] = {"on": {"comment_added": f"{sys.executable} {script} {signals}"}}
    config_path.chmod(0o600)
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    hosted_env.write_remote(run_board_hooks=True)
    repo = _attached(hosted_env, tmp_path)
    locks = repo / ".lattice" / "locks"

    write = _Thread(lambda: run_cli(repo, "comment", "DEM-1", "hi", "--actor", "agent:dev"))
    write.start()
    _wait_for((signals / "started").exists, "the hook")

    applied = threading.Event()

    def apply() -> None:  # an apply's lock, from another "process"
        fd = cache._lock(locks / "cache_rw.lock", True, None)
        applied.set()
        time.sleep(0.2)
        os.close(fd)

    writer = _Thread(apply)
    writer.start()
    # The parent waits on its hook: it must not hold the read lock meanwhile,
    # so the apply gets in while the hook is still running.
    applied_during_hook = applied.wait(10)
    (signals / "go").touch()
    write.join(60)
    writer.join(60)
    assert write.error is None and writer.error is None, (write.error, writer.error)
    assert write.result.exit_code == 0, write.result.output
    assert applied_during_hook, "the apply waited for the parent while it waited on its hook"
    result = signals / "result"
    assert result.exists() and result.read_text() == "0", "the hook's child never finished"


@pytest.mark.timeout(120)
def test_a_wait_whose_condition_needs_an_apply_finishes(
    hosted_env: HostedEnv, tmp_path: Path
) -> None:
    """``lattice wait`` in its own process, on the checkout a write then syncs:
    the write's apply must not wait for the waiter, and the waiter must see it."""
    repo = _attached(hosted_env, tmp_path)
    waiter = subprocess.Popen(
        [sys.executable, "-c", CLI, "wait", "DEM-1", "--status", "in_planning", "--timeout", "40"],
        cwd=repo,
        env=dict(os.environ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        time.sleep(1.0)  # let it start waiting
        write = _Thread(lambda: walk_to(repo, "DEM-1", "in_planning"))
        write.start()
        write.join(40)
        assert not write.is_alive(), "the write's apply waited on the waiter"
        assert write.error is None, write.error
        out, err = waiter.communicate(timeout=40)
    finally:
        waiter.kill()
    assert waiter.returncode == 0, (out, err)


# ---------------------------------------------------------------------------
# The audited call sites that start a process: none holds the read lock
# ---------------------------------------------------------------------------


def _read_lock_free(repo: Path) -> bool:
    """Whether no descriptor holds ``cache_rw.lock`` (shared or not) right now."""
    fd = cache._lock(repo / ".lattice" / "locks" / "cache_rw.lock", True, time.monotonic())
    if fd is None:
        return False
    os.close(fd)
    return True


def test_show_runs_git_after_its_reads_without_the_read_lock(
    hosted_env: HostedEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lattice.cli import query_cmds

    repo = _attached(hosted_env, tmp_path)
    seen: list[bool] = []
    real = query_cmds._get_all_git_branches

    def branches(lattice_dir: Path) -> list[str]:
        seen.append(_read_lock_free(repo))
        return real(lattice_dir)

    monkeypatch.setattr(query_cmds, "_get_all_git_branches", branches)
    result = run_cli(repo, "show", "DEM-1", "--json")
    assert result.exit_code == 0, result.output
    assert seen == [True]


def test_completion_attestations_run_git_without_the_read_lock(
    hosted_env: HostedEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lattice.boards import resolve_board
    from lattice.cli import attestations
    from lattice.remote import session

    repo = _attached(hosted_env, tmp_path)
    seen: list[bool] = []

    def compute(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        seen.append(_read_lock_free(repo))
        return []

    monkeypatch.setattr(attestations, "compute_reachable_review_commits", compute)
    board = resolve_board(repo)
    policy = {
        "workflow": {"completion_policies": {"done": {"require_reachable_review_commit": True}}}
    }
    try:
        attestations.completion_attestations(board, policy, "DEM-1", "done", worktree=repo)
    finally:
        session.reset_process_state()
    assert seen == [True]


@pytest.mark.parametrize("identity", ["--actor", "--name"])
def test_code_review_runs_git_without_the_read_lock(
    hosted_env: HostedEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, identity: str
) -> None:
    """Round 2, Astra 4: ``--name`` resolves the session from the cache, which
    takes the read lock again; it is released again before the diff's git."""
    from lattice.cli import review_cmds
    from lattice.core.review import DiffResolution

    repo = _attached(hosted_env, tmp_path)
    started = run_cli(
        repo, "session", "start", "--name", "Reviewer", "--model", "human", "--quiet"
    )
    assert started.exit_code == 0, started.output
    who = ["--name", started.stdout.strip()] if identity == "--name" else ["--actor", "agent:rev"]
    seen: list[bool] = []

    def diff(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        seen.append(_read_lock_free(repo))
        return DiffResolution(
            success=True,
            diff="diff --git a/a b/a\n+hello",
            base_ref="base",
            head_ref="head",
            base_sha="a" * 40,
            head_sha="b" * 40,
            worktree=repo,
            source="explicit",
        )

    monkeypatch.setattr(review_cmds, "_normalize_worktree", lambda path: (repo, None))
    monkeypatch.setattr(review_cmds, "resolve_diff", diff)
    monkeypatch.setattr(review_cmds, "_run_single_and_store", lambda **kwargs: None)
    result = run_cli(repo, "code-review", "DEM-1", "--mode", "single", *who, "--force", "--json")
    assert result.exit_code == 0, result.output
    assert seen == [True]
