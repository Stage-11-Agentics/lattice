"""Short ID index management: load, save, allocate, resolve, register."""

from __future__ import annotations

import json
from pathlib import Path

from lattice.storage.fs import atomic_write
from lattice.storage.locks import lattice_lock


def _default_index() -> dict:
    """Return a fresh empty v2 index structure."""
    return {"schema_version": 2, "next_seqs": {}, "map": {}}


def _migrate_v1_to_v2(index: dict, project_code: str | None = None) -> dict:
    """Migrate a v1 index (single next_seq) to v2 (per-prefix next_seqs).

    If *project_code* is provided, the old ``next_seq`` is assigned to that
    prefix.  Otherwise the prefix is inferred from the first entry in ``map``.
    """
    if index.get("schema_version", 1) >= 2:
        return index  # already v2

    old_seq = index.get("next_seq", 1)
    id_map = index.get("map", {})

    # Infer prefix from map entries if no project_code given
    if not project_code and id_map:
        first_key = next(iter(id_map))
        # rsplit on last '-' to get prefix
        project_code = first_key.rsplit("-", 1)[0]

    next_seqs: dict[str, int] = {}

    if project_code:
        next_seqs[project_code] = old_seq

    # Also scan the map to discover any prefixes and ensure next_seqs covers them
    prefix_maxes: dict[str, int] = {}
    for short_id in id_map:
        prefix, num_str = short_id.rsplit("-", 1)
        try:
            num = int(num_str)
        except ValueError:
            continue
        if prefix not in prefix_maxes or num > prefix_maxes[prefix]:
            prefix_maxes[prefix] = num

    for prefix, max_num in prefix_maxes.items():
        needed = max_num + 1
        if prefix not in next_seqs or next_seqs[prefix] < needed:
            next_seqs[prefix] = needed

    return {
        "schema_version": 2,
        "next_seqs": next_seqs,
        "map": id_map,
    }


def load_id_index(lattice_dir: Path) -> dict:
    """Load and parse ``.lattice/ids.json``, transparently migrating v1 to v2."""
    ids_path = lattice_dir / "ids.json"
    if not ids_path.exists():
        return _default_index()
    try:
        index = json.loads(ids_path.read_text())
    except (json.JSONDecodeError, OSError):
        return _default_index()

    # Lazy migration: convert v1 -> v2 in memory (persisted on next save)
    if index.get("schema_version", 1) < 2:
        index = _migrate_v1_to_v2(index)

    return index


def save_id_index(lattice_dir: Path, index: dict) -> None:
    """Atomic write of the ID index to ``.lattice/ids.json``."""
    ids_path = lattice_dir / "ids.json"
    content = json.dumps(index, sort_keys=True, indent=2) + "\n"
    atomic_write(ids_path, content)


def register_short_id(index: dict, short_id: str, task_ulid: str) -> dict:
    """Add a mapping to the index dict (pure, no I/O). Returns the index."""
    index["map"][short_id] = task_ulid
    return index


def allocate_short_id(lattice_dir: Path, prefix: str) -> tuple[str, dict]:
    """Allocate the next short ID for *prefix* under lock.

    Returns (short_id, updated_index). The index is saved to disk
    and the lock is released before returning.
    """
    locks_dir = lattice_dir / "locks"
    with lattice_lock(locks_dir, "ids_json"):
        index = load_id_index(lattice_dir)
        next_seqs = index.get("next_seqs", {})
        seq = next_seqs.get(prefix, 1)
        short_id = f"{prefix}-{seq}"
        next_seqs[prefix] = seq + 1
        index["next_seqs"] = next_seqs
        save_id_index(lattice_dir, index)
    return short_id, index


def resolve_short_id(lattice_dir: Path, short_id: str) -> str | None:
    """Look up a short ID and return the corresponding ULID, or None."""
    index = load_id_index(lattice_dir)
    return index.get("map", {}).get(short_id.upper())
