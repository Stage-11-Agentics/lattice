"""AC-34, AC-35: a script follows the move steps ``project import`` prints, literally.

A git repository tracks its board and has a feature branch cut before the move.
The board is imported from a copy; then each printed step's commands run in the
checkout (``<alias>`` filled in): the old board is moved aside, the binding
attaches the checkout, and the move is committed and pushed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("lattice.remote.binding", reason="the move ends in H-11's remote attach")
hosted = pytest.importorskip("tests.test_remote.hosted")

from lattice.server import tokens  # noqa: E402
from lattice.storage.integrity import check_board  # noqa: E402

SLUG = "moved"


def _json(result) -> dict:  # noqa: ANN001 - click Result
    assert result.exit_code == 0, result.output
    return json.loads(result.output)["data"]


def _run_step(repo: Path, command: str) -> None:
    """One printed command, as an agent would run it in the checkout."""
    if command.startswith("lattice "):
        result = hosted.run_cli(repo, *command.split()[1:])
        assert result.exit_code == 0, f"{command}\n{result.output}"
    else:
        subprocess.run(command, shell=True, cwd=repo, check=True, capture_output=True)


def test_the_printed_move_steps_move_a_git_tracked_board(hosted_env, tmp_path: Path) -> None:  # noqa: ANN001
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    repo = hosted.make_repo(tmp_path / "repo")
    hosted.git(repo, "remote", "add", "origin", str(origin))

    init = hosted.run_cli(
        repo, "init", "--project-code", "MOV", "--actor", "human:alice", "--preset", "stage11",
        "--no-setup-claude", "--no-setup-agents",
    )  # fmt: skip
    assert init.exit_code == 0, init.output
    first = _json(hosted.run_cli(repo, "create", "First", "--actor", "human:alice", "--json"))
    hosted.run_cli(repo, "create", "Second", "--actor", "human:alice")
    (repo / ".lattice" / "reviews").mkdir()
    (repo / ".lattice" / "reviews" / "old.md").write_text("an unmanaged review\n")
    hosted.git(repo, "add", "-A")
    hosted.git(repo, "commit", "-q", "-m", "track the board")
    hosted.git(repo, "push", "-q", "-u", "origin", "main")
    hosted.git(repo, "branch", "feature")  # cut before the move; still tracks the board

    # Step 1 is stopping writers (none run here). Step 2: import from a copy.
    copy = tmp_path / "board-copy"
    shutil.copytree(repo / ".lattice", copy / ".lattice", symlinks=True)
    token_id = tokens.parse_token(hosted_env.token)[0]
    tokens.grant(hosted_env.server_root, token_id, projects=(SLUG,))
    result = hosted.run_cli(
        tmp_path, "server", "project", "import", SLUG, "--from", str(copy),
        "--root", str(hosted_env.server_root), "--json",
    )  # fmt: skip
    imported = _json(result)
    assert ("reviews/old.md", "unmanaged") in {
        (row["path"], row["class"]) for row in imported["not_copied"]
    }

    steps = {step["step"]: step for step in imported["move_steps"]}
    for command in steps[3]["commands"]:
        _run_step(repo, command)
    for command in steps[4]["commands"]:
        _run_step(repo, command.replace("<alias>", hosted.REMOTE))

    aside = list(repo.glob(".lattice.pre-hosted-*"))
    assert len(aside) == 1 and (aside[0] / "reviews" / "old.md").exists()
    status = hosted.git(repo, "status", "--porcelain")
    assert f"D  .lattice/events/{first['id']}.jsonl" in status.splitlines()
    assert "?? .lattice-remote.json" in status.splitlines()
    assert not [line for line in status.splitlines() if ".lattice.pre-hosted" in line]
    assert not [line for line in status.splitlines() if line.startswith("?? .lattice/")]

    for command in steps[5]["commands"]:
        _run_step(repo, command)
    assert hosted.git(repo, "ls-files", ".lattice") == ""
    assert hosted.git(repo, "ls-tree", "-r", "--name-only", "origin/main", "--", ".lattice") == ""

    # Doctor is clean on the server and on the cache; the cache serves the moved tasks.
    assert check_board(hosted_env.server_root / "projects" / SLUG / ".lattice").errors == 0
    doctor = _json(hosted.run_cli(repo, "doctor", "--json"))
    assert doctor["summary"]["errors"] == 0, doctor
    shown = _json(hosted.run_cli(repo, "show", first["short_id"], "--json"))
    assert shown["id"] == first["id"]
    remote_status = _json(hosted.run_cli(repo, "remote", "status", "--json"))
    assert "feature" in json.dumps(remote_status)
