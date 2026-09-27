"""Board resolution: the single entry every CLI, MCP, and dashboard write uses.

``resolve_board(start)`` finds the board a write from *start* belongs to and
returns an object whose ``execute`` runs a named operation on it. Today every
board is a ``LocalBoard``; a hosted checkout will resolve to a ``HostedBoard``
with the same ``execute`` signature.
"""

from __future__ import annotations

import functools
import getpass
import re
import socket
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from lattice.core.errors import OpError
from lattice.core.ids import generate_op_id
from lattice.storage.fs import LATTICE_DIR, LatticeRootError, find_root

_DETACHED_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_HEADS_PREFIX = "ref: refs/heads/"
# git's reftable backend keeps HEAD pointing at this placeholder; the real
# branch lives in the reftable, which only git itself can read.
_REFTABLE_PLACEHOLDER = ".invalid"


# ---------------------------------------------------------------------------
# Reported origin (SPEC §4)
# ---------------------------------------------------------------------------


@functools.cache
def _process_origin() -> dict[str, str]:
    """``host``, ``os_user``, ``client_version``: fixed for the process."""
    fields: dict[str, str] = {}
    try:
        fields["host"] = socket.gethostname()
    except OSError:
        pass
    try:
        fields["os_user"] = getpass.getuser()
    except (OSError, KeyError, ImportError):
        pass
    try:
        from lattice import __version__

        fields["client_version"] = __version__
    except Exception:  # noqa: BLE001 - an uninstalled tree has no version; omit it
        pass
    return fields


def git_worktree(start: Path) -> Path | None:
    """The nearest ancestor of *start* (inclusive) holding ``.git``."""
    try:
        current = start.resolve()
    except OSError:
        return None
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _head_path(worktree: Path) -> Path | None:
    git = worktree / ".git"
    if git.is_dir():
        return git / "HEAD"
    try:
        content = git.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not content.startswith("gitdir:"):
        return None
    gitdir = Path(content[len("gitdir:") :].strip())
    if not gitdir.is_absolute():
        gitdir = worktree / gitdir
    return gitdir / "HEAD"


def _branch_from_git(worktree: Path) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    name = proc.stdout.strip()
    if proc.returncode != 0 or not name or name == "HEAD":
        return None
    return name


def git_branch(worktree: Path) -> str | None:
    """The branch checked out in *worktree*, read from its ``HEAD`` file.

    A detached ``HEAD`` has no branch. Only a ``HEAD`` this cannot read
    (missing, unexpected content, git's reftable placeholder) falls back to
    asking git.
    """
    head = _head_path(worktree)
    content = None
    if head is not None:
        try:
            content = head.read_text(encoding="utf-8").strip()
        except OSError:
            content = None
    if content is not None:
        if _DETACHED_RE.match(content):
            return None
        if content.startswith(_HEADS_PREFIX):
            name = content[len(_HEADS_PREFIX) :]
            if name and name != _REFTABLE_PLACEHOLDER:
                return name
    return _branch_from_git(worktree)


def reported_origin(start: Path) -> dict[str, str]:
    """What this client reports about an operation started in *start*.

    ``host``, ``os_user``, and ``client_version`` are cached per process;
    ``worktree`` and ``branch`` are derived for each operation, because one
    process can serve several checkouts and outlive a branch switch. A field
    whose lookup fails is omitted.
    """
    fields = dict(_process_origin())
    worktree = git_worktree(start)
    if worktree is not None:
        fields["worktree"] = str(worktree)
        branch = git_branch(worktree)
        if branch is not None:
            fields["branch"] = branch
    return fields


# ---------------------------------------------------------------------------
# Boards
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LocalBoard:
    """A board on this machine's filesystem, written in-process."""

    root: Path
    start: Path

    @property
    def lattice_dir(self) -> Path:
        return self.root / LATTICE_DIR

    def load_config(self) -> dict:
        import json

        return json.loads((self.lattice_dir / "config.json").read_text())

    def execute(self, op_name: str, params: Any, caller: Any = None) -> Any:
        """Run *op_name* here with hooks in-process, stamping this client's origin.

        Each call is one operation with a fresh ``op_id`` unless the caller
        supplied one.
        """
        from lattice.ops import Caller, execute

        caller = caller if caller is not None else Caller()
        origin = dict(caller.origin)
        origin.setdefault("op_id", generate_op_id())
        origin.setdefault("reported", reported_origin(self.start))
        return execute(
            self.lattice_dir,
            op_name,
            params,
            replace(caller, origin=origin),
            run_hooks=True,
        )


def resolve_board(start: Path | None = None) -> LocalBoard:
    """The board a write started in *start* (default: the cwd) belongs to.

    Raises ``OpError("NOT_INITIALIZED")`` when there is none.
    """
    start_dir = Path.cwd() if start is None else Path(start)
    try:
        root = find_root(start_dir)
    except LatticeRootError as exc:
        raise OpError("NOT_INITIALIZED", str(exc)) from exc
    if root is None:
        raise OpError(
            "NOT_INITIALIZED",
            "Not a Lattice project (no .lattice/ found). Run 'lattice init' first.",
        )
    return LocalBoard(root=root, start=start_dir)
