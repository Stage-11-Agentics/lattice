"""``watch``, ``wait``, and ``dashboard`` on a hosted checkout (landing check):
they catch up before their first read like every command, so a binding-only
checkout is bootstrapped and answers correctly, and an unconfigured remote is
the typed ``REMOTE_NOT_CONFIGURED`` (SPEC §9.5, §9.1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_remote.hosted import HostedEnv, make_repo, run_cli


@pytest.fixture()
def clone(hosted_env: HostedEnv, tmp_path: Path) -> Path:
    """A fresh clone holding only the binding; the server already has DEM-1."""
    hosted_env.server_op("task.create", {"title": "Waited on"}, actor="human:alice")
    repo = make_repo(tmp_path / "clone")
    hosted_env.bind(repo)
    assert not (repo / ".lattice").exists()
    return repo


def test_wait_bootstraps_a_binding_only_checkout(clone: Path) -> None:
    result = run_cli(clone, "wait", "DEM-1", "--status", "backlog", "--timeout", "3", "--json")
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    assert data["all_complete"] is True
    assert (clone / ".lattice" / "cache" / "state.json").is_file()


def test_watch_bootstraps_and_resolves_short_ids(clone: Path) -> None:
    result = run_cli(clone, "watch", "--task", "DEM-1", "--timeout", "1", "--json")
    assert result.exit_code == 0, result.output
    assert "not found" not in result.output.lower()
    assert (clone / ".lattice" / "cache" / "state.json").is_file()


def test_dashboard_bootstraps_before_reading_config(
    clone: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lattice.dashboard import server as dashboard_server

    seen: dict = {}

    def create_server(lattice_dir, host, port, **kwargs):  # noqa: ANN001, ANN202
        seen["config"] = json.loads((Path(lattice_dir) / "config.json").read_text())
        seen["port"] = port
        raise SystemExit(0)

    monkeypatch.setattr(dashboard_server, "create_server", create_server)
    result = run_cli(clone, "dashboard")
    assert result.exit_code == 0, result.output
    assert seen["config"]["project_code"] == "DEM"
    assert (clone / ".lattice" / "cache" / "state.json").is_file()


@pytest.mark.parametrize(
    "argv",
    [
        ["wait", "DEM-1", "--status", "backlog", "--timeout", "1"],
        ["watch", "--task", "DEM-1", "--timeout", "1"],
        ["dashboard"],
    ],
    ids=["wait", "watch", "dashboard"],
)
@pytest.mark.parametrize("as_json", [False, True], ids=["plain", "json"])
def test_an_unconfigured_remote_is_typed(
    hosted_env: HostedEnv, tmp_path: Path, argv: list[str], as_json: bool
) -> None:
    repo = make_repo(tmp_path / "repo")
    (repo / ".lattice-remote.json").write_text(
        json.dumps({"remote": "nowhere", "project": "demo"})
    )
    result = run_cli(repo, *argv, *(["--json"] if as_json else []))
    assert result.exit_code == 1, result.output
    if as_json:
        error = json.loads(result.stdout)["error"]
        assert error["code"] == "REMOTE_NOT_CONFIGURED"
        assert "lattice remote add nowhere <url> --token-env <VAR>" in error["message"]
    else:
        assert result.stderr.startswith("Error: no remote named 'nowhere'")
        assert "lattice remote add nowhere <url> --token-env <VAR>" in result.stderr
    assert not (repo / ".lattice").exists()
