"""One hosted project at runtime: its locks, lease, journal, and write path (SPEC §8.5-§8.7).

Each project has two locks. ``admission`` is an ``asyncio.Lock`` a request
awaits on the event loop (with a timeout) before it takes a worker thread;
``work`` is a ``threading.Lock`` held in that thread for everything that
reads or writes the board's durable files. Waiting requests therefore hold no
worker threads, and a stalled project cannot starve another (AC-15).

Everything that runs under ``work`` goes through :meth:`Project.locked`,
which also sets the owner flag (the board primitives refuse a server-owned
board to anyone else) and the project the unknown-event-type reporter logs.

:meth:`Project.run_write` is the single write path: each call is one
transaction (undo log, receipt, journal commit point, recovery;
:mod:`lattice.server.transactions`), after the idempotency check.
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
from lattice.ops.base import Authorizer, Caller, OpResult, execute
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
from lattice.server.log import ServerLog, describe_error
from lattice.server.transactions import (
    IndexEntry,
    Quarantine,
    Transaction,
    read_receipt,
    result_json,
)
from lattice.storage.fs import (
    MutationKind,
    atomic_write,
    ensure_dir,
    recording,
    strict_durability,
    unlink_path,
)
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
    #: SPEC §3.7 step 3, run by ``execute`` after it validates the session
    #: name and resolves the actor, and before anything is written (the
    #: session touch included): authorizes the permission identity (a session
    #: actor's is ``agent:<base_name>``) against the token.
    authorize: Authorizer | None = None
    #: The server minted the ``op_id`` (the request sent none): never deduplicated.
    minted: bool = False


@dataclass
class WriteOutcome:
    #: The result as the response sends it (``OpResult`` JSON, ``paths`` dropped).
    result_data: dict
    seq: int
    #: The operation's result; ``None`` for a replay, which comes from its receipt.
    result: OpResult | None = None
    journal_line: dict = field(default_factory=dict)

    @property
    def replayed(self) -> bool:
        return bool(self.result_data.get("replayed"))


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

    def __init__(
        self,
        slug: str,
        directory: Path,
        log: ServerLog,
        server_id: str,
        on_state_change: Callable[[], None] | None = None,
    ) -> None:
        self.slug = slug
        self.directory = directory
        self.board = directory / ".lattice"
        self.log = log
        self.server_id = server_id
        self.state = UNLOADED
        self.reason: str | None = None
        self._on_state_change = on_state_change
        self.admission = asyncio.Lock()
        self.work = threading.Lock()
        self.journal: Journal | None = None
        self.floors = ShortIdFloors()
        self._lease_fd: int | None = None
        self._watched: dict[str, tuple[int, int, int] | None] = {}
        self._reported_types: set[str] = set()
        #: The idempotency index, ``(token_id, op_id) -> IndexEntry``, and the
        #: current epoch's op-status map, ``(token_id, op_id) -> seq`` (SPEC §8.6).
        #: Filled as operations commit; rebuilt from disk at load by H-22.
        self.index: dict[tuple[str | None, str], IndexEntry] = {}
        self.op_seqs: dict[tuple[str | None, str], int] = {}
        #: Called with each committed journal line, in ``seq`` order, under the locks
        #: (the stream broadcaster connects here, H-10a).
        self.publish: Callable[[dict], None] | None = None
        #: Called when publication failed for a committed operation: close the
        #: project's open streams so followers reconnect and replay (H-10a).
        self.close_streams: Callable[[], None] | None = None

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
        self._set_state(LOADING, None)
        try:
            with self.locked():
                self._load()
        except BaseException as exc:
            self._mark_unavailable(f"load failed: {describe_error(exc)}")
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
        if self.journal is None or self.journal.epoch != journal.epoch:
            self.op_seqs = {}  # the op-status map covers the current epoch only
        self.journal = journal
        # TODO(H-22): undo logs a crash or a quarantine left in hosted/undo/ are
        # recovered here, in SPEC §8.7's order (torn tails, the missing-journal
        # quarantine, then each transaction), with the idempotency index and the
        # op-status map rebuilt from disk. H-22a recovers only in process.
        try:
            discover_task_authorities(board)
        except AuthoritativeLogError as exc:
            self._mark_unavailable(f"integrity check failed: {exc}")
            return
        self.floors = ShortIdFloors.from_board(board)
        ensure_dir(board / HOSTED_DIR / control.CONTROL_DIR)
        self.remember_watched()
        self._set_state(LOADED, None)
        self.log.info(
            "project_load", project=self.slug, epoch=journal.epoch, head_seq=journal.head_seq
        )

    def _set_state(self, state: str, reason: str | None) -> None:
        """Change state and publish it (``server_status.json``, read by
        ``lattice server project list``)."""
        self.state, self.reason = state, reason
        if self._on_state_change is not None:
            try:
                self._on_state_change()
            except Exception as exc:  # noqa: BLE001 - publishing never breaks a request
                self.log.warning("status_publish_failed", error=describe_error(exc))

    def _mark_unavailable(self, reason: str) -> None:
        self._set_state(UNAVAILABLE, reason)
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
        self._set_state(UNLOADED, None)

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

    def remember_watched(self, names: list[str] | tuple[str, ...] = WATCHED_FILES) -> None:
        """Take a new baseline for *names*. Called only for files just journaled (or at
        load), so a hand edit made meanwhile to another watched file is still detected."""
        for name in names:
            if name in WATCHED_FILES:
                self._watched[name] = _stat_key(self.board / name)

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
            self._mark_unavailable(f"journal append failed: {describe_error(exc)}")
            raise OpError("BOARD_UNAVAILABLE", f"project {self.slug} is unavailable") from exc
        self.remember_watched(changed)
        self.log.warning("external_change", project=self.slug, paths=changed, seq=seq)

    def run_control_requests(self) -> int:
        """Run every pending control request; returns how many ran.

        Pending hand edits are journaled first, on every path that gets here
        (admission and the background poller alike).
        """
        ran = 0
        pending = control.pending_requests(self.board)
        if pending and self.state == LOADED and self.journal is not None:
            self.check_external_changes()
        for path in pending:
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
        # Hand edits are journaled before any control request can rewrite the same
        # file, so none is ever adopted silently (SPEC §8.7).
        self.check_external_changes()
        self.run_control_requests()

    # -- the write path ------------------------------------------------------

    def run_write(self, request: WriteRequest) -> WriteOutcome:
        """Execute one operation as a transaction. Call under :meth:`locked`, after
        :meth:`admit`, so the idempotency check sees every earlier attempt."""
        op_id = request.caller.origin.get("op_id")
        if not request.minted:
            known = self.index.get((request.token_id, op_id))
            if known is not None:
                return self._replay(known, request, op_id)
        tracker = MutationTracker(self.board, request.op)
        # One configuration governs the whole write: read once, under the lock.
        config = self.read_config()

        def work(txn: Transaction) -> OpResult:
            return execute(
                self.board,
                request.op,
                request.params,
                request.caller,
                run_hooks=False,
                config=config,
                on_mutation=txn.before_mutation,
                authorize=request.authorize,
                short_id_floor=self.floors.max_observed,
            )

        return self._transact(
            op=request.op,
            op_id=op_id,
            token_id=request.token_id,
            fp=request.fp,
            tracker=tracker,
            work=work,
        )

    def _replay(self, known: IndexEntry, request: WriteRequest, op_id: str) -> WriteOutcome:
        """A retried ``(token_id, op_id)``: the stored result, or ``OP_ID_REUSED``."""
        if known.fp != request.fp:
            raise OpError(
                "CONFLICT",
                f"operation id {op_id} was already used with different arguments",
                {"reason": "OP_ID_REUSED", "seq": known.seq},
            )
        receipt = read_receipt(self.board, known)
        data = {**receipt["result"], "replayed": True}
        return WriteOutcome(result_data=data, seq=known.seq)

    def _transact(
        self,
        *,
        op: str,
        op_id: str,
        token_id: str | None,
        fp: str | None,
        tracker: MutationTracker,
        work: Callable[[Transaction], OpResult],
    ) -> WriteOutcome:
        """Run *work* as one transaction (SPEC §8.6), recovering in process on failure."""
        if self.journal is None:
            raise OpError("BOARD_UNAVAILABLE", f"project {self.slug} is not loaded")
        txn = Transaction(self, op=op, op_id=op_id, token_id=token_id, fp=fp, tracker=tracker)
        quarantine: Quarantine | None = None
        with board_scope(self.board), strict_durability():
            try:
                txn.begin()
                result = work(txn)
                task_id = (result.task or {}).get("id")
                if task_id is None and result.events:
                    task_id = result.events[0].get("task_id")
                entry = {
                    "op": op,
                    "op_id": op_id,
                    "fp": fp,
                    "token_id": token_id,
                    "task_id": task_id,
                    "event_ids": [e.get("id") for e in result.events],
                    "paths": list(result.paths),
                    "lengths": tracker.lengths(),
                }
                result_data = result_json(result)
                txn.commit(entry, result_data, list(result.events))
                txn.finish()
            except BaseException as exc:
                failure = exc
                quarantine = self._recover(txn, exc)
                if quarantine is None:
                    raise
        if quarantine is not None:
            # Outside the board scope: quarantining publishes the server's status file.
            self._mark_unavailable(str(quarantine))
            raise OpError(
                "BOARD_UNAVAILABLE",
                f"project {self.slug} could not complete operation {op_id}; it is "
                "unavailable until it is reloaded",
            ) from failure
        return WriteOutcome(
            result_data=result_data, seq=txn.seq, result=result, journal_line=txn.line
        )

    def _recover(self, txn: Transaction, exc: BaseException) -> Quarantine | None:
        """Transaction recovery before the project admits another request (SPEC §8.6).
        Returns ``None`` when the caller should get *exc*, or the reason to quarantine."""
        try:
            txn.recover()
        except Quarantine as problem:
            self.log.error(
                "transaction_quarantine",
                project=self.slug,
                op=txn.op,
                op_id=txn.op_id,
                reason=str(problem),
                error=describe_error(exc),
            )
            return problem
        if txn.committed:
            self.log.error(
                "transaction_finish_failed",
                project=self.slug,
                op=txn.op,
                op_id=txn.op_id,
                seq=txn.seq,
                error=describe_error(exc),
            )
        else:
            self.log.info(
                "transaction_rollback",
                project=self.slug,
                op=txn.op,
                op_id=txn.op_id,
                paths=txn.tracker.relative_paths(),
                error=describe_error(exc),
            )
        return None

    def publication_failed(self, seq: int) -> None:
        """Publication failed for committed *seq*: streams reconnect and replay."""
        self.log.warning("publication_failed", project=self.slug, seq=seq)
        if self.close_streams is not None:
            self.close_streams()

    # -- op status (SPEC §8.6) ---------------------------------------------

    def op_status(self, token_id: str | None, op_id: str) -> dict:
        """The outcome of one of *token_id*'s operations. Reads only memory, the
        committed receipt line, and retained epoch journals (which never change),
        so it needs no lock."""
        key = (token_id, op_id)
        known = self.index.get(key)
        if known is not None:
            data: dict[str, Any] = {"state": "committed", "epoch": known.epoch, "seq": known.seq}
            try:
                data["result"] = read_receipt(self.board, known)["result"]
            except (OSError, ValueError, KeyError):
                pass  # the receipt is no longer retained
            return data
        journal = self.journal
        seq = self.op_seqs.get(key)
        if seq is not None and journal is not None:
            return {"state": "committed", "epoch": journal.epoch, "seq": seq}
        return _scan_retained_journals(self.board, token_id, op_id) or {"state": "not_found"}

    def read_config(self) -> dict:
        """The board's ``config.json`` as it is now (call under the work lock)."""
        try:
            config = json.loads((self.board / "config.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise OpError(
                "INTEGRITY_ERROR",
                f"project {self.slug}: config.json is unreadable ({describe_error(exc)})",
            ) from exc
        if not isinstance(config, dict):
            raise OpError("INTEGRITY_ERROR", f"project {self.slug}: config.json is not an object")
        return config

    # -- server-started transactions -----------------------------------------

    def set_config(self, changes: dict[str, Any]) -> dict:
        """Apply an allowlisted review-workflow change (SPEC §8.2) as a transaction.

        ``server.set_config`` with a server-minted ``op_id`` and ``token_id: null``;
        its undo log is ``hosted/undo/server--<op_id>.jsonl``.
        """
        op = "server.set_config"
        tracker = MutationTracker(self.board, op)
        config = self.read_config()
        config.update(changes)

        def work(txn: Transaction) -> OpResult:
            with recording(txn.before_mutation) as recorder:
                atomic_write(self.board / "config.json", serialize_config(config))
            paths = recorder.relative_paths(self.board)
            return OpResult(
                value={"project": self.slug, "set": changes, "paths": paths}, paths=tuple(paths)
            )

        outcome = self._transact(
            op=op,
            op_id=generate_op_id(),
            token_id=None,
            fp=fingerprint(op, {"set": changes}, None, None, {}, None),
            tracker=tracker,
            work=work,
        )
        return {**outcome.result_data["value"], "seq": outcome.seq}

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


def _scan_retained_journals(board: Path, token_id: str | None, op_id: str) -> dict | None:
    """Find *token_id*'s *op_id* in a retained ``hosted/journal.<epoch>.jsonl``."""
    hosted = board / HOSTED_DIR
    try:
        names = sorted(os.listdir(hosted))
    except OSError:
        return None
    needle = op_id.encode("ascii")
    for name in names:
        if not (name.startswith("journal.ep_") and name.endswith(".jsonl")):
            continue
        epoch = name[len("journal.") : -len(".jsonl")]
        try:
            data = (hosted / name).read_bytes()
        except OSError:
            continue
        for raw in data.splitlines():
            if needle not in raw:
                continue
            try:
                line = json.loads(raw)
            except ValueError:
                continue
            if line.get("op_id") == op_id and line.get("token_id") == token_id:
                return {"state": "committed", "epoch": epoch, "seq": line.get("seq")}
    return None


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
