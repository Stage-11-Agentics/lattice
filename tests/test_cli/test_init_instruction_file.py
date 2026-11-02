"""``lattice init`` and ``lattice demo init`` honour the repo's instruction file.

End to end: each test drives the real ``lattice`` command on a temp git repo.
File names are read from the directory listing, since a case-insensitive disk
answers ``exists()`` for any spelling.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from lattice.cli import instruction_file
from lattice.cli.main import cli

LATTICE = Path(sys.executable).with_name("lattice")
USER_TEXT = "# Project\n\nThe repo's own instructions.\n"
BLOCK_HEADING = "## Lattice"


def _lattice(root: Path, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if key != "LATTICE_ROOT"}
    return subprocess.run(
        [str(LATTICE), *args],
        cwd=root,
        env=env,
        input=stdin if stdin is not None else "",
        capture_output=True,
        text=True,
        check=False,
    )


def _init(root: Path, *flags: str) -> subprocess.CompletedProcess[str]:
    result = _lattice(root, "init", "--actor", "human:test", "--project-code", "TST", *flags)
    assert result.returncode == 0, result.stderr
    return result


def _demo(root: Path) -> subprocess.CompletedProcess[str]:
    return _lattice(root, "demo", "init", "--path", str(root), "--no-dashboard")


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    ).stdout


def _instruction_files(root: Path) -> list[str]:
    """Exact on-disk names of the instruction files, from the directory listing."""
    return sorted(n for n in os.listdir(root) if n.casefold() in ("agents.md", "claude.md"))


def _snapshot(root: Path) -> dict[str, object]:
    """Everything under *root* but .git: bytes, mtimes, link targets, and the git state."""
    state: dict[str, object] = {"git status": _git(root, "status", "--porcelain=v1")}
    for directory, subdirs, files in os.walk(root):
        if ".git" in subdirs:
            subdirs.remove(".git")
        for name in [*files, *subdirs]:
            path = Path(directory, name)
            rel = str(path.relative_to(root))
            if path.is_symlink():
                state[rel] = ("link", os.readlink(path))
            elif path.is_dir():
                state[rel] = "dir"
            else:
                state[rel] = (path.read_bytes(), path.stat().st_mtime_ns)
    return state


def _assert_second_run_changes_nothing(root: Path, *flags: str) -> None:
    """A second init is a no-op, and so is one that finds the block already there."""
    before = _snapshot(root)
    result = _init(root, *flags)
    assert "already initialized" in result.stdout
    assert _snapshot(root) == before

    # With .lattice/ gone the writer itself runs again, and must not add a thing.
    instruction_bytes = {n: (root / n).read_bytes() for n in _instruction_files(root)}
    shutil.rmtree(root / ".lattice")
    _init(root, *flags)
    assert {n: (root / n).read_bytes() for n in _instruction_files(root)} == instruction_bytes
    assert _instruction_files(root) == sorted(instruction_bytes)


def _case_insensitive_disk(directory: Path) -> bool:
    probe = directory / "CaseProbe"
    probe.write_text("")
    try:
        return (directory / "caseprobe").exists()
    finally:
        probe.unlink()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    return root


def _commit_all(root: Path) -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "start")


class TestInitPerStartingState:
    """One test per row: the repo's starting state and what init leaves."""

    def test_only_agents_md(self, repo: Path) -> None:
        (repo / "AGENTS.md").write_text(USER_TEXT)

        result = _init(repo)

        assert _instruction_files(repo) == ["AGENTS.md"]
        content = (repo / "AGENTS.md").read_text()
        assert content.startswith(USER_TEXT)
        assert content.count(BLOCK_HEADING) == 1
        assert "Updated AGENTS.md" in result.stdout
        _assert_second_run_changes_nothing(repo)

    def test_only_claude_md(self, repo: Path) -> None:
        (repo / "CLAUDE.md").write_text(USER_TEXT)

        result = _init(repo)

        assert _instruction_files(repo) == ["CLAUDE.md"]
        content = (repo / "CLAUDE.md").read_text()
        assert content.startswith(USER_TEXT)
        assert content.count(BLOCK_HEADING) == 1
        assert "Updated CLAUDE.md" in result.stdout
        _assert_second_run_changes_nothing(repo)

    @pytest.mark.parametrize(
        ("real", "link"), [("CLAUDE.md", "AGENTS.md"), ("AGENTS.md", "CLAUDE.md")]
    )
    def test_both_one_a_symlink_to_the_other(self, repo: Path, real: str, link: str) -> None:
        (repo / real).write_text(USER_TEXT)
        (repo / link).symlink_to(real)

        result = _init(repo)

        assert _instruction_files(repo) == ["AGENTS.md", "CLAUDE.md"]
        assert (repo / link).is_symlink()
        assert os.readlink(repo / link) == real
        assert not (repo / real).is_symlink()
        content = (repo / real).read_text()
        assert content.startswith(USER_TEXT)
        assert content.count(BLOCK_HEADING) == 1
        assert "one linked file" in result.stdout
        _assert_second_run_changes_nothing(repo)
        assert os.readlink(repo / link) == real

    def test_both_separate_files(self, repo: Path) -> None:
        (repo / "AGENTS.md").write_text("# For other agents\n")
        (repo / "CLAUDE.md").write_text("# For Claude\n")

        _init(repo)

        assert _instruction_files(repo) == ["AGENTS.md", "CLAUDE.md"]
        agents = (repo / "AGENTS.md").read_text()
        claude = (repo / "CLAUDE.md").read_text()
        assert agents.startswith("# For other agents\n")
        assert claude.startswith("# For Claude\n")
        assert agents.count(BLOCK_HEADING) == 1
        assert claude.count(BLOCK_HEADING) == 1
        _assert_second_run_changes_nothing(repo)

    def test_lowercase_agents_md_untracked(self, repo: Path) -> None:
        (repo / "agents.md").write_text(USER_TEXT)

        result = _init(repo)

        assert _instruction_files(repo) == ["AGENTS.md"]
        content = (repo / "AGENTS.md").read_text()
        assert content.startswith(USER_TEXT)
        assert content.count(BLOCK_HEADING) == 1
        assert "Renamed agents.md to AGENTS.md" in result.stdout
        assert _git(repo, "ls-files") == ""
        _assert_second_run_changes_nothing(repo)

    def test_lowercase_agents_md_tracked(self, repo: Path) -> None:
        (repo / "agents.md").write_text(USER_TEXT)
        _commit_all(repo)

        _init(repo)

        assert _instruction_files(repo) == ["AGENTS.md"]
        assert (repo / "AGENTS.md").read_text().count(BLOCK_HEADING) == 1
        # git has the new name too, with the rename staged the way git mv stages it.
        assert _git(repo, "ls-files") == "AGENTS.md\n"
        assert _git(repo, "diff", "--cached", "--name-status", "-M") == (
            "R100\tagents.md\tAGENTS.md\n"
        )
        _assert_second_run_changes_nothing(repo)

    def test_neither(self, repo: Path) -> None:
        result = _init(repo)

        assert _instruction_files(repo) == ["AGENTS.md"]
        content = (repo / "AGENTS.md").read_text()
        assert content.startswith(f"# {repo.name}\n")
        assert content.count(BLOCK_HEADING) == 1
        assert "Created AGENTS.md" in result.stdout
        _assert_second_run_changes_nothing(repo)


