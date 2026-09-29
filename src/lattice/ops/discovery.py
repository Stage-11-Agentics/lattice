"""Find every operation: Lattice's own modules, then installed plugins.

Discovery runs once, on the first lookup that needs it, rather than at package
import: the CLI imports ``lattice.ops`` on every start, and scanning entry
points there would slow read commands that never write. A lookup of a built-in
name imports only that operation's module (:func:`import_builtin`); listing the
registry, or a name no built-in module registers, runs full discovery.
"""

from __future__ import annotations

import importlib
import os
import pkgutil
import sys
import threading
from importlib.metadata import entry_points

OPERATIONS_GROUP = "lattice.operations"

_discovered = False
_lock = threading.RLock()


def discover() -> None:
    """Import every module in ``lattice.ops`` (sorted), then every module named
    by an entry point in the ``lattice.operations`` group (sorted by name).

    A plugin that fails to import, or registers a name that is taken, is
    reported on stderr and skipped; it never breaks the built-in operations.
    """
    global _discovered
    if _discovered:
        return
    with _lock:
        if _discovered:
            return
        _discover()
        _discovered = True


def import_builtin(name: str) -> None:
    """Import the built-in module for operation *name*, if Lattice has one.

    ``task.status`` lives in ``lattice.ops.task_status``. A missing module is
    not an error (the name may be a plugin's); any other import failure is.
    """
    module = f"lattice.ops.{name.replace('.', '_')}"
    try:
        importlib.import_module(module)
    except ModuleNotFoundError as exc:
        if exc.name != module:
            raise


def _discover() -> None:
    import lattice.ops as package

    for info in sorted(pkgutil.iter_modules(package.__path__), key=lambda m: m.name):
        importlib.import_module(f"{package.__name__}.{info.name}")

    for ep in sorted(entry_points(group=OPERATIONS_GROUP), key=lambda e: e.name):
        try:
            ep.load()
        except Exception as exc:  # noqa: BLE001 - a broken plugin must not break Lattice
            print(f"lattice: failed to load operation plugin '{ep.name}': {exc}", file=sys.stderr)
            if os.environ.get("LATTICE_DEBUG"):
                import traceback

                traceback.print_exc(file=sys.stderr)
