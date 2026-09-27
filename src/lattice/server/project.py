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
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from types import MappingProxyType
from typing import Any, TypeVar

from lattice.core.config import serialize_config
from lattice.core.errors import OpError
from lattice.core.ids import generate_op_id
from lattice.ops.base import Authorizer, Caller, OpResult, execute
from lattice.server import control, recovery, transactions
from lattice.server.floors import ShortIdFloors
from lattice.server.journal import (
    HOSTED_DIR,
    ROTATION,
    Journal,
    JournalError,
    JournalIndex,
    finish_rotation,
    fingerprint,
    now_ms,
    rotate_epoch,
)
from lattice.server.log import ServerLog, describe_error
from lattice.server.stream import Broadcaster, journal_frame
from lattice.server.syncstate import Manifest, entry_events
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
UNLOADED_REASON = "unloaded by an admin"
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


@dataclass(frozen=True)
class FinalizedState:
    """Everything the committed-line finalizer maintains, as one immutable value
    (SPEC §8.6 step 6): the journal index (line hashes, offsets, length
    history, head), the manifest, the short-ID floors, and the watched-file
    baselines. The project publishes a new value with one assignment."""

    journal: JournalIndex
    manifest: Manifest
    floors: ShortIdFloors
    watched: Mapping[str, tuple[int, int, int] | None]


