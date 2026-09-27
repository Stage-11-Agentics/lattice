"""Operation discovery: built-in modules and the ``lattice.operations`` entry-point group."""

from __future__ import annotations

import json
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest

import lattice.ops.discovery as discovery
from lattice.boards import resolve_board
from lattice.ops import OPERATIONS_GROUP, Caller, registered_operations


def _fake_entry_points(*eps: EntryPoint):  # noqa: ANN202
    def entry_points(*, group: str):  # noqa: ANN202
        return [ep for ep in eps if ep.group == group]

    return entry_points


@pytest.fixture()
def fresh_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(discovery, "_discovered", False)


def test_builtin_operations_are_discovered() -> None:
    names = set(registered_operations())
    assert {"task.create", "task.status", "task.comment", "task.record_auto_review"} <= names


def test_entry_point_operation_is_discovered_and_runs_locally(
    fresh_discovery: None,
    monkeypatch: pytest.MonkeyPatch,
    initialized_root: Path,
) -> None:
    ep = EntryPoint(name="xplugin", value="tests.test_ops.plugin_ops", group=OPERATIONS_GROUP)
    monkeypatch.setattr(discovery, "entry_points", _fake_entry_points(ep))
    monkeypatch.setenv("LATTICE_ROOT", str(initialized_root))

    assert "xplugin.ping" in registered_operations()

    board = resolve_board(initialized_root)
    created = board.execute("task.create", {"title": "Plugin target"}, Caller(actor="human:t"))
    task_id = created.task["id"]
    result = board.execute(
        "xplugin.ping", {"task": task_id, "note": "hello"}, Caller(actor="agent:plugin")
    )

    assert result.value == "hello"
    assert [e["type"] for e in result.events] == ["x_plugin_ping"]
    log = initialized_root / ".lattice" / "events" / f"{task_id}.jsonl"
    last = json.loads(log.read_text().splitlines()[-1])
    assert last["type"] == "x_plugin_ping"
    assert last["origin"]["op"] == "xplugin.ping"


def test_broken_plugin_is_skipped_with_a_message(
    fresh_discovery: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ep = EntryPoint(name="broken", value="tests.test_ops.no_such_module", group=OPERATIONS_GROUP)
    monkeypatch.setattr(discovery, "entry_points", _fake_entry_points(ep))

    names = registered_operations()

    assert "task.create" in names
    assert "failed to load operation plugin 'broken'" in capsys.readouterr().err
