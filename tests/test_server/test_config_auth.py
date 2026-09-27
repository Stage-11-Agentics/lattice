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
