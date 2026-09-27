"""AC-18: ``remote attach`` writes a binding with exactly ``remote`` and
``project``; plus the rest of attach's contract (SPEC §9.2)."""

from __future__ import annotations

import json
import stat
from pathlib import Path

from tests.test_remote.hosted import HostedEnv, git, make_repo, run_cli


def test_binding_has_exactly_remote_and_project(hosted_env: HostedEnv, tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    result = run_cli(repo, "remote", "attach", "team", "demo")
    assert result.exit_code == 0, result.output
    binding = json.loads((repo / ".lattice-remote.json").read_text())
    assert binding == {"remote": "team", "project": "demo"}
    text = (repo / ".lattice-remote.json").read_text()
    assert hosted_env.url not in text and hosted_env.token not in text
    # What to commit, and the refresh commands for pre-v2 agent instructions.
    assert "git add .lattice-remote.json .gitignore" in result.stdout
    assert "lattice setup-claude --force" in result.stdout
    assert "lattice setup-claude-skill --force" in result.stdout
    # The initial sync made the cache: private and read-only.
    lattice_dir = repo / ".lattice"
    assert (lattice_dir / "config.json").is_file()
    assert stat.S_IMODE(lattice_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((lattice_dir / "config.json").stat().st_mode) == 0o400
    # Nothing under .lattice/ shows up for git: only the two files to commit.
    assert set(git(repo, "status", "--porcelain").splitlines()) == {
        "?? .gitignore",
        "?? .lattice-remote.json",
    }


def test_attach_to_an_unknown_project_writes_nothing(
    hosted_env: HostedEnv, tmp_path: Path
) -> None:
    repo = make_repo(tmp_path / "repo")
    result = run_cli(repo, "remote", "attach", "team", "nope", "--json")
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] == "NOT_FOUND"
    assert not (repo / ".lattice-remote.json").exists()
    assert not (repo / ".lattice").exists()


def test_attach_twice_is_idempotent(hosted_env: HostedEnv, tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    assert run_cli(repo, "remote", "attach", "team", "demo").exit_code == 0
    again = run_cli(repo, "remote", "attach", "team", "demo", "--json")
    assert again.exit_code == 0, again.output
    data = json.loads(again.stdout)["data"]
    assert data["commit"] == [".lattice-remote.json"]  # .gitignore already had the line


def test_init_on_a_bound_checkout_has_its_own_message(
    hosted_env: HostedEnv, tmp_path: Path
) -> None:
    repo = make_repo(tmp_path / "repo")
    assert run_cli(repo, "remote", "attach", "team", "demo").exit_code == 0
    result = run_cli(repo, "init", "--actor", "human:a", "--project-code", "X")
    assert result.exit_code == 1
    assert "This checkout is bound to 'team/demo'; its board lives on the server." in result.stderr
