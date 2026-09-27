"""Resource snapshot readers shared by the ``resource.*`` operations and the CLI.

Reads only; every resource write goes through ``write_resource_event``
(``storage/operations.py``). ``find_resource`` raises ``OpError`` with the
code and message the CLI has always printed.
"""

from __future__ import annotations

import json
from pathlib import Path

from lattice.core.errors import OpError
from lattice.core.ids import validate_id


def read_resource_snapshot(lattice_dir: Path, resource_name: str) -> dict | None:
    """Read a resource snapshot by name, returning None if not found."""
    snap_path = lattice_dir / "resources" / resource_name / "resource.json"
    if not snap_path.exists():
        return None
    return json.loads(snap_path.read_text())


def list_all_resources(lattice_dir: Path) -> list[dict]:
    """Return a list of all resource snapshots."""
    resources_dir = lattice_dir / "resources"
    results = []
    if not resources_dir.is_dir():
        return results
    for res_dir in sorted(resources_dir.iterdir()):
        if not res_dir.is_dir():
            continue
        snap_path = res_dir / "resource.json"
        if snap_path.exists():
            results.append(json.loads(snap_path.read_text()))
    return results


def find_resource(
    lattice_dir: Path, name_or_id: str, config: dict
) -> tuple[str, str, dict | None]:
    """Resolve a resource name or ID to ``(resource_id, name, snapshot_or_None)``.

    Resolution order:
    1. A ``res_`` ULID: the resource whose snapshot has that ID (``NOT_FOUND`` if none)
    2. A snapshot under ``.lattice/resources/*/resource.json`` with that ``name``
    3. A key of ``config["resources"]``: ``("", name, None)``, meaning auto-create
    4. ``NOT_FOUND``
    """
    resources_dir = lattice_dir / "resources"
    if validate_id(name_or_id, "res"):
        if resources_dir.is_dir():
            for res_dir in resources_dir.iterdir():
                if not res_dir.is_dir():
                    continue
                snap_path = res_dir / "resource.json"
                if snap_path.exists():
                    snap = json.loads(snap_path.read_text())
                    if snap.get("id") == name_or_id:
                        return name_or_id, snap["name"], snap
        raise OpError("NOT_FOUND", f"Resource with ID '{name_or_id}' not found.")

    if resources_dir.is_dir():
        for res_dir in resources_dir.iterdir():
            if not res_dir.is_dir():
                continue
            snap_path = res_dir / "resource.json"
            if snap_path.exists():
                snap = json.loads(snap_path.read_text())
                if snap.get("name") == name_or_id:
                    return snap["id"], name_or_id, snap

    if name_or_id in config.get("resources", {}):
        return "", name_or_id, None  # empty id signals auto-create needed

    raise OpError(
        "NOT_FOUND",
        f"Resource '{name_or_id}' not found. Create it with 'lattice resource create {name_or_id}'.",
    )
