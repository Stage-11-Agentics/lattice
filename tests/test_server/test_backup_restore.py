"""AC-25: tar a stopped project, extract it under a new root, serve it: doctor is clean
and every durable file's hash equals the original's."""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

from click.testing import CliRunner

from lattice.cli.main import cli
from lattice.server import admin
from lattice.server.testing import running_server
from tests.test_server.conftest import board_hash, create_task, mint


def test_backup_and_restore(root: Path, tmp_path: Path) -> None:
    token = mint(root)
    with running_server(root) as server:
        task = create_task(server, token, title="survives")
        server.op("alpha", "task.comment", {"task": task["id"], "text": "kept"}, token=token)
    original = board_hash(root, "alpha")
    archive = tmp_path / "alpha.tar"
    with tarfile.open(archive, "w") as tar:
        tar.add(root / "projects" / "alpha", arcname="alpha")

    new_root = tmp_path / "restored"
    admin.init_root(new_root)
    with tarfile.open(archive) as tar:
        tar.extractall(new_root / "projects", filter="tar")
    new_token = mint(new_root)
    with running_server(new_root) as server:
        assert server.project("alpha").state == "loaded"
        status, _, body = server.request("GET", "/v1/projects/alpha/tasks/ALP-1", token=new_token)
        assert status == 200 and body["data"]["snapshot"]["title"] == "survives"
    assert board_hash(new_root, "alpha") == original
    doctor = CliRunner().invoke(
        cli, ["doctor", "--json"], env={"LATTICE_ROOT": str(new_root / "projects" / "alpha")}
    )
    payload = json.loads(doctor.output)
    assert payload["ok"] is True
    assert not [f for f in payload["data"]["findings"] if f.get("level") == "error"]
