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
3. **Client-cache writers stay with the client cache.** The mutating APIs of
   ``lattice.remote.cache_paths`` (a hosted checkout's own directories and the
   files in them, never through a symlink) are writer primitives too, tracked
   through that module's own helpers. Only ``CACHE_OWNERS`` may use them.
4. **The lists stay true.** Every listed module exists and still does what it
   is listed for, so an entry that is no longer needed fails until removed.
"""

from __future__ import annotations

import ast
import functools
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
    "lattice.cli.dashboard_cmd": "private dashboard launch/restart metadata and logs in "
    "the user's cache dir (runtime; no board state)",
    "lattice.cli.main": "setup-claude / setup-agents: CLAUDE.md, AGENTS.md, skills (repo files)",
    "lattice.cli.remote_cmds": "remote attach: .gitignore and .git/info/exclude of the checkout",
    "lattice.cli.review_cmds": "the review text's temp file in the system temp dir",
    "lattice.core.agent_spawn": "the spawn prompt and output temp files (tmp-prompts/, runtime)",
    "lattice.core.review": "review_state/ records, failures.jsonl and temp prompts (runtime)",
    "lattice.dashboard.media_prep": "a filed video or HEIC photo's scratch copy in the system "
    "temp dir for ffmpeg or sips (never a board path)",
    "lattice.integrations.c11": "the c11 bridge's state file in the user's data dir, and the "
    "trident pane's prompt under <cwd>/.lattice/tmp-prompts/ (runtime; refused on a hosted "
    "checkout whose .lattice is not a real directory, SPEC §9.4)",
    "lattice.remote.cache": "cache control (SPEC §6.1): cache/incoming staging, cache/rescued, "
    "cache/applying, cache/unreachable_until",
    "lattice.remote.acked": "cache/acked.jsonl and its lock (cache control, SPEC §6.1, §9.2)",
    "lattice.remote.cache_paths": "the client's own directories under a hosted .lattice/ "
    "(cache/, runtime) and files in them, never through a symlink (SPEC §6.1, §9.4)",
    "lattice.remote.config": "remotes.json in the user's config dir",
    "lattice.remote.issue_media": "the client-private issue-media cache under cache/issue-media/ "
    "(runtime, never synced) and the media files an upload reads",
    "lattice.remote.session": "opens locks/cache_sync.lock to probe it (runtime); its cache "
    "files go through lattice.remote.cache_paths",
    "lattice.server.control": "control requests under hosted/control (server control)",
    "lattice.server.journal": "the journal under hosted/ (server control)",
    "lattice.server.transactions": "undo logs and receipts under hosted/ (server control)",
    "lattice.server.admin": "the server root: server.json, projects/.creating-* staging",
    "lattice.server.audit": "the project directory's .gitignore, outside the board (SPEC §8.10)",
    "lattice.server.issue_media": "hosted issue media: transport staging under the project's "
    "server-runtime directory and the finalize step into issues/media/, outside the board's "
    "synced and recorded paths (SPEC §8.4)",
    "lattice.server.importer": "project import: reads the source without following links, "
    "then renames or removes its projects/.importing-* staging (SPEC §11)",
    "lattice.server.testing": "server.json of a test server root (test helper)",
    "lattice.storage.agent_spawn": "the headless agent's prompt and log files (tmp-prompts/)",
    "lattice.storage.locks": "the task gate locks/task_gate.lock (runtime, SPEC §6.1)",
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
    "lattice.server.issue_media": "the owning server: finalizes, reconciles and removes issue media "
    "blobs after the named transaction commits; media is marker-checked but not synced or "
    "recorded (SPEC §8.4)",
    "lattice.server.tokens": "tokens.json in the server root, outside any board (SPEC §8.3)",
    "lattice.server.sessions": "web_sessions.json in the server root, outside any board "
    "(SPEC §10)",
    "lattice.remote.cache": "the cache syncer, the only writer of a cache (SPEC §6.2, §9.4)",
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

#: The mutating APIs of lattice.remote.cache_paths: they create, chmod, replace,
#: or remove under a hosted checkout's .lattice/ (SPEC §6.1 cache control and
#: runtime paths, §9.4).
CACHE_PATHS_MODULE = "lattice.remote.cache_paths"
CACHE_PRIMITIVES = frozenset({"open_child", "open_dir", "opened_dir", "write_file", "remove_file"})

#: The client-cache writers: the only modules that may use a cache_paths writer.
CACHE_OWNERS: dict[str, str] = {
    "lattice.remote.acked": "cache/acked.jsonl and its lock (SPEC §9.2, §9.5)",
    "lattice.remote.cache": "the cache syncer and cache clear: .lattice/, cache/, locks/, "
    "runtime directories, applying, state.json, staging, rescue (SPEC §9.4)",
    "lattice.remote.session": "cache/unreachable_until (SPEC §9.5) and cache/server_info.json "
    "(SPEC §15)",
    "lattice.remote.follower": "the follower's cache/follower.json (SPEC §9.6)",
    "lattice.remote.issue_media": "the client-private issue-media cache under cache/issue-media/ "
    "(SPEC §6.1 runtime, never synced)",
}

#: Board writers that still bypass operations, each removed by the named ticket
#: (operator ruling on PR #82): the converting ticket deletes its own entry.
AWAITING_CONVERSION: dict[str, str] = {}

# Review state is transient runtime data (SPEC §6.1), not task board data. Its
# owning core module may persist it only through this storage helper; all other
# calls to transitive storage writers remain behind operations.
RUNTIME_STORAGE_WRITERS: dict[str, frozenset[str]] = {
    "lattice.core.review": frozenset({"lattice.storage.review_state.write_review_state_file"}),
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


@functools.cache
def modules() -> dict[str, Module]:
    """Every module of the package, parsed once, on first use: parsing at import
    would cost every xdist worker the parse during collection."""
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


def _scope_parents(module: Module) -> dict[int, ast.AST]:
    return {
        id(child): node for node in ast.walk(module.tree) for child in ast.iter_child_nodes(node)
    }


def _function_binds(function: ast.FunctionDef | ast.AsyncFunctionDef, name: str) -> bool:
    """Whether *name* is local anywhere in *function*'s lexical scope."""
    arguments = function.args
    if any(
        arg.arg == name for arg in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs)
    ):
        return True
    if arguments.vararg is not None and arguments.vararg.arg == name:
        return True
    if arguments.kwarg is not None and arguments.kwarg.arg == name:
        return True

    pending: list[ast.AST] = list(function.body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == name:
                return True
            # A nested definition binds its name in this function, but its body
            # belongs to another scope.
            continue
        if isinstance(node, ast.Lambda):
            continue
        if isinstance(node, ast.Global) and name in node.names:
            return True
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            if node.id == name:
                return True
        if isinstance(node, ast.alias):
            bound = node.asname or (node.name.split(".")[0] if "." in node.name else node.name)
            if bound == name:
                return True
        if isinstance(node, ast.ExceptHandler) and node.name == name:
            return True
        if isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name == name:
            return True
        if isinstance(node, ast.MatchMapping) and node.rest == name:
            return True
        pending.extend(ast.iter_child_nodes(node))
    return False


def _function_shadowed(module: Module, expr: ast.expr, name: str) -> bool:
    """Whether a lexical function surrounding *expr* binds *name*."""
    parents = _scope_parents(module)
    scopes: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    cursor = parents.get(id(expr))
    while cursor is not None:
        if isinstance(cursor, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scopes.append(cursor)
        cursor = parents.get(id(cursor))
    return any(_function_binds(scope, name) for scope in scopes)


def _module_bindings(module: Module, name: str) -> list[tuple[str, ast.AST | str]]:
    """Bindings for *name* in module scope, including reassignments.

    Function, class, lambda, and comprehension bodies have their own scopes;
    assignments in module-level conditionals still bind the module name.
    """
    found: list[tuple[str, ast.AST | str]] = []
    pending: list[ast.AST] = list(module.tree.body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == name:
                found.append(("definition", node))
            continue
        if isinstance(
            node, (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
        ):
            continue
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                if bound == name:
                    found.append(("module", alias.asname and alias.name or bound))
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bound = alias.asname or alias.name
                if bound == name and node.module and node.level == 0:
                    found.append(("symbol", f"{node.module}.{alias.name}"))
                elif bound == name:
                    found.append(("unknown", node))
        elif isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
                found.append(("value", node.value if len(node.targets) == 1 else node))
            elif any(name in _target_names(target) for target in node.targets):
                found.append(("unknown", node))
        elif isinstance(node, ast.AnnAssign):
            if name in _target_names(node.target):
                found.append(("value", node.value if isinstance(node.target, ast.Name) else node))
        elif isinstance(node, (ast.AugAssign, ast.NamedExpr, ast.Delete)):
            targets = (
                node.targets if isinstance(node, (ast.AugAssign, ast.Delete)) else [node.target]
            )
            if any(name in _target_names(target) for target in targets):
                found.append(("unknown", node))
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            if name in _target_names(node.target):
                found.append(("unknown", node))
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            if any(
                item.optional_vars is not None and name in _target_names(item.optional_vars)
                for item in node.items
            ):
                found.append(("unknown", node))
        elif isinstance(node, ast.ExceptHandler) and node.name == name:
            found.append(("unknown", node))
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name == name:
            found.append(("unknown", node))
        elif isinstance(node, ast.MatchMapping) and node.rest == name:
            found.append(("unknown", node))
        pending.extend(ast.iter_child_nodes(node))
    return found


def _target_names(target: ast.AST) -> set[str]:
    return {
        node.id
        for node in ast.walk(target)
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del))
    }


def _os_module_binding(module: Module, name: str, expr: ast.expr) -> str | None:
    if _function_shadowed(module, expr, name):
        return None
    bindings = _module_bindings(module, name)
    if len(bindings) != 1:
        return None
    kind, binding = bindings[0]
    if kind == "module" and binding == "os":
        return "os"
    return None


def _safe_flag_target(
    module: Module, expr: ast.expr, seen: frozenset[str] = frozenset()
) -> str | None:
    """Resolve only statically stable, non-writing os.open flag expressions."""
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.BitOr):
        if _read_only_flag_expr(module, expr.left, seen) and _read_only_flag_expr(
            module, expr.right, seen
        ):
            return "os.__read_only_combination__"
        return None
    if isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name):
        if _os_module_binding(module, expr.value.id, expr) == "os":
            return f"os.{expr.attr}"
        return None
    if isinstance(expr, ast.Name):
        if expr.id in seen or _function_shadowed(module, expr, expr.id):
            return None
        bindings = _module_bindings(module, expr.id)
        if len(bindings) != 1:
            return None
        kind, binding = bindings[0]
        if kind == "symbol" and isinstance(binding, str):
            return binding
        if kind == "value" and isinstance(binding, ast.AST):
            return _safe_flag_target(module, binding, seen | {expr.id})
        return None
    if (
        isinstance(expr, ast.Call)
        and isinstance(expr.func, ast.Name)
        and expr.func.id == "getattr"
        and len(expr.args) == 3
        and not expr.keywords
        and not _function_shadowed(module, expr, "getattr")
        and not _module_bindings(module, "getattr")
        and isinstance(expr.args[0], ast.Name)
        and _os_module_binding(module, expr.args[0].id, expr) == "os"
        and isinstance(expr.args[1], ast.Constant)
        and expr.args[1].value
        in {"O_BINARY", "O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK"}
        and isinstance(expr.args[2], ast.Constant)
        and expr.args[2].value == 0
    ):
        return f"os.{expr.args[1].value}"
    return None


