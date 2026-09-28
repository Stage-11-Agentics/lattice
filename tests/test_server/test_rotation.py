"""Epoch rotation (SPEC §8.2) beyond AC-23's stream case: the plan-review
resolution (a heartbeat never names a new epoch before its ``reset``; rotation
keeps deduplication), the offline command, the step order, and a failed rotation."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from click.testing import CliRunner

from lattice.cli.main import cli
from lattice.server.testing import BoardServer, serve_board

HEARTBEAT = 0.2


@pytest.fixture()
def board(tmp_path: Path) -> Iterator[BoardServer]:
    with serve_board(tmp_path, audit=False, heartbeat_seconds=HEARTBEAT) as served:
        yield served


def create(board: BoardServer, title: str = "t") -> str:
    return board.op("task.create", {"title": title})["task"]["id"]


def test_no_heartbeat_names_the_new_epoch_before_its_reset(
    board: BoardServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plan-review resolution 3: rotation publishes the new epoch's state, then
    (here, held) queues ``reset``. Heartbeats during the hold name the old epoch;
    the first heartbeat naming the new epoch follows the reset."""
    create(board)
    old_epoch = board.project.journal.epoch
    entered, release = threading.Event(), threading.Event()
    broadcaster = board.project.broadcaster
    real_reset = broadcaster.reset

    def held_reset(epoch: str) -> None:
        entered.set()
        assert release.wait(10)
        real_reset(epoch)

    with board.stream() as reader:
        assert reader.next().event == "heartbeat"
        monkeypatch.setattr(broadcaster, "reset", held_reset)
        rotation = threading.Thread(target=board.rotate_epoch)
        rotation.start()
        assert entered.wait(10)
        assert board.project.journal.epoch != old_epoch  # published, reset not yet queued
        during = [reader.next_of("heartbeat") for _ in range(3)]
        assert all(b.data["epoch"] == old_epoch for b in during)
        release.set()
        rotation.join(10)
        seen = []
        while not any(m.event == "heartbeat" and m.data["epoch"] != old_epoch for m in seen):
            seen.append(reader.next())
    kinds = [(m.event, m.data.get("epoch")) for m in seen]
    new_epoch = board.project.journal.epoch
    assert kinds.index(("reset", new_epoch)) < kinds.index(("heartbeat", new_epoch))


def test_rotation_keeps_deduplication_and_op_status(board: BoardServer) -> None:
    """Plan-review resolution 4 (SPEC §8.6: rotation does not affect deduplication)."""
    op_id = "op_01J9Z0000000000000000000RT"
    body = {"op_id": op_id, "actor": board.user}
    status, _, first = board.handle.op(
        "demo", "task.create", {"title": "once"}, token=board.token, **body
    )
    assert status == 200 and first["data"]["seq"] == 1
    old_epoch = board.project.journal.epoch
    board.rotate_epoch()
    status, _, again = board.handle.op(
        "demo", "task.create", {"title": "once"}, token=board.token, **body
    )
    assert status == 200
    assert again["data"]["result"]["replayed"] is True
    assert again["data"]["seq"] == 1
    assert board.project.journal.head_seq == 0  # nothing new in the new epoch
    status, _, looked = board.handle.request(
        "GET", f"/v1/projects/demo/ops/{op_id}", token=board.token
    )
    assert status == 200
    assert looked["data"]["state"] == "committed"
    assert looked["data"]["epoch"] == old_epoch and looked["data"]["seq"] == 1


# ---------------------------------------------------------------------------
# Offline rotation (the CLI, no server running)
# ---------------------------------------------------------------------------


def _rotate_cli(root: Path, slug: str = "demo") -> tuple[int, dict]:
    result = CliRunner().invoke(
        cli, ["server", "project", "rotate-epoch", slug, "--root", str(root), "--json"]
    )
    return result.exit_code, json.loads(result.output)


def test_offline_rotation_and_its_refusal_while_an_undo_log_exists(tmp_path: Path) -> None:
    from lattice.server import admin

    root = tmp_path / "server-root"
    admin.init_root(root)
    admin.create_project(root, "demo", code="DEM")
    hosted = root / "projects" / "demo" / ".lattice" / "hosted"
    old_epoch = json.loads((hosted / "journal_meta.json").read_text())["epoch"]
    (hosted / "undo").mkdir(exist_ok=True)
    (hosted / "undo" / "tok_x--op_y.jsonl").write_text('{"epoch":"x"}\n')
    code, refused = _rotate_cli(root)
    assert code != 0 and refused["error"]["code"] == "CONFLICT"
    assert "undo log" in refused["error"]["message"]
    assert json.loads((hosted / "journal_meta.json").read_text())["epoch"] == old_epoch
    (hosted / "undo" / "tok_x--op_y.jsonl").unlink()
    code, done = _rotate_cli(root)
    assert code == 0 and done["ok"] is True
    data = done["data"]
    assert data["via"] == "offline" and data["old_epoch"] == old_epoch
    assert json.loads((hosted / "journal_meta.json").read_text())["epoch"] == data["epoch"]
    assert (hosted / f"journal.{old_epoch}.jsonl").exists()
    assert (hosted / "journal.jsonl").read_bytes() == b""
    assert not (hosted / "rotation.json").exists()


def test_rotation_steps_run_in_the_specified_order(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    """SPEC §8.2: marker, rename, meta, empty journal, marker removed."""
    from lattice.server import admin, journal

    root = tmp_path / "server-root"
    admin.init_root(root)
    admin.create_project(root, "demo", code="DEM")
    steps: list[str] = []
    real_write, real_rename, real_unlink = (
        journal.atomic_write,
        journal._rename,
        journal.unlink_path,
    )
    monkeypatch.setattr(
        journal,
        "atomic_write",
        lambda p, d: (steps.append(f"write {Path(p).name}"), real_write(p, d)),
    )
    monkeypatch.setattr(
        journal,
        "_rename",
        lambda a, b: (steps.append(f"rename {Path(b).name[:8]}"), real_rename(a, b)),
    )
    monkeypatch.setattr(
        journal,
        "unlink_path",
        lambda p, **kw: (steps.append(f"unlink {Path(p).name}"), real_unlink(p, **kw)),
    )
    admin.rotate_project_epoch(root, "demo")
    assert steps == [
        "write rotation.json",
        "rename journal.",
        "write journal_meta.json",
        "write journal.jsonl",
        "unlink rotation.json",
    ]


def test_a_live_project_is_quarantined_if_rotation_fails(
    board: BoardServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lattice.core.errors import OpError
    from lattice.server import journal

    create(board)

    def broken(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(journal, "finish_rotation", broken)
    with board.stream() as reader:
        reader.next()
        with pytest.raises(OpError) as caught:
            board.rotate_epoch()
        assert caught.value.code == "BOARD_UNAVAILABLE"
        assert board.project.state == "unavailable"
        # Its streams are closed: the stream ends, with nothing but heartbeats
        # (queued before the close) ahead of the end.
        deadline = time.monotonic() + 10  # a safety bound only
        while (message := reader.next(timeout=10)) is not None:
            assert message.event == "heartbeat", message
            assert time.monotonic() < deadline, "the stream stayed open"
    assert (board.board / "hosted" / "rotation.json").exists()  # finished at next load
