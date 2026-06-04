"""Integration tests with a real `git worktree`.

These exercise the worktree auto-route end-to-end:
- A non-exception command (e.g. ``lattice status``) invoked from a worktree
  writes to the **primary** repo's ``.lattice/events/<task_id>.jsonl``.
- ``lattice branch-link`` (an exception command):
  - In tracked-``.lattice/`` projects (worktree has its own ``.lattice/``):
    the event lands in the worktree.
  - In gitignored-``.lattice/`` projects (worktree starts empty): falls back
    to the primary.
- ``lattice init`` from a worktree errors with the Layer F message.

Skipped when ``git`` is not on ``$PATH``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

# Skip the whole file when git isn't available.
pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not on $PATH")


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {args!r} failed in {cwd}:\n{result.stderr}")
    return result


def _lattice(
    cwd: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run `lattice` as a subprocess (not via CliRunner) so cwd really matters."""
    lattice_bin = shutil.which("lattice")
    if lattice_bin is None:
        pytest.skip("lattice binary not on $PATH")
    full_env = os.environ.copy()
    # Make sure no LATTICE_ROOT leaks from the parent process; we want pure
    # cwd-based resolution for these tests.
    full_env.pop("LATTICE_ROOT", None)
    if env:
        full_env.update(env)
    return subprocess.run(
        [lattice_bin, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=full_env,
    )


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


@pytest.fixture
def primary_and_worktree(tmp_path: Path) -> tuple[Path, Path, str, str]:
    """Build a primary repo with .lattice/ initialized + a linked worktree.

    Returns (primary_path, worktree_path, task_ulid, short_id).
    """
    primary = tmp_path / "primary"
    primary.mkdir()
    _git(primary, "init", "--initial-branch=main")
    _git(primary, "config", "user.email", "test@example.com")
    _git(primary, "config", "user.name", "Test")
    (primary / "README.md").write_text("# primary\n")
    _git(primary, "add", "README.md")
    _git(primary, "commit", "-m", "initial")

    # lattice init in the primary
    result = _lattice(primary, "init", "--actor", "human:test", "--project-code", "TEST")
    assert result.returncode == 0, result.stderr or result.stdout

    # Create one task in the primary. --json gives us both the ULID (canonical
    # event-file key) and the short_id.
    create = _lattice(
        primary,
        "create",
        "Test task",
        "--actor",
        "human:test",
        "--json",
    )
    assert create.returncode == 0, create.stderr or create.stdout
    envelope = json.loads(create.stdout)
    assert envelope["ok"], envelope
    task_ulid = envelope["data"]["id"]
    short_id = envelope["data"].get("short_id", task_ulid)

    # Create a linked worktree on a feature branch
    worktree = tmp_path / "wt-feature"
    _git(primary, "worktree", "add", str(worktree), "-b", "feat/test-feature")

    return primary, worktree, task_ulid, short_id


class TestNonExceptionCommandRoutesToPrimary:
    def test_status_from_worktree_writes_to_primary(
        self, primary_and_worktree: tuple[Path, Path, str, str]
    ) -> None:
        primary, worktree, task_id, short_id = primary_and_worktree

        # Move the task through in_planning from the worktree.
        result = _lattice(
            worktree,
            "status",
            task_id,
            "in_planning",
            "--actor",
            "human:test",
        )
        assert result.returncode == 0, result.stderr or result.stdout

        # Event must be in the primary's events log.
        primary_events = _read_jsonl(primary / ".lattice" / "events" / f"{task_id}.jsonl")
        assert any(
            e.get("type") == "status_changed" and e.get("data", {}).get("to") == "in_planning"
            for e in primary_events
        ), f"status_changed not found in primary events: {primary_events}"

        # And no .lattice/ should have been auto-created in the worktree.
        assert not (worktree / ".lattice").exists()


class TestBranchLinkException:
    def test_branch_link_with_worktree_lattice_writes_to_worktree(
        self, primary_and_worktree: tuple[Path, Path, str, str], tmp_path: Path
    ) -> None:
        """Simulate a tracked-.lattice/ project: copy the primary's .lattice/
        into the worktree (the way `git worktree add` would for a tracked
        .lattice/), then run branch-link from the worktree."""
        primary, worktree, task_id, _short_id = primary_and_worktree

        shutil.copytree(primary / ".lattice", worktree / ".lattice")

        result = _lattice(
            worktree,
            "branch-link",
            task_id,
            "feat/test-feature",
            "--actor",
            "human:test",
        )
        assert result.returncode == 0, result.stderr or result.stdout

        # Event must land in the worktree's events log.
        wt_events = _read_jsonl(worktree / ".lattice" / "events" / f"{task_id}.jsonl")
        assert any(
            e.get("type") == "branch_linked"
            and e.get("data", {}).get("branch") == "feat/test-feature"
            for e in wt_events
        ), f"branch_linked not found in worktree events: {wt_events}"

        # And NOT in the primary's events log (this is the whole point — the
        # artifact rides the feature branch).
        primary_events = _read_jsonl(primary / ".lattice" / "events" / f"{task_id}.jsonl")
        assert not any(e.get("type") == "branch_linked" for e in primary_events), (
            "branch_linked leaked into primary events"
        )

    def test_branch_link_without_worktree_lattice_falls_back_to_primary(
        self, primary_and_worktree: tuple[Path, Path, str, str]
    ) -> None:
        """Lattice-style gitignored .lattice/: the worktree has none, so
        branch-link falls back to the primary."""
        primary, worktree, task_id, _short_id = primary_and_worktree
        assert not (worktree / ".lattice").exists()

        result = _lattice(
            worktree,
            "branch-link",
            task_id,
            "feat/test-feature",
            "--actor",
            "human:test",
        )
        assert result.returncode == 0, result.stderr or result.stdout

        primary_events = _read_jsonl(primary / ".lattice" / "events" / f"{task_id}.jsonl")
        assert any(
            e.get("type") == "branch_linked"
            and e.get("data", {}).get("branch") == "feat/test-feature"
            for e in primary_events
        ), f"branch_linked not found in primary events: {primary_events}"

        # The worktree should still have no .lattice/ — fallback must not
        # auto-create one.
        assert not (worktree / ".lattice").exists()


class TestInitInWorktreeRefused:
    def test_init_from_worktree_errors_with_layer_f_message(
        self, primary_and_worktree: tuple[Path, Path, str, str]
    ) -> None:
        _primary, worktree, _task_id, _short_id = primary_and_worktree

        result = _lattice(
            worktree,
            "init",
            "--actor",
            "human:test",
            "--project-code",
            "TEST",
        )
        assert result.returncode != 0
        combined = (result.stdout + result.stderr).lower()
        assert "worktree" in combined
        assert "refusing" in combined or "refuse" in combined
        # And no .lattice/ should have been created.
        assert not (worktree / ".lattice").exists()