def _read_only_flag_expr(
    module: Module, flags: ast.expr, seen: frozenset[str] = frozenset()
) -> bool:
    """Whether *flags* combines only known non-writing ``os.open`` flags."""
    if isinstance(flags, ast.BinOp) and isinstance(flags.op, ast.BitOr):
        return _read_only_flag_expr(module, flags.left, seen) and _read_only_flag_expr(
            module, flags.right, seen
        )
    safe_flags = {
        "os.O_RDONLY",
        "os.O_DIRECTORY",
        "os.O_NOFOLLOW",
        "os.O_CLOEXEC",
        "os.O_BINARY",
        "os.O_NONBLOCK",
        "os.__read_only_combination__",
    }
    return _safe_flag_target(module, flags, seen) in safe_flags


def _read_only_flags(module: Module, call: ast.Call) -> bool:
    """Whether an ``os.open`` call uses only known non-writing flags."""
    flags = call.args[1] if len(call.args) > 1 else None
    for keyword in call.keywords:
        if keyword.arg == "flags":
            flags = keyword.value
    return flags is not None and _read_only_flag_expr(module, flags)


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
        elif target == "os.open" and call is not None and _read_only_flags(module, call):
            pass  # os.open(path, os.O_RDONLY): a read
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
    for module in modules().values():
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


