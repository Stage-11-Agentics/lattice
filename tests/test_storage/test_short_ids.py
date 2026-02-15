"""Tests for storage/short_ids.py: index load/save, allocation, resolution, v2 migration."""

from __future__ import annotations

import json
from pathlib import Path

from lattice.storage.short_ids import (
    _default_index,
    _migrate_v1_to_v2,
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


class TestDefaultIndex:
    def test_returns_v2_schema(self) -> None:
        index = _default_index()
        assert index == {"schema_version": 2, "next_seqs": {}, "map": {}}


class TestMigrateV1ToV2:
    def test_migrates_empty_v1(self) -> None:
        v1 = {"schema_version": 1, "next_seq": 1, "map": {}}
        v2 = _migrate_v1_to_v2(v1)
        assert v2["schema_version"] == 2
        assert v2["next_seqs"] == {}
        assert v2["map"] == {}

    def test_migrates_v1_with_project_code(self) -> None:
        v1 = {"schema_version": 1, "next_seq": 5, "map": {"LAT-1": "task_a", "LAT-4": "task_b"}}
        v2 = _migrate_v1_to_v2(v1, project_code="LAT")
        assert v2["schema_version"] == 2
        assert v2["next_seqs"]["LAT"] == 5
        assert v2["map"] == v1["map"]

    def test_infers_prefix_from_map(self) -> None:
        v1 = {"schema_version": 1, "next_seq": 3, "map": {"PRJ-1": "task_x", "PRJ-2": "task_y"}}
        v2 = _migrate_v1_to_v2(v1)
        assert v2["next_seqs"]["PRJ"] == 3

    def test_already_v2_is_noop(self) -> None:
        v2 = {"schema_version": 2, "next_seqs": {"LAT": 5}, "map": {"LAT-1": "task_a"}}
        result = _migrate_v1_to_v2(v2)
        assert result is v2  # same object, no copy

    def test_corrects_low_next_seq(self) -> None:
        """If v1 next_seq is lower than max in map, migration fixes it."""
        v1 = {"schema_version": 1, "next_seq": 2, "map": {"LAT-5": "task_a"}}
        v2 = _migrate_v1_to_v2(v1, project_code="LAT")
        assert v2["next_seqs"]["LAT"] == 6  # max(5)+1 > old 2


class TestLoadIdIndex:
    def test_missing_file_returns_default(self, tmp_path: Path) -> None:
        lattice_dir = _make_lattice_dir(tmp_path)
        index = load_id_index(lattice_dir)
        assert index == {"schema_version": 2, "next_seqs": {}, "map": {}}

    def test_loads_v2_existing(self, tmp_path: Path) -> None:
        lattice_dir = _make_lattice_dir(tmp_path)
        data = {"schema_version": 2, "next_seqs": {"LAT": 5}, "map": {"LAT-1": "task_01ABC"}}
        (lattice_dir / "ids.json").write_text(json.dumps(data))
        index = load_id_index(lattice_dir)
        assert index["next_seqs"]["LAT"] == 5
        assert index["map"]["LAT-1"] == "task_01ABC"

    def test_transparently_migrates_v1(self, tmp_path: Path) -> None:
        lattice_dir = _make_lattice_dir(tmp_path)
        v1_data = {"schema_version": 1, "next_seq": 5, "map": {"LAT-1": "task_01ABC"}}
        (lattice_dir / "ids.json").write_text(json.dumps(v1_data))
        index = load_id_index(lattice_dir)
        assert index["schema_version"] == 2
        assert "next_seqs" in index
        assert "next_seq" not in index

    def test_corrupt_file_returns_default(self, tmp_path: Path) -> None:
        lattice_dir = _make_lattice_dir(tmp_path)
        (lattice_dir / "ids.json").write_text("not json")
        index = load_id_index(lattice_dir)
        assert index == {"schema_version": 2, "next_seqs": {}, "map": {}}


class TestSaveIdIndex:
    def test_writes_valid_json(self, tmp_path: Path) -> None:
        lattice_dir = _make_lattice_dir(tmp_path)
        index = {
            "schema_version": 2,
            "next_seqs": {"LAT": 3},
            "map": {"LAT-1": "task_x", "LAT-2": "task_y"},
        }
        save_id_index(lattice_dir, index)
        loaded = json.loads((lattice_dir / "ids.json").read_text())
        assert loaded == index

    def test_has_trailing_newline(self, tmp_path: Path) -> None:
        lattice_dir = _make_lattice_dir(tmp_path)
        save_id_index(lattice_dir, _default_index())
        raw = (lattice_dir / "ids.json").read_text()
        assert raw.endswith("\n")


class TestRegisterShortId:
    def test_adds_mapping(self) -> None:
        index = _default_index()
        result = register_short_id(index, "LAT-1", "task_abc")
        assert result["map"]["LAT-1"] == "task_abc"
        assert result is index  # mutates in place


class TestAllocateShortId:
    def test_allocates_sequential(self, tmp_path: Path) -> None:
        lattice_dir = _make_lattice_dir(tmp_path)
        save_id_index(lattice_dir, _default_index())

        sid1, _ = allocate_short_id(lattice_dir, "LAT")
        assert sid1 == "LAT-1"

        sid2, _ = allocate_short_id(lattice_dir, "LAT")
        assert sid2 == "LAT-2"

    def test_increments_counter(self, tmp_path: Path) -> None:
        lattice_dir = _make_lattice_dir(tmp_path)
        save_id_index(lattice_dir, {"schema_version": 2, "next_seqs": {"PRJ": 10}, "map": {}})

        sid, idx = allocate_short_id(lattice_dir, "PRJ")
        assert sid == "PRJ-10"
        # Verify persisted
        loaded = load_id_index(lattice_dir)
        assert loaded["next_seqs"]["PRJ"] == 11

    def test_independent_prefix_counters(self, tmp_path: Path) -> None:
        """Different prefixes have independent sequence counters."""
        lattice_dir = _make_lattice_dir(tmp_path)
        save_id_index(lattice_dir, _default_index())

        sid1, _ = allocate_short_id(lattice_dir, "AUT")
        sid2, _ = allocate_short_id(lattice_dir, "AUT-F")
        sid3, _ = allocate_short_id(lattice_dir, "AUT")

        assert sid1 == "AUT-1"
        assert sid2 == "AUT-F-1"
        assert sid3 == "AUT-2"


class TestResolveShortId:
    def test_found(self, tmp_path: Path) -> None:
        lattice_dir = _make_lattice_dir(tmp_path)
        save_id_index(
            lattice_dir,
            {"schema_version": 2, "next_seqs": {"LAT": 2}, "map": {"LAT-1": "task_01REAL"}},
        )
        assert resolve_short_id(lattice_dir, "LAT-1") == "task_01REAL"

    def test_case_insensitive(self, tmp_path: Path) -> None:
        lattice_dir = _make_lattice_dir(tmp_path)
        save_id_index(
            lattice_dir,
            {"schema_version": 2, "next_seqs": {"LAT": 2}, "map": {"LAT-1": "task_01REAL"}},
        )
        assert resolve_short_id(lattice_dir, "lat-1") == "task_01REAL"

    def test_not_found(self, tmp_path: Path) -> None:
        lattice_dir = _make_lattice_dir(tmp_path)
        save_id_index(lattice_dir, _default_index())
        assert resolve_short_id(lattice_dir, "LAT-99") is None

    def test_resolve_subproject_id(self, tmp_path: Path) -> None:
        lattice_dir = _make_lattice_dir(tmp_path)
        save_id_index(
            lattice_dir,
            {
                "schema_version": 2,
                "next_seqs": {"AUT-F": 2},
                "map": {"AUT-F-1": "task_01SUB"},
            },
        )
        assert resolve_short_id(lattice_dir, "AUT-F-1") == "task_01SUB"
