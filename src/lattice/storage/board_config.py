"""The one writer operations use for ``config.json``.

Board configuration is admin-only (SPEC §3.9): workflow, review modes and
toggles, completion policies, hooks, and every other key change only through
the admin path (``lattice server project config`` on a server, or a direct
edit locally). Three ordinary operations are the exception, and each may
change only its own key: ``board.set_project_code`` (``project_code``),
``board.set_subproject_code`` (``subproject_code``), and
``board.set_dashboard_config`` (``dashboard``). This writer refuses any other
key with ``FORBIDDEN`` before it reads or writes anything, and it writes back
the config it read with only that one key changed, under the ``config`` lock
the dashboard settings POST already takes.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from lattice.core.config import serialize_config
from lattice.core.errors import OpError
from lattice.storage.fs import atomic_write
from lattice.storage.locks import multi_lock

OPERATION_CONFIG_KEYS = frozenset({"project_code", "subproject_code", "dashboard"})

UNCHANGED = object()
"""Returned by a ``decide`` callback: leave the key as it is and write nothing."""

REMOVE = object()
"""Returned by a ``decide`` callback: remove the key."""


def forbidden_config_key(key: str) -> OpError:
    return OpError(
        "FORBIDDEN",
        f"'{key}' is board configuration, which no operation may change. Only "
        "project_code, subproject_code, and the dashboard settings change through "
        "operations; an admin changes the rest ('lattice server project config' on a "
        "hosted board, config.json locally).",
        {"key": key},
    )


def update_config_key(
    lattice_dir: Path, key: str, decide: Callable[[dict], Any]
) -> tuple[dict, bool]:
    """Change one operation-writable *key* of the board's ``config.json``.

    Under the ``config`` lock, reads the config and calls ``decide(config)``,
    which returns the key's new value, ``REMOVE``, or ``UNCHANGED`` (or raises
    ``OpError``). Returns ``(config after, whether it was written)``.
    """
    if key not in OPERATION_CONFIG_KEYS:
        raise forbidden_config_key(key)
    config_path = lattice_dir / "config.json"
    locks_dir = lattice_dir / "locks"
    locks_dir.mkdir(parents=True, exist_ok=True)
    with multi_lock(locks_dir, ["config"]):
        config = json.loads(config_path.read_text())
        value = decide(json.loads(json.dumps(config)))
        if value is UNCHANGED:
            return config, False
        if value is REMOVE:
            config.pop(key, None)
        else:
            config[key] = value
        atomic_write(config_path, serialize_config(config))
    return config, True