@functools.cache
def writers() -> frozenset[str]:
    """:func:`storage_writers` of the real package, computed once, on first use."""
    return frozenset(storage_writers())


@functools.cache
def cache_writers() -> frozenset[str]:
    """The cache_paths writer primitives and every cache_paths function that uses
    one (``opened_dir`` through ``open_dir`` through ``open_child``), computed
    once, on first use."""
    module = modules()[CACHE_PATHS_MODULE]
    definitions = {f"{module.name}.{name}": node for name, node in _definitions(module).items()}
    writers = {f"{CACHE_PATHS_MODULE}.{name}" for name in CACHE_PRIMITIVES}
    changed = True
    while changed:
        changed = False
        for key, node in definitions.items():
            if key not in writers and _writer_uses(module, node, writers):
                writers.add(key)
                changed = True
    return frozenset(writers)


def cache_writer_calls(module: Module) -> list[str]:
    return [
        f"{module.name}:{line}: {writer}"
        for line, writer in _writer_uses(module, module.tree, cache_writers())
    ]


def board_writer_calls(module: Module) -> list[str]:
    return [
        f"{module.name}:{line}: {writer}"
        for line, writer in _writer_uses(module, module.tree, writers())
    ]


# ---------------------------------------------------------------------------


def test_the_scan_sees_the_package() -> None:
    assert "lattice.storage.fs" in modules() and "lattice.ops.base" in modules()
    assert "lattice.storage.operations.mutate_task" in writers()
    # Transitive storage writers are found (a session file is written through atomic_write).
    assert "lattice.storage.sessions.create_session" in writers()


