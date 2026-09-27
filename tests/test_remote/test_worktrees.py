"""AC-10: one binding and one cache per clone, reached from every worktree
(SPEC §9.2, §9.3)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from lattice.remote import cache
from lattice.storage.board_init import create_board
from tests.test_remote.hosted import HostedEnv, add_worktree, git, make_repo, run_cli


def _commit_binding(env: HostedEnv, repo: Path) -> None:
    assert run_cli(repo, "remote", "attach", "team", "demo").exit_code == 0
    git(repo, "add", ".lattice-remote.json", ".gitignore")
    git(repo, "commit", "-q", "-m", "bind")


def _title(cwd: Path, short_id: str) -> str:
    result = run_cli(cwd, "show", short_id, "--json")
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)["data"]["title"]


def test_two_worktrees_share_one_cache(hosted_env: HostedEnv, tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    _commit_binding(hosted_env, repo)
    wt1 = add_worktree(repo, tmp_path / "wt1", "feat-1")
    wt2 = add_worktree(repo, tmp_path / "wt2", "feat-2", relative=True)
    assert not (wt2 / ".git").read_text().split(":", 1)[1].strip().startswith("/")
    sub = wt2 / "src" / "deep"
    sub.mkdir(parents=True)

    assert run_cli(wt1, "create", "From worktree one", "--actor", "agent:a").exit_code == 0
    # From a subdirectory of the relative-gitdir worktree: the gitdir resolves
    # against the .git file's directory, not the process cwd.
    assert _title(sub, "DEM-1") == "From worktree one"
    assert run_cli(sub, "create", "From worktree two", "--actor", "agent:b").exit_code == 0
    assert _title(wt1, "DEM-2") == "From worktree two"

    assert (repo / ".lattice" / "cache" / "state.json").is_file()
    assert not (wt1 / ".lattice").exists()
    assert not (wt2 / ".lattice").exists()
    for tree in (repo, wt1, wt2):
        assert ".lattice" not in git(tree, "status", "--porcelain", "--ignored=no")


def test_lattice_root_naming_a_hosted_root_before_its_first_sync(
    hosted_env: HostedEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hosted_env.server_op("task.create", {"title": "Already there"}, actor="human:alice")
    clone = make_repo(tmp_path / "clone")
    hosted_env.bind(clone)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setenv("LATTICE_ROOT", str(clone))
    assert _title(elsewhere, "DEM-1") == "Already there"
    assert (clone / ".lattice" / "cache" / "state.json").is_file()


def test_branch_without_the_binding_still_routes(hosted_env: HostedEnv, tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    git(repo, "branch", "before-binding")
    _commit_binding(hosted_env, repo)
    wt = add_worktree(repo, tmp_path / "wt", "feat")
    assert run_cli(wt, "create", "Routed", "--actor", "agent:a").exit_code == 0

    git(repo, "checkout", "-q", "before-binding")
    assert not (repo / ".lattice-remote.json").exists()
    assert run_cli(repo, "create", "Still routed", "--actor", "agent:a").exit_code == 0
    assert _title(wt, "DEM-2") == "Still routed"
    assert _title(repo, "DEM-1") == "Routed"


def test_clone_after_the_move_adopts_runtime_leftovers(
    hosted_env: HostedEnv, tmp_path: Path
) -> None:
    hosted_env.server_op("task.create", {"title": "On the server"}, actor="human:alice")
    origin = make_repo(tmp_path / "origin")
    create_board(origin, project_code="OLD", actor="human:alice")
    git(origin, "add", "-A")
    git(origin, "commit", "-q", "-m", "track the board")
    teammate = tmp_path / "teammate"
    git(tmp_path, "clone", "-q", str(origin), str(teammate))
    git(teammate, "config", "user.email", "t@example.com")
    git(teammate, "config", "user.name", "T")
    # Runtime state the teammate's own lattice runs left behind (ignored by the
    # board's .gitignore, so git keeps it).
    (teammate / ".lattice" / "locks").mkdir(exist_ok=True)
    (teammate / ".lattice" / "locks" / "tasks.lock").write_text("")

    # The move commit: untrack the board, commit the binding.
    git(origin, "rm", "-r", "-q", "--cached", ".lattice")
    subprocess.run(["rm", "-rf", str(origin / ".lattice")], check=True)
    hosted_env.bind(origin)
    (origin / ".gitignore").write_text("/.lattice/\n")
    git(origin, "add", ".lattice-remote.json", ".gitignore")
    git(origin, "commit", "-q", "-m", "move the board to the server")
    git(teammate, "pull", "-q", "--no-rebase")

    leftovers = sorted(p.name for p in (teammate / ".lattice").iterdir())
    assert "config.json" not in leftovers and leftovers
    assert not cache.synced_files(teammate / ".lattice")
    assert _title(teammate, "DEM-1") == "On the server"
    assert (teammate / ".lattice" / "cache" / "state.json").is_file()


def test_attach_from_a_linked_worktree(hosted_env: HostedEnv, tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    wt = add_worktree(repo, tmp_path / "wt", "feat")
    result = run_cli(wt, "remote", "attach", "team", "demo", "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["data"]["root"] == str(repo.resolve())
    binding = json.loads((repo / ".lattice-remote.json").read_text())
    assert binding == {"project": "demo", "remote": "team"}
    assert not (wt / ".lattice-remote.json").exists()
    exclude = (repo / ".git" / "info" / "exclude").read_text().splitlines()
    assert "/.lattice/" in exclude
    assert "/.lattice/" in (repo / ".gitignore").read_text().splitlines()
    # Idempotent.
    assert run_cli(wt, "remote", "attach", "team", "demo").exit_code == 0
    assert (repo / ".git" / "info" / "exclude").read_text().count("/.lattice/") == 1
    assert (repo / ".gitignore").read_text().count("/.lattice/") == 1


def test_binding_beside_a_local_board_is_a_conflict(hosted_env: HostedEnv, tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    create_board(repo, project_code="LOC", actor="human:alice")
    attach = run_cli(repo, "remote", "attach", "team", "demo", "--json")
    assert attach.exit_code == 1
    error = json.loads(attach.stdout)["error"]
    assert error["code"] == "BINDING_CONFLICT"
    assert "Moving a board" in error["message"]
    assert not (repo / ".lattice-remote.json").exists()

    hosted_env.bind(repo)
    for args in (("list", "--json"), ("create", "x", "--actor", "agent:a", "--json")):
        result = run_cli(repo, *args)
        assert result.exit_code == 1, result.output
        error = json.loads(result.stdout)["error"]
        assert error["code"] == "BINDING_CONFLICT"
        assert "Moving a board" in error["message"]


def test_marker_naming_another_project_is_a_conflict(
    hosted_env: HostedEnv, tmp_path: Path
) -> None:
    repo = make_repo(tmp_path / "repo")
    assert run_cli(repo, "remote", "attach", "team", "demo").exit_code == 0
    (repo / ".lattice-remote.json").write_text(json.dumps({"remote": "team", "project": "other"}))
    result = run_cli(repo, "list", "--json")
    assert result.exit_code == 1
    error = json.loads(result.stdout)["error"]
    assert error["code"] == "BINDING_CONFLICT"
    assert "lattice cache clear --forget" in error["message"]


def test_status_lists_branches_that_still_track_the_board(
    hosted_env: HostedEnv, tmp_path: Path
) -> None:
    repo = make_repo(tmp_path / "repo")
    create_board(repo, project_code="OLD", actor="human:alice")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "track the board")
    git(repo, "branch", "pre-move")
    git(repo, "rm", "-r", "-q", "--cached", ".lattice")
    subprocess.run(["rm", "-rf", str(repo / ".lattice")], check=True)
    git(repo, "commit", "-q", "-m", "untrack the board")
    assert run_cli(repo, "remote", "attach", "team", "demo").exit_code == 0

    plain = run_cli(repo, "remote", "status")
    assert plain.exit_code == 0, plain.output
    assert "pre-move" in plain.stdout
    assert "git rm -r --cached .lattice" in plain.stdout
    as_json = json.loads(run_cli(repo, "remote", "status", "--json").stdout)["data"]
    assert as_json["branches_tracking_board"] == ["pre-move"]
    assert as_json["cache"]["stale"] is False
    assert as_json["identity"]["user"] == "human:alice"


def test_sync_routes_like_every_command(hosted_env: HostedEnv, tmp_path: Path) -> None:
    """``lattice sync`` uses the same routing (the worktree jump to the primary
    checkout's cache), and ``hosted_root_of`` refuses a binding beside a local
    board instead of reading it as not hosted."""
    from lattice.core.errors import OpError
    from lattice.remote.follower import hosted_root_of

    repo = make_repo(tmp_path / "repo")
    _commit_binding(hosted_env, repo)
    wt = add_worktree(repo, tmp_path / "wt", "feat")
    hosted_env.server_op("task.create", {"title": "Synced"}, actor="human:alice")
    synced = run_cli(wt, "sync", "--json")
    assert synced.exit_code == 0, synced.output
    assert json.loads(synced.stdout)["data"]["status"] == "applied"
    assert not (wt / ".lattice").exists()
    assert hosted_root_of(repo) == repo

    local = make_repo(tmp_path / "local")
    create_board(local, project_code="LOC", actor="human:alice")
    hosted_env.bind(local)
    with pytest.raises(OpError) as exc:
        hosted_root_of(local)
    assert exc.value.code == "BINDING_CONFLICT"
    refused = run_cli(local, "sync", "--json")
    assert refused.exit_code == 1
    assert json.loads(refused.stdout)["error"]["code"] == "BINDING_CONFLICT"
    assert hosted_root_of(tmp_path / "nowhere") is None


def test_attach_from_a_worktree_prints_a_commit_command_for_the_primary(
    hosted_env: HostedEnv, tmp_path: Path
) -> None:
    """The printed commit command works from the worktree it is run in: it
    names the primary checkout, where attach wrote the files."""
    import shlex

    repo = make_repo(tmp_path / "repo")
    wt = add_worktree(repo, tmp_path / "wt", "feat")
    result = run_cli(wt, "remote", "attach", "team", "demo")
    assert result.exit_code == 0, result.output
    line = next(ln for ln in result.stdout.splitlines() if ln.startswith("Commit: "))
    for part in line.removeprefix("Commit: ").split(" && "):
        subprocess.run(shlex.split(part), cwd=wt, check=True, capture_output=True)
    assert git(repo, "log", "-1", "--format=%s") == "Bind the board to team/demo"
    assert git(repo, "show", "--name-only", "--format=", "HEAD").splitlines() == [
        ".gitignore",
        ".lattice-remote.json",
    ]
    assert git(wt, "log", "-1", "--format=%s") != "Bind the board to team/demo"
