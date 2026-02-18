"""Tests for lattice.core.artifacts."""

from __future__ import annotations

import json

from lattice.core.artifacts import (
    ARTIFACT_TYPES,
    create_artifact_metadata,
    serialize_artifact,
)


# ---------------------------------------------------------------------------
# ARTIFACT_TYPES
# ---------------------------------------------------------------------------


class TestArtifactTypes:
    def test_contains_all_types(self) -> None:
        expected = {"conversation", "note", "prompt", "file", "log", "reference"}
        assert ARTIFACT_TYPES == expected

    def test_is_frozenset(self) -> None:
        assert isinstance(ARTIFACT_TYPES, frozenset)


# ---------------------------------------------------------------------------
# create_artifact_metadata
# ---------------------------------------------------------------------------


class TestCreateArtifactMetadata:
    _ART_ID = "art_01EXAMPLE0000000000000000"
    _ACTOR = "human:atin"

    def test_all_required_fields_present(self) -> None:
        meta = create_artifact_metadata(
            self._ART_ID,
            "file",
            "debug.log",
            created_by=self._ACTOR,
        )
        assert meta["schema_version"] == 1
        assert meta["id"] == self._ART_ID
        assert meta["type"] == "file"
        assert meta["title"] == "debug.log"
        assert meta["created_by"] == self._ACTOR
        assert "created_at" in meta
        assert meta["created_at"].endswith("Z")

    def test_defaults(self) -> None:
        meta = create_artifact_metadata(
            self._ART_ID,
            "file",
            "debug.log",
            created_by=self._ACTOR,
        )
        assert meta["summary"] is None
        assert meta["model"] is None
        assert meta["tags"] == []
        assert meta["token_usage"] is None
        assert meta["sensitive"] is False
        assert meta["custom_fields"] == {}

    def test_payload_nested_correctly(self) -> None:
        meta = create_artifact_metadata(
            self._ART_ID,
            "file",
            "debug.log",
            created_by=self._ACTOR,
            payload_file="art_01EXAMPLE0000000000000000.log",
            content_type="text/plain",
            size_bytes=1024,
        )
        payload = meta["payload"]
        assert payload["file"] == "art_01EXAMPLE0000000000000000.log"
        assert payload["content_type"] == "text/plain"
        assert payload["size_bytes"] == 1024

    def test_payload_defaults_to_none(self) -> None:
        meta = create_artifact_metadata(
            self._ART_ID,
            "reference",
            "Link",
            created_by=self._ACTOR,
        )
        payload = meta["payload"]
        assert payload["file"] is None
        assert payload["content_type"] is None
        assert payload["size_bytes"] is None

    def test_sensitive_flag(self) -> None:
        meta = create_artifact_metadata(
            self._ART_ID,
            "file",
            "secrets.env",
            created_by=self._ACTOR,
            sensitive=True,
        )
        assert meta["sensitive"] is True

    def test_custom_fields(self) -> None:
        meta = create_artifact_metadata(
            self._ART_ID,
            "reference",
            "Docs",
            created_by=self._ACTOR,
            custom_fields={"url": "https://example.com"},
        )
        assert meta["custom_fields"] == {"url": "https://example.com"}

    def test_tags_passed_through(self) -> None:
        meta = create_artifact_metadata(
            self._ART_ID,
            "log",
            "Build log",
            created_by=self._ACTOR,
            tags=["ci", "build"],
        )
        assert meta["tags"] == ["ci", "build"]

    def test_model_included(self) -> None:
        meta = create_artifact_metadata(
            self._ART_ID,
            "conversation",
            "Chat transcript",
            created_by="agent:claude",
            model="claude-opus-4",
        )
        assert meta["model"] == "claude-opus-4"

    def test_summary_included(self) -> None:
        meta = create_artifact_metadata(
            self._ART_ID,
            "prompt",
            "Review prompt",
            created_by=self._ACTOR,
            summary="A code review prompt",
        )
        assert meta["summary"] == "A code review prompt"


# ---------------------------------------------------------------------------
# serialize_artifact
# ---------------------------------------------------------------------------


class TestSerializeArtifact:
    def test_valid_json(self) -> None:
        meta = create_artifact_metadata(
            "art_01EXAMPLE0000000000000000",
            "file",
            "test.txt",
            created_by="human:test",
        )
        serialized = serialize_artifact(meta)
        parsed = json.loads(serialized)
        assert parsed == meta

    def test_sorted_keys(self) -> None:
        meta = {"z_field": 1, "a_field": 2}
        output = serialize_artifact(meta)
        assert output.index('"a_field"') < output.index('"z_field"')

    def test_trailing_newline(self) -> None:
        meta = {"id": "art_test"}
        output = serialize_artifact(meta)
        assert output.endswith("\n")
        assert not output.endswith("\n\n")

    def test_two_space_indent(self) -> None:
        meta = {"payload": {"file": "test.txt"}}
        output = serialize_artifact(meta)
        assert '    "file"' in output