def test_no_raw_file_writes_outside_storage_fs() -> None:
    offenders = [
        hit
        for module in modules().values()
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
        for module in modules().values()
        if not module.inside_boundary and module.name not in allowed
        for hit in board_writer_calls(module)
        if hit.partition(": ")[2] not in RUNTIME_STORAGE_WRITERS.get(module.name, frozenset())
    ]
    assert offenders == [], (
        "outside lattice.ops and lattice.storage, write a board through an operation "
        "(resolve_board(...).execute):\n" + "\n".join(offenders)
    )


def test_cache_writers_are_called_only_by_the_client_cache() -> None:
    offenders = [
        hit
        for module in modules().values()
        if module.name != CACHE_PATHS_MODULE and module.name not in CACHE_OWNERS
        for hit in cache_writer_calls(module)
    ]
    assert offenders == [], (
        "only the client cache (CACHE_OWNERS) writes under a hosted checkout's .lattice/ "
        "through lattice.remote.cache_paths; add a new caller there with its SPEC reason:\n"
        + "\n".join(offenders)
    )


def test_the_scan_sees_the_cache_writers() -> None:
    assert f"{CACHE_PATHS_MODULE}.opened_dir" in cache_writers()  # through open_dir
    assert f"{CACHE_PATHS_MODULE}.read_file" not in cache_writers()
    module = load_module(
        "lattice.cli.example",
        Path("example.py"),
        ast.parse(
            """
from lattice.remote import cache_paths
from lattice.remote.cache_paths import write_file as put

def f(fd):
    put(fd, "x", b"")
    cache_paths.remove_file(fd, "x")
    with cache_paths.opened_dir(fd, "x"):
        cache_paths.read_file(fd, "x")
"""
        ),
    )
    found = [hit.split(": ", 1)[1] for hit in cache_writer_calls(module)]
    assert found == [
        f"{CACHE_PATHS_MODULE}.write_file",  # through the ``put`` alias
        f"{CACHE_PATHS_MODULE}.remove_file",
        f"{CACHE_PATHS_MODULE}.opened_dir",
    ]


