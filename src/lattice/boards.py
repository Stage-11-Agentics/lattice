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
from collections.abc import Callable
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


def browser_reported_origin() -> dict[str, str]:
    """What a dashboard reports for a write made from a browser: this process's
    ``host``, ``os_user``, and ``client_version``, with ``source: "browser"``
    and no worktree or branch (SPEC §4)."""
    return {**_process_origin(), "source": "browser"}


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

    def refresh(self) -> None:
        """Bring this client's view of the board up to date before a retry.

        A local board is its own source of truth, so there is nothing to do;
        a hosted board catches its cache up with the server here (H-11).
        """

    def execute(
        self, op_name: str, params: Any, caller: Any = None, *, config: dict | None = None
    ) -> Any:
        """Run *op_name* here with hooks in-process, stamping this client's origin.

        Each call is one operation with a fresh ``op_id`` unless the caller
        supplied one. *config*: see ``lattice.ops.execute``.
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
            config=config,
        )


class HostedBoard:
    """A board on a Lattice server, reached through this checkout's binding.

    Writes go to the server as operations (SPEC §9.5); reads use the checkout's
    cache, ``lattice_dir``. ``remote`` is resolved when the board is.
    """

    def __init__(self, hosted: Any, start: Path, remote: Any) -> None:
        self.hosted = hosted
        self.root: Path = hosted.root
        self.start = start
        self.remote = remote

    @property
    def cache_dir(self) -> Path:
        """The cache's ``.lattice/`` as a path, without reading it."""
        return self.root / LATTICE_DIR

    @property
    def lattice_dir(self) -> Path:
        """The cache's ``.lattice/``, ready to read: caught up (once per command)
        and under the cache's shared read lock, which stays held until
        :meth:`execute` sends its request or the command ends (SPEC §9.4, §9.5).
        Every read a write command makes goes through here."""
        from lattice.core.errors import HostedReadError
        from lattice.remote import session

        try:
            session.ensure_fresh(self.hosted)
            session.hold_read_lock(self.hosted)
        except OpError as exc:
            raise HostedReadError(exc.code, exc.message, exc.details) from exc
        return self.cache_dir

    @property
    def label(self) -> str:
        return self.hosted.label

    def load_config(self) -> dict:
        """The project's ``config.json`` from the cache (the read phase of a write
        command, SPEC §9.5)."""
        import json

        return json.loads((self.lattice_dir / "config.json").read_text())

    def refresh(self) -> None:
        """Catch the cache up before a retry (a stale attestation, SPEC §3.4); the
        next read takes the read lock again."""
        from lattice.remote import session

        session.catch_up_and_report(self.hosted, after_write=True)
        session.mark_fresh(self.hosted)

    def execute(
        self, op_name: str, params: Any, caller: Any = None, *, config: dict | None = None
    ) -> Any:
        """Run *op_name* on the server and bring the cache up to it.

        One operation call with one ``op_id`` (the caller's, when given), retried
        per SPEC §8.6. A server rejection is the same ``OpError`` the command
        prints locally. After success the cache catches up (a failure there is
        only a notice) and the board's hooks run here when the remote sets
        ``run_board_hooks``, from *config* (default: the synced ``config.json``).
        """
        from lattice.ops import Caller
        from lattice.remote import session
        from lattice.remote.client import post_operation, result_from_json, wire_params

        caller = caller if caller is not None else Caller()
        body: dict[str, Any] = {
            "op_id": caller.origin.get("op_id") or generate_op_id(),
            "params": wire_params(op_name, params),
            "origin": {"reported": caller.origin.get("reported") or reported_origin(self.start)},
        }
        if caller.actor is not None:
            body["actor"] = caller.actor
        if caller.actor_name is not None:
            body["actor_name"] = caller.actor_name
        if caller.attestations:
            body["attestations"] = caller.attestations
        if caller.expect_last_event_id is not None:
            body["expect"] = {"last_event_id": caller.expect_last_event_id}
        # The read phase ends here: never hold the read lock across the network
        # call or the post-write sync (which takes it exclusively).
        offline = session.window_open_at_start(self.hosted)
        session.release_read_lock(self.root)
        session.check_protocol(self.hosted)
        since = session.sync_ticket(self.hosted)
        try:
            data = post_operation(self.remote, self.hosted.project, op_name, body, offline=offline)
        except OpError as exc:
            if exc.code == "SERVER_UNREACHABLE":
                # Nothing was sent; the next write should not wait again (SPEC §8.6),
                # unless a sync that began after this write has succeeded since.
                session.open_unreachable_window_after(self.hosted, since)
            raise
        session.close_unreachable_window(self.hosted)
        # Acknowledged: into the ledger before anything else can fail or die
        # (the post-write sync included), so verify always knows of it (SPEC §9.5).
        self._record_ack(data.get("op_id") or body["op_id"], data.get("seq"))
        result = result_from_json(data.get("result") or {})
        session.catch_up_and_report(self.hosted, after_write=True)
        session.mark_fresh(self.hosted)
        if self.remote.run_board_hooks:
            self._run_hooks(result, config)
        return result

    def _record_ack(self, op_id: str, seq: Any) -> None:
        """Append the acknowledged write to ``cache/acked.jsonl`` for ``lattice remote
        verify`` (SPEC §9.5), before the post-write sync. The write already
        succeeded: any failure to record it is one line on stderr, never an error.

        ``epoch`` is the epoch the cache knew when the server acknowledged the
        write (the op response carries none); verify asks by ``op_id``, so it is
        informational."""
        import json
        import sys

        from lattice.remote import acked

        cache = self.cache_dir / "cache"
        try:
            try:
                state = json.loads((cache / "state.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                state = None
            epoch = state.get("epoch") if isinstance(state, dict) else None
            if not isinstance(epoch, str):
                epoch = None
            acked.record(cache, op_id=op_id, project=self.hosted.project, epoch=epoch, seq=seq)
        except Exception as exc:  # noqa: BLE001 - never fail a write the server applied
            print(
                f"lattice: could not record operation {op_id} in cache/acked.jsonl ({exc!r}); "
                "lattice remote verify will not check it",
                file=sys.stderr,
            )

    def _run_hooks(self, result: Any, config: dict | None) -> None:
        import json

        from lattice.storage.hooks import execute_hooks, execute_resource_hooks

        if config is None:
            try:
                config = json.loads((self.cache_dir / "config.json").read_text())
            except (OSError, ValueError):
                return
        if not config.get("hooks"):
            return
        # A hook may run lattice itself; its post-write sync needs the lock free.
        from lattice.remote import session

        session.release_read_lock(self.root)
        for event in result.events:
            if result.resource_id and result.resource_name:
                execute_resource_hooks(
                    config, self.cache_dir, result.resource_id, result.resource_name, event
                )
            elif event.get("task_id"):
                execute_hooks(config, self.cache_dir, event["task_id"], event)


def resolve_board(
    start: Path | None = None, *, honor_env: bool = True
) -> LocalBoard | HostedBoard:
    """The board a write started in *start* (default: the cwd) belongs to.

    ``honor_env=False`` ignores ``LATTICE_ROOT``: an MCP tool call that names its
    ``lattice_root`` starts there, whatever the server process's environment.

    A hosted checkout (SPEC §9.3) resolves to a :class:`HostedBoard`. Raises
    ``OpError("NOT_INITIALIZED")`` when there is no board, and the routing and
    first-contact errors (``BINDING_CONFLICT``, ``REMOTE_NOT_CONFIGURED``,
    ``TOKEN_ENV_UNSET``, ``INSECURE_URL``, ``HOSTED_UNSUPPORTED_PLATFORM``).
    """
    start_dir = Path.cwd() if start is None else Path(start)
    try:
        root = find_root(start_dir, honor_env=honor_env)
    except LatticeRootError as exc:
        raise OpError("NOT_INITIALIZED", str(exc)) from exc
    if root is None:
        raise OpError(
            "NOT_INITIALIZED",
            "Not a Lattice project (no .lattice/ found). Run 'lattice init' first.",
        )
    from lattice.remote.binding import classify, require_supported

    hosted = classify(root)
    if hosted is None:
        return LocalBoard(root=root, start=start_dir)
    require_supported()
    from lattice.remote.config import resolve_remote

    return HostedBoard(hosted, start_dir, resolve_remote(hosted.remote))


# ---------------------------------------------------------------------------
# Local-only maintenance commands (SPEC §3.5)
# ---------------------------------------------------------------------------

LOCAL_ONLY_COMMANDS: tuple[str, ...] = (
    "init",
    "demo init",
    "rebuild",
    "doctor --fix",
    "backfill-ids",
    "migrate needs-human",
)
"""Commands that operate directly on a data directory, refused on a hosted checkout."""


def hosted_binding(start: Path | None) -> str | None:
    """The ``<alias>/<project>`` a checkout is bound to, or ``None`` for a local
    board or no board.

    ``start=None`` is the command's own board: the cwd, honoring LATTICE_ROOT
    (an invalid LATTICE_ROOT is ``NOT_INITIALIZED``, never "not hosted"). A path
    is an explicit target (``init --path``), resolved without LATTICE_ROOT.
    Classification needs no network and no ``fcntl``, so a bound checkout is
    recognized on every platform. ``BINDING_CONFLICT`` propagates: a binding
    beside a local board is refused, never treated as local.
    """
    from lattice.remote.binding import classify

    try:
        if start is None:
            root = find_root(Path.cwd())
        else:
            root = find_root(Path(start), honor_env=False)
    except LatticeRootError as exc:
        raise OpError("NOT_INITIALIZED", str(exc)) from exc
    if root is None:
        return None
    hosted = classify(root)
    return hosted.label if hosted is not None else None


def local_only_error(command: str, binding: str) -> OpError:
    """The ``LOCAL_ONLY`` refusal of *command* on a checkout bound to *binding*."""
    if command == "init":
        return OpError(
            "LOCAL_ONLY",
            f"This checkout is bound to '{binding}'; its board lives on the server. "
            "For a separate local board, work in a checkout without .lattice-remote.json.",
            {"command": command},
        )
    return OpError(
        "LOCAL_ONLY",
        f"'lattice {command}' is a local-only maintenance command, and this checkout's "
        f"board lives on the server ('{binding}'). Unload the project "
        "('lattice server project unload <slug>') or stop the server, then run it on the "
        "server host against <server_root>/projects/<slug> with --offline-maintenance.",
        {"command": command},
    )


def check_local_only(
    command: str,
    start: Path | None = None,
    *,
    binding_of: Callable[[Path | None], str | None] = hosted_binding,
) -> None:
    """Refuse a ``LOCAL_ONLY_COMMANDS`` entry on a hosted checkout (``LOCAL_ONLY``).

    *start* is an explicit target path (``init``, ``demo init``); ``None`` means
    the command's own board (the cwd, honoring LATTICE_ROOT). *binding_of* is
    the hosted-checkout predicate (default :func:`hosted_binding`).
    """
    if command not in LOCAL_ONLY_COMMANDS:
        raise ValueError(f"{command!r} is not a local-only command")
    binding = binding_of(None if start is None else Path(start))
    if binding is not None:
        raise local_only_error(command, binding)
