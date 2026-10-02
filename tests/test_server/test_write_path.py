"""The journal line of a server write (SPEC §8.6): ``paths`` holds every durable path
changed; ``lengths`` only logs the write did nothing to but append (and that survive)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lattice.server.floors import ShortIdFloors
from lattice.server.project import MutationTracker
from lattice.server.testing import ServerHandle, running_server
from tests.test_server.conftest import create_task, mint


def test_lengths_only_for_append_only_survivors(tmp_path: Path) -> None:
    board = tmp_path / ".lattice"
    board.mkdir()
    files = {
        "appended.jsonl": ["append", "append"],
        "created.jsonl": ["create"],
        "append_then_unlink.jsonl": ["append", "unlink"],
        "replace_then_append.jsonl": ["replace", "append"],
        "append_then_replace.jsonl": ["append", "replace"],
        "gone.jsonl": ["append"],
    }
    tracker = MutationTracker(board, "task.archive")
    for name, kinds in files.items():
        if name != "gone.jsonl":
            (board / name).write_text("0123456789")
        for kind in kinds:
            tracker(board.resolve() / name, kind)
    assert tracker.lengths() == {"appended.jsonl": 10}
    assert tracker.relative_paths() == sorted(files)
    assert tracker.kinds[board.resolve() / "replace_then_append.jsonl"] == ["replace", "append"]


def _journal(root: Path) -> list[dict]:
    path = root / "projects" / "alpha" / ".lattice" / "hosted" / "journal.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_journal_lines_of_real_writes(server: ServerHandle, root: Path) -> None:
    token = mint(root)
    task = create_task(server, token)
    board = root / "projects" / "alpha" / ".lattice"
    log = f"events/{task['id']}.jsonl"
    status, _, body = server.op(
        "alpha", "task.comment", {"task": task["id"], "text": "hello"}, token=token
    )
    assert status == 200
    created, commented = _journal(root)
    assert created["seq"] == 1 and commented["seq"] == 2
    assert created["op"] == "task.create" and created["task_id"] == task["id"]
    assert created["token_id"].startswith("tok_") and len(created["fp"]) == 32
    assert log in created["paths"] and f"tasks/{task['id']}.json" in created["paths"]
    assert created["lengths"][log] < commented["lengths"][log]
    assert commented["lengths"][log] == (board / log).stat().st_size
    assert f"tasks/{task['id']}.json" not in commented["lengths"]
    assert commented["event_ids"] == [e["id"] for e in body["data"]["result"]["events"]]
    # a no-op still commits a journal line
    server.op("alpha", "xtest.sleep", {"ms": 0}, token=token)
    last = _journal(root)[-1]
    assert last["op"] == "xtest.sleep" and last["paths"] == [] and last["lengths"] == {}


def test_short_id_floors_follow_creates(server: ServerHandle, root: Path) -> None:
    token = mint(root)
    for _ in range(3):
        create_task(server, token)
    floors = server.project("alpha").floors
    assert floors.max_observed == {"ALP": 3} and floors.floor_for("ALP") == 4
    assert floors.event_short_ids == {"ALP-1", "ALP-2", "ALP-3"}
    rescanned = ShortIdFloors.from_board(root / "projects" / "alpha" / ".lattice")
    assert rescanned.max_observed == {"ALP": 3}
    assert rescanned.event_short_ids == floors.event_short_ids
    assert rescanned.floor_for("NEW") == 1


def test_allocation_uses_the_in_memory_floor(
    server: ServerHandle, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import lattice.storage.operations as operations

    token = mint(root)
    create_task(server, token)
    calls = []
    real = operations.short_id_inventory

    def counting(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        calls.append(args[0])
        return real(*args, **kwargs)

    monkeypatch.setattr(operations, "short_id_inventory", counting)
    task = create_task(server, token)
    assert task["short_id"] == "ALP-2"
    assert calls == []  # the server's floor, never a rescan


def test_a_regressed_ids_json_never_reissues_a_logged_id(root: Path) -> None:
    token = mint(root)
    with running_server(root) as server:
        issued = [create_task(server, token)["short_id"] for _ in range(3)]
    board = root / "projects" / "alpha" / ".lattice"
    index = json.loads((board / "ids.json").read_text())
    index["next_seqs"]["ALP"] = 1
    index["map"] = {}
    (board / "ids.json").write_text(json.dumps(index))
    with running_server(root) as server:
        assert server.project("alpha").floors.max_observed == {"ALP": 3}
        fresh = create_task(server, token)["short_id"]
    assert fresh not in issued and fresh == "ALP-4"


def test_server_startup_floor_includes_map_only_reservations(root: Path) -> None:
    token = mint(root)
    with running_server(root) as server:
        existing = create_task(server, token)
    board = root / "projects" / "alpha" / ".lattice"
    index = json.loads((board / "ids.json").read_text())
    index["next_seqs"]["ALP"] = 2
    index["map"]["ALP-8"] = existing["id"]
    (board / "ids.json").write_text(json.dumps(index))

    with running_server(root) as server:
        floors = server.project("alpha").floors
        assert floors.max_observed == {"ALP": 8}
        assert floors.event_short_ids == {"ALP-1"}
        assert create_task(server, token)["short_id"] == "ALP-9"


def test_the_write_seam_passes_one_fresh_config(
    server: ServerHandle, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B3: run_write reads config.json once under the work lock and hands that object
    to execute(), so rules and policy see exactly one configuration."""
    import lattice.server.project as project_module

    seen: list[dict] = []
    real = project_module.execute

    def spy(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        seen.append(kwargs.get("config"))
        return real(*args, **kwargs)

    monkeypatch.setattr(project_module, "execute", spy)
    token = mint(root)
    create_task(server, token)
    board = root / "projects" / "alpha" / ".lattice"
    assert seen and isinstance(seen[0], dict)
    assert seen[0] == json.loads((board / "config.json").read_text())
    assert seen[0]["project_code"] == "ALP"
