"""G-10 (client side): a hosted board's hooks run on a client only when its
remote sets ``run_board_hooks: true``, and never see the remote's credentials
(SPEC §3.4)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_remote.hosted import TOKEN_ENV, HostedEnv, make_repo, run_cli, walk_to


def _add_hooks(env: HostedEnv, sentinels: Path) -> None:
    """Hand-edit the server board's config (the server journals the edit)."""
    path = env.board / "config.json"
    config = json.loads(path.read_text())
    config["hooks"] = {
        "post_event": f"env > {sentinels}/post-$LATTICE_EVENT_TYPE-$LATTICE_EVENT_ID.env",
        "on": {"comment_added": f"touch {sentinels}/on-comment-$LATTICE_EVENT_ID"},
        "transitions": {"* -> in_planning": f"touch {sentinels}/transition-$LATTICE_EVENT_ID"},
    }
    path.chmod(0o600)
    path.write_text(json.dumps(config, indent=2) + "\n")


def _drive(repo: Path) -> None:
    assert run_cli(repo, "create", "Hooked", "--actor", "agent:dev").exit_code == 0
    assert run_cli(repo, "comment", "DEM-1", "hello", "--actor", "agent:dev").exit_code == 0
    walk_to(repo, "DEM-1", "in_planning")


@pytest.fixture()
def sentinels(tmp_path: Path) -> Path:
    path = tmp_path / "sentinels"
    path.mkdir()
    return path


def test_hooks_do_not_run_by_default(
    hosted_env: HostedEnv, tmp_path: Path, sentinels: Path
) -> None:
    _add_hooks(hosted_env, sentinels)
    repo = make_repo(tmp_path / "repo")
    assert run_cli(repo, "remote", "attach", "team", "demo").exit_code == 0
    assert "hooks" in json.loads((repo / ".lattice" / "config.json").read_text())
    _drive(repo)
    assert list(sentinels.iterdir()) == []  # neither the server nor this client ran one


def test_opted_in_client_runs_hooks_without_credentials(
    hosted_env: HostedEnv, tmp_path: Path, sentinels: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_hooks(hosted_env, sentinels)
    monkeypatch.setenv("PROXY_ID_VAR", "proxy-value")
    monkeypatch.setenv("LATTICE_REMOTE_TEAM_URL", hosted_env.url)
    hosted_env.write_remote(run_board_hooks=True, headers={"X-Proxy-Id": {"env": "PROXY_ID_VAR"}})
    repo = make_repo(tmp_path / "repo")
    assert run_cli(repo, "remote", "attach", "team", "demo").exit_code == 0
    _drive(repo)

    names = sorted(p.name for p in sentinels.iterdir())
    assert any(n.startswith("post-task_created-") for n in names)
    assert any(n.startswith("post-comment_added-") for n in names)
    assert any(n.startswith("on-comment-") for n in names)
    assert any(n.startswith("transition-") for n in names)
    for dump in sentinels.glob("post-*.env"):
        env = dict(line.split("=", 1) for line in dump.read_text().splitlines() if "=" in line)
        assert TOKEN_ENV not in env
        assert "PROXY_ID_VAR" not in env
        assert not [k for k in env if k.startswith("LATTICE_REMOTE_")]
        assert hosted_env.token not in dump.read_text()
        assert env["LATTICE_ROOT"] == str(repo)
