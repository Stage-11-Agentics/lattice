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
    "lattice.remote.acked": "cache/acked.jsonl and its lock (cache control, SPEC §6.1, §9.2)",
    "lattice.remote.config": "remotes.json in the user's config dir",
    "lattice.remote.session": "cache/unreachable_until and cache/acked.jsonl (cache control)",
    "lattice.server.control": "control requests under hosted/control (server control)",
    "lattice.server.journal": "the journal under hosted/ (server control)",
    "lattice.server.transactions": "undo logs and receipts under hosted/ (server control)",
    "lattice.server.admin": "the server root: server.json, projects/.creating-* staging",
    "lattice.server.audit": "the project directory's .gitignore, outside the board (SPEC §8.10)",
    "lattice.server.importer": "project import: reads the source without following links, "
    "then renames or removes its projects/.importing-* staging (SPEC §11)",
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
    "lattice.server.recovery": "the owning server: startup recovery from undo logs, receipts, "
    "and the journal (SPEC §8.7)",
    "lattice.server.audit": "the owning server: hosted/audit.json settings (SPEC §8.10)",
    "lattice.server.importer": "project import on the server host, doctor-gated, into its "
    "staging board (SPEC §11)",
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
}

#: Methods that change the filesystem when the receiver is a path whose type the
#: scan cannot see (``p.unlink()`` on a local variable).
RAW_METHODS = frozenset(
    {
        "write_text",
        "write_bytes",
        "mkdir",
        "touch",
        "unlink",
        "rmdir",
        "rename",
        "replace",
        "symlink_to",
        "hardlink_to",
    }
)
#: Qualified names that change the filesystem, however they are reached: called,
#: aliased, or passed as a callback.
RAW_NAMES = frozenset(
    {
        *(
            f"os.{name}"
            for name in (
                "replace",
                "rename",
                "renames",
                "remove",
                "unlink",
                "mkdir",
                "makedirs",
                "rmdir",
                "removedirs",
                "truncate",
                "symlink",
                "link",
                "open",
            )
        ),
        *(
            f"shutil.{name}"
            for name in ("rmtree", "copy", "copy2", "copyfile", "copytree", "move")
        ),
        *(f"pathlib.{cls}.{name}" for cls in ("Path", "PosixPath") for name in RAW_METHODS),
    }
)
#: Openers that write only in a write mode (checked on the call).
OPENERS = frozenset({"builtins.open", "io.open", "codecs.open"})
WRITE_MODE_CHARS = frozenset("wax+")


@dataclass
class Module:
    name: str
    path: Path
    tree: ast.Module
    #: local name -> qualified name: ``from X import name as local``, and
    #: ``local = <resolvable expression>`` aliases anywhere in the module
    symbols: dict[str, str] = field(default_factory=dict)
    #: local name -> module, for ``import X as local`` and ``import X``
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
                module.symbols[alias.asname or alias.name] = f"{node.module}.{alias.name}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    module.modules[alias.asname] = alias.name
                else:
                    head = alias.name.split(".")[0]
                    module.modules[head] = head
    # ``aw = atomic_write`` / ``kill = Path.unlink``: the alias resolves like the original.
    for _ in range(3):
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, (ast.Name, ast.Attribute))
            ):
                local = node.targets[0].id
                target = resolve(module, node.value)
                if target is not None and target != f"{module.name}.{local}":
                    module.symbols.setdefault(local, target)
    return module


def _modules() -> dict[str, Module]:
    found: dict[str, Module] = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        parts = path.relative_to(SRC).with_suffix("").parts
        name = ".".join(parts[:-1] if parts[-1] == "__init__" else parts)
        found[name] = load_module(name, path, ast.parse(path.read_text(encoding="utf-8")))
    return found


def resolve(module: Module, expr: ast.expr) -> str | None:
    """The qualified name *expr* refers to (``os.unlink``, ``pathlib.Path.replace``,
    ``lattice.storage.example.Saver.save``). A name the module defines or binds
    locally is ``<module>.<name>``; an attribute of a call (``Saver().save``,
    ``Path(p).unlink``) is an attribute of what was called."""
    if isinstance(expr, ast.Name):
        if expr.id in module.symbols:
            return module.symbols[expr.id]
        if expr.id in module.modules:
            return module.modules[expr.id]
        if expr.id == "open":
            return "builtins.open"
        return f"{module.name}.{expr.id}"
    if isinstance(expr, ast.Attribute):
        value = expr.value.func if isinstance(expr.value, ast.Call) else expr.value
        if isinstance(value, (ast.Name, ast.Attribute)):
            base = resolve(module, value)
            if base is not None:
                return f"{base}.{expr.attr}"
    return None


MODULES = _modules()


def _loads(tree: ast.AST) -> list[ast.expr]:
    """Every loaded name or attribute in *tree*, in source order."""
    nodes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, ast.Load)
    ]
    return sorted(nodes, key=lambda n: (n.lineno, n.col_offset))


def _constant_mode(call: ast.Call, positions: tuple[int, ...]) -> str | None:
    for position in positions:
        if len(call.args) > position and isinstance(call.args[position], ast.Constant):
            value = call.args[position].value
            if isinstance(value, str):
                return value
    for keyword in call.keywords:
        if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant):
            return keyword.value.value
    return None


def _writes_mode(mode: object) -> bool:
    return isinstance(mode, str) and bool(WRITE_MODE_CHARS & set(mode))


