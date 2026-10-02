"""Admin commands: server init, project create/list/unlock/config (AC-17 create, AC-49 server side)."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from click.testing import CliRunner

from lattice.cli.main import cli
from lattice.core.errors import OpError
from lattice.server import admin
from lattice.server.journal import Journal
from lattice.storage.ownership import board_state

_INIT_ONLY_KEYS = {"instance_id", "default_actor"}


def _invoke(*args: str, env: dict | None = None):
    return CliRunner().invoke(cli, list(args), env=env, catch_exceptions=False)


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    admin.init_root(root)
    return root


def test_init_is_idempotent_and_private(tmp_path: Path) -> None:
    root = tmp_path / "root"
    first = _invoke("server", "init", "--root", str(root), "--json")
    assert first.exit_code == 0
    assert json.loads(first.output)["data"]["created"] == ["server.json", "tokens.json"]
    second = _invoke("server", "init", "--root", str(root), "--json")
    assert json.loads(second.output)["data"]["created"] == []
    assert stat.S_IMODE((root / "tokens.json").stat().st_mode) == 0o600
    assert json.loads((root / "tokens.json").read_text()) == {"tokens": []}


def test_project_create_yields_a_board_that_passes_doctor(root: Path) -> None:
    result = _invoke("server", "project", "create", "demo", "--code", "dem", "--root", str(root))
    assert result.exit_code == 0, result.output
    project = root / "projects" / "demo"
    board = project / ".lattice"
    assert board_state(board) == "hosted"
    journal = Journal.load(board)
    assert journal.head_seq == 0 and journal.epoch.startswith("ep_")
    doctor = _invoke("doctor", "--json", env={"LATTICE_ROOT": str(project)})
    payload = json.loads(doctor.output)
    assert payload["ok"] is True, doctor.output
    assert not [f for f in payload["data"]["findings"] if f.get("level") == "error"]
    assert not list((root / "projects").glob(".creating-*"))


def test_project_create_refuses_an_existing_slug_and_bad_input(root: Path) -> None:
    admin.create_project(root, "demo")
    with pytest.raises(OpError) as exc:
        admin.create_project(root, "demo")
    assert exc.value.code == "CONFLICT"
    for slug in ("Demo", "-x", "a" * 64, "../x", ""):
        with pytest.raises(OpError):
            admin.create_project(root, slug)
    with pytest.raises(OpError):
        admin.create_project(root, "x", code="toolong")
    with pytest.raises(OpError):
        admin.create_project(root, "y", subproject_code="F")


@pytest.mark.parametrize(
    ("server_args", "init_args"),
    [
        ([], []),
        (["--review-mode", "triple"], ["--review-mode", "triple"]),
        (["--plan-review-mode", "inline"], ["--plan-review-mode", "inline"]),
        (["--plan-approval", "human"], ["--plan-approval", "human"]),
        (["--subproject-code", "F"], ["--subproject-code", "F"]),
    ],
)
def test_project_create_writes_the_config_init_writes(
    root: Path, tmp_path: Path, server_args: list[str], init_args: list[str]
) -> None:
    _invoke("server", "project", "create", "p", "--code", "PRJ", *server_args, "--root", str(root))
    local = tmp_path / "local"
    local.mkdir()
    result = _invoke(
        "init",
        "--path",
        str(local),
        "--actor",
        "human:x",
        "--project-code",
        "PRJ",
        "--no-setup-claude",
        "--no-setup-agents",
        *init_args,
    )
    assert result.exit_code == 0, result.output

    def config(board: Path) -> dict:
        data = json.loads((board / "config.json").read_text())
        return {k: v for k, v in data.items() if k not in _INIT_ONLY_KEYS}

    served = root / "projects" / "p" / ".lattice"
    assert config(served) == config(local / ".lattice")
    for name in ("context.md", "ids.json", ".gitignore"):
        assert (served / name).read_bytes() == (local / ".lattice" / name).read_bytes()


def test_review_toggles(root: Path) -> None:
    admin.create_project(root, "p", auto_code_review=False, auto_plan_review=True)
    config = json.loads((root / "projects" / "p" / ".lattice" / "config.json").read_text())
    assert config["auto_code_review_on_transition"] is False
    assert config["auto_plan_review_on_transition"] is True


def test_project_list(root: Path) -> None:
    admin.create_project(root, "b")
    admin.create_project(root, "a", code="AAA")
    (root / "projects" / ".creating-c-x").mkdir()
    rows = admin.list_projects(root)
    assert [r["slug"] for r in rows] == ["a", "b"]
    assert rows[0]["project_code"] == "AAA"
    assert rows[0]["state"] == "unloaded" and rows[0]["head_seq"] == 0


def test_project_config_refuses_before_writing(root: Path) -> None:
    admin.create_project(root, "p")
    config_path = root / "projects" / "p" / ".lattice" / "config.json"
    before = config_path.read_bytes()
    for bad in (
        {"workflow": "x"},
        {"review_mode": "double"},
        {"plan_approval": "maybe"},
        {"hooks": "x"},
        {"auto_code_review_on_transition": "yes"},
        {"task_types": []},
        {"task_types": ["bug", "chore"]},
        {"unallowlisted": "x"},
    ):
        with pytest.raises(OpError) as exc:
            admin.set_project_config(root, "p", bad)
        assert exc.value.code == "VALIDATION_ERROR"
    assert config_path.read_bytes() == before
    assert not (root / "projects" / "p" / ".lattice" / "hosted" / "maintenance.json").exists()
    result = _invoke("server", "project", "config", "p", "--set", "hooks=x", "--root", str(root))
    assert result.exit_code == 1 and "cannot be set" in result.output


@pytest.mark.parametrize(
    "assignment",
    [
        "task_types=[",
        'task_types={"task":true}',
        'task_types="task"',
        'task_types=["task", 1]',
        'task_types=["task",""]',
        'task_types=["task","  "]',
        'task_types=["task","bug","bug"]',
        'task_types=["bug"]',
        'task_types=["task "]',
    ],
)
def test_task_types_assignment_validation(assignment: str) -> None:
    if assignment == "task_types=[":
        with pytest.raises(OpError, match="task_types must be a JSON array"):
            admin.parse_config_assignments([assignment])
        return
    with pytest.raises(OpError):
        admin.validate_config_changes(admin.parse_config_assignments([assignment]))


def test_task_types_json_array_validation_is_idempotent() -> None:
    parsed = admin.parse_config_assignments(['task_types=["task","bug","research"]'])
    once = admin.validate_config_changes(parsed)
    twice = admin.validate_config_changes(once)
    assert once == twice == {"task_types": ["task", "bug", "research"]}


def test_task_types_is_not_a_string_choice_and_help_names_it() -> None:
    assert "task_types" not in admin.CONFIG_CHOICES
    result = _invoke("server", "project", "config", "--help")
    assert result.exit_code == 0
    assert "task_types" in result.output


def test_project_config_with_the_server_stopped_writes_config_and_maintenance(
    root: Path,
) -> None:
    admin.create_project(root, "p")
    board = root / "projects" / "p" / ".lattice"
    result = _invoke(
        "server",
        "project",
        "config",
        "p",
        "--set",
        "review_mode=triple",
        "--set",
        "auto_plan_review_on_transition=false",
        "--set",
        'task_types=["task","bug","chore","research"]',
        "--root",
        str(root),
    )
    assert result.exit_code == 0, result.output
    config = json.loads((board / "config.json").read_text())
    assert config["review_mode"] == "triple"
    assert config["auto_plan_review_on_transition"] is False
    assert config["task_types"] == ["task", "bug", "chore", "research"]
    record = json.loads((board / "hosted" / "maintenance.json").read_text())
    assert record["command"] == "project config"


def test_unlock_removes_a_stale_marker(root: Path) -> None:
    admin.create_project(root, "p")
    marker = root / "projects" / "p" / ".lattice" / "hosted" / "owner.json"
    assert marker.exists()
    assert admin.unlock_project(root, "p") == {"slug": "p", "removed": True}
    assert not marker.exists()
    assert admin.unlock_project(root, "p")["removed"] is False


def test_unlock_refused_while_the_lease_is_held(root: Path) -> None:
    from lattice.storage.ownership import release_owner_flock, try_owner_flock

    admin.create_project(root, "p")
    fd = try_owner_flock(root / "projects" / "p" / ".lattice")
    try:
        with pytest.raises(OpError) as exc:
            admin.unlock_project(root, "p")
        assert exc.value.code == "BOARD_BUSY"
    finally:
        release_owner_flock(fd)


def test_commands_need_an_initialized_root(tmp_path: Path) -> None:
    result = _invoke("server", "project", "list", "--root", str(tmp_path / "none"), "--json")
    assert result.exit_code == 1
    assert json.loads(result.output)["error"]["code"] == "NOT_INITIALIZED"
    assert not os.path.exists(tmp_path / "none")
