"""G-1 over HTTP: hostile names never build a path, and nothing outside the target
board's durable paths changes. (The attach-payload case is H-12's; its naming rule is
unit-tested in test_ops_limits.py.)"""

from __future__ import annotations

from pathlib import Path

import pytest

from lattice.server.testing import ServerHandle
from lattice.storage.ownership import owning_board
from lattice.storage.sessions import create_session
from tests.test_server.conftest import mint, tree_hash


def _outside_target(root: Path) -> dict[str, str]:
    """Hashes of every file under the server root except the target board's durable data,
    its locks, and server logs that legitimately change on a request."""
    target = root / "projects" / "alpha" / ".lattice"
    hashes = tree_hash(root)
    return {
        k: v
        for k, v in hashes.items()
        if not (root / k).is_relative_to(target) and k != "server_status.json"
    }


@pytest.mark.parametrize(
    "envelope",
    [
        {"op_id": "op_../../x"},
        {"op_id": "op_" + "../" * 9},
        {"op_id": "../../../tmp/x"},
        {"actor_name": "../../beta/.lattice/sessions/Vesper-1"},
        {"actor_name": "../../../tokens"},
    ],
)
def test_hostile_names_are_refused(server: ServerHandle, root: Path, envelope: dict) -> None:
    beta = root / "projects" / "beta" / ".lattice"
    with owning_board(beta):
        create_session(beta, base_name="Vesper", model="m", framework="pytest")
    token = mint(root, actors=["*:*"])
    before = _outside_target(root)
    status, _, body = server.op("alpha", "task.create", {"title": "x"}, token=token, **envelope)
    assert status == 400 and body["error"]["code"] == "VALIDATION_ERROR", body
    assert _outside_target(root) == before
    assert not list((root / "projects" / "alpha" / ".lattice" / "events").glob("task_*"))


@pytest.mark.parametrize("slug", ["..", "...", "a/../b", ".creating-x"])
def test_hostile_project_slugs(server: ServerHandle, root: Path, slug: str) -> None:
    token = mint(root)
    before = tree_hash(root)
    status, _, _ = server.op(slug, "task.create", {"title": "x"}, token=token)
    assert status in (403, 404)
    after = tree_hash(root)
    assert {k: v for k, v in after.items() if k != "server_status.json"} == {
        k: v for k, v in before.items() if k != "server_status.json"
    }


@pytest.mark.parametrize("name", ["../../tmp/x", "../../../tokens.json", "a/b", "..", "x\x00"])
def test_hostile_resource_names_are_refused(server: ServerHandle, root: Path, name: str) -> None:
    token = mint(root)
    before = _outside_target(root)
    for op in ("resource.create", "resource.acquire"):
        status, _, body = server.op("alpha", op, {"name": name}, token=token)
        assert status == 400 and body["error"]["code"] == "VALIDATION_ERROR", (op, body)
    assert _outside_target(root) == before
    assert not list((root / "projects" / "alpha" / ".lattice" / "resources").iterdir())
