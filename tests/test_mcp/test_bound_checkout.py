"""The MCP server refuses a checkout bound to a Lattice server (LAT-346)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lattice.mcp.resources import resource_all_tasks
from lattice.mcp.tools import lattice_create, lattice_list
from lattice.storage.fs import BINDING_FILE, BoundCheckoutError


@pytest.fixture()
def bound_env(lattice_env: Path) -> Path:
    (lattice_env / BINDING_FILE).write_text(
        json.dumps({"remote": "http://127.0.0.1:8765", "project": "demo"}), encoding="utf-8"
    )
    return lattice_env


def _tree(root: Path) -> list[tuple[str, int]]:
    return [(str(p.relative_to(root)), p.stat().st_mtime_ns) for p in sorted(root.rglob("*"))]


def test_tool_read_refuses(bound_env: Path) -> None:
    with pytest.raises(BoundCheckoutError, match="install Lattice v2"):
        lattice_list()


def test_tool_write_refuses_and_changes_nothing(bound_env: Path) -> None:
    before = _tree(bound_env)
    with pytest.raises(BoundCheckoutError):
        lattice_create(title="New", actor="human:test")
    assert _tree(bound_env) == before


def test_explicit_lattice_root_refuses(bound_env: Path, monkeypatch) -> None:
    monkeypatch.delenv("LATTICE_ROOT")
    with pytest.raises(BoundCheckoutError):
        lattice_list(lattice_root=str(bound_env))


def test_resource_refuses(bound_env: Path) -> None:
    with pytest.raises(BoundCheckoutError):
        resource_all_tasks()


def test_unbound_unaffected(lattice_env: Path) -> None:
    lattice_create(title="Local", actor="human:test")
    assert [t["title"] for t in lattice_list()] == ["Local"]


def test_explicit_root_in_worktree_of_bound_primary_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_cli.test_bound_checkout import bound_primary_with_worktree

    monkeypatch.delenv("LATTICE_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    _primary, worktree = bound_primary_with_worktree(tmp_path)
    with pytest.raises(BoundCheckoutError):
        lattice_list(lattice_root=str(worktree))
