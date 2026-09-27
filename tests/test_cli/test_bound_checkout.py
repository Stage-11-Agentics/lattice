"""A v1 lattice refuses a checkout bound to a Lattice server (LAT-346).

Lattice v2 binds a checkout with a committed ``.lattice-remote.json`` and keeps
``.lattice/`` as a read-only cache of the server's board. A v1 lattice on PATH
must refuse such a checkout before it reads or writes anything.
"""

from __future__ import annotations

import json
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from lattice.cli.main import cli
from lattice.storage.fs import (
    BINDING_FILE,
    LATTICE_DIR,
    BoundCheckoutError,
    find_root,
)

BOUND_MESSAGE = (
    "Error: this checkout is bound to a Lattice server (.lattice-remote.json). "
    "This lattice is v1; install Lattice v2 to use it."
)
BINDING = {"remote": "http://127.0.0.1:8765", "project": "demo"}


def _bind(root: Path) -> None:
    (root / BINDING_FILE).write_text(json.dumps(BINDING) + "\n", encoding="utf-8")


def _tree(root: Path) -> dict[str, tuple[bytes, int] | None]:
    """Every path under *root* with its content and mtime (``None`` for dirs)."""
    state: dict[str, tuple[bytes, int] | None] = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        state[rel] = None if path.is_dir() else (path.read_bytes(), path.stat().st_mtime_ns)
    return state


def _command_paths(group: click.Group, prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    paths: list[tuple[str, ...]] = []
    for name, command in sorted(group.commands.items()):
        path = (*prefix, name)
        paths.append(path)
        if isinstance(command, click.Group):
            paths.extend(_command_paths(command, path))
    return paths


ALL_COMMANDS = _command_paths(cli)


@pytest.fixture()
def bound_root(initialized_root: Path) -> Path:
    """A checkout holding a v1-shaped board with a v2 binding beside it."""
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["create", "Existing task", "--actor", "human:test"],
        env={"LATTICE_ROOT": str(initialized_root)},
    )
    assert result.exit_code == 0, result.output
    _bind(initialized_root)
    return initialized_root


def _run(args: list[str], env: dict[str, str | None]):
    return CliRunner().invoke(cli, args, env=env)


class TestEveryCommandRefuses:
    @pytest.mark.parametrize("command", ALL_COMMANDS, ids=" ".join)
    def test_command_refuses_and_changes_nothing(
        self, bound_root: Path, command: tuple[str, ...]
    ) -> None:
        before = _tree(bound_root)
        result = _run(list(command), {"LATTICE_ROOT": str(bound_root)})
        assert result.exit_code != 0
        assert BOUND_MESSAGE in result.output
        assert "Traceback" not in result.output
        assert _tree(bound_root) == before

    def test_covers_every_command_group(self) -> None:
        names = {path[0] for path in ALL_COMMANDS}
        # One representative per group; a new group is picked up automatically.
        assert {"init", "create", "list", "status", "dashboard", "session"} <= names

    def test_bare_lattice_refuses(self, bound_root: Path) -> None:
        result = _run([], {"LATTICE_ROOT": str(bound_root)})
        assert result.exit_code != 0
        assert BOUND_MESSAGE in result.output

    def test_help_still_works(self, bound_root: Path) -> None:
        result = _run(["list", "--help"], {"LATTICE_ROOT": str(bound_root)})
        assert result.exit_code == 0
        assert "Usage" in result.output


