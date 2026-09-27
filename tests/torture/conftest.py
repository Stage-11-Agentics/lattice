"""Torture fixtures shared across modules."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.torture.harness import chmod_tree_writable, reap_all
from tests.torture.load import LoadRig


@pytest.fixture(autouse=True)
def _reap_children(tmp_path: Path) -> Iterator[None]:
    """Unconditional cleanup: whatever servers and child processes a test started
    through the harness are stopped when it ends, however it ends."""
    try:
        yield
    finally:
        reap_all()
        chmod_tree_writable(tmp_path)


@pytest.fixture()
def load_rig(tmp_path: Path) -> Iterator[LoadRig]:
    """AC-42's rig: a server subprocess with a 1,000-task board (``tests/torture/load.py``).
    Start followers, readers, and writers on it; it closes them all afterwards."""
    rig = LoadRig.build(tmp_path)
    try:
        yield rig
    finally:
        rig.close()
