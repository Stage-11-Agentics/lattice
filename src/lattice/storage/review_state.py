"""Persistence helpers for transient review state under ``.lattice/review_state``."""

from __future__ import annotations

from pathlib import Path

from lattice.storage.fs import atomic_write, ensure_dir


def write_review_state_file(lattice_dir: Path, task_id: str, content: str) -> None:
    """Atomically persist a record under this board's ``review_state/`` directory."""
    if lattice_dir.name != ".lattice":
        raise ValueError("review state writes require a .lattice directory")
    if not task_id or any(char in task_id for char in ("/", "\\", "\0")) or task_id in {".", ".."}:
        raise ValueError("review state task_id must be one path component")

    state_dir = lattice_dir / "review_state"
    ensure_dir(state_dir)
    atomic_write(state_dir / f"{task_id}.json", content)
