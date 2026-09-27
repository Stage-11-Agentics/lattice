"""AC-5 (hosted part, H-12): ``lattice board write`` of an orchestration file
and of a loose review pack under ``plans/`` on one bound checkout, read from
another checkout's cache; paths outside the workspace rules are refused and
change nothing."""

from __future__ import annotations

import json
from pathlib import Path

from tests.test_remote.conftest import tree_hashes
from tests.test_remote.hosted import HostedEnv, make_repo, run_cli


def test_board_files_written_on_one_client_are_read_from_anothers_cache(
    hosted_env: HostedEnv, tmp_path: Path
) -> None:
    a = make_repo(tmp_path / "machine-a" / "repo")
    b = make_repo(tmp_path / "machine-b" / "repo")
    for repo in (a, b):
        assert run_cli(repo, "remote", "attach", "team", "demo").exit_code == 0

    run_state = run_cli(
        a, "board", "write", "orchestration/run-state.md", "--stdin", input="# Run state\n"
    )
    assert run_state.exit_code == 0, run_state.output
    pack = run_cli(
        a, "board", "write", "plans/review-pack.md", "--stdin", "--json", input="Pack.\n"
    )
    assert pack.exit_code == 0, pack.output

    assert run_cli(b, "list").exit_code == 0
    assert (b / ".lattice" / "orchestration" / "run-state.md").read_text() == "# Run state\n"
    assert (b / ".lattice" / "plans" / "review-pack.md").read_text() == "Pack.\n"

    # Refused before anything is written: a task's own plan, a runtime path, `..`.
    created = run_cli(a, "create", "Planned", "--actor", "agent:dev", "--json")
    task_id = json.loads(created.stdout)["data"]["id"]
    before = tree_hashes(hosted_env.board)
    for path in (f"plans/{task_id}.md", "locks/x.lock", "../escape.md"):
        refused = run_cli(a, "board", "write", path, "--stdin", "--json", input="x\n")
        assert refused.exit_code == 1, path
        assert json.loads(refused.stdout)["error"]["code"] == "VALIDATION_ERROR", path
    assert tree_hashes(hosted_env.board) == before
    assert not (hosted_env.server_root / "projects" / "demo" / "escape.md").exists()
