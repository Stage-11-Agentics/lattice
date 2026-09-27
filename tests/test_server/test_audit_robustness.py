"""Audit history robustness (SPEC §8.10, AC-26, review round 1 of LAT-318).

The shutdown and unload order, requeue and load-time reconciliation, staging
that git's ignore and attribute files cannot influence, git isolated from
ambient configuration, strict push targets with URLs kept out of the log, and
commits that keep to ``max_interval_seconds`` while a push stalls.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime
from io import StringIO
from pathlib import Path

import pytest
from click.testing import CliRunner

from lattice.cli.main import cli
from lattice.server import admin, audit
from lattice.server.config import AuditConfig, ServerConfigError, parse_config
from lattice.server.testing import make_root, running_server, wait_for
from tests.test_server.audit_helpers import (
    bare_remote,
    close,
    commits,
    direct_project,
    durable_files,
    git_out,
    head_tree,
    install_git_shim,
    log_lines,
)
from tests.test_server.conftest import create_task, mint
from tests.test_server.faults import request, run

pytestmark = [
    pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed"),
    # Servers, git subprocesses, and waits: generous for slow CI runners.
    pytest.mark.timeout(60),
]

SLOW = AuditConfig(debounce_seconds=30, max_interval_seconds=30)
QUICK = AuditConfig(debounce_seconds=0.05, max_interval_seconds=1)


def events(stream: StringIO, name: str) -> list[dict]:
    return [line for line in log_lines(stream) if line["event"] == name]


# ---------------------------------------------------------------------------
# Shutdown and unload order (finding 1)
# ---------------------------------------------------------------------------


def test_drain_never_needs_the_work_lock_and_commit_follows_stage(tmp_path: Path) -> None:
    root = make_root(tmp_path, projects={"alpha": {"code": "ALP"}})
    directory = root / "projects" / "alpha"
    project, stream = direct_project(root, "alpha", QUICK)
    committer = project.committer
    run(project, request("task.create", {"title": "one"}))
    project.work.acquire()  # an operation "in flight": the committer's staging must wait
    try:
        assert wait_for(lambda: committer.waiting_for_lock, timeout=4)
        drained = threading.Thread(target=committer.drain)
        drained.start()
        drained.join(2)
        assert not drained.is_alive(), "drain waited for the work lock"
        project.audit_stage()  # step 1, under the lock
        assert commits(directory) == ["audit: project created"]
    finally:
        project.work.release()
    project.audit_commit_and_stop()  # step 2, outside it
    assert commits(directory)[-1] == "audit: seq 1-1 (1 ops)"
    assert [line["final"] for line in events(stream, "audit_commit")] == [True]
    assert not committer._thread.is_alive()
    assert project.committer is None
    with project.work:
        project.release()
    assert head_tree(directory) == durable_files(directory / ".lattice")


def _instrument_phases(
    monkeypatch: pytest.MonkeyPatch, project, seen: list[tuple[str, bool]]
) -> None:  # noqa: ANN001
    """Record each shutdown/unload step with whether the work lock was held."""
    real_stage, real_commit = audit.Stager.stage, audit.commit_tree
    real_clean, real_release = project.write_clean_shutdown, project.release

    def stage(self):  # noqa: ANN001, ANN202
        seen.append(("stage", project.work.locked()))
        return real_stage(self)

    def commit(*args):  # noqa: ANN002, ANN202
        seen.append(("commit", project.work.locked()))
        return real_commit(*args)

    def clean() -> None:
        seen.append(("clean_shutdown", project.work.locked()))
        real_clean()

    def release() -> None:
        seen.append(("release_lease", project.work.locked()))
        real_release()

    monkeypatch.setattr(audit.Stager, "stage", stage)
    monkeypatch.setattr(audit, "commit_tree", commit)
    monkeypatch.setattr(project, "write_clean_shutdown", clean)
    monkeypatch.setattr(project, "release", release)


def test_server_shutdown_runs_the_audit_in_h22_phase_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SIGTERM path (``ProjectRegistry.stop`` → ``SHUTDOWN_PHASES``): drain, stage
    under the work lock, commit outside it, clean_shutdown, release the lease."""
    config = {"audit": {"debounce_seconds": 60, "max_interval_seconds": 60}}
    root = make_root(tmp_path, projects={"alpha": {"code": "ALP"}}, config=config)
    directory = root / "projects" / "alpha"
    seen: list[tuple[str, bool]] = []
    with running_server(root) as server:
        project = server.project("alpha")
        create_task(server, mint(root))
        epoch = project.journal.epoch
        _instrument_phases(monkeypatch, project, seen)
    assert seen == [
        ("stage", True),
        ("commit", False),
        ("clean_shutdown", True),
        ("release_lease", True),
    ], seen
    assert commits(directory)[-1] == "audit: seq 1-1 (1 ops)"
    assert project.committer is None
    # The audit writes nothing under .lattice/: the next load's clean_shutdown
    # fingerprint still matches, so the epoch does not rotate.
    monkeypatch.undo()
    with running_server(root) as server:
        assert server.project("alpha").journal.epoch == epoch
        assert server.project("alpha").committer is not None


