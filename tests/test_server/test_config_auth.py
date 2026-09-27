"""AC-12 (server half): no operation outside the three board-config operations may
mutate config.json, by any mutation kind; the admin path is ``project config``."""

from __future__ import annotations

from pathlib import Path

import pytest

from lattice.core.errors import OpError
from lattice.server.project import CONFIG_WRITERS, MutationTracker
from lattice.server.testing import ServerHandle
from tests.test_server.conftest import mint


@pytest.mark.parametrize("kind", ["append", "create", "replace", "unlink"])
def test_tracker_refuses_every_kind_for_other_operations(tmp_path: Path, kind: str) -> None:
    board = tmp_path / ".lattice"
    board.mkdir()
    tracker = MutationTracker(board, "task.create")
    with pytest.raises(OpError) as exc:
        tracker(board.resolve() / "config.json", kind)
    assert exc.value.code == "FORBIDDEN"
    assert tracker.kinds == {}


@pytest.mark.parametrize("op", sorted(CONFIG_WRITERS))
@pytest.mark.parametrize("kind", ["append", "create", "replace", "unlink"])
def test_tracker_allows_the_config_operations(tmp_path: Path, op: str, kind: str) -> None:
    board = tmp_path / ".lattice"
    board.mkdir()
    tracker = MutationTracker(board, op)
    tracker(board.resolve() / "config.json", kind)
    assert tracker.kinds == {board.resolve() / "config.json": [kind]}


@pytest.mark.parametrize("kind", ["append", "replace", "unlink"])
def test_an_operation_cannot_mutate_config_over_http(
    server: ServerHandle, root: Path, kind: str
) -> None:
    config = root / "projects" / "alpha" / ".lattice" / "config.json"
    before = config.read_bytes()
    token = mint(root)
    status, _, body = server.op("alpha", "xtest.touch_config", {"kind": kind}, token=token)
    assert status == 403 and body["error"]["code"] == "FORBIDDEN", body
    assert "lattice server project config" in body["error"]["message"]
    assert config.read_bytes() == before


def _config(root: Path) -> dict:
    import json

    return json.loads((root / "projects" / "alpha" / ".lattice" / "config.json").read_text())


def _journal_tail(root: Path) -> dict:
    import json

    path = root / "projects" / "alpha" / ".lattice" / "hosted" / "journal.jsonl"
    return json.loads(path.read_text().splitlines()[-1])


def test_the_board_config_operations_work_over_http(server: ServerHandle, root: Path) -> None:
    token = mint(root)
    status, _, body = server.op(
        "alpha", "board.set_project_code", {"code": "NEW", "force": True}, token=token
    )
    assert status == 200, body
    assert _config(root)["project_code"] == "NEW"
    assert (
        _journal_tail(root)["paths"] == ["config.json", "ids.json"]
        or "config.json" in (_journal_tail(root)["paths"])
    )
    status, _, body = server.op("alpha", "board.set_subproject_code", {"code": "SUB"}, token=token)
    assert status == 200, body
    assert _config(root)["subproject_code"] == "SUB"
    status, _, body = server.op(
        "alpha", "board.set_dashboard_config", {"settings": {"theme": "dark"}}, token=token
    )
    assert status == 200, body
    assert _config(root)["dashboard"]["theme"] == "dark"


@pytest.mark.parametrize(
    "settings",
    [{"review_mode": "triple"}, {"hooks": {"post_event": "touch /tmp/x"}}, {"workflow": {}}],
)
def test_admin_keys_are_forbidden_through_the_dashboard_operation(
    server: ServerHandle, root: Path, settings: dict
) -> None:
    config = root / "projects" / "alpha" / ".lattice" / "config.json"
    before = config.read_bytes()
    token = mint(root)
    status, _, body = server.op(
        "alpha", "board.set_dashboard_config", {"settings": settings}, token=token
    )
    assert status == 403 and body["error"]["code"] == "FORBIDDEN", body
    assert config.read_bytes() == before