class TestInitLowercaseCorners:
    def test_link_that_points_at_the_lowercase_file_follows_the_rename(self, repo: Path) -> None:
        (repo / "agents.md").write_text(USER_TEXT)
        (repo / "CLAUDE.md").symlink_to("agents.md")
        _commit_all(repo)

        _init(repo)

        assert _instruction_files(repo) == ["AGENTS.md", "CLAUDE.md"]
        assert os.readlink(repo / "CLAUDE.md") == "AGENTS.md"
        assert (repo / "AGENTS.md").read_text().count(BLOCK_HEADING) == 1
        # The rename and the repointed link are staged together, so a commit stays whole.
        assert sorted(_git(repo, "ls-files").split()) == ["AGENTS.md", "CLAUDE.md"]
        assert "CLAUDE.md" in _git(repo, "diff", "--cached", "--name-only")
        _assert_second_run_changes_nothing(repo)

    def test_lowercase_link_is_renamed_and_stays_a_link(self, repo: Path) -> None:
        (repo / "CLAUDE.md").write_text(USER_TEXT)
        (repo / "agents.md").symlink_to("CLAUDE.md")

        _init(repo)

        assert _instruction_files(repo) == ["AGENTS.md", "CLAUDE.md"]
        assert os.readlink(repo / "AGENTS.md") == "CLAUDE.md"
        assert (repo / "CLAUDE.md").read_text().count(BLOCK_HEADING) == 1
        _assert_second_run_changes_nothing(repo)

    def test_both_cases_on_a_case_sensitive_disk_destroy_nothing(self, repo: Path) -> None:
        if _case_insensitive_disk(repo):
            pytest.skip("agents.md and AGENTS.md can only coexist on a case-sensitive disk")
        (repo / "agents.md").write_text("lowercase\n")
        (repo / "AGENTS.md").write_text("uppercase\n")

        result = _init(repo)

        assert (repo / "agents.md").read_text() == "lowercase\n"
        assert (repo / "AGENTS.md").read_text().startswith("uppercase\n")
        assert (repo / "AGENTS.md").read_text().count(BLOCK_HEADING) == 1
        assert "agents.md sits beside AGENTS.md" in result.stderr
        _assert_second_run_changes_nothing(repo)

    def test_lowercase_claude_md_counts_and_keeps_its_name(self, repo: Path) -> None:
        (repo / "claude.md").write_text(USER_TEXT)

        _init(repo)

        assert _instruction_files(repo) == ["claude.md"]
        assert (repo / "claude.md").read_text().count(BLOCK_HEADING) == 1
        _assert_second_run_changes_nothing(repo)

    def test_broken_link_counts_as_a_file_and_creates_nothing(self, repo: Path) -> None:
        (repo / "AGENTS.md").symlink_to("missing.md")

        result = _init(repo)

        assert "could not update AGENTS.md" in result.stderr
        assert _instruction_files(repo) == ["AGENTS.md"]
        assert (repo / "AGENTS.md").is_symlink()
        assert not (repo / "missing.md").exists()


