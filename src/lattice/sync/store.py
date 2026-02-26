"""File-backed persistence for Automerge documents.

Each task gets a binary ``.automerge`` file in ``.lattice/sync/docs/``.
Uses Lattice's ``atomic_write()`` for crash safety.
"""

from __future__ import annotations

from pathlib import Path

from automerge import Document, core

from lattice.storage.fs import atomic_write


class AutomergeStore:
    """Load, cache, and persist Automerge documents to disk."""

    def __init__(self, sync_dir: Path) -> None:
        self.docs_dir = sync_dir / "docs"
        self.docs_dir.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, Document] = {}

    def has(self, task_id: str) -> bool:
        """Check if a document exists on disk or in cache."""
        return task_id in self._cache or self._doc_path(task_id).exists()

    def get_or_create(self, task_id: str) -> Document:
        """Load an existing Automerge doc or create a new empty one."""
        if task_id in self._cache:
            return self._cache[task_id]

        path = self._doc_path(task_id)
        if path.exists():
            core_doc = core.Document.load(path.read_bytes())
            doc = _wrap_core_doc(core_doc)
        else:
            doc = Document()

        self._cache[task_id] = doc
        return doc

    def save(self, task_id: str, doc: Document) -> None:
        """Persist an Automerge document to disk atomically."""
        path = self._doc_path(task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, doc._doc.save())
        self._cache[task_id] = doc

    def remove(self, task_id: str) -> None:
        """Remove a document from cache and disk."""
        self._cache.pop(task_id, None)
        path = self._doc_path(task_id)
        if path.exists():
            path.unlink()

    def list_task_ids(self) -> list[str]:
        """Return task IDs for all stored documents."""
        ids = []
        if self.docs_dir.exists():
            for path in self.docs_dir.glob("*.automerge"):
                ids.append(path.stem)
        return sorted(ids)

    def _doc_path(self, task_id: str) -> Path:
        return self.docs_dir / f"{task_id}.automerge"


def _wrap_core_doc(core_doc: core.Document) -> Document:
    """Wrap a core.Document in the high-level Document class."""
    doc = Document.__new__(Document)
    doc._doc = core_doc
    # Initialize the MapReadProxy base class
    from automerge.document import MapReadProxy

    MapReadProxy.__init__(doc, core_doc, core.ROOT, None)
    return doc
