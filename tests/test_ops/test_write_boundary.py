"""G-1 (SPEC §14): the write boundary, kept by an AST scan of ``src/lattice``.

Every durable write goes through a recorded, marker-checked primitive of
``lattice.storage.fs`` (SPEC §6.2). This test keeps that true for future code:

1. **No raw file writes.** Nothing in ``src/lattice`` writes, creates, renames,
   or removes a file or directory except ``lattice/storage/fs.py`` and the
   modules in ``RAW_WRITERS``, each of which writes only runtime paths or files
   outside any board, named with a one-line reason.
2. **Board writers stay behind operations.** No module outside ``lattice.ops``
   and ``lattice.storage`` calls a storage writer: a write primitive of
   ``storage/fs.py``, ``mutate_task``, or any ``lattice.storage`` function that
   (transitively) calls one. The exceptions are ``BOARD_OWNERS``, the writers
   SPEC names besides operations (the owning server, the cache syncer, the
   local-only maintenance commands), and ``AWAITING_CONVERSION``, each naming
   the ticket that removes it.
3. **The lists stay true.** Every listed module exists and still does what it
   is listed for, so an entry that is no longer needed fails until removed.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
PACKAGE = SRC / "lattice"
FS_MODULE = "lattice.storage.fs"

#: The write primitives of storage/fs.py (SPEC §6.2), and mutate_task.
PRIMITIVES = frozenset(
    {
        "atomic_write",
        "jsonl_append",
        "ensure_dir",
        "unlink_path",
        "unlink_entry",
        "truncate_file",
        "remove_dir",
        "ensure_artifact_dirs",
        "ensure_lattice_dirs",
        "mutate_task",
    }
)

#: Modules that write files directly, each only runtime paths (SPEC §6.1) or
#: files outside any board.
RAW_WRITERS: dict[str, str] = {
    "lattice.agent_runner": "an agent's output, done, and error files in the prompt temp dir",
    "lattice.cli.auto_review": "the auto-review spawn log under .daemon/ (runtime)",
    "lattice.cli.demo_cmd": "demo init: the demo checkout's directory, CLAUDE.md, agents.md",
    "lattice.cli.main": "setup-claude / setup-agents: CLAUDE.md, AGENTS.md, skills (repo files)",
    "lattice.cli.remote_cmds": "remote attach: .gitignore and .git/info/exclude of the checkout",
    "lattice.cli.review_cmds": "the review text's temp file in the system temp dir",
    "lattice.core.agent_spawn": "the spawn prompt and output temp files (tmp-prompts/, runtime)",
    "lattice.core.review": "review_state/ records, failures.jsonl and temp prompts (runtime)",
    "lattice.integrations.c11": "the c11 bridge's state file in the user's data dir",
    "lattice.remote.cache": "cache control (SPEC §6.1): cache/incoming staging, cache/rescued, "
    "cache/applying, cache/unreachable_until",
    "lattice.remote.config": "remotes.json in the user's config dir",
    "lattice.remote.session": "cache/unreachable_until and cache/acked.jsonl (cache control)",
    "lattice.server.control": "control requests under hosted/control (server control)",
    "lattice.server.journal": "the journal under hosted/ (server control)",
    "lattice.server.transactions": "undo logs and receipts under hosted/ (server control)",
    "lattice.server.admin": "the server root: server.json, projects/.creating-* staging",
    "lattice.server.testing": "server.json of a test server root (test helper)",
    "lattice.storage.agent_spawn": "the headless agent's prompt and log files (tmp-prompts/)",
    "lattice.storage.ownership": "the owner lease file hosted/owner.lock (server control)",
    "lattice.update_check": "the update-check cache in the user's cache dir",
}

#: Writers of board data besides operations: the ones SPEC names, and today's
#: local session touch. All write through the marker-checked primitives.
BOARD_OWNERS: dict[str, str] = {
    "lattice.server.project": "the owning server (SPEC §6.2): lease, load, server transactions",
    "lattice.server.transactions": "the owning server: rollback from the undo log (SPEC §8.6)",
    "lattice.server.journal": "the owning server: journal and epoch rotation (SPEC §8.6)",
    "lattice.server.admin": "project create and offline maintenance on the server host (§8.2)",
    "lattice.server.registry": "the owning server: server_status.json in the server root (SPEC §8.2)",
    "lattice.server.tokens": "tokens.json in the server root, outside any board (SPEC §8.3)",
    "lattice.remote.cache": "the cache syncer, the only writer of a cache (SPEC §6.2, §9.4)",
    "lattice.remote.follower": "the follower's cache/follower.json (cache control, SPEC §6.1, "
    "§9.6)",
    "lattice.server.testing": "test helper: a server project built from a fixture board, as "
    "the owning server's import would (SPEC §11)",
    "lattice.cli.main": "init and its example tasks (LOCAL_ONLY, SPEC §3.5)",
    "lattice.cli.demo_cmd": "demo init (LOCAL_ONLY, SPEC §3.5)",
    "lattice.cli.integrity_cmds": "rebuild, doctor --fix, backfill-ids (LOCAL_ONLY, SPEC §3.5)",
    "lattice.cli.migration_cmds": "migrate needs-human (LOCAL_ONLY, SPEC §3.5)",
    "lattice.cli.maintenance": "--offline-maintenance: the owner flock and "
    "hosted/maintenance.json (SPEC §3.5)",
    "lattice.cli.helpers": "require_actor's session touch on a local board, as today (SPEC §3.7); "
    "skipped on a cache (§9.5), refused on a server-owned board by the markers (§6.2)",
}

#: Board writers that still bypass operations, each removed by the named ticket
#: (operator ruling on PR #82): the converting ticket deletes its own entry.
AWAITING_CONVERSION: dict[str, str] = {
    "lattice.dashboard.server": "H-13a (LAT-313): the dashboard's POSTs call operations",
    "lattice.mcp.tools": "H-21 (LAT-315): the MCP tools call operations",
}

#: ``Path`` / file-object methods that change the filesystem.
RAW_METHODS = frozenset(
    {"write_text", "write_bytes", "mkdir", "touch", "unlink", "rmdir", "rename", "symlink_to"}
)
#: Module functions that change the filesystem.
RAW_FUNCTIONS = frozenset(
    {
        ("os", "replace"),
        ("os", "rename"),
        ("os", "remove"),
        ("os", "unlink"),
        ("os", "mkdir"),
        ("os", "makedirs"),
        ("os", "rmdir"),
        ("os", "removedirs"),
        ("os", "truncate"),
        ("os", "symlink"),
        ("os", "link"),
        ("shutil", "rmtree"),
        ("shutil", "copy"),
        ("shutil", "copy2"),
        ("shutil", "copyfile"),
        ("shutil", "copytree"),
        ("shutil", "move"),
    }
)
WRITE_MODE_CHARS = frozenset("wax+")


@dataclass
class Module:
    name: str
    path: Path
    tree: ast.Module
    #: local name -> (module, original name): ``from X import name as local``, and
    #: ``local = <resolvable name>`` aliases anywhere in the module
    symbols: dict[str, tuple[str, str]] = field(default_factory=dict)
    #: local name -> module, for ``import X as local`` / ``from pkg import module``
    modules: dict[str, str] = field(default_factory=dict)

    @property
    def inside_boundary(self) -> bool:
        return self.name.startswith(("lattice.ops.", "lattice.storage.")) or self.name in (
            "lattice.ops",
            "lattice.storage",
        )


def load_module(name: str, path: Path, tree: ast.Module) -> Module:
    """*tree* with its imports and name aliases resolved."""
    module = Module(name, path, tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                local = alias.asname or alias.name
                module.symbols[local] = (node.module, alias.name)
                module.modules[local] = f"{node.module}.{alias.name}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    module.modules[alias.asname] = alias.name
                else:
                    module.modules[alias.name.split(".")[0]] = alias.name.split(".")[0]
                    module.modules[alias.name] = alias.name
    # ``aw = atomic_write`` / ``zap = os.unlink``: the alias resolves like the original.
    for _ in range(3):
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, (ast.Name, ast.Attribute))
            ):
                target = resolve(module, node.value)
                if target is not None and target[1] != node.targets[0].id:
                    module.symbols.setdefault(node.targets[0].id, target)
    return module


def _modules() -> dict[str, Module]:
    found: dict[str, Module] = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        parts = path.relative_to(SRC).with_suffix("").parts
        name = ".".join(parts[:-1] if parts[-1] == "__init__" else parts)
        found[name] = load_module(name, path, ast.parse(path.read_text(encoding="utf-8")))
    return found


def resolve(module: Module, expr: ast.expr) -> tuple[str, str] | None:
    """What *expr* names: ``(module, symbol)``. An attribute of an imported
    object (``Saver.save``, ``Saver().save``) resolves to that object."""
    if isinstance(expr, ast.Name):
        if expr.id in module.symbols:
            return module.symbols[expr.id]
        if expr.id in module.modules:
            return None  # a module itself
        return (module.name, expr.id)
    if isinstance(expr, ast.Attribute):
        value = expr.value
        if isinstance(value, ast.Name) and value.id in module.modules:
            return (module.modules[value.id], expr.attr)
        if isinstance(value, ast.Attribute):
            dotted = _dotted(value)
            if dotted is not None and dotted in module.modules.values():
                return (dotted, expr.attr)
        if isinstance(value, ast.Name) and value.id in module.symbols:
            return module.symbols[value.id]
        if isinstance(value, ast.Call):
            return resolve(module, value.func)
    return None


def _dotted(expr: ast.expr) -> str | None:
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        head = _dotted(expr.value)
        return None if head is None else f"{head}.{expr.attr}"
    return None


MODULES = _modules()


def _calls(tree: ast.AST) -> list[ast.Call]:
    """Every call in *tree*, in source order."""
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    return sorted(calls, key=lambda c: (c.lineno, c.col_offset))


def _constant_mode(call: ast.Call, position: int) -> str | None:
    if len(call.args) > position and isinstance(call.args[position], ast.Constant):
        return call.args[position].value
    for keyword in call.keywords:
        if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant):
            return keyword.value.value
    return None


OPENERS = frozenset({("builtins", "open"), ("io", "open"), ("codecs", "open")})


def raw_writes(module: Module) -> list[str]:
    """Each direct filesystem change in *module*, as ``line: call``."""
    hits: list[str] = []
    for call in _calls(module.tree):
        func = call.func
        where = f"{module.name}:{call.lineno}"
        target = resolve(module, func)
        if target == (module.name, "open") and isinstance(func, ast.Name):
            target = ("builtins", "open")  # not shadowed: the builtin
        if target in OPENERS:
            mode = _constant_mode(call, 1)
            if mode is None and (len(call.args) > 1 or call.keywords):
                hits.append(f"{where}: open(<mode>)")
            elif isinstance(mode, str) and WRITE_MODE_CHARS & set(mode):
                hits.append(f"{where}: open({mode!r})")
        elif target in RAW_FUNCTIONS or target == ("os", "open"):
            hits.append(f"{where}: {target[0]}.{target[1]}")
        elif isinstance(func, ast.Attribute):
            if func.attr in RAW_METHODS:
                hits.append(f"{where}: .{func.attr}()")
            elif func.attr == "replace" and len(call.args) == 1 and not call.keywords:
                hits.append(f"{where}: .replace(target)")  # str.replace takes two
            elif func.attr == "open":
                mode = _constant_mode(call, 0)
                if isinstance(mode, str) and WRITE_MODE_CHARS & set(mode):
                    hits.append(f"{where}: .open({mode!r})")
    return hits


def _definitions(module: Module) -> dict[str, ast.AST]:
    """Top-level functions and classes by name; a class stands for all its methods,
    and a function for everything nested in it."""
    return {
        node.name: node
        for node in module.tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }


def storage_writers() -> set[tuple[str, str]]:
    """``(module, name)`` for every lattice.storage function or class that writes a
    board: a primitive of storage/fs.py, mutate_task, or anything that uses one,
    directly, through a nested helper or a method, or through another such writer."""
    definitions: dict[tuple[str, str], ast.AST] = {}
    for module in MODULES.values():
        if module.name.startswith("lattice.storage"):
            for name, node in _definitions(module).items():
                definitions[(module.name, name)] = node
    writers = {(FS_MODULE, name) for name in PRIMITIVES if name != "mutate_task"}
    writers.add(("lattice.storage.operations", "mutate_task"))
    changed = True
    while changed:
        changed = False
        for key, node in definitions.items():
            if key not in writers and _uses_writer(MODULES[key[0]], node, writers):
                writers.add(key)
                changed = True
    return writers


def _uses_writer(module: Module, tree: ast.AST, writers: set[tuple[str, str]]) -> bool:
    return bool(_writer_uses(module, tree, writers))


def _writer_uses(
    module: Module, tree: ast.AST, writers: set[tuple[str, str]]
) -> list[tuple[int, tuple[str, str]]]:
    """Every call of, or reference to, a writer in *tree* (a reference covers a writer
    passed as a callback or bound to another name)."""
    found: list[tuple[int, tuple[str, str]]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, ast.Load):
            target = resolve(module, node)
            if target in writers:
                found.append((node.lineno, target))
    return sorted(set(found))


WRITERS = storage_writers()


def board_writer_calls(module: Module) -> list[str]:
    return [
        f"{module.name}:{line}: {target[0]}.{target[1]}"
        for line, target in _writer_uses(module, module.tree, WRITERS)
    ]


# ---------------------------------------------------------------------------


def test_the_scan_sees_the_package() -> None:
    assert "lattice.storage.fs" in MODULES and "lattice.ops.base" in MODULES
    assert ("lattice.storage.operations", "mutate_task") in WRITERS
    # Transitive storage writers are found (a session file is written through atomic_write).
    assert ("lattice.storage.sessions", "create_session") in WRITERS


def test_no_raw_file_writes_outside_storage_fs() -> None:
    offenders = [
        hit
        for module in MODULES.values()
        if module.name != FS_MODULE and module.name not in RAW_WRITERS
        for hit in raw_writes(module)
    ]
    assert offenders == [], (
        "write board files through lattice.storage.fs (atomic_write, jsonl_append, "
        "ensure_dir, unlink_path); a module that writes only runtime paths or files "
        "outside any board goes in RAW_WRITERS with its reason:\n" + "\n".join(offenders)
    )


def test_board_writers_are_called_only_behind_operations() -> None:
    allowed = set(BOARD_OWNERS) | set(AWAITING_CONVERSION)
    offenders = [
        hit
        for module in MODULES.values()
        if not module.inside_boundary and module.name not in allowed
        for hit in board_writer_calls(module)
    ]
    assert offenders == [], (
        "outside lattice.ops and lattice.storage, write a board through an operation "
        "(resolve_board(...).execute):\n" + "\n".join(offenders)
    )


def test_every_listed_module_still_needs_its_entry() -> None:
    for name in RAW_WRITERS:
        assert name in MODULES, f"RAW_WRITERS names a missing module {name}"
        assert raw_writes(MODULES[name]), f"{name} no longer writes raw files; remove it"
    for name in (*BOARD_OWNERS, *AWAITING_CONVERSION):
        assert name in MODULES, f"missing module {name}"
        assert board_writer_calls(MODULES[name]), f"{name} no longer writes a board; remove it"
        assert not MODULES[name].inside_boundary
    assert not set(BOARD_OWNERS) & set(AWAITING_CONVERSION)


def _scan(source: str, name: str = "lattice.cli.example") -> tuple[list[str], list[str]]:
    module = load_module(name, Path("example.py"), ast.parse(source))
    raw = [hit.split(": ", 1)[1] for hit in raw_writes(module)]
    board = [hit.split(": ", 1)[1] for hit in board_writer_calls(module)]
    return raw, board


def test_the_scan_catches_plain_writes() -> None:
    raw, board = _scan(
        """
