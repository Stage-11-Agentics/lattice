"""One hosted project at runtime: its locks, lease, journal, and write path (SPEC §8.5-§8.7).

Each project has two locks. ``admission`` is an ``asyncio.Lock`` a request
awaits on the event loop (with a timeout) before it takes a worker thread;
``work`` is a ``threading.Lock`` held in that thread for everything that
reads or writes the board's durable files. Waiting requests therefore hold no
worker threads, and a stalled project cannot starve another (AC-15).

Everything that runs under ``work`` goes through :meth:`Project.locked`,
which also sets the owner flag (the board primitives refuse a server-owned
board to anyone else) and the project the unknown-event-type reporter logs.

:meth:`Project.run_write` is the single write path. H-22a wraps it in a
transaction (undo log, receipt, commit point); keep it one function.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
import socket
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from lattice.core.config import serialize_config
from lattice.core.errors import OpError
from lattice.core.ids import generate_op_id
from lattice.ops.base import Caller, OpResult, check_path_component, execute
from lattice.server import control
from lattice.server.floors import ShortIdFloors
from lattice.server.journal import (
    HOSTED_DIR,
    ROTATION,
    Journal,
    JournalError,
    finish_rotation,
    fingerprint,
    now_ms,
    rotate_epoch,
)
from lattice.server.log import ServerLog
from lattice.storage.fs import MutationKind, atomic_write, ensure_dir, recording, unlink_path
from lattice.storage.locks import LockTimeout
from lattice.storage.operations import AuthoritativeLogError, discover_task_authorities
from lattice.storage.ownership import (
    board_scope,
    owning_board,
    release_owner_flock,
    try_owner_flock,
)

T = TypeVar("T")

LOADED, LOADING, UNLOADED, UNAVAILABLE = "loaded", "loading", "unloaded", "unavailable"
STATES = (LOADED, LOADING, UNLOADED, UNAVAILABLE)

#: The only operations that may rewrite ``config.json`` over the op path
#: (SPEC §3.9). Every other configuration key is admin-only.
CONFIG_WRITING_OPS = frozenset(
    {"board.set_project_code", "board.set_subproject_code", "board.set_dashboard_config"}
)
CONFIG_WRITERS = CONFIG_WRITING_OPS | {"server.set_config"}

#: Files an admin may edit by hand while the server runs; detected by stat at
#: each admission and journaled as ``external`` (SPEC §8.7).
WATCHED_FILES = ("config.json", "context.md")

CURRENT_PROJECT: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "lattice_server_project", default=None
)


@dataclass
class WriteRequest:
    """One operation call, already authenticated and checked by the transport."""

    op: str
    params: Any  # the parsed Params instance
    caller: Caller  # origin carries op_id, reported, authenticated
    token_id: str | None
    fp: str
    #: Authorizes a ``--name`` session actor's permission identity
    #: (``agent:<base_name>``) before anything is written (SPEC §3.7 step 3).
    authorize_identity: Callable[[str], None] | None = None


@dataclass
class WriteOutcome:
    result: OpResult
    seq: int
    journal_line: dict = field(default_factory=dict)


def _stat_key(path: Path) -> tuple[int, int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


class MutationTracker:
    """The write recorder's callback for one server write.

    Keeps every mutation kind per path, in order, and refuses any mutation of
    ``config.json`` by an operation that may not change it (SPEC §3.9): the
    three board-config operations (whose own key checks apply) and the
    server's ``server.set_config``.
    """

    def __init__(self, board: Path, op: str) -> None:
        self.board = board.resolve()
        self.op = op
        self.config_path = self.board / "config.json"
        self.kinds: dict[Path, list[str]] = {}

    def __call__(self, path: Path, kind: MutationKind) -> None:
        if path == self.config_path and self.op not in CONFIG_WRITERS:
            raise OpError(
                "FORBIDDEN",
                f"operation {self.op} may not change config.json; board configuration "
                "changes only through 'lattice server project config' on the server host.",
                {"reason": "CONFIG_ADMIN_ONLY", "kind": kind},
            )
        self.kinds.setdefault(path, []).append(kind)

    def relative_paths(self) -> list[str]:
        return sorted(_relative(self.board, p) for p in self.kinds)

    def lengths(self) -> dict[str, int]:
        """Length after the write of every log this write only appended to (and that
        still exists): the journal's ``lengths`` (SPEC §8.6). A path with any
        other mutation (created whole, replaced, unlinked) is in ``paths`` only."""
        lengths: dict[str, int] = {}
        for path, kinds in self.kinds.items():
            if not all(k == "append" for k in kinds):
                continue
            try:
                lengths[_relative(self.board, path)] = path.stat().st_size
            except FileNotFoundError:
                continue
        return dict(sorted(lengths.items()))


class Project:
    """A project directory ``<server_root>/projects/<slug>/`` and its board."""

    def __init__(self, slug: str, directory: Path, log: ServerLog, server_id: str) -> None:
        self.slug = slug
        self.directory = directory
        self.board = directory / ".lattice"
        self.log = log
        self.server_id = server_id
        self.state = UNLOADED
        self.reason: str | None = None
        self.admission = asyncio.Lock()
        self.work = threading.Lock()
        self.journal: Journal | None = None
        self.floors = ShortIdFloors()
        self._lease_fd: int | None = None
        self._watched: dict[str, tuple[int, int, int] | None] = {}
        self._reported_types: set[str] = set()

    # -- locking -----------------------------------------------------------

    @contextmanager
    def locked(self, *, timeout: float | None = None) -> Iterator[None]:
        """Hold the work lock (call from a worker thread). The owner flag, which
        lets the board primitives write this server-owned board, is set only
        while this process holds the project's owner lease."""
        acquired = self.work.acquire(timeout=-1 if timeout is None else timeout)
        if not acquired:
            raise OpError("BOARD_BUSY", f"project {self.slug} is busy; retry shortly.")
        token = CURRENT_PROJECT.set(self.slug)
        try:
            with owning_board(self.board) if self._lease_fd is not None else nullcontext():
                yield
        finally:
            CURRENT_PROJECT.reset(token)
            self.work.release()

    def report_unknown_type(self, etype: str) -> None:
        if etype not in self._reported_types:
            self._reported_types.add(etype)
            self.log.warning("unknown_event_type", project=self.slug, type=etype)

    # -- load (SPEC §8.7; transactions and foreign appends are H-22's) ------

    def load(self) -> None:
        """Take the lease and bring the project to ``loaded`` or ``unavailable``.

        Call from a worker thread while holding admission, never on the loop.
        """
        self.state, self.reason = LOADING, None
        try:
            with self.locked():
                self._load()
        except BaseException as exc:
            self._mark_unavailable(f"load failed: {type(exc).__name__}: {exc}")
            if not isinstance(exc, Exception):
                raise

    def _load(self) -> None:
        board = self.board
        if not (board / "config.json").is_file():
            self._mark_unavailable("not a board: config.json is missing")
            return
        if not (board / HOSTED_DIR).is_dir():
            self._mark_unavailable("not a server project: hosted/ is missing")
            return
        fd = try_owner_flock(board)
        if fd is None:
            self._mark_unavailable("another process holds this project's owner lease")
            return
        self._lease_fd = fd
        with owning_board(board):
            self._load_owned(board)

    def _load_owned(self, board: Path) -> None:
        owner_path = board / HOSTED_DIR / "owner.json"
        if owner_path.exists():
            self.log.info("lease_takeover", project=self.slug, previous=_read_json(owner_path))
        atomic_write(
            owner_path,
            json.dumps(
                {
                    "server_id": self.server_id,
                    "host": socket.gethostname(),
                    "pid": os.getpid(),
                    "started_at": now_ms(),
                },
                sort_keys=True,
                indent=2,
            )
            + "\n",
        )
        if (board / HOSTED_DIR / ROTATION).exists():
            finish_rotation(board)
            self.log.info("epoch_rotation_completed", project=self.slug)
        try:
            journal = Journal.load(board)
        except JournalError as exc:
            journal = rotate_epoch(board, old_epoch=None)
            self.log.warning(
                "journal_missing", project=self.slug, reason=str(exc), epoch=journal.epoch
            )
        maintenance = board / HOSTED_DIR / "maintenance.json"
        if maintenance.exists():
            record = _read_json(maintenance)
            journal = journal.rotate()
            unlink_path(maintenance)
            self.log.info(
                "maintenance_rotation",
                project=self.slug,
                command=record.get("command"),
                epoch=journal.epoch,
            )
        self.journal = journal
        try:
            discover_task_authorities(board)
        except AuthoritativeLogError as exc:
            self._mark_unavailable(f"integrity check failed: {exc}")
            return
        self.floors = ShortIdFloors.from_board(board)
        ensure_dir(board / HOSTED_DIR / control.CONTROL_DIR)
        self._remember_watched()
        self.state = LOADED
        self.log.info(
            "project_load", project=self.slug, epoch=journal.epoch, head_seq=journal.head_seq
        )

    def _mark_unavailable(self, reason: str) -> None:
        self.state, self.reason = UNAVAILABLE, reason
        self.journal = None
        if self._lease_fd is not None:
            release_owner_flock(self._lease_fd)
            self._lease_fd = None
        self.log.warning("project_unavailable", project=self.slug, reason=reason)

    def release(self) -> None:
        """Release the owner lease (server shutdown)."""
        if self._lease_fd is not None:
            release_owner_flock(self._lease_fd)
            self._lease_fd = None
        self.state = UNLOADED

    def require_loaded(self) -> None:
        if self.state == UNAVAILABLE:
            raise OpError(
                "BOARD_UNAVAILABLE",
                f"project {self.slug} is unavailable: {self.reason}",
                {"reason": self.reason},
            )
        if self.state != LOADED:
            raise OpError("BOARD_UNAVAILABLE", f"project {self.slug} is not loaded")

    # -- admission-time checks (call under the work lock) -------------------

    def _remember_watched(self) -> None:
        self._watched = {name: _stat_key(self.board / name) for name in WATCHED_FILES}

    def check_external_changes(self) -> None:
        """Journal hand edits of ``config.json`` / ``context.md`` as ``external``."""
        changed = [
            name
            for name in WATCHED_FILES
            if _stat_key(self.board / name) != self._watched.get(name)
        ]
        if not changed:
            return
        if self.journal is None:
            raise OpError("BOARD_UNAVAILABLE", f"project {self.slug} is not loaded")
        try:
            seq, _ = self.journal.append(
                {
                    "op": "external",
                    "op_id": None,
                    "fp": None,
                    "token_id": None,
                    "task_id": None,
                    "event_ids": [],
                    "paths": changed,
                    "lengths": {},
                }
            )
        except BaseException as exc:
            self._mark_unavailable(f"journal append failed: {type(exc).__name__}: {exc}")
            raise OpError("BOARD_UNAVAILABLE", f"project {self.slug} is unavailable") from exc
        self._remember_watched()
        self.log.warning("external_change", project=self.slug, paths=changed, seq=seq)

    def run_control_requests(self) -> int:
        """Run every pending control request; returns how many ran."""
        ran = 0
        for path in control.pending_requests(self.board):
            if self.state != LOADED or self._lease_fd is None or self.journal is None:
                # Never act on a board this server does not hold: answer, write nothing.
                answer = {
                    "ok": False,
                    "error": {
                        "code": "BOARD_UNAVAILABLE",
                        "message": f"project {self.slug} is {self.state}: {self.reason}",
                    },
                }
                control.answer_unowned(path, answer)
            else:
                answer = control.run_request(self, path, self.log)
                atomic_write(path.with_suffix(".done"), json.dumps(answer, sort_keys=True) + "\n")
                unlink_path(path, missing_ok=True)
            self.log.info(
                "control_request",
                project=self.slug,
                request=path.stem,
                ok=answer.get("ok"),
                error_code=(answer.get("error") or {}).get("code"),
            )
            ran += 1
        return ran

    def admit(self) -> None:
        """The checks every admitted request runs first (under the work lock)."""
        self.require_loaded()
        self.run_control_requests()
        self.check_external_changes()

    # -- the write path ------------------------------------------------------

    def run_write(self, request: WriteRequest) -> WriteOutcome:
        """Execute one operation and journal it. Call under :meth:`locked`, after :meth:`admit`.

        This is the one function H-22a wraps in a transaction.
        """
        tracker = MutationTracker(self.board, request.op)
        caller = request.caller
        if caller.actor_name is not None and request.authorize_identity is not None:
            # SPEC §3.1, §3.7: validate the name, resolve, authorize; execute touches.
            check_path_component(caller.actor_name, "session name")
            request.authorize_identity(self.session_permission_identity(caller.actor_name))
        try:
            result = execute(
                self.board,
                request.op,
                request.params,
                caller,
                run_hooks=False,
                on_mutation=tracker,
            )
        except BaseException:
            self._log_uncommitted(tracker, request.op, caller.origin.get("op_id"))
            raise
        task_id = (result.task or {}).get("id")
        if task_id is None and result.events:
            task_id = result.events[0].get("task_id")
        seq, line = self._commit(
            op=request.op,
            op_id=caller.origin.get("op_id"),
            fp=request.fp,
            token_id=request.token_id,
            task_id=task_id,
            event_ids=[e.get("id") for e in result.events],
            paths=list(result.paths),
            tracker=tracker,
        )
        self.floors.observe_events(result.events)
        return WriteOutcome(result=result, seq=seq, journal_line=line)

    def _commit(
        self,
        *,
        op: str,
        op_id: str | None,
        fp: str | None,
        token_id: str | None,
        task_id: str | None,
        event_ids: list,
        paths: list[str],
        tracker: MutationTracker,
    ) -> tuple[int, dict]:
        """Append the journal line: the operation's commit point (SPEC §8.6 step 5)."""
        if self.journal is None:
            raise OpError("BOARD_UNAVAILABLE", f"project {self.slug} is not loaded")
        try:
            seq, line = self._append_journal(
                op, op_id, fp, token_id, task_id, event_ids, paths, tracker
            )
        except BaseException as exc:
            # The commit point failed: durability unknown, so quarantine (SPEC §8.6).
            self._log_uncommitted(tracker, op, op_id)
            self._mark_unavailable(f"journal append failed: {type(exc).__name__}: {exc}")
            raise OpError(
                "BOARD_UNAVAILABLE",
                f"project {self.slug} could not record operation {op_id}; it is "
                "unavailable until it is reloaded",
            ) from exc
        self._remember_watched()
        return seq, line

    def _append_journal(
        self,
        op: str,
        op_id: str | None,
        fp: str | None,
        token_id: str | None,
        task_id: str | None,
        event_ids: list,
        paths: list[str],
        tracker: MutationTracker,
    ) -> tuple[int, dict]:
        assert self.journal is not None
        return self.journal.append(
            {
                "op": op,
                "op_id": op_id,
                "fp": fp,
                "token_id": token_id,
                "task_id": task_id,
                "event_ids": event_ids,
                "paths": paths,
                "lengths": tracker.lengths(),
            }
        )

    def _log_uncommitted(self, tracker: MutationTracker, op: str, op_id: str | None) -> None:
        # H-22a replaces this with rollback from the undo log.
        if tracker.kinds:
            self.log.warning(
                "uncommitted_writes",
                project=self.slug,
                op=op,
                op_id=op_id,
                paths=tracker.relative_paths(),
            )

    def session_permission_identity(self, actor_name: str) -> str:
        """``agent:<base_name>`` of the session *actor_name* names (read only).

        The stand-in for H-5's step-3 hook: under the work lock nothing can
        change the session between this read and ``execute``'s own. The caller
        has already checked *actor_name* is one safe path component.
        """
        from lattice.core.actors import build_actor_dict
        from lattice.storage.sessions import resolve_session

        session = resolve_session(self.board, actor_name)
        if session is None:
            raise OpError(
                "SESSION_NOT_FOUND",
                f"No active session named '{actor_name}'. Start one with 'lattice session start'.",
            )
        return f"agent:{build_actor_dict(session)['base_name']}"

    # -- server-started transactions -----------------------------------------

    def set_config(self, changes: dict[str, Any]) -> dict:
        """Apply an allowlisted review-workflow change (SPEC §8.2) and journal it.

        The same owner, recorder, and journal seam as an operation, as
        ``server.set_config`` with a server-minted ``op_id`` and ``token_id: null``.
        """
        op = "server.set_config"
        op_id = generate_op_id()
        tracker = MutationTracker(self.board, op)
        config = json.loads((self.board / "config.json").read_text(encoding="utf-8"))
        config.update(changes)
        try:
            with board_scope(self.board), recording(tracker) as recorder:
                atomic_write(self.board / "config.json", serialize_config(config))
        except BaseException:
            self._log_uncommitted(tracker, op, op_id)
            raise
        paths = recorder.relative_paths(self.board)
        seq, _ = self._commit(
            op=op,
            op_id=op_id,
            fp=fingerprint(op, {"set": changes}, None, None, {}, None),
            token_id=None,
            task_id=None,
            event_ids=[],
            paths=paths,
            tracker=tracker,
        )
        return {"project": self.slug, "set": changes, "seq": seq, "paths": paths}

    # -- reads -------------------------------------------------------------

    def head(self) -> dict:
        journal = self.journal
        return {
            "epoch": journal.epoch if journal else None,
            "head_seq": journal.head_seq if journal else None,
        }


def _relative(board: Path, path: Path) -> str:
    try:
        return path.relative_to(board.resolve()).as_posix()
    except ValueError:
        return str(path)


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


@control.action("set-config")
def _set_config_action(project: Project, request: dict) -> dict:
    from lattice.server.admin import validate_config_changes

    changes = validate_config_changes(request.get("set"))
    return project.set_config(changes)


def lock_timeout_error(exc: LockTimeout) -> OpError:
    """A storage lock that timed out inside the server: ``BOARD_BUSY`` (SPEC §3.1)."""
    return OpError("BOARD_BUSY", str(exc))
