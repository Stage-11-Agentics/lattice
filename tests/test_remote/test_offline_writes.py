"""G-8 for write commands that read first (review round 1): a refused offline
write leaves the checkout byte for byte as it was, including the offline
window its own read phase would otherwise open (SPEC §9.5)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_remote.hosted import HostedEnv, make_repo, run_cli, tree_hash

WINDOW = Path(".lattice/cache/unreachable_until")


@pytest.fixture()
def repo(hosted_env: HostedEnv, tmp_path: Path) -> Path:
    repo = make_repo(tmp_path / "repo")
    assert run_cli(repo, "remote", "attach", "team", "demo").exit_code == 0
    assert run_cli(repo, "create", "Offline writing", "--actor", "human:alice").exit_code == 0
    return repo


@pytest.mark.parametrize(
    "args",
    [
        ("status", "DEM-1", "in_planning", "--actor", "human:alice"),
        ("archive", "DEM-1", "--actor", "human:alice"),
    ],
    ids=["status", "archive"],
)
def test_a_write_that_reads_first_changes_nothing_offline(
    hosted_env: HostedEnv, repo: Path, args: tuple[str, ...]
) -> None:
    """G-8 for write commands with a read phase: its failed catch-up must not
    leave the offline window behind when the write itself is refused."""
    hosted_env.write_remote(retry_seconds=0.2)
    with hosted_env.stopped():
        assert not (repo / WINDOW).exists()
        before = tree_hash(repo)
        result = run_cli(repo, *args, "--json")
        assert result.exit_code == 1, result.output
        assert json.loads(result.stdout)["error"]["code"] == "SERVER_UNREACHABLE"
        assert tree_hash(repo) == before
        # An existing window is left exactly as it was, too.
        assert run_cli(repo, "list").exit_code == 0
        before = tree_hash(repo)
        assert run_cli(repo, *args).exit_code == 1
        assert tree_hash(repo) == before
