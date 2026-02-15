"""Tests for storage/short_ids.py: index load/save, allocation, resolution."""

from __future__ import annotations

import json
from pathlib import Path

from lattice.storage.short_ids import (
    allocate_short_id,
    load_id_index,
    register_short_id,
    resolve_short_id,
    save_id_index,
)


def _make_lattice_dir(tmp_path: Path) -> Path:
    """Create a minimal .lattice directory for testing."""
    lattice_dir = tmp_path / ".lattice"
    lattice_dir.mkdir()
    (lattice_dir / "locks").mkdir()
    return lattice_dir


class TestLoadIdIndex:
    def test_missing_file_returns_default(self, tmp_path: Path) -> None:
        lattice_dir = _make_lattice_dir(tmp_path)
        index = load_id_index(lattice_dir)
        assert index == {"schema_version": 1, "next_seq": 1, "map": {}}

    def test_loads_existing(self, tmp_path: Path) -> None:
        lattice_dir = _make_lattice_dir(tmp_path)
        data = {"schema_version": 1, "next_seq": 5, "map": {"LAT-1": "task_01ABC"}}
        (lattice_dir / "ids.json").write_text(json.dumps(data))
        index = load_id_index(lattice_dir)
        assert index["next_seq"] == 5
        assert index["map"]["LAT-1"] == "task_01ABC"

    def test_corrupt_file_returns_default(self, tmp_path: Path) -> None:
        lattice_dir = _make_lattice_dir(tmp_path)
        (lattice_dir / "ids.json").write_text("not json")
        index = load_id_index(lattice_dir)
        assert index == {"schema_version": 1, "next_seq": 1, "map": {}}


class TestSaveIdIndex:
    def test_writes_valid_json(self, tmp_path: Path) -> None:
        lattice_dir = _make_lattice_dir(tmp_path)
        index = {"schema_version": 1, "next_seq": 3, "map": {"LAT-1": "task_x", "LAT-2": "task_y"}}
        save_id_index(lattice_dir, index)
        loaded = json.loads((lattice_dir / "ids.json").read_text())
        assert loaded == index

    def test_has_trailing_newline(self, tmp_path: Path) -> None:
        lattice_dir = _make_lattice_dir(tmp_path)
        save_id_index(lattice_dir, {"schema_version": 1, "next_seq": 1, "map": {}})
        raw = (lattice_dir / "ids.json").read_text()
        assert raw.endswith("\n")


class TestRegisterShortId:
    def test_adds_mapping(self) -> None:
        index = {"schema_version": 1, "next_seq": 1, "map": {}}
        result = register_short_id(index, "LAT-1", "task_abc")
        assert result["map"]["LAT-1"] == "task_abc"
        assert result is index  # mutates in place


class TestAllocateShortId:
    def test_allocates_sequential(self, tmp_path: Path) -> None:
        lattice_dir = _make_lattice_dir(tmp_path)
        save_id_index(lattice_dir, {"schema_version": 1, "next_seq": 1, "map": {}})

        sid1, _ = allocate_short_id(lattice_dir, "LAT")
        assert sid1 == "LAT-1"

        sid2, _ = allocate_short_id(lattice_dir, "LAT")
        assert sid2 == "LAT-2"

    def test_increments_counter(self, tmp_path: Path) -> None:
        lattice_dir = _make_lattice_dir(tmp_path)
        save_id_index(lattice_dir, {"schema_version": 1, "next_seq": 10, "map": {}})

        sid, idx = allocate_short_id(lattice_dir, "PRJ")
        assert sid == "PRJ-10"
        # Verify persisted
        loaded = load_id_index(lattice_dir)
        assert loaded["next_seq"] == 11


class TestResolveShortId:
    def test_found(self, tmp_path: Path) -> None:
        lattice_dir = _make_lattice_dir(tmp_path)
        save_id_index(
            lattice_dir,
            {"schema_version": 1, "next_seq": 2, "map": {"LAT-1": "task_01REAL"}},
        )
        assert resolve_short_id(lattice_dir, "LAT-1") == "task_01REAL"

    def test_case_insensitive(self, tmp_path: Path) -> None:
        lattice_dir = _make_lattice_dir(tmp_path)
        save_id_index(
            lattice_dir,
            {"schema_version": 1, "next_seq": 2, "map": {"LAT-1": "task_01REAL"}},
        )
        assert resolve_short_id(lattice_dir, "lat-1") == "task_01REAL"

    def test_not_found(self, tmp_path: Path) -> None:
        lattice_dir = _make_lattice_dir(tmp_path)
        save_id_index(lattice_dir, {"schema_version": 1, "next_seq": 1, "map": {}})
        assert resolve_short_id(lattice_dir, "LAT-99") is None
