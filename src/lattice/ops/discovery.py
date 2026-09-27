"""Find every operation: Lattice's own modules, then installed plugins.

Discovery runs once, on the first registry lookup, rather than at package
import: the CLI imports ``lattice.ops`` on every start, and scanning entry
points there would slow read commands that never write.
"""

from __future__ import annotations

import importlib
import os
import pkgutil
import sys
from importlib.metadata import entry_points

OPERATIONS_GROUP = "lattice.operations"

_discovered = False


def discover() -> None:
    """Import every module in ``lattice.ops`` (sorted), then every module named
    by an entry point in the ``lattice.operations`` group (sorted by name).

    A plugin that fails to import, or registers a name that is taken, is
    reported on stderr and skipped; it never breaks the built-in operations.
    """
    global _discovered
    if _discovered:
        return
    _discovered = True

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
