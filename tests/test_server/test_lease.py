"""AC-3 (running server): one writer per board. A second server, a local CLI write, and
offline maintenance are refused while a server holds the project."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from lattice.cli.main import cli
from lattice.core.errors import OpError
from lattice.server.project import UNAVAILABLE, Project
from lattice.server.log import ServerLog
from lattice.server.serve import acquire_server_lock
from lattice.server.testing import running_server
from lattice.storage.ownership import offline_maintenance
from tests.test_server.conftest import board_hash, mint


def test_a_second_server_on_the_root_fails_to_start(root: Path) -> None:
    with running_server(root):
        with pytest.raises(OpError) as exc:
            acquire_server_lock(root)
        assert "already running" in exc.value.message
        result = CliRunner().invoke(cli, ["server", "serve", "--root", str(root), "--port", "0"])
        assert result.exit_code == 1 and "already running" in result.output
        result = CliRunner().invoke(
            cli, ["server", "serve", "--root", str(root), "--port", "0", "--json"]
        )
        assert result.exit_code == 1
        envelope = json.loads(result.output)
        assert envelope["ok"] is False and "already running" in envelope["error"]["message"]


def test_a_second_owner_of_a_held_board_is_refused(root: Path) -> None:
    with running_server(root) as server:
        assert server.project("alpha").state == "loaded"
        rival = Project("alpha", root / "projects" / "alpha", ServerLog("warning"), "rival")
        rival.load()
        assert rival.state == UNAVAILABLE and "owner lease" in rival.reason


def test_a_local_cli_write_is_refused(root: Path) -> None:
    token = mint(root)
    with running_server(root) as server:
        assert server.op("alpha", "task.create", {"title": "x"}, token=token)[0] == 200
        before = board_hash(root, "alpha")
        result = CliRunner().invoke(
            cli,
            ["create", "sneaky", "--actor", "human:x", "--json"],
            env={"LATTICE_ROOT": str(root / "projects" / "alpha")},
        )
        assert result.exit_code == 1
        assert json.loads(result.output)["error"]["code"] == "BOARD_IS_HOSTED"
        assert board_hash(root, "alpha") == before


def test_offline_maintenance_is_refused_while_serving(root: Path) -> None:
    board = root / "projects" / "alpha" / ".lattice"
    with running_server(root):
        with pytest.raises(OpError) as exc:
            with offline_maintenance(board, "rebuild"):
                pass
        assert exc.value.code == "BOARD_IS_HOSTED"
        result = CliRunner().invoke(
            cli,
            ["rebuild", "--all", "--offline-maintenance", "--json"],
            env={"LATTICE_ROOT": str(root / "projects" / "alpha")},
        )
        assert result.exit_code == 1
        assert json.loads(result.output)["error"]["code"] == "BOARD_IS_HOSTED"
    # with the server stopped, offline maintenance runs and the next load rotates the epoch
    with offline_maintenance(board, "rebuild"):
        pass
    assert (board / "hosted" / "maintenance.json").exists()


def test_a_stale_owner_marker_is_taken_over(root: Path) -> None:
    owner = root / "projects" / "alpha" / ".lattice" / "hosted" / "owner.json"
    owner.write_text(json.dumps({"server_id": "srv_dead", "host": "x", "pid": 1}))
    with running_server(root) as server:
        takeovers = [x for x in server.log_lines if x["event"] == "lease_takeover"]
        assert any(
            t["project"] == "alpha" and t["previous"]["server_id"] == "srv_dead" for t in takeovers
        )
        assert json.loads(owner.read_text())["server_id"] == server.state.server_id


def test_serve_errors_render_as_json(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        cli, ["server", "serve", "--root", str(tmp_path / "missing"), "--json"]
    )
    assert result.exit_code == 1
    assert json.loads(result.output)["error"]["code"] == "NOT_INITIALIZED"
