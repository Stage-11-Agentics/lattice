"""G-10: the server runs no board-configured hook, whatever the board's config says."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from lattice.cli.main import cli
from lattice.server.testing import running_server
from tests.test_server.conftest import mint


def _hooks(sentinel: Path) -> dict:
    touch = f"touch {sentinel}"
    return {
        "post_event": touch,
        "on": {"task_created": touch, "status_changed": touch, "comment_added": touch},
        "transitions": {"* -> *": touch, "backlog -> in_planning": touch},
    }


def test_writes_through_the_server_run_no_hook(root: Path, tmp_path: Path) -> None:
    sentinel = tmp_path / "hook-fired"
    config_path = root / "projects" / "alpha" / ".lattice" / "config.json"
    config = json.loads(config_path.read_text())
    config["hooks"] = _hooks(sentinel)
    config_path.write_text(json.dumps(config, sort_keys=True, indent=2) + "\n")
    token = mint(root)
    with running_server(root) as server:
        status, _, body = server.op("alpha", "task.create", {"title": "hooked"}, token=token)
        assert status == 200, body
        task = body["data"]["result"]["task"]["id"]
        assert (
            server.op(
                "alpha", "task.status", {"task": task, "new_status": "in_planning"}, token=token
            )[0]
            == 200
        )
        assert (
            server.op("alpha", "task.comment", {"task": task, "text": "hi"}, token=token)[0] == 200
        )
    assert not sentinel.exists()

    # Control: the same config on a local board fires the hooks.
    local = tmp_path / "local"
    local.mkdir()
    runner = CliRunner()
    init = runner.invoke(
        cli,
        [
            "init",
            "--path",
            str(local),
            "--actor",
            "human:x",
            "--project-code",
            "LOC",
            "--no-setup-claude",
            "--no-setup-agents",
        ],
    )
    assert init.exit_code == 0
    local_config = local / ".lattice" / "config.json"
    data = json.loads(local_config.read_text())
    data["hooks"] = _hooks(sentinel)
    local_config.write_text(json.dumps(data))
    result = runner.invoke(
        cli, ["create", "hooked", "--actor", "human:x"], env={"LATTICE_ROOT": str(local)}
    )
    assert result.exit_code == 0, result.output
    assert sentinel.exists()
