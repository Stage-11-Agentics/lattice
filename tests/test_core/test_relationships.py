"""Tests for lattice.core.relationships."""

from __future__ import annotations

from lattice.core.relationships import (
    RELATIONSHIP_TYPES,
    build_relationship_record,
    validate_relationship_type,
)

# ---------------------------------------------------------------------------
# RELATIONSHIP_TYPES
# ---------------------------------------------------------------------------


class TestRelationshipTypes:
    """The RELATIONSHIP_TYPES frozenset contains all seven specified types."""

    EXPECTED_TYPES = {
        "blocks",
        "depends_on",
        "subtask_of",
        "related_to",
        "spawned_by",
        "duplicate_of",
        "supersedes",
    }

    def test_contains_all_seven_types(self) -> None:
        assert RELATIONSHIP_TYPES == self.EXPECTED_TYPES

    def test_exactly_seven_types(self) -> None:
        assert len(RELATIONSHIP_TYPES) == 7

    def test_is_frozenset(self) -> None:
        assert isinstance(RELATIONSHIP_TYPES, frozenset)

    def test_each_type_present(self) -> None:
        for rt in self.EXPECTED_TYPES:
            assert rt in RELATIONSHIP_TYPES


# ---------------------------------------------------------------------------
# validate_relationship_type
# ---------------------------------------------------------------------------


class TestValidateRelationshipType:
    """validate_relationship_type returns True for valid types, False otherwise."""

    def test_all_valid_types(self) -> None:
        for rt in RELATIONSHIP_TYPES:
            assert validate_relationship_type(rt) is True

    def test_invalid_type(self) -> None:
        assert validate_relationship_type("parent_of") is False

    def test_empty_string(self) -> None:
        assert validate_relationship_type("") is False

    def test_none_returns_false(self) -> None:
        assert validate_relationship_type(None) is False  # type: ignore[arg-type]

    def test_integer_returns_false(self) -> None:
        assert validate_relationship_type(42) is False  # type: ignore[arg-type]

    def test_similar_but_wrong(self) -> None:
        assert validate_relationship_type("block") is False
        assert validate_relationship_type("Blocks") is False
        assert validate_relationship_type("BLOCKS") is False
        assert validate_relationship_type("depends-on") is False


# ---------------------------------------------------------------------------
# build_relationship_record
# ---------------------------------------------------------------------------


class TestBuildRelationshipRecord:
    """build_relationship_record builds the correct dict shape."""

    def test_full_record(self) -> None:
        record = build_relationship_record(
            rel_type="blocks",
            target_task_id="task_01TARGET000000000000000000",
            created_by="human:atin",
            created_at="2026-02-15T03:45:00Z",
            note="Blocking deploy",
        )
        assert record == {
            "type": "blocks",
            "target_task_id": "task_01TARGET000000000000000000",
            "created_at": "2026-02-15T03:45:00Z",
            "created_by": "human:atin",
            "note": "Blocking deploy",
        }

    def test_note_defaults_to_none(self) -> None:
        record = build_relationship_record(
            rel_type="depends_on",
            target_task_id="task_01TARGET000000000000000000",
            created_by="agent:claude",
            created_at="2026-02-15T04:00:00Z",
        )
        assert record["note"] is None

    def test_all_expected_keys_present(self) -> None:
        record = build_relationship_record(
            rel_type="related_to",
            target_task_id="task_01TARGET000000000000000000",
            created_by="team:frontend",
            created_at="2026-02-15T04:00:00Z",
        )
        assert set(record.keys()) == {
            "type",
            "target_task_id",
            "created_at",
            "created_by",
            "note",
        }

    def test_preserves_exact_values(self) -> None:
        record = build_relationship_record(
            rel_type="supersedes",
            target_task_id="task_01ABCDEFGHIJKLMNOPQRSTUVWX",
            created_by="agent:codex",
            created_at="2026-01-01T00:00:00Z",
            note="Replaced by new approach",
        )
        assert record["type"] == "supersedes"
        assert record["target_task_id"] == "task_01ABCDEFGHIJKLMNOPQRSTUVWX"
        assert record["created_by"] == "agent:codex"
        assert record["created_at"] == "2026-01-01T00:00:00Z"
        assert record["note"] == "Replaced by new approach"
