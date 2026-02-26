"""Tests for sync/documents.py — snapshot ↔ Automerge round-trip."""

from __future__ import annotations

import pytest

automerge = pytest.importorskip("automerge")

from automerge import Document  # noqa: E402

from lattice.sync.documents import (  # noqa: E402
    apply_field_update_to_doc,
    automerge_to_snapshot_fields,
    snapshot_to_automerge,
)


def _make_snapshot(**overrides):
    """Return a minimal task snapshot dict."""
    base = {
        "schema_version": 1,
        "id": "task_test123",
        "short_id": "LAT-1",
        "last_event_id": "ev_test456",
        "title": "Test task",
        "description": "A test task description",
        "status": "backlog",
        "priority": "medium",
        "urgency": "normal",
        "complexity": "medium",
        "type": "task",
        "assigned_to": "human:alice",
        "created_by": "human:alice",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "done_at": None,
        "last_status_changed_at": "2026-01-01T00:00:00Z",
        "tags": ["test", "sync"],
    }
    base.update(overrides)
    return base


class TestSnapshotToAutomerge:
    def test_round_trip_preserves_fields(self):
        snapshot = _make_snapshot()
        doc = Document()
        snapshot_to_automerge(doc, snapshot)
        result = automerge_to_snapshot_fields(doc)

        assert result["id"] == "task_test123"
        assert result["title"] == "Test task"
        assert result["description"] == "A test task description"
        assert result["status"] == "backlog"
        assert result["priority"] == "medium"
        assert result["assigned_to"] == "human:alice"
        assert result["tags"] == ["test", "sync"]
        assert result["schema_version"] == 1

    def test_empty_description(self):
        snapshot = _make_snapshot(description="")
        doc = Document()
        snapshot_to_automerge(doc, snapshot)
        result = automerge_to_snapshot_fields(doc)
        assert result.get("description", "") == ""

    def test_none_values_handled(self):
        snapshot = _make_snapshot(done_at=None, assigned_to=None)
        doc = Document()
        snapshot_to_automerge(doc, snapshot)
        result = automerge_to_snapshot_fields(doc)
        assert "done_at" not in result or result["done_at"] == ""

    def test_empty_tags(self):
        snapshot = _make_snapshot(tags=[])
        doc = Document()
        snapshot_to_automerge(doc, snapshot)
        result = automerge_to_snapshot_fields(doc)
        assert result["tags"] == []


class TestApplyFieldUpdate:
    def test_update_status(self):
        doc = Document()
        snapshot_to_automerge(doc, _make_snapshot())
        apply_field_update_to_doc(doc, "status", "in_progress")
        result = automerge_to_snapshot_fields(doc)
        assert result["status"] == "in_progress"

    def test_update_title(self):
        doc = Document()
        snapshot_to_automerge(doc, _make_snapshot())
        apply_field_update_to_doc(doc, "title", "New Title")
        result = automerge_to_snapshot_fields(doc)
        assert result["title"] == "New Title"

    def test_update_tags(self):
        doc = Document()
        snapshot_to_automerge(doc, _make_snapshot())
        apply_field_update_to_doc(doc, "tags", ["new", "tags"])
        result = automerge_to_snapshot_fields(doc)
        assert result["tags"] == ["new", "tags"]
