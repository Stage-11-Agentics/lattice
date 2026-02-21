"""Shell completion callbacks for the Lattice CLI."""

from __future__ import annotations

from click.shell_completion import CompletionItem


def _find_lattice_root():
    """Locate .lattice/ root independently of Click context."""
    from lattice.storage.fs import find_root  # deferred to avoid import-time side effects
    return find_root()


def complete_task_id(ctx, param, incomplete):
    """Complete task short IDs (e.g. LAT-1) from .lattice/ids.json."""
    try:
        from lattice.storage.short_ids import load_id_index
        root = _find_lattice_root()
        if root is None:
            return []
        lattice_dir = root / ".lattice"
        index = load_id_index(lattice_dir)
        id_map = index.get("map", {})
        return [
            CompletionItem(short_id)
            for short_id in sorted(id_map)
            if short_id.startswith(incomplete)
        ]
    except Exception:
        return []
