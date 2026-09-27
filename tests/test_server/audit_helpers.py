"""Helpers for the audit history tests (SPEC §8.10): git inspection, a project
driven without HTTP, and a git shim that logs, stalls, or fails chosen calls."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from io import StringIO
from pathlib import Path

import pytest

from lattice.server.config import AuditConfig
from lattice.server.log import ServerLog
from lattice.server.project import Project
from lattice.storage.ownership import RECORDED_CLASSES, classify_path

FAST = {"audit": {"debounce_seconds": 0.1, "max_interval_seconds": 2}}
MESSAGE_RE = re.compile(r"^audit: seq (\d+)-(\d+) \((\d+) ops\)$")


def git_out(directory: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(directory), *args],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout


def commits(directory: Path) -> list[str]:
    """Commit subjects, oldest first."""
    return git_out(directory, "log", "--reverse", "--format=%s").splitlines()


def head_tree(directory: Path, rev: str = "HEAD") -> dict[str, bytes]:
    """Path (relative to ``.lattice/``) -> bytes of every file in the commit's tree."""
    names = [
        n for n in git_out(directory, "ls-tree", "-r", "-z", "--name-only", rev).split("\0") if n
    ]
    out = {}
    for name in names:
        assert name.startswith(".lattice/"), name
        blob = subprocess.run(
            ["git", "-C", str(directory), "cat-file", "blob", f"{rev}:{name}"],
            capture_output=True,
            check=True,
            timeout=30,
        ).stdout
        out[name[len(".lattice/") :]] = blob
    return out


def durable_files(board: Path) -> dict[str, bytes]:
    """The board's durable and workspace files (SPEC §6.1) and their bytes."""
    out = {}
    for dirpath, _dirs, files in os.walk(board):
        for name in files:
            path = Path(dirpath) / name
            rel = path.relative_to(board).as_posix()
            if classify_path(rel) in RECORDED_CLASSES:
                out[rel] = path.read_bytes()
    return out


def last_committed_seq(directory: Path) -> int:
    match = MESSAGE_RE.match(commits(directory)[-1])
    return int(match.group(2)) if match else 0


def direct_project(
    root: Path, slug: str, config: AuditConfig, stream: StringIO | None = None
) -> tuple[Project, StringIO]:
    """A loaded project with a committer, driven without HTTP (as the worker does)."""
    stream = stream if stream is not None else StringIO()
    project = Project(
        slug,
        root / "projects" / slug,
        ServerLog("debug", stream),
        "srv_test",
        audit_config=config,
    )
    project.load()
    assert project.state == "loaded", project.reason
    return project, stream


def close(project: Project) -> None:
    """Shutdown's audit order: stage under the lock, commit outside it, then the lease."""
    with project.work:
        project.audit_stage()
    project.audit_commit_and_stop()
    with project.work:
        project.release()


def log_lines(stream: StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line]


def bare_remote(tmp_path: Path, directory: Path, name: str = "remote.git") -> Path:
    bare = tmp_path / name
    subprocess.run(["git", "init", "--quiet", "--bare", str(bare)], check=True, timeout=30)
    git_out(directory, "remote", "add", "backup", str(bare))
    return bare


def remote_head(bare: Path, branch: str = "audit") -> str | None:
    done = subprocess.run(
        ["git", "-C", str(bare), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return done.stdout.strip() or None


@dataclass
class GitShim:
    """A ``git`` first on ``PATH`` that logs each call's arguments, holds a push
    for as long as ``stall`` exists, and fails a
    ``<subcommand>`` once for each ``fail-<subcommand>`` file (consumed)."""

    directory: Path
    calls: Path

    @property
    def stall(self) -> Path:
        return self.directory / "stall"

    def fail_once(self, subcommand: str) -> None:
        (self.directory / f"fail-{subcommand}").write_text("")

    def subcommands(self) -> list[list[str]]:
        """Each call's arguments after ``-C <dir>`` (the audit's calls only)."""
        out = []
        for line in self.calls.read_text().splitlines():
            words = line.split()
            if "-C" in words:
                out.append(words[words.index("-C") + 2 :])
        return out


def install_git_shim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> GitShim:
    real_git = shutil.which("git")
    assert real_git
    directory = tmp_path / "shim"
    directory.mkdir()
    calls = tmp_path / "git-calls.log"
    script = f"""#!/bin/sh
echo "$*" >> "{calls}"
sub=""
skip=0
for arg in "$@"; do
  if [ $skip = 1 ]; then skip=0; continue; fi
  case "$arg" in
    -C|-c) skip=1 ;;
    -*) ;;
    *) sub="$arg"; break ;;
  esac
done
if [ "$sub" = push ]; then
  while [ -e "{directory}/stall" ]; do sleep 0.02; done
fi
if [ -n "$sub" ] && [ -e "{directory}/fail-$sub" ]; then
  rm -f "{directory}/fail-$sub"
  echo "fatal: injected failure of $sub" >&2
  exit 1
fi
exec "{real_git}" "$@"
"""
    shim = directory / "git"
    shim.write_text(script)
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{directory}{os.pathsep}{os.environ['PATH']}")
    return GitShim(directory, calls)
