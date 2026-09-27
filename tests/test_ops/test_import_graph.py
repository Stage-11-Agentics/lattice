"""G-5 and G-6: ``lattice.ops`` (and ``lattice.boards``) import nothing from
``lattice.cli`` or ``lattice.integrations``, so no ``output_error`` and no
``SystemExit`` can be reached from an operation, and no c11 code runs there."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import lattice

SRC = Path(lattice.__file__).parent
FORBIDDEN = ("lattice.cli", "lattice.integrations", "lattice.dashboard", "lattice.mcp")


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


def test_ops_and_boards_source_imports_nothing_forbidden() -> None:
    files = [*sorted((SRC / "ops").rglob("*.py")), SRC / "boards.py"]
    offenders = {
        str(path.relative_to(SRC)): sorted(n for n in _imports(path) if n.startswith(FORBIDDEN))
        for path in files
    }
    assert {k: v for k, v in offenders.items() if v} == {}


def test_running_discovery_loads_nothing_forbidden() -> None:
    code = (
        "import sys, lattice.ops, lattice.boards\n"
        "lattice.ops.discover()\n"
        f"bad = sorted(m for m in sys.modules if m.startswith({FORBIDDEN!r}))\n"
        "print(bad)\n"
        "assert not bad, bad\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_server_source_imports_nothing_forbidden() -> None:
    """G-5: ``lattice.server`` imports nothing from ``lattice.cli`` or ``lattice.integrations``."""
    files = sorted((SRC / "server").rglob("*.py"))
    assert files
    offenders = {
        str(path.relative_to(SRC)): sorted(
            n for n in _imports(path) if n.startswith(("lattice.cli", "lattice.integrations"))
        )
        for path in files
    }
    assert {k: v for k, v in offenders.items() if v} == {}


def test_running_the_server_app_loads_nothing_forbidden() -> None:
    code = (
        "import sys, lattice.server.app, lattice.server.serve, lattice.server.testing\n"
        "import lattice.ops\n"
        "lattice.ops.discover()\n"
        "bad = sorted(m for m in sys.modules if m.startswith(('lattice.cli', 'lattice.integrations')))\n"
        "print(bad)\n"
        "assert not bad, bad\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
