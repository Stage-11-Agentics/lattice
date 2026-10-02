"""Persistence helpers for transient review state under ``.lattice/review_state``."""

from __future__ import annotations

from pathlib import Path

from lattice.storage.fs import atomic_write, ensure_dir


def write_review_state_file(path: Path, content: str) -> None:
    """Create the runtime directory and atomically persist one review-state record."""
    ensure_dir(path.parent)
    atomic_write(path, content)
