"""Shared fixtures for MCP tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from lattice.core.config import default_config, serialize_config
from lattice.storage.fs import LATTICE_DIR, atomic_write, ensure_lattice_dirs


@pytest.fixture()
def lattice_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Set up a temporary .lattice/ directory and configure LATTICE_ROOT.

    Returns the tmp_path (project root). Sets LATTICE_ROOT so find_root()
    discovers it without needing cwd changes.
    """
    ensure_lattice_dirs(tmp_path)
    lattice_dir = tmp_path / LATTICE_DIR
    config = default_config()
    config["project_code"] = "TST"
    atomic_write(lattice_dir / "config.json", serialize_config(config))
    (lattice_dir / "events" / "_lifecycle.jsonl").touch()

    monkeypatch.setenv("LATTICE_ROOT", str(tmp_path))
    return tmp_path


@pytest.fixture()
def lattice_dir(lattice_env: Path) -> Path:
    """Return the .lattice/ directory path."""
    return lattice_env / LATTICE_DIR