def _watched_after(
    board: Path, watched: Mapping[str, tuple[int, int, int] | None], names: Iterable[str]
) -> Mapping[str, tuple[int, int, int] | None]:
    """New baselines for the watched files among *names*; *watched* is unchanged."""
    updated = dict(watched)
    for name in names:
        if name in WATCHED_FILES:
            updated[name] = _stat_key(board / name)
    return MappingProxyType(updated)


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
        self._lease_fd: int | None = None
        self._reported_types: set[str] = set()
        #: Set by ``project unload``: the project stays unloaded (503) until
        #: ``project load``, and neither a request nor the prewarm loads it.
        self.held_unloaded = False
        #: The UTC day receipt files and index entries were last checked for
        #: retention (SPEC §8.6); the index check runs before every replay.
        self._receipt_day: date | None = None
        self._index_day: date | None = None
        #: The idempotency index, ``(token_id, op_id) -> IndexEntry``, and the
        #: current epoch's op-status map, ``(token_id, op_id) -> seq`` (SPEC §8.6).
        #: Filled as operations commit, and rebuilt from disk at every load.
        self.index: dict[tuple[str | None, str], IndexEntry] = {}
        #: Guards every change to ``index`` (a commit's insert, expiry, the load's
        #: rebuild). Never the work lock: op status reads without admission.
        #: The index is only ever changed in place under it, never replaced from
        #: a lock-free path, so no committed entry can be lost (AC-46).
        self.index_lock = threading.Lock()
        self.op_seqs: dict[tuple[str | None, str], int] = {}
        #: The finalized memory (journal index, manifest, floors, watched baselines),
        #: published by one assignment per committed line; ``None`` until loaded.
        self._state: FinalizedState | None = None
        #: The project's open streams (SPEC §8.9).
        self.broadcaster = Broadcaster()
        #: One reset assembly at a time: held before admission (SPEC §8.8).
        self.reset_gate = asyncio.Lock()
        #: Called with each committed journal line, in ``seq`` order, under the locks.
        self.publish: Callable[[dict], None] | None = self._publish
        #: Called when publication failed for a committed operation: close the
        #: project's open streams so followers reconnect and replay.
        self.close_streams: Callable[[], None] | None = self.broadcaster.close_all

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

    # -- load (SPEC §8.7; the recovery steps are in lattice.server.recovery) --

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
        # SPEC §8.7, in order (see lattice.server.recovery).
        if (board / HOSTED_DIR / ROTATION).exists():  # 1. an interrupted rotation
            finish_rotation(board)
            self.log.info("epoch_rotation_completed", project=self.slug)
        with strict_durability():
            journal = self._recover_on_disk(board)
        if journal is None:
            return
        self.journal = journal
        try:
            discover_task_authorities(board)
        except AuthoritativeLogError as exc:
            self._mark_unavailable(f"integrity check failed: {exc}")
            return
        # SPEC §8.7 step 8: the sync path's state, the floors, and the watched
        # baselines, built once and published together.
        self._adopt(
            journal,
            FinalizedState(
                journal=journal.index,
                manifest=Manifest.build(board, journal.head_seq),
                floors=ShortIdFloors.from_board(board),
                watched=_watched_after(board, {}, WATCHED_FILES),
            ),
        )
        self.broadcaster.announce(journal.epoch, journal.head_seq)
        ensure_dir(board / HOSTED_DIR / control.CONTROL_DIR)
        self._set_state(LOADED, None)
        self.log.info(
            "project_load", project=self.slug, epoch=journal.epoch, head_seq=journal.head_seq
        )

    def _recover_on_disk(self, board: Path) -> Journal | None:
        """SPEC §8.7 steps 2 to 6. Returns the journal to serve, or ``None`` after
        marking the project unavailable."""
        recovery.drop_torn_tails(board)  # 2 (Journal.load drops the journal's own)
        rotated = False
        try:
            journal: Journal | None = Journal.load(board)
        except JournalError as exc:  # 3. a missing journal
            journal = None
            if recovery.undo_log_paths(board):
                self._mark_unavailable(
                    f"the journal is missing or unreadable ({exc}) and undo logs remain; "
                    + recovery.recover_hint(self.slug)
                )
                return None
            journal = rotate_epoch(board, old_epoch=None)
            rotated = True
            self.log.warning(
                "journal_missing", project=self.slug, reason=str(exc), epoch=journal.epoch
            )
        try:  # 4. transactions
            settled = recovery.settle_undo_logs(board, journal, log=self.log, slug=self.slug)
        except recovery.NeedsRecover as exc:
            self._mark_unavailable(f"{exc}; " + recovery.recover_hint(self.slug))
            return None
        index, op_seqs, orphans = recovery.rebuild_index(board, journal)
        with self.index_lock:
            self.index.clear()
            self.index.update(index)
        self._receipt_day = self._index_day = recovery.utc_today()
        if settled.committed or settled.rolled_back or orphans:
            self.log.info(
                "recovery",
                project=self.slug,
                committed=len(settled.committed),
                rolled_back=len(settled.rolled_back),
                orphan_receipts=orphans,
            )
        maintenance = board / HOSTED_DIR / "maintenance.json"  # 5.
        if maintenance.exists():
            record = _read_json(maintenance)
            journal = journal.rotate()
            rotated = True
            unlink_path(maintenance)
            self.log.info(
                "maintenance_rotation",
                project=self.slug,
                command=record.get("command"),
                epoch=journal.epoch,
            )
        elif isinstance(journal.clean_shutdown, dict):
            if journal.clean_shutdown.get("tree_fingerprint") != recovery.tree_fingerprint(board):
                journal = journal.rotate()
                rotated = True
                self.log.info("restore_rotation", project=self.slug, epoch=journal.epoch)
        if journal.clean_shutdown is not None:
            recovery.write_meta(journal, None)
        self.op_seqs = {} if rotated else op_seqs
        if not rotated:  # 6. foreign appends
            foreign = recovery.foreign_appends(board, journal)
            if foreign:
                seq, _ = journal.append(
                    {
                        "op": "external",
                        "op_id": None,
                        "fp": None,
                        "token_id": None,
                        "task_id": None,
                        "event_ids": [],
                        "paths": sorted(foreign),
                        "lengths": foreign,
                    }
                )
                self.log.warning(
                    "external_change", project=self.slug, paths=sorted(foreign), seq=seq
                )
        return journal

    def _set_state(self, state: str, reason: str | None) -> None:
        """Change state and publish it (``server_status.json``, read by
        ``lattice server project list``)."""
        self.state, self.reason = state, reason
        if self._on_state_change is not None:
            try:
                self._on_state_change()
            except Exception as exc:  # noqa: BLE001 - publishing never breaks a request
                self.log.warning("status_publish_failed", error=describe_error(exc))

    # -- the finalized memory ------------------------------------------------

    @property
    def manifest(self) -> Manifest | None:
        state = self._state
        return state.manifest if state is not None else None

    @property
    def floors(self) -> ShortIdFloors:
        state = self._state
        return state.floors if state is not None else ShortIdFloors()

    def _published_index(self) -> JournalIndex:
        state = self._state
        if state is None:
            raise OpError("BOARD_UNAVAILABLE", f"project {self.slug} is not loaded")
        return state.journal

    def _adopt(self, journal: Journal, state: FinalizedState) -> None:
        """Publish *state* for *journal*, which reads its index from it from now on. A
        journal this replaces keeps the last index it had."""
        previous = self.journal
        if previous is not None and previous is not journal:
            frozen = previous.index
            previous.bind(lambda: frozen)
        self._state = state
        journal.bind(self._published_index)
        self.journal = journal

    def remember_watched(self, names: Iterable[str]) -> None:
        """Re-baseline the watched files among *names* (a rollback restored them: not
        a hand edit). One assignment, like every change to the finalized state."""
        state = self._state
        if state is not None:
            self._state = replace(state, watched=_watched_after(self.board, state.watched, names))

    def _mark_unavailable(self, reason: str) -> None:
        self._set_state(UNAVAILABLE, reason)
        self.journal = None
        self._state = None
        self.broadcaster.close_all()
        if self._lease_fd is not None:
            release_owner_flock(self._lease_fd)
            self._lease_fd = None
        self.log.warning("project_unavailable", project=self.slug, reason=reason)

    def release(self) -> None:
        """Release the owner lease (server shutdown)."""
        self.broadcaster.close_all()
        if self._lease_fd is not None:
            release_owner_flock(self._lease_fd)
            self._lease_fd = None
        self._set_state(UNLOADED, None)

    @property
    def holds_lease(self) -> bool:
        return self._lease_fd is not None

    def unload(self) -> None:
        """``project unload`` (SPEC §8.2): close the streams, release the lease, and
        stay unloaded until ``project load``. Call under the work lock, after
        admission, so no operation is in flight."""
        if self.close_streams is not None:
            try:
                self.close_streams()
            except Exception as exc:  # noqa: BLE001 - the lease is released regardless
                self.log.warning("close_streams_failed", project=self.slug, error=repr(exc))
        self.journal = None
        self._state = None
        self.held_unloaded = True
        self.release()
        self._set_state(UNLOADED, UNLOADED_REASON)
        self.log.info("project_unload", project=self.slug)

    def write_clean_shutdown(self) -> None:
        """Record ``clean_shutdown`` (SPEC §8.7) for a project this server holds.
        Call under the work lock with no operation in flight."""
        journal = self.journal
        if journal is None or self.state != LOADED or self._lease_fd is None:
            return
        with owning_board(self.board), strict_durability():
            recovery.write_meta(
                journal,
                {
                    "head_seq": journal.head_seq,
                    "tree_fingerprint": recovery.tree_fingerprint(self.board),
                },
            )

    def require_loaded(self) -> None:
        if self.state == UNAVAILABLE:
            raise OpError(
                "BOARD_UNAVAILABLE",
                f"project {self.slug} is unavailable: {self.reason}",
                {"reason": self.reason},
            )
        if self.held_unloaded:
            raise OpError(
                "BOARD_UNAVAILABLE",
                f"project {self.slug} is unloaded; an admin runs "
                f"'lattice server project load {self.slug}' to serve it again",
                {"reason": "unloaded"},
            )
        if self.state != LOADED:
            raise OpError("BOARD_UNAVAILABLE", f"project {self.slug} is not loaded")

    # -- admission-time checks (call under the work lock) -------------------

    def check_external_changes(self) -> None:
        """Journal hand edits of ``config.json`` / ``context.md`` as ``external``."""
        changed = [
            name
            for name in WATCHED_FILES
            if self._state is None or _stat_key(self.board / name) != self._state.watched.get(name)
        ]
        if not changed:
            return
        if self.journal is None:
            raise OpError("BOARD_UNAVAILABLE", f"project {self.slug} is not loaded")
        journal = self.journal
        try:
            seq, line, raw = journal.write(
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
        try:
            self.finalize_committed(line, raw)
        except BaseException as exc:
            self._mark_unavailable(
                f"finalizing external seq {seq} in memory failed: {describe_error(exc)}"
            )
            raise OpError("BOARD_UNAVAILABLE", f"project {self.slug} is unavailable") from exc
        self.log.warning("external_change", project=self.slug, paths=changed, seq=seq)
        try:
            self._publish(line)
        except Exception:  # noqa: BLE001 - committed; followers reconnect and replay
            self.publication_failed(seq)

    def run_control_requests(self) -> int:
        """Run pending control requests, oldest first, up to the first lifecycle
        request (``unload``, ``load``, ``reload``, ``doctor``), which only the
        registry runs, outside the work lock; returns how many ran.

        Pending hand edits are journaled first, on every path that gets here
        (admission and the background poller alike).
        """
        ran = 0
        pending = control.pending_requests(self.board)
        if pending and self.state == LOADED and self.journal is not None:
            self.check_external_changes()
        for path in pending:
            if control.request_action(path) in control.LIFECYCLE_ACTIONS:
                break
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
            self.expire_index()  # past retention, a retry runs again (SPEC §8.6)
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
            self._retain_receipts()
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

    def expire_index(self) -> None:
        """Forget index entries whose receipt is past retention (SPEC §8.6), decided
        by date alone, so a receipt file that could not be deleted never lets an
        expired operation replay. Runs before every replay lookup (under the work
        lock); deletes the expired keys in place under :attr:`index_lock`, so an
        entry a commit inserts meanwhile is never lost."""
        today = recovery.utc_today()
        if today == self._index_day:
            return
        with self.index_lock:
            expired = [
                key
                for key, entry in self.index.items()
                if recovery.receipt_expired(entry.receipt, today)
            ]
            for key in expired:
                del self.index[key]
            self._index_day = today
        if expired:
            self.log.info("receipts_expired_from_index", project=self.slug, entries=len(expired))

    def _retain_receipts(self) -> None:
        """Receipt retention (SPEC §8.6): on the first write of each UTC day, delete
        receipt files past the window. A failure is logged and retried on the
        next write; it never fails this one, and the index has already
        forgotten those receipts (:meth:`expire_index`)."""
        self.expire_index()
        today = recovery.utc_today()
        if today == self._receipt_day:
            return
        try:
            expired = recovery.expired_receipt_files(self.board, today)
            for path in expired:
                unlink_path(path)
            if expired:
                self.log.info(
                    "receipts_expired", project=self.slug, files=sorted(p.name for p in expired)
                )
            self._receipt_day = today
        except Exception as exc:  # noqa: BLE001 - retention never blocks a write
            self.log.warning("receipt_retention_failed", project=self.slug, error=repr(exc))

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

    # -- the sync path's memory and the stream (SPEC §8.6 step 6, §8.9) -----

    def finalize_committed(self, line: dict, raw: bytes, events: list | tuple = ()) -> None:
        """Bring memory up to date with one committed journal line (SPEC §8.6 step 6):
        the journal index, the manifest entries of its ``paths``, the short-ID
        floors, and the watched-file baselines.

        The one finalizer for transactions and ``external`` entries. It computes
        the complete next :class:`FinalizedState` from the current one, off to the
        side, with every fallible step (manifest hashing among them) in that
        computation, then publishes it with one assignment. A failure anywhere
        before that assignment leaves the live state unchanged, and running this
        again completes it; a line the state already includes is a no-op.
        Callers quarantine the project on failure (plan-review resolution 2).
        """
        state = self._state
        if state is None:
            raise RuntimeError(f"project {self.slug} is not loaded")
        seq = line["seq"]
        if seq <= state.journal.head_seq:
            return
        paths = [p for p in line.get("paths") or () if isinstance(p, str)]
        journal = state.journal.advance(line, raw)
        transactions._fault("finish.memory", seq=seq)
        manifest = state.manifest.advanced(
            self.board, paths, frozenset(line.get("lengths") or ()), seq
        )
        transactions._fault("finish.memory.manifest", seq=seq)
        floors = state.floors.with_events(events)
        transactions._fault("finish.memory.floors", seq=seq)
        watched = _watched_after(self.board, state.watched, paths)
        transactions._fault("finish.memory.watched", seq=seq)
        self._state = FinalizedState(journal, manifest, floors, watched)

    def _publish(self, line: dict) -> None:
        """Hand a committed line to every open stream (under the locks, in ``seq``
        order). Its events are read back only when someone is listening."""
        journal = self.journal
        if journal is None:
            raise RuntimeError(f"project {self.slug} is not loaded")
        seq = line["seq"]
        data = None
        if self.broadcaster.has_subscribers():
            digest = journal.hash_at(seq)
            if digest is None:
                raise RuntimeError(f"journal line {seq} has no hash; was it accepted?")
            events = entry_events(self.board, journal, line)
            data = journal_frame(journal.epoch, line, digest, events)
        self.broadcaster.publish(journal.epoch, seq, data)

    def rotate_epoch(self) -> dict:
        """Start a new epoch under the locks (the ``rotate-epoch`` control request,
        SPEC §8.2) and broadcast ``reset``. A failure part-way quarantines the
        project; its next load finishes the rotation from ``rotation.json``."""
        journal = self.journal
        if journal is None:
            raise OpError("BOARD_UNAVAILABLE", f"project {self.slug} is not loaded")
        old_epoch = journal.epoch
        state = self._state
        assert state is not None
        try:
            with board_scope(self.board), strict_durability():
                rotated = journal.rotate()
            renewed = FinalizedState(
                journal=rotated.index,
                manifest=Manifest.build(self.board),
                floors=state.floors,
                watched=state.watched,
            )
            self.op_seqs = {}  # the op-status map covers the current epoch only
            self._adopt(rotated, renewed)
        except BaseException as exc:
            self._mark_unavailable(f"epoch rotation failed: {describe_error(exc)}")
            raise OpError(
                "BOARD_UNAVAILABLE",
                f"project {self.slug}: epoch rotation failed; it finishes at the next load",
            ) from exc
        # Queued to every stream before heartbeats may name the new epoch (SPEC §8.9).
        self.broadcaster.reset(rotated.epoch)
        self.log.info("epoch_rotated", project=self.slug, old_epoch=old_epoch, epoch=rotated.epoch)
        return {"project": self.slug, "old_epoch": old_epoch, "epoch": rotated.epoch}

    def publication_failed(self, seq: int) -> None:
        """Publication failed for committed *seq*: streams reconnect and replay."""
        self.log.warning("publication_failed", project=self.slug, seq=seq)
        if self.close_streams is not None:
            self.close_streams()

    # -- op status (SPEC §8.6) ---------------------------------------------

    def op_status(self, token_id: str | None, op_id: str) -> dict:
        """The outcome of one of *token_id*'s operations. Reads only memory, the
        committed receipt line, and retained epoch journals (which never change),
        so it needs no lock, and it changes nothing: an entry past retention is
        simply not used (its result is gone; the journals still say committed)."""
        key = (token_id, op_id)
        known = self.index.get(key)
        if known is not None and recovery.receipt_expired(known.receipt):
            known = None
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


@control.action("rotate-epoch")
def _rotate_epoch_action(project: Project, request: dict) -> dict:
    return project.rotate_epoch()


@control.action("set-config")
def _set_config_action(project: Project, request: dict) -> dict:
    from lattice.server.admin import validate_config_changes

    changes = validate_config_changes(request.get("set"))
    return project.set_config(changes)


def lock_timeout_error(exc: LockTimeout) -> OpError:
    """A storage lock that timed out inside the server: ``BOARD_BUSY`` (SPEC §3.1)."""
    return OpError("BOARD_BUSY", str(exc))