def test_unload_and_load_control_requests_run_the_audit_phases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``lattice server project unload`` / ``load`` (H-22) through the running server."""
    config = {"audit": {"debounce_seconds": 60, "max_interval_seconds": 60}}
    root = make_root(tmp_path, projects={"alpha": {"code": "ALP"}}, config=config)
    directory = root / "projects" / "alpha"
    token = mint(root)
    seen: list[tuple[str, bool]] = []
    with running_server(root) as server:
        project = server.project("alpha")
        create_task(server, token)
        _instrument_phases(monkeypatch, project, seen)
        admin.project_lifecycle(root, "alpha", "unload")
        assert seen[:3] == [("stage", True), ("commit", False), ("clean_shutdown", True)], seen
        assert commits(directory)[-1] == "audit: seq 1-1 (1 ops)"
        assert project.committer is None
        monkeypatch.undo()
        admin.project_lifecycle(root, "alpha", "load")
        assert project.committer is not None
        project.committer.config = AuditConfig(debounce_seconds=0.05, max_interval_seconds=1)
        create_task(server, token)
        assert wait_for(lambda: commits(directory)[-1] == "audit: seq 2-2 (1 ops)", timeout=10)
        admin.project_lifecycle(root, "alpha", "reload")
        create_task(server, token)
    assert commits(directory)[-1] == "audit: seq 3-3 (1 ops)"
    assert head_tree(directory) == durable_files(directory / ".lattice")


def test_unload_then_reload(tmp_path: Path) -> None:
    root = make_root(tmp_path, projects={"alpha": {"code": "ALP"}})
    directory = root / "projects" / "alpha"
    project, _ = direct_project(root, "alpha", SLOW)
    run(project, request("task.create", {"title": "before unload"}))
    close(project)  # unload: stage, commit, release the lease
    assert commits(directory)[-1] == "audit: seq 1-1 (1 ops)"
    project.audit_config = QUICK
    project.load()  # reload: a fresh committer
    try:
        assert project.state == "loaded" and project.committer is not None
        run(project, request("task.create", {"title": "after reload"}))
        assert wait_for(lambda: commits(directory)[-1] == "audit: seq 2-2 (1 ops)", timeout=4)
    finally:
        close(project)
    assert head_tree(directory) == durable_files(directory / ".lattice")


def test_release_without_the_audit_steps_still_commits(tmp_path: Path) -> None:
    """A caller that releases the lease under the lock without the two steps (for
    example an older unload path) loses no audit work."""
    root = make_root(tmp_path, projects={"alpha": {"code": "ALP"}})
    directory = root / "projects" / "alpha"
    project, _ = direct_project(root, "alpha", SLOW)
    committer = project.committer
    run(project, request("task.create", {"title": "t"}))
    with project.work:
        project.release()
    assert commits(directory)[-1] == "audit: seq 1-1 (1 ops)"
    committer._thread.join(4)
    assert not committer._thread.is_alive()


# ---------------------------------------------------------------------------
# Requeue and reconciliation (finding 2)
# ---------------------------------------------------------------------------


def test_a_failed_commit_is_requeued(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shim = install_git_shim(tmp_path, monkeypatch)
    root = make_root(tmp_path, projects={"alpha": {"code": "ALP"}})
    directory = root / "projects" / "alpha"
    project, stream = direct_project(root, "alpha", QUICK)
    try:
        shim.fail_once("commit-tree")
        run(project, request("task.create", {"title": "one"}))
        run(project, request("task.create", {"title": "two"}))
        assert wait_for(lambda: project.committer.commits == 1, timeout=4)
        failures = events(stream, "audit_failed")
        assert len(failures) == 1 and failures[0]["retry"] == "requeued", failures
        assert commits(directory)[-1] == "audit: seq 1-2 (2 ops)"
        assert head_tree(directory) == durable_files(directory / ".lattice")
    finally:
        close(project)


def test_a_crash_before_notify_is_reconciled_at_the_next_load(tmp_path: Path) -> None:
    root = make_root(tmp_path, projects={"alpha": {"code": "ALP"}})
    directory = root / "projects" / "alpha"
    board = directory / ".lattice"
    project, _ = direct_project(root, "alpha", SLOW)
    # Journal fsynced, then the process died before the committer heard of it.
    project._journaled = lambda seq: None
    run(project, request("task.create", {"title": "one"}))
    run(project, request("task.create", {"title": "two"}))
    project._abandon_audit()
    with project.work:
        project.release()
    assert commits(directory) == ["audit: project created"]

    project, stream = direct_project(root, "alpha", QUICK)
    try:
        assert wait_for(lambda: len(commits(directory)) == 2, timeout=4)
        assert commits(directory)[-1] == "audit: seq 1-2 (2 ops)"
        assert events(stream, "audit_reconcile")[0]["ops"] == 2
        assert head_tree(directory) == durable_files(board)
        body = git_out(directory, "log", "-1", "--format=%B")
        assert f"Lattice-Epoch: {project.journal.epoch}" in body
        assert "Lattice-Seq: 2" in body
    finally:
        close(project)

    # A board changed while no server ran, with nothing journaled: still recorded.
    (board / "context.md").write_text("edited by hand while the server was down\n")
    project, _ = direct_project(root, "alpha", QUICK)
    try:
        assert wait_for(lambda: len(commits(directory)) == 3, timeout=4)
        assert commits(directory)[-1] == "audit: seq 2-2 (0 ops)"
        assert head_tree(directory)["context.md"] == (board / "context.md").read_bytes()
    finally:
        close(project)


# ---------------------------------------------------------------------------
# Exact tree and bytes (finding 3)
# ---------------------------------------------------------------------------


def test_ignore_and_attribute_files_cannot_change_the_tree(tmp_path: Path) -> None:
    root = make_root(tmp_path, projects={"alpha": {"code": "ALP"}})
    directory = root / "projects" / "alpha"
    board = directory / ".lattice"
    # The board's own (durable) .gitignore tries to hide durable data.
    (board / ".gitignore").write_text("*\nplans/\nnotes/\n")
    # Attributes everywhere git would look, normalizing text and running a filter.
    (board / "notes" / ".gitattributes").write_text("* text eol=lf\n*.md filter=evil\n")
    (directory / ".gitattributes").write_text("* text\n")
    (directory / ".git" / "info").mkdir(exist_ok=True)
    (directory / ".git" / "info" / "attributes").write_text("* text filter=evil\n")
    git_out(directory, "config", "filter.evil.clean", "tr a-z A-Z")
    git_out(directory, "config", "filter.evil.required", "true")
    (board / "notes" / "crlf.md").write_bytes(b"one\r\ntwo\r\n")
    project, _ = direct_project(root, "alpha", QUICK)
    try:
        run(project, request("task.create", {"title": "planned"}))
        # The load-time check records the planted files first, then seq 1.
        assert wait_for(lambda: commits(directory)[-1] == "audit: seq 1-1 (1 ops)", timeout=4)
        tree = head_tree(directory)
        assert tree == durable_files(board)
        assert tree["notes/crlf.md"] == b"one\r\ntwo\r\n"
        assert any(k.startswith("plans/task_") for k in tree)
        assert tree[".gitignore"] == b"*\nplans/\nnotes/\n"
        assert ".gitattributes" not in tree  # the project directory's: never board data
    finally:
        close(project)


def test_adversarial_file_names_are_staged_intact(tmp_path: Path) -> None:
    """Every regular file under a durable directory is durable (SPEC §6.1),
    whatever its name: a newline, a leading "-", option-like names."""
    root = make_root(tmp_path, projects={"alpha": {"code": "ALP"}})
    directory = root / "projects" / "alpha"
    board = directory / ".lattice"
    names = {
        "notes/line\nbreak.md": b"newline in the name\n",
        "notes/-rf.md": b"leading dash\n",
        "plans/--help": b"option-like\n",
        "orchestration/-/--stdin-paths": b"nested\n",
        "resources/tab\tand space .json": b"{}\n",
        "notes/trailing-newline\n": b"x\n",
    }
    for rel, data in names.items():
        path = board / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    project, _ = direct_project(root, "alpha", QUICK)
    try:
        run(project, request("task.create", {"title": "t"}))
        assert wait_for(lambda: commits(directory)[-1] == "audit: seq 1-1 (1 ops)", timeout=4)
        tree = head_tree(directory)
        for rel, data in names.items():
            assert tree.get(rel) == data, rel
        assert tree == durable_files(board)
        # A rename is picked up too (the stat cache follows the listing).
        (board / "notes" / "line\nbreak.md").rename(board / "notes" / "line\nmoved.md")
        run(project, request("task.create", {"title": "u"}))
        assert wait_for(lambda: commits(directory)[-1] == "audit: seq 2-2 (1 ops)", timeout=4)
        tree = head_tree(directory)
        assert "notes/line\nbreak.md" not in tree and "notes/line\nmoved.md" in tree
    finally:
        close(project)


# ---------------------------------------------------------------------------
# Git isolated from ambient configuration (finding 4)
# ---------------------------------------------------------------------------


def test_git_ignores_system_user_and_injected_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hostile = tmp_path / "hostile"
    hooks = hostile / "hooks"
    hooks.mkdir(parents=True)
    hook_marker = hostile / "hook-ran"
    for name in ("reference-transaction", "post-commit", "pre-push"):
        hook = hooks / name
        hook.write_text(f'#!/bin/sh\ntouch "{hook_marker}"\n')
        hook.chmod(0o755)

    def template(name: str) -> Path:
        path = hostile / f"template-{name}"
        path.mkdir()
        (path / f"marker-{name}").write_text(name)
        return path

    for scope in ("global", "system"):
        (hostile / f"{scope}.cfg").write_text(
            f"[init]\n\ttemplateDir = {template(scope)}\n"
            f"[core]\n\thooksPath = {hooks}\n"
            "[user]\n\tname = Hostile\n\temail = hostile@example.com\n"
            "[commit]\n\tgpgSign = true\n"
        )
    # Without the test harness's help: ambient system and user config, and injection.
    monkeypatch.delenv("GIT_CONFIG_NOSYSTEM", raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(hostile / "global.cfg"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(hostile / "system.cfg"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "init.templateDir")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(template("count")))
    monkeypatch.setenv("GIT_CONFIG_KEY_1", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_1", str(hooks))
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", f"'init.templatedir'='{template('params')}'")

    # The environment really is hostile to a plain git.
    scratch = tmp_path / "scratch"
    subprocess.run(["git", "init", "-q", str(scratch)], check=True, timeout=30)
    assert list((scratch / ".git").glob("marker-*"))
    # Newer git (2.50 on macOS, verified) fires the reference-transaction hook
    # during a plain `git init`, so the proof above may itself leave the hook
    # marker. Clear it: the assertion below is about the audit's own git calls.
    hook_marker.unlink(missing_ok=True)

    root = make_root(tmp_path, projects={"alpha": {"code": "ALP"}})
    directory = root / "projects" / "alpha"
    assert not list((directory / ".git").glob("marker-*"))
    listing = audit.git_text(directory, "config", "--list", "--show-origin")
    assert "hostile" not in listing.lower(), listing
    project, _ = direct_project(root, "alpha", QUICK)
    try:
        run(project, request("task.create", {"title": "t"}))
        assert wait_for(lambda: project.committer.commits == 1, timeout=4)
    finally:
        close(project)
    assert not hook_marker.exists()
    authors = audit.git_text(directory, "log", "--format=%an <%ae> %G?").splitlines()
    assert authors and all(a == "Lattice Hosted <lattice-hosted@localhost> N" for a in authors)


# ---------------------------------------------------------------------------
# Push targets and URLs (finding 5)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "push",
    [
        {"remote": "--upload-pack=touch /tmp/x", "branch": "audit"},
        {"remote": "-oProxyCommand=x", "branch": "audit"},
        {"remote": "https://example.com/x.git", "branch": "audit"},
        {"remote": "back up", "branch": "audit"},
        {"remote": "backup", "branch": "-f"},
        {"remote": "backup", "branch": "a..b"},
        {"remote": "backup", "branch": "x.lock"},
        {"remote": "backup", "branch": "foo.lock/bar"},
        {"remote": "backup", "branch": "foo/.hidden"},
        {"remote": "backup", "branch": "foo//bar"},
        {"remote": "backup", "branch": "foo/"},
        {"remote": "backup", "branch": "foo."},
        {"remote": "backup", "branch": "a:b"},
        {"remote": "backup", "branch": "+main"},
        {"remote": "", "branch": "audit"},
        {"remote": "backup"},
    ],
)
def test_one_strict_validator_refuses_option_like_and_malformed_targets(push: dict) -> None:
    with pytest.raises(ServerConfigError):
        parse_config({"audit": {"push": push}})
    with pytest.raises(Exception) as caught:
        audit.validate_push_settings({"push": push})
    assert getattr(caught.value, "code", None) == "VALIDATION_ERROR"


BRANCH_CANDIDATES = [
    "audit",
    "release/2026.09",
    "a.b/c-d_e",
    "foo.lock/bar",
    "foo/bar.lock",
    "foo.lock",
    "foo/.bar",
    ".foo",
    "foo..bar",
    "foo/",
    "foo//bar",
    "foo.",
    "foo/bar.",
    "a.lockx/b",
]


@pytest.mark.parametrize("branch", BRANCH_CANDIDATES)
def test_every_accepted_branch_passes_git_check_ref_format(branch: str) -> None:
    from lattice.server.config import check_push

    try:
        check_push({"remote": "backup", "branch": branch})
        accepted = True
    except ValueError:
        accepted = False
    git_ok = (
        subprocess.run(
            ["git", "check-ref-format", f"refs/heads/{branch}"], timeout=30, check=False
        ).returncode
        == 0
    )
    # Never looser than git (it may be stricter: a first character that is a letter
    # or digit).
    if accepted:
        assert git_ok, branch
    if not git_ok:
        assert not accepted, branch
    if branch in ("audit", "release/2026.09", "a.b/c-d_e", "a.lockx/b"):
        assert accepted, branch


def test_the_cli_refuses_an_option_like_remote(tmp_path: Path) -> None:
    root = make_root(tmp_path, projects={"alpha": {}})
    result = CliRunner().invoke(
        cli,
        [
            "server",
            "project",
            "audit",
            "alpha",
            "--root",
            str(root),
            "--push-remote=--receive-pack=touch x",
            "--branch",
            "audit",
            "--json",
        ],
        catch_exceptions=False,
    )
    assert json.loads(result.output)["error"]["code"] == "VALIDATION_ERROR"
    assert not (root / "projects" / "alpha" / ".lattice" / "hosted" / "audit.json").exists()


def test_an_invalid_audit_json_is_never_pushed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shim = install_git_shim(tmp_path, monkeypatch)
    root = make_root(tmp_path, projects={"alpha": {"code": "ALP"}})
    directory = root / "projects" / "alpha"
    settings = {"push": {"remote": "--receive-pack=touch pwned", "branch": "audit"}}
    (directory / ".lattice" / "hosted" / "audit.json").write_text(json.dumps(settings))
    project, stream = direct_project(root, "alpha", QUICK)
    try:
        run(project, request("task.create", {"title": "t"}))
        assert wait_for(lambda: project.committer.maintenance.gc_runs >= 1, timeout=4)
        assert wait_for(lambda: events(stream, "audit_push_config_invalid"), timeout=4)
    finally:
        close(project)
    assert not any(words and words[0] == "push" for words in shim.subcommands())
    assert len(events(stream, "audit_push_config_invalid")) == 1


def test_push_passes_an_option_terminator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shim = install_git_shim(tmp_path, monkeypatch)
    root = make_root(tmp_path, projects={"alpha": {"code": "ALP"}})
    directory = root / "projects" / "alpha"
    bare_remote(tmp_path, directory)
    config = AuditConfig(
        debounce_seconds=0.05, max_interval_seconds=1, push={"remote": "backup", "branch": "audit"}
    )
    project, _ = direct_project(root, "alpha", config)
    try:
        run(project, request("task.create", {"title": "t"}))
        assert wait_for(lambda: project.committer.maintenance.push_attempts >= 1, timeout=4)
    finally:
        close(project)
    pushes = [words for words in shim.subcommands() if words and words[0] == "push"]
    assert pushes
    for words in pushes:
        assert words[words.index("--") + 1 :] == ["backup", "HEAD:refs/heads/audit"], words


def test_credentials_in_a_remote_url_never_reach_the_log(tmp_path: Path) -> None:
    root = make_root(tmp_path, projects={"alpha": {"code": "ALP"}})
    directory = root / "projects" / "alpha"
    # Port 9 on loopback: refused at once, so git reports the URL it could not reach.
    url = "https://audit-user:s3cretpass@127.0.0.1:9/history.git?access_token=tok3n"
    git_out(directory, "remote", "add", "backup", url)
    config = AuditConfig(
        debounce_seconds=0.05, max_interval_seconds=1, push={"remote": "backup", "branch": "audit"}
    )
    project, stream = direct_project(root, "alpha", config)
    try:
        run(project, request("task.create", {"title": "t"}))
        assert wait_for(lambda: events(stream, "audit_push_failed"), timeout=10)
    finally:
        close(project)
    text = stream.getvalue()
    for secret in ("s3cretpass", "tok3n", "audit-user", "127.0.0.1:9"):
        assert secret not in text, secret


def test_redact_replaces_whole_urls() -> None:
    stderr = (
        "fatal: unable to access 'https://user:secret@example.com/x.git/?token=abc': denied\n"
        "\nPlease make sure you have the correct access rights\nand the repository exists.\n"
    )
    assert audit.redact(stderr) == "fatal: unable to access '<url>': denied"
    assert audit.redact("error: ssh://git@host:22/r.git?key=k refused") == ("error: <url> refused")


# ---------------------------------------------------------------------------
# Slow push and gc never delay commits (finding 6)
# ---------------------------------------------------------------------------


def test_commits_keep_to_max_interval_while_a_push_stalls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shim = install_git_shim(tmp_path, monkeypatch, stall_push=1.5)
    root = make_root(tmp_path, projects={"alpha": {"code": "ALP"}})
    directory = root / "projects" / "alpha"
    bare_remote(tmp_path, directory)
    max_interval = 0.3
    config = AuditConfig(
        debounce_seconds=1.0,  # continuous writes: the debounce never elapses
        max_interval_seconds=max_interval,
        push={"remote": "backup", "branch": "audit"},
    )
    project, stream = direct_project(root, "alpha", config)
    committer = project.committer
    shim.stall.write_text("")
    try:
        started = time.monotonic()
        n = 0
        while time.monotonic() - started < 1.6:
            n += 1
            run(project, request("task.create", {"title": f"t{n}"}))
            time.sleep(0.03)
        stalled_commits = project.committer.commits
        assert project.committer.maintenance.push_attempts >= 1
    finally:
        shim.stall.unlink()
        close(project)
    assert stalled_commits >= 3, stalled_commits
    # Several commits landed during one stalled push; each still got its own gc.
    assert committer.maintenance.gc_runs == committer.commits, (
        committer.maintenance.gc_runs,
        committer.commits,
    )
    stamps = [
        datetime.fromisoformat(line["ts"].replace("Z", "+00:00")).timestamp()
        for line in events(stream, "audit_commit")
        if not line["final"]
    ]
    gaps = [b - a for a, b in zip(stamps, stamps[1:], strict=False)]
    assert gaps and max(gaps) <= max_interval + 0.4, gaps
    assert len(commits(directory)) >= 4


def test_audit_state_on_create_reports_git_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shim = install_git_shim(tmp_path, monkeypatch)
    root = make_root(tmp_path)
    shim.fail_once("init")
    data = admin.create_project(root, "gamma")
    assert data["audit"]["repo"] is False and "init" in data["audit"]["reason"]
    assert os.path.isdir(root / "projects" / "gamma" / ".lattice")
