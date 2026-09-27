"""Torture fixtures shared across modules."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.torture.harness import chmod_tree_writable
from tests.torture.load import LOAD_TASKS, LoadRig


@pytest.fixture()
def load_rig(tmp_path: Path) -> Iterator[LoadRig]:
    """AC-42's rig: a server subprocess with a 1,000-task board (``tests/torture/load.py``).
    Start followers, readers, and writers on it; it closes them all afterwards."""
    rig = LoadRig.build(tmp_path, tasks=LOAD_TASKS)
    try:
        yield rig
    finally:
        rig.close()
        chmod_tree_writable(tmp_path)