class TestInitFlags:
    def test_setup_claude_creates_claude_md_when_repo_has_none(self, repo: Path) -> None:
        result = _init(repo, "--setup-claude")

        assert _instruction_files(repo) == ["CLAUDE.md"]
        assert "Created CLAUDE.md" in result.stdout
        _assert_second_run_changes_nothing(repo, "--setup-claude")

    @pytest.mark.parametrize("existing", ["AGENTS.md", "agents.md"])
    def test_setup_claude_never_adds_a_second_file(self, repo: Path, existing: str) -> None:
        (repo / existing).write_text(USER_TEXT)

        result = _init(repo, "--setup-claude")

        assert _instruction_files(repo) == ["AGENTS.md"]
        assert (repo / "AGENTS.md").read_text().count(BLOCK_HEADING) == 1
        assert "--setup-claude: did not create CLAUDE.md" in result.stdout
        _assert_second_run_changes_nothing(repo, "--setup-claude")

    def test_setup_claude_updates_an_existing_claude_md(self, repo: Path) -> None:
        (repo / "CLAUDE.md").write_text(USER_TEXT)

        result = _init(repo, "--setup-claude")

        assert _instruction_files(repo) == ["CLAUDE.md"]
        assert (repo / "CLAUDE.md").read_text().count(BLOCK_HEADING) == 1
        assert "did not create" not in result.stdout

    def test_setup_agents_follows_an_existing_claude_md(self, repo: Path) -> None:
        (repo / "CLAUDE.md").write_text(USER_TEXT)

        result = _init(repo, "--setup-agents")

        assert _instruction_files(repo) == ["CLAUDE.md"]
        assert (repo / "CLAUDE.md").read_text().count(BLOCK_HEADING) == 1
        assert "--setup-agents: did not create AGENTS.md" in result.stdout

    def test_both_setup_flags_on_an_empty_repo_create_one_file(self, repo: Path) -> None:
        _init(repo, "--setup-agents", "--setup-claude")

        assert _instruction_files(repo) == ["CLAUDE.md"]

    def test_no_setup_claude_leaves_a_lone_claude_md_and_says_so(self, repo: Path) -> None:
        (repo / "CLAUDE.md").write_text(USER_TEXT)

        result = _init(repo, "--no-setup-claude")

        assert _instruction_files(repo) == ["CLAUDE.md"]
        assert (repo / "CLAUDE.md").read_text() == USER_TEXT
        assert "Left CLAUDE.md as it is" in result.stdout

    def test_no_setup_claude_still_writes_agents_md(self, repo: Path) -> None:
        (repo / "AGENTS.md").write_text("# Agents\n")
        (repo / "CLAUDE.md").write_text(USER_TEXT)

        _init(repo, "--no-setup-claude")

        assert (repo / "AGENTS.md").read_text().count(BLOCK_HEADING) == 1
        assert (repo / "CLAUDE.md").read_text() == USER_TEXT

    def test_no_setup_claude_covers_a_linked_pair(self, repo: Path) -> None:
        (repo / "CLAUDE.md").write_text(USER_TEXT)
        (repo / "AGENTS.md").symlink_to("CLAUDE.md")

        _init(repo, "--no-setup-claude")

        assert (repo / "CLAUDE.md").read_text() == USER_TEXT

    def test_no_setup_agents_touches_nothing_and_does_not_rename(self, repo: Path) -> None:
        (repo / "agents.md").write_text(USER_TEXT)

        _init(repo, "--no-setup-agents")

        assert _instruction_files(repo) == ["agents.md"]
        assert (repo / "agents.md").read_text() == USER_TEXT

    def test_no_setup_agents_with_setup_claude_beside_agents_md(self, repo: Path) -> None:
        (repo / "AGENTS.md").write_text(USER_TEXT)

        result = _init(repo, "--no-setup-agents", "--setup-claude")

        assert _instruction_files(repo) == ["AGENTS.md"]
        assert (repo / "AGENTS.md").read_text() == USER_TEXT
        assert "--setup-claude: did not create CLAUDE.md" in result.stdout

    def test_interactive_init_follows_the_rule_and_names_the_file_in_its_summary(
        self, repo: Path
    ) -> None:
        (repo / "CLAUDE.md").write_text(USER_TEXT)

        # name, project name, project code, done display, integration yes, OpenClaw no
        result = _lattice(repo, "init", stdin="\n\n\n\ny\nn\n")

        assert result.returncode == 0, result.stderr
        assert _instruction_files(repo) == ["CLAUDE.md"]
        assert (repo / "CLAUDE.md").read_text().count(BLOCK_HEADING) == 1
        assert "CLAUDE.md        Claude Code integration" in result.stdout
        assert "check CLAUDE.md" in result.stdout
        assert "agents.md" not in result.stdout

    def test_help_states_the_rule(self, repo: Path) -> None:
        output = " ".join(_lattice(repo, "init", "--help").stdout.split())

        assert "agents.md" not in output
        assert "or a new AGENTS.md if it has neither" in output
        assert "never added beside an existing one" in output