def raw_writes(module: Module) -> list[str]:
    """Each direct filesystem change in *module*, as ``line: what``: any reference to
    a raw writer (a call, an alias, a callback), an opener called in a write mode,
    and a writing method called on a receiver the scan cannot type."""
    calls = {id(node.func): node for node in ast.walk(module.tree) if isinstance(node, ast.Call)}
    hits: list[str] = []
    for node in _loads(module.tree):
        where = f"{module.name}:{node.lineno}"
        call = calls.get(id(node))
        target = resolve(module, node)
        if target in OPENERS:
            if call is not None:
                mode = _constant_mode(call, (1,))
                if mode is None and (len(call.args) > 1 or call.keywords):
                    hits.append(f"{where}: open(<mode>)")
                elif _writes_mode(mode):
                    hits.append(f"{where}: open({mode!r})")
        elif target in RAW_NAMES:
            hits.append(f"{where}: {target}")
        elif isinstance(node, ast.Attribute) and call is not None:
            if node.attr in RAW_METHODS - {"replace"}:
                hits.append(f"{where}: .{node.attr}()")
            elif node.attr == "replace" and len(call.args) == 1 and not call.keywords:
                hits.append(f"{where}: .replace(target)")  # str.replace takes two
            elif node.attr == "open" and _writes_mode(_constant_mode(call, (0, 1))):
                hits.append(f"{where}: .open({_constant_mode(call, (0, 1))!r})")
    return hits


def _definitions(module: Module) -> dict[str, ast.AST]:
    """Top-level functions and classes by name; a class stands for all its methods,
    and a function for everything nested in it."""
    return {
        node.name: node
        for node in module.tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }


def _writer_of(target: str | None, writers: set[str]) -> str | None:
    """The writer *target* is or belongs to (``Saver.save`` belongs to ``Saver``)."""
    if target is None:
        return None
    parts = target.split(".")
    for end in range(len(parts), 1, -1):
        prefix = ".".join(parts[:end])
        if prefix in writers:
            return prefix
    return None


def storage_writers() -> set[str]:
    """Every lattice.storage function or class that writes a board, by qualified
    name: a primitive of storage/fs.py, mutate_task, or anything that uses one,
    directly, through a nested helper or a method, or through another such writer."""
    definitions: dict[str, tuple[Module, ast.AST]] = {}
    for module in MODULES.values():
        if module.name.startswith("lattice.storage"):
            for name, node in _definitions(module).items():
                definitions[f"{module.name}.{name}"] = (module, node)
    writers = {f"{FS_MODULE}.{name}" for name in PRIMITIVES if name != "mutate_task"}
    writers.add("lattice.storage.operations.mutate_task")
    changed = True
    while changed:
        changed = False
        for key, (module, node) in definitions.items():
            if key not in writers and _writer_uses(module, node, writers):
                writers.add(key)
                changed = True
    return writers


def _writer_uses(module: Module, tree: ast.AST, writers: set[str]) -> list[tuple[int, str]]:
    """Every call of, or reference to, a writer in *tree* (a reference covers a writer
    passed as a callback or bound to another name)."""
    found = set()
    for node in _loads(tree):
        writer = _writer_of(resolve(module, node), writers)
        if writer is not None:
            found.add((node.lineno, writer))
    return sorted(found)


WRITERS = storage_writers()


def board_writer_calls(module: Module) -> list[str]:
    return [
        f"{module.name}:{line}: {writer}"
        for line, writer in _writer_uses(module, module.tree, WRITERS)
    ]


# ---------------------------------------------------------------------------


def test_the_scan_sees_the_package() -> None:
    assert "lattice.storage.fs" in MODULES and "lattice.ops.base" in MODULES
    assert "lattice.storage.operations.mutate_task" in WRITERS
    # Transitive storage writers are found (a session file is written through atomic_write).
    assert "lattice.storage.sessions.create_session" in WRITERS


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
        "os.remove",  # the ``unlinker = system.remove`` alias line
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


def test_the_scan_catches_raw_writers_whatever_the_shape_of_the_reference() -> None:
    """Round-2 counterexamples: a pathlib method bound to a name, a raw writer
    passed as a callback, and an unbound pathlib method called with two arguments."""
    raw, _ = _scan(
        """
from os import unlink as zap
from pathlib import Path

kill = Path.unlink

def f(path, source, target):
    kill(path)
    schedule(zap)
    Path.replace(source, target)
    Path(path).write_text("x")
    run_later(Path.rmdir)
"""
    )
    assert raw == [
        "pathlib.Path.unlink",  # the ``kill = Path.unlink`` alias line
        "pathlib.Path.unlink",
        "os.unlink",
        "pathlib.Path.replace",
        "pathlib.Path.write_text",
        "pathlib.Path.rmdir",
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
        assert "lattice.storage.example.Saver" in writers
        assert "lattice.storage.example.outer" in writers
        assert "lattice.storage.example.reader" not in writers
        caller = load_module(
            "lattice.cli.example",
            Path("caller.py"),
            ast.parse(
                """
from lattice.storage.example import Saver, outer, reader

save = Saver.save

def f(p):
    Saver().save(p)
    save(Saver(), p)
    outer(p)
    reader(p)
"""
            ),
        )
        used = [writer for _, writer in _writer_uses(caller, caller.tree, writers)]
        assert sorted(set(used)) == [
            "lattice.storage.example.Saver",
            "lattice.storage.example.outer",
        ]
        assert len(used) == 4  # the alias line, the two Saver calls' line each, outer
    finally:
        del MODULES[storage.name]
