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
from lattice.remote import acked
from tests.test_remote.hosted import PROJECT, HostedEnv, make_repo, run_cli
from tests.test_remote.hosted import hosted_env as hosted_env  # noqa: F401 - fixture
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


def test_remote_verify_reports_a_write_a_restored_backup_lost(
    hosted_env: HostedEnv,  # noqa: F811 - the imported fixture
    tmp_path: Path,
) -> None:
    """AC-46 (H-22): write, 'remote verify' confirms it; restore an older backup
    that lacks the write; 'remote verify' reports it missing and exits 1."""
    import shutil

    repo = make_repo(tmp_path / "repo")
    assert run_cli(repo, "remote", "attach", "team", PROJECT).exit_code == 0
    assert run_cli(repo, "create", "Before the backup", "--actor", "agent:dev").exit_code == 0
    project_dir = hosted_env.server_root / "projects" / PROJECT
    backup = tmp_path / "backup"
    with hosted_env.stopped():  # a backup taken with the server stopped
        shutil.copytree(project_dir, backup, symlinks=True)

    later = run_cli(repo, "create", "After the backup", "--actor", "agent:dev", "--json")
    assert later.exit_code == 0, later.output
    lost = acked.read(repo / ".lattice" / "cache")[-1]
    confirmed = run_cli(repo, "remote", "verify", "--json")
    assert confirmed.exit_code == 0, confirmed.output
    assert json.loads(confirmed.stdout)["data"]["confirmed"] == 2

    with hosted_env.stopped():  # restore the older backup
        shutil.rmtree(project_dir)
        shutil.copytree(backup, project_dir, symlinks=True)

    plain = run_cli(repo, "remote", "verify")
    assert plain.exit_code == 1
    assert f"MISSING {lost['op_id']}" in plain.stdout
    as_json = run_cli(repo, "remote", "verify", "--json")
    assert as_json.exit_code == 1
    data = json.loads(as_json.stdout)["data"]
    assert [m["op_id"] for m in data["missing"]] == [lost["op_id"]]
    assert data["checked"] == 2 and data["confirmed"] == 1
    # Kept: reported again until someone deals with it.
    again = json.loads(run_cli(repo, "remote", "verify", "--json").stdout)["data"]
    assert [m["op_id"] for m in again["missing"]] == [lost["op_id"]]