def test_every_listed_module_still_needs_its_entry() -> None:
    for name in RAW_WRITERS:
        assert name in modules(), f"RAW_WRITERS names a missing module {name}"
        assert raw_writes(modules()[name]), f"{name} no longer writes raw files; remove it"
    for name in (*BOARD_OWNERS, *AWAITING_CONVERSION):
        assert name in modules(), f"missing module {name}"
        assert board_writer_calls(modules()[name]), f"{name} no longer writes a board; remove it"
        assert not modules()[name].inside_boundary
    for name, expected in RUNTIME_STORAGE_WRITERS.items():
        assert name in modules(), f"RUNTIME_STORAGE_WRITERS names a missing module {name}"
        actual = {hit.partition(": ")[2] for hit in board_writer_calls(modules()[name])}
        assert actual == expected, f"{name} runtime storage writer exception changed: {actual}"
    assert not set(BOARD_OWNERS) & set(AWAITING_CONVERSION)
    for name in CACHE_OWNERS:
        assert name in modules(), f"CACHE_OWNERS names a missing module {name}"
        assert cache_writer_calls(modules()[name]), f"{name} no longer writes the cache; remove it"


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


def test_os_open_accepts_known_read_only_flag_combinations() -> None:
    raw, _ = _scan(
        """
import os
from os import O_RDONLY, O_DIRECTORY, O_NOFOLLOW

def f(p, flags):
    os.open(p, os.O_RDONLY)
    os.open(p, O_RDONLY)
    os.open(p, flags=os.O_RDONLY)
    os.open(p, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK)
    os.open(p, O_RDONLY | O_DIRECTORY | O_NOFOLLOW)
    os.open(p, os.O_WRONLY | os.O_CREAT)
    os.open(p, os.O_RDONLY | os.O_CREAT)
    os.open(p, flags)
    os.open(p, os.O_RDWR)
    schedule(os.open)
"""
    )
    assert raw == ["os.open", "os.open", "os.open", "os.open", "os.open"]


def test_os_open_read_only_guard_rejects_shadowed_or_reassigned_flags() -> None:
    shadowed, _ = _scan(
        """
import os
from os import O_RDONLY

def f(path, O_RDONLY):
    os.open(path, O_RDONLY)
"""
    )
    reassigned, _ = _scan(
        """
import os

SAFE_FLAGS = os.O_RDONLY
SAFE_FLAGS = os.O_WRONLY

def f(path):
    os.open(path, SAFE_FLAGS)
"""
    )
    stable_constant, _ = _scan(
        """
import os

SAFE_FLAGS = os.O_RDONLY | os.O_DIRECTORY

def f(path):
    os.open(path, SAFE_FLAGS)
"""
    )
    assert shadowed == ["os.open"]
    assert reassigned == ["os.open"]
    assert stable_constant == []


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
    modules()[storage.name] = storage
    try:
        found = storage_writers()
        assert "lattice.storage.example.Saver" in found
        assert "lattice.storage.example.outer" in found
        assert "lattice.storage.example.reader" not in found
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
        used = [writer for _, writer in _writer_uses(caller, caller.tree, found)]
        assert sorted(set(used)) == [
            "lattice.storage.example.Saver",
            "lattice.storage.example.outer",
        ]
        assert len(used) == 4  # the alias line, the two Saver calls' line each, outer
    finally:
        del modules()[storage.name]
