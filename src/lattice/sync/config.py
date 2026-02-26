"""Sync configuration management.

Stored in ``.lattice/sync/config.json``.
"""

from __future__ import annotations

import json
from pathlib import Path

from lattice.core.ids import generate_instance_id


def default_sync_config() -> dict:
    """Return default sync configuration."""
    return {
        "enabled": False,
        "peer_id": generate_instance_id().replace("inst_", "peer_"),
        "listen": {
            "host": "127.0.0.1",
            "port": 9800,
        },
        "peers": [],
    }


def load_sync_config(lattice_dir: Path) -> dict:
    """Load sync configuration, creating defaults if missing."""
    sync_dir = lattice_dir / "sync"
    config_path = sync_dir / "config.json"

    if config_path.exists():
        return json.loads(config_path.read_text())

    return default_sync_config()


def save_sync_config(lattice_dir: Path, config: dict) -> None:
    """Save sync configuration to disk."""
    from lattice.storage.fs import atomic_write

    sync_dir = lattice_dir / "sync"
    sync_dir.mkdir(parents=True, exist_ok=True)
    atomic_write(
        sync_dir / "config.json",
        json.dumps(config, sort_keys=True, indent=2) + "\n",
    )
