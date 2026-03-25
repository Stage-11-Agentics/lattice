"""Tests for sync/store.py — Automerge document persistence."""

from __future__ import annotations

import pytest

automerge = pytest.importorskip("automerge")

from automerge import Document  # noqa: E402

from lattice.sync.documents import automerge_to_snapshot_fields, snapshot_to_automerge  # noqa: E402
from lattice.sync.store import AutomergeStore  # noqa: E402


@pytest.fixture
def store(tmp_path):
    """Create an AutomergeStore in a temp directory."""
    return AutomergeStore(tmp_path / "sync")


def _make_doc(title="Test", status="backlog"):
    doc = Document()
    snapshot_to_automerge(doc, {
        "schema_version": 1,
        "id": "task_test1",
        "title": title,
        "status": status,
        "type": "task",
        "priority": "medium",
        "tags": [],
    })
    return doc


class TestAutoStore:
    def test_create_and_retrieve(self, store):
        task_id = "task_test1"
        doc = store.get_or_create(task_id)
        assert doc is not None

    def test_save_and_load(self, store):
        task_id = "task_test1"
        doc = _make_doc(title="Persisted")
        store.save(task_id, doc)

        # Clear cache to force disk load
        store._cache.clear()
        loaded = store.get_or_create(task_id)
        fields = automerge_to_snapshot_fields(loaded)
        assert fields["title"] == "Persisted"

    def test_has(self, store):
        assert not store.has("task_nonexistent")
        doc = _make_doc()
        store.save("task_test1", doc)
        assert store.has("task_test1")

    def test_remove(self, store):
        doc = _make_doc()
        store.save("task_test1", doc)
        assert store.has("task_test1")
        store.remove("task_test1")
        assert not store.has("task_test1")

    def test_list_task_ids(self, store):
        store.save("task_a", _make_doc())
        store.save("task_b", _make_doc())
        store.save("task_c", _make_doc())
        ids = store.list_task_ids()
        assert ids == ["task_a", "task_b", "task_c"]

    def test_cache_returns_same_instance(self, store):
        task_id = "task_test1"
        doc1 = store.get_or_create(task_id)
        doc2 = store.get_or_create(task_id)
        assert doc1 is doc2
