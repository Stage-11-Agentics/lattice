"""Operation discovery: built-in modules and the ``lattice.operations`` entry-point group."""

from __future__ import annotations

import json
import subprocess
import sys
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest

import lattice.ops.base as base
import lattice.ops.discovery as discovery
from lattice.boards import resolve_board
from lattice.ops import OPERATIONS_GROUP, Caller, OpError, get_operation, registered_operations


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


def test_concurrent_first_lookups_see_the_whole_registry(fresh_discovery: None) -> None:
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _i: set(registered_operations()), range(8)))
    assert all("task.status" in names for names in results)


def test_every_builtin_operation_lives_in_its_named_module() -> None:
    """``get_operation`` imports ``lattice.ops.<group>_<verb>`` first; a built-in
    registered elsewhere would still resolve, but only through full discovery."""
    builtins = {
        name: cls
        for name, cls in registered_operations().items()
        if cls.__module__.startswith("lattice.ops.")
    }
    assert builtins
    assert {
        n: c.__module__
        for n, c in builtins.items()
        if c.__module__ != f"lattice.ops.{n.replace('.', '_')}"
    } == {}


def test_builtin_lookup_imports_only_its_module() -> None:
    """A CLI write resolves one operation without importing the others or scanning
    entry points (LAT-359: full discovery cost ~40 ms per command)."""
    code = (
        "import sys, importlib.metadata as md\n"
        "import lattice.ops.discovery as d\n"
        "def boom(**kw): raise AssertionError('entry points scanned')\n"
        "d.entry_points = boom\n"
        "from lattice.ops import get_operation\n"
        "assert get_operation('task.status').name == 'task.status'\n"
        "assert get_operation('task.create').name == 'task.create'\n"
        "assert not d._discovered\n"
        "assert 'lattice.ops.task_comment' not in sys.modules\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_plugin_lookup_falls_back_to_full_discovery(
    fresh_discovery: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    ep = EntryPoint(name="xplugin", value="tests.test_ops.plugin_ops", group=OPERATIONS_GROUP)
    monkeypatch.setattr(discovery, "entry_points", _fake_entry_points(ep))
    monkeypatch.setattr(
        base, "_REGISTRY", {k: v for k, v in base._REGISTRY.items() if k != "xplugin.ping"}
    )

    assert get_operation("xplugin.ping").name == "xplugin.ping"
    assert discovery._discovered


def test_unknown_operation_still_raises_after_full_discovery(fresh_discovery: None) -> None:
    for name in ("task.no_such_verb", "discovery", "../etc.passwd"):
        with pytest.raises(OpError) as err:
            get_operation(name)
        assert err.value.code == "UNKNOWN_OP"