class TestRefusalShapes:
    def test_json_envelope(self, bound_root: Path) -> None:
        before = _tree(bound_root)
        result = _run(["list", "--json"], {"LATTICE_ROOT": str(bound_root)})
        assert result.exit_code != 0
        payload = json.loads(result.output)
        assert payload["ok"] is False
        assert payload["error"]["code"] == "BOUND_CHECKOUT"
        assert payload["error"]["message"] == BOUND_MESSAGE.removeprefix("Error: ")
        assert _tree(bound_root) == before

    def test_json_write_envelope(self, bound_root: Path) -> None:
        before = _tree(bound_root)
        result = _run(
            ["create", "New", "--actor", "human:test", "--json"],
            {"LATTICE_ROOT": str(bound_root)},
        )
        assert result.exit_code != 0
        assert json.loads(result.output)["error"]["code"] == "BOUND_CHECKOUT"
        assert _tree(bound_root) == before

    def test_walk_up_from_subdirectory(
        self, bound_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        nested = bound_root / "src" / "pkg"
        nested.mkdir(parents=True)
        monkeypatch.chdir(nested)
        before = _tree(bound_root)
        result = _run(["create", "New", "--actor", "human:test"], {"LATTICE_ROOT": None})
        assert result.exit_code != 0
        assert BOUND_MESSAGE in result.output
        assert _tree(bound_root) == before

    def test_binding_without_cache_stops_the_walk(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fresh clone holds the binding but no cache yet; v1 must not walk
        past it to an ancestor's board, nor create a board beside it."""
        (tmp_path / LATTICE_DIR).mkdir()  # an unrelated ancestor board
        clone = tmp_path / "clone"
        clone.mkdir()
        _bind(clone)
        monkeypatch.chdir(clone)
        result = _run(["list"], {"LATTICE_ROOT": None})
        assert result.exit_code != 0
        assert BOUND_MESSAGE in result.output
        assert not (clone / LATTICE_DIR).exists()

    def test_cache_marker_without_binding(self, initialized_root: Path) -> None:
        """A v2 cache on a branch without the binding still routes as hosted."""
        cache = initialized_root / LATTICE_DIR / "cache"
        cache.mkdir()
        (cache / "state.json").write_text(json.dumps(BINDING), encoding="utf-8")
        result = _run(["list", "--json"], {"LATTICE_ROOT": str(initialized_root)})
        assert result.exit_code != 0
        error = json.loads(result.output)["error"]
        assert error["code"] == "BOUND_CHECKOUT"
        assert "Lattice server cache" in error["message"]

    def test_init_path_into_bound_checkout(self, tmp_path: Path) -> None:
        clone = tmp_path / "clone"
        clone.mkdir()
        _bind(clone)
        result = CliRunner().invoke(
            cli,
            ["init", "--path", str(clone), "--actor", "human:test", "--project-code", "TST"],
            env={"LATTICE_ROOT": None},
        )
        assert result.exit_code != 0
        assert BOUND_MESSAGE in result.output
        assert not (clone / LATTICE_DIR).exists()


class TestUnboundUnaffected:
    def test_read_and_write(self, initialized_root: Path) -> None:
        env = {"LATTICE_ROOT": str(initialized_root)}
        created = _run(["create", "Local task", "--actor", "human:test", "--json"], env)
        assert created.exit_code == 0, created.output
        listed = _run(["list", "--json"], env)
        assert listed.exit_code == 0, listed.output
        titles = [task["title"] for task in json.loads(listed.output)["data"]]
        assert titles == ["Local task"]

    def test_walk_up(self, initialized_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(initialized_root)
        result = _run(["list"], {"LATTICE_ROOT": None})
        assert result.exit_code == 0, result.output


class TestFindRoot:
    def test_raises_for_bound_root(self, bound_root: Path, monkeypatch) -> None:
        monkeypatch.delenv("LATTICE_ROOT", raising=False)
        with pytest.raises(BoundCheckoutError) as exc:
            find_root(start=bound_root)
        assert exc.value.code == "BOUND_CHECKOUT"

    def test_raises_for_bound_env_root(self, bound_root: Path, monkeypatch) -> None:
        monkeypatch.setenv("LATTICE_ROOT", str(bound_root))
        with pytest.raises(BoundCheckoutError):
            find_root()

    def test_unbound_root_returned(self, initialized_root: Path, monkeypatch) -> None:
        monkeypatch.delenv("LATTICE_ROOT", raising=False)
        assert find_root(start=initialized_root) == initialized_root
