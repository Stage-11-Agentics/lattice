"""Shell completion callbacks for the Lattice CLI."""

from __future__ import annotations

import json

from click.shell_completion import CompletionItem


def _find_lattice_root():
    """Locate .lattice/ root independently of Click context."""
    try:
        from lattice.storage.fs import find_root
        return find_root()
    except Exception:
        return None


def complete_task_id(ctx, param, incomplete):
    """Complete task short IDs (e.g. LAT-1) from .lattice/ids.json."""
    try:
        root = _find_lattice_root()
        if root is None:
            return []
        ids_file = root / ".lattice" / "ids.json"
        if not ids_file.exists():
            return []
        data = json.loads(ids_file.read_text())
        id_map = data.get("map", {})
        return [
            CompletionItem(short_id)
            for short_id in sorted(id_map)
            if short_id.startswith(incomplete)
        ]
    except Exception:
        return []