def test_a_different_default_is_one_constant(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(instruction_file, "DEFAULT_INSTRUCTION_FILE", "CLAUDE.md")

    result = CliRunner().invoke(
        cli, ["init", "--path", str(tmp_path), "--actor", "human:test", "--project-code", "TST"]
    )

    assert result.exit_code == 0, result.output
    assert _instruction_files(tmp_path) == ["CLAUDE.md"]


class TestDemo:
    """``lattice demo init`` writes by the same rule."""

    @pytest.mark.parametrize(
        ("start", "expected"),
        [
            ({}, ["AGENTS.md"]),
            ({"AGENTS.md": USER_TEXT}, ["AGENTS.md"]),
            ({"CLAUDE.md": USER_TEXT}, ["CLAUDE.md"]),
            ({"agents.md": USER_TEXT}, ["AGENTS.md"]),
            ({"AGENTS.md": "# A\n", "CLAUDE.md": "# C\n"}, ["AGENTS.md", "CLAUDE.md"]),
        ],
        ids=["neither", "only-agents", "only-claude", "lowercase", "both-separate"],
    )
    def test_demo_per_starting_state(
        self, repo: Path, start: dict[str, str], expected: list[str]
    ) -> None:
        for name, text in start.items():
            (repo / name).write_text(text)

        result = _demo(repo)

        assert result.returncode == 0, result.stderr
        assert _instruction_files(repo) == expected
        for name in expected:
            assert (repo / name).read_text().count(BLOCK_HEADING) == 1

        before = _snapshot(repo)
        again = _demo(repo)
        assert again.returncode != 0
        assert "Demo already exists" in again.stderr
        assert _snapshot(repo) == before

    def test_demo_writes_once_through_a_link(self, repo: Path) -> None:
        (repo / "CLAUDE.md").write_text(USER_TEXT)
        (repo / "AGENTS.md").symlink_to("CLAUDE.md")

        result = _demo(repo)

        assert result.returncode == 0, result.stderr
        assert os.readlink(repo / "AGENTS.md") == "CLAUDE.md"
        assert (repo / "CLAUDE.md").read_text().count(BLOCK_HEADING) == 1