from pathlib import Path
from lattice.storage.fs import atomic_write
from lattice.storage import operations
import os, shutil

def f(p: Path):
    p.write_text("x")
    open(p, "a").close()
    os.replace(p, p)
    shutil.rmtree(p)
    p.replace(p)
    "a".replace("a", "b")
    open(p).read()
    atomic_write(p, "x")
    operations.mutate_task(p, "t", None, {})
"""
    )
    assert raw == [".write_text()", "open('a')", "os.replace", "shutil.rmtree", ".replace(target)"]
    assert board == ["lattice.storage.fs.atomic_write", "lattice.storage.operations.mutate_task"]


def test_the_scan_sees_through_aliases_and_imported_functions() -> None:
    raw, board = _scan(
        """
import io
import os as system
import lattice.storage.fs as board_fs
from os import unlink as zap, rename
from shutil import rmtree as nuke, move
from lattice.storage.fs import atomic_write as aw

again = aw
unlinker = system.remove

def f(p):
    zap(p)
    rename(p, p)
    nuke(p)
    move(p, p)
    unlinker(p)
    io.open(p, "w")
    system.makedirs(p)
    aw(p, "x")
    again(p, "x")
    board_fs.unlink_path(p)
    schedule(aw)
"""
    )
    assert raw == [
        "os.unlink",
        "os.rename",
        "shutil.rmtree",
        "shutil.move",
        "os.remove",
        "open('w')",
        "os.makedirs",
    ]
    assert board == [
        "lattice.storage.fs.atomic_write",  # the ``again = aw`` alias line
        "lattice.storage.fs.atomic_write",
        "lattice.storage.fs.atomic_write",
        "lattice.storage.fs.unlink_path",
        "lattice.storage.fs.atomic_write",  # passed as a callback
    ]


def test_a_storage_writer_behind_a_method_or_a_nested_helper_is_a_writer() -> None:
    """Transitive writers include classes whose methods write and functions whose
    nested helpers write; calling them from outside is caught."""
    storage = load_module(
        "lattice.storage.example",
        Path("example.py"),
        ast.parse(
            """
from lattice.storage.fs import atomic_write

class Saver:
    def save(self, p):
        atomic_write(p, "x")

def outer(p):
    def inner():
        atomic_write(p, "x")
    inner()

def reader(p):
    return p.read_text()
"""
        ),
    )
    MODULES[storage.name] = storage
    try:
        writers = storage_writers()
        assert ("lattice.storage.example", "Saver") in writers
        assert ("lattice.storage.example", "outer") in writers
        assert ("lattice.storage.example", "reader") not in writers
        caller = load_module(
            "lattice.cli.example",
            Path("caller.py"),
            ast.parse(
                """
from lattice.storage.example import Saver, outer, reader

def f(p):
    Saver().save(p)
    Saver.save(Saver(), p)
    outer(p)
    reader(p)
"""
            ),
        )
        used = {target for _, target in _writer_uses(caller, caller.tree, writers)}
        assert used == {("lattice.storage.example", "Saver"), ("lattice.storage.example", "outer")}
    finally:
        del MODULES[storage.name]
