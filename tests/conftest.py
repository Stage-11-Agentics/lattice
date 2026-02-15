"""Shared test fixtures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner


@pytest.fixture()
def lattice_root(tmp_path: Path) -> Path:
    """Return a temporary directory suitable for initializing .lattice/ in."""
    return tmp_path


@pytest.fixture()
def initialized_root(lattice_root: Path) -> Path:
    """Return a temporary directory with .lattice/ already initialized."""
    from lattice.core.config import default_config, serialize_config
    from lattice.storage.fs import LATTICE_DIR, atomic_write, ensure_lattice_dirs

    ensure_lattice_dirs(lattice_root)
    lattice_dir = lattice_root / LATTICE_DIR
    atomic_write(lattice_dir / "config.json", serialize_config(default_config()))
    (lattice_dir / "events" / "_global.jsonl").touch()
    return lattice_root


@pytest.fixture()
def cli_runner() -> CliRunner:
    """Return a Click CliRunner for invoking CLI commands."""
    return CliRunner()


@pytest.fixture()
def cli_env(initialized_root: Path) -> dict[str, str]:
    """Return env dict with LATTICE_ROOT pointing to initialized_root."""
    return {"LATTICE_ROOT": str(initialized_root)}


@pytest.fixture()
def invoke(cli_runner: CliRunner, cli_env: dict[str, str]):
    """Return a helper that invokes CLI commands with the right environment.

    Usage::

        result = invoke("create", "My task", "--actor", "human:test")
    """
    from lattice.cli.main import cli

    def _invoke(*args: str, **kwargs):
        return cli_runner.invoke(cli, list(args), env=cli_env, **kwargs)

    return _invoke


@pytest.fixture()
def invoke_json(invoke):
    """Like invoke, but appends --json and parses the response.

    Returns (parsed_dict, exit_code) tuple.
    """

    def _invoke_json(*args: str) -> tuple[dict, int]:
        result = invoke(*args, "--json")
        parsed = json.loads(result.output)
        return parsed, result.exit_code

    return _invoke_json


@pytest.fixture()
def create_task(cli_runner: CliRunner, cli_env: dict[str, str]):
    """Factory fixture: create a task and return its snapshot dict.

    Usage::

        task = create_task("My task", "--priority", "high")
    """
    from lattice.cli.main import cli

    def _create(title: str = "Test task", *extra_args: str, actor: str = "human:test"):
        args = ["create", title, "--actor", actor, "--json", *extra_args]
        result = cli_runner.invoke(cli, args, env=cli_env)
        assert result.exit_code == 0, f"create failed: {result.output}"
        return json.loads(result.output)["data"]

    return _create
