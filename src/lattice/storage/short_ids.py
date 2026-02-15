"""Short ID index management: load, save, allocate, resolve, register."""

from __future__ import annotations

import json
from pathlib import Path

from lattice.storage.fs import atomic_write
from lattice.storage.locks import lattice_lock


def _default_index() -> dict:
    """Return a fresh empty index structure."""
    return {"schema_version": 1, "next_seq": 1, "map": {}}


def load_id_index(lattice_dir: Path) -> dict:
    """Load and parse ``.lattice/ids.json``, returning empty default if missing."""
    ids_path = lattice_dir / "ids.json"
    if not ids_path.exists():
        return _default_index()
    try:
        return json.loads(ids_path.read_text())
    except (json.JSONDecodeError, OSError):
        return _default_index()


def save_id_index(lattice_dir: Path, index: dict) -> None:
    """Atomic write of the ID index to ``.lattice/ids.json``."""
    ids_path = lattice_dir / "ids.json"
    content = json.dumps(index, sort_keys=True, indent=2) + "\n"
    atomic_write(ids_path, content)


def register_short_id(index: dict, short_id: str, task_ulid: str) -> dict:
    """Add a mapping to the index dict (pure, no I/O). Returns the index."""
    index["map"][short_id] = task_ulid
    return index


def allocate_short_id(lattice_dir: Path, project_code: str) -> tuple[str, dict]:
    """Allocate the next short ID under lock.

    Returns (short_id, updated_index). The index is saved to disk
    and the lock is released before returning.
    """
    locks_dir = lattice_dir / "locks"
    with lattice_lock(locks_dir, "ids_json"):
        index = load_id_index(lattice_dir)
        seq = index.get("next_seq", 1)
        short_id = f"{project_code}-{seq}"
        index["next_seq"] = seq + 1
        save_id_index(lattice_dir, index)
    return short_id, index


def resolve_short_id(lattice_dir: Path, short_id: str) -> str | None:
    """Look up a short ID and return the corresponding ULID, or None."""
    index = load_id_index(lattice_dir)
    return index.get("map", {}).get(short_id.upper())
