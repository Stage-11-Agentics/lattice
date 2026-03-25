"""Automerge CRDT sync layer for peer-to-peer Lattice collaboration.

Optional module — requires ``pip install lattice-tracker[sync]``.
"""

from __future__ import annotations

from lattice.sync.bridge import SyncBridge
from lattice.sync.documents import automerge_to_snapshot_fields, snapshot_to_automerge
from lattice.sync.store import AutomergeStore

__all__ = [
    "AutomergeStore",
    "SyncBridge",
    "automerge_to_snapshot_fields",
    "snapshot_to_automerge",
]
