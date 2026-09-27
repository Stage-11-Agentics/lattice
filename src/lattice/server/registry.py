"""The projects a server hosts, their lazy load and prewarm, and worker threads (SPEC §8.1, §8.5).

A project is created in memory the first time anything names it and loads on
its first admission (or during the startup prewarm, which loads every project
in slug order). Loads, and all board I/O, run in worker threads; the event
loop only awaits admission. ``/healthz`` therefore answers from the first
second, whatever the projects are doing.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, TypeVar

import anyio.to_thread

from lattice.core.errors import OpError
from lattice.core.tasks import set_unknown_type_reporter
from lattice.server import admin, control
from lattice.server.config import STATUS_JSON, ServerConfig
from lattice.server.journal import now_ms
from lattice.server.log import ServerLog
from lattice.server.project import CURRENT_PROJECT, LOADED, LOADING, STATES, UNLOADED, Project
from lattice.storage.fs import atomic_write
from lattice.storage.ownership import release_owner_flock, try_owner_flock

T = TypeVar("T")

CONTROL_POLL_SECONDS = 2.0


class WorkerCrash(Exception):
    """A worker raised a ``BaseException`` that is not an ``Exception``
    (``SystemExit``, ``KeyboardInterrupt``, ...); contained so the server keeps serving."""

    def __init__(self, original: BaseException) -> None:
        super().__init__(f"{type(original).__name__}: {original}")
        self.original = original


def _contained(fn: Callable[[], T]) -> tuple[bool, Any]:
    try:
        return True, fn()
    except BaseException as exc:  # noqa: BLE001 - nothing may escape a worker thread (G-6)
        return False, exc


async def in_worker(fn: Callable[[], T]) -> T:
    """Run *fn* in a worker thread. Any exception comes back here; a
    ``BaseException`` that is not an ``Exception`` becomes :class:`WorkerCrash`,
    so it never unwinds the event loop."""
    ok, value = await anyio.to_thread.run_sync(_contained, fn)
    if ok:
        return value
    if isinstance(value, Exception):
        raise value
    raise WorkerCrash(value)


class ProjectRegistry:
    def __init__(self, root: Path, config: ServerConfig, log: ServerLog, server_id: str) -> None:
        self.root = Path(root)
        self.config = config
        self.log = log
        self.server_id = server_id
        self._projects: dict[str, Project] = {}
        self._tasks: list[asyncio.Task] = []
        self.prewarm_done = asyncio.Event()
        self.control_poll_seconds = CONTROL_POLL_SECONDS
        self._status_lock = threading.Lock()

    # -- lookup --------------------------------------------------------------

    def slugs(self) -> list[str]:
        return admin.project_slugs(self.root)

    def get(self, slug: str) -> Project | None:
        project = self._projects.get(slug)
        if project is not None:
            return project
        if not admin.SLUG_RE.fullmatch(slug):
            return None
        if not (self.root / "projects" / slug / ".lattice" / "config.json").is_file():
            return None
        project = Project(
            slug,
            self.root / "projects" / slug,
            self.log,
            self.server_id,
            on_state_change=self.write_status,
        )
        self._projects[slug] = project
        return project

    def counts(self) -> dict[str, int]:
        counts = dict.fromkeys(STATES, 0)
        for slug in self.slugs():
            project = self._projects.get(slug)
            counts[project.state if project else UNLOADED] += 1
        return counts

    # -- admission -----------------------------------------------------------

    @asynccontextmanager
    async def admission_only(self, project: Project) -> AsyncIterator[Project]:
        """Hold *project*'s admission lock (``BOARD_BUSY`` after
        ``lock_timeout_seconds``), without loading it."""
        timeout = self.config.limits.lock_timeout_seconds
        try:
            async with asyncio.timeout(timeout):
                await project.admission.acquire()
        except TimeoutError:
            raise OpError(
                "BOARD_BUSY",
                f"project {project.slug} is busy; retry shortly.",
                {"retry_after": 2},
            ) from None
        try:
            yield project
        finally:
            project.admission.release()

    @asynccontextmanager
    async def admitted(self, project: Project) -> AsyncIterator[Project]:
        """Hold *project*'s admission lock, loading it first if it has never loaded
        (a project an admin unloaded stays unloaded)."""
        async with self.admission_only(project):
            if project.state in (UNLOADED, LOADING) and not project.held_unloaded:
                await in_worker(project.load)
            yield project

    async def run_locked(self, project: Project, fn: Callable[[], T], *, admit: bool = True) -> T:
        """Admit, then run *fn* in a worker under the project's work lock (after the
        admission checks when *admit*)."""
        async with self.admitted(project):

            def work() -> T:
                with project.locked():
                    if admit:
                        project.admit()
                    return fn()

            return await in_worker(work)

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Start the prewarm and the control-request poller (call on the loop)."""
        set_unknown_type_reporter(self._report_unknown_type)
        self._tasks.append(asyncio.create_task(self._prewarm()))
        self._tasks.append(asyncio.create_task(self._poll_control()))
        self.write_status()

    def close_all_streams(self) -> None:
        """End every open stream (thread-safe). Called as shutdown begins, so an open
        follower never holds a graceful shutdown for its whole timeout."""
        for project in list(self._projects.values()):
            project.broadcaster.close_all()

    async def stop(self) -> None:
        """Graceful shutdown, in named phases (SPEC §8.11): stop the background
        tasks; drain (take each project's admission, so its in-flight operation
        finishes and no other starts); the final audit commit (H-16's hook);
        write ``clean_shutdown``; release the lease."""
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks.clear()
        for project in list(self._projects.values()):
            async with project.admission:  # drain
                await in_worker(lambda p=project: self._shut_down(p))
        set_unknown_type_reporter(None)
        self.write_status(stopped=True)

    def _shut_down(self, project: Project) -> None:
        with project.work:
            for phase in SHUTDOWN_PHASES:
                try:
                    phase(self, project)
                except Exception as exc:  # noqa: BLE001 - every project still releases
                    self.log.error(
                        "shutdown_phase_failed",
                        project=project.slug,
                        phase=phase.__name__,
                        error=repr(exc),
                    )
            if project.holds_lease:
                project.release()

    def _report_unknown_type(self, etype: str) -> None:
        slug = CURRENT_PROJECT.get()
        project = self._projects.get(slug) if slug else None
        if project is not None:
            project.report_unknown_type(etype)
        else:
            self.log.warning("unknown_event_type", type=etype)

    async def _prewarm(self) -> None:
        for slug in self.slugs():
            project = self.get(slug)
            if project is None or project.state != UNLOADED:
                continue
            try:
                async with self.admitted(project):
                    pass
            except OpError as exc:
                self.log.warning("prewarm_skipped", project=slug, error_code=exc.code)
            except Exception as exc:  # noqa: BLE001 - one project never stops the prewarm
                self.log.error("prewarm_failed", project=slug, error=repr(exc))
        self.prewarm_done.set()

    async def _poll_control(self) -> None:
        while True:
            await asyncio.sleep(self.control_poll_seconds)
            await self.run_pending_control()

    async def run_pending_control(self) -> None:
        """Run the control requests waiting in any project directory, oldest first.
        Lifecycle requests run here, at the admission layer (:meth:`run_lifecycle`);
        every other request runs under the project's work lock."""
        for slug in self.slugs():
            board = self.root / "projects" / slug / ".lattice"
            if not control.pending_requests(board):
                continue
            project = self.get(slug)
            if project is None:
                continue
            try:
                while True:
                    pending = control.pending_requests(board)
                    if not pending:
                        break
                    head = pending[0]
                    if control.request_action(head) in control.LIFECYCLE_ACTIONS:
                        await self.run_lifecycle(project, head)
                        continue
                    ran = await self.run_locked(project, project.run_control_requests, admit=False)
                    if not ran:
                        break
            except OpError as exc:
                self.log.warning("control_deferred", project=slug, error_code=exc.code)
            except Exception as exc:  # noqa: BLE001
                self.log.error("control_failed", project=slug, error=repr(exc))

    # -- lifecycle control requests (SPEC §8.2) -------------------------------

    async def run_lifecycle(self, project: Project, path: Path) -> None:
        """Run one ``unload``, ``load``, ``reload``, or ``doctor`` request and answer it.

        Holds the project's admission lock (so the operation in flight, if any,
        finishes first and none starts), never a work lock across a load: a
        load takes the work lock itself.
        """
        action = control.request_action(path)
        try:
            result = await self._lifecycle(project, action or "")
            answer: dict = {"ok": True, "result": result}
        except OpError as exc:
            answer = {"ok": False, "error": exc.to_dict()}
        except Exception as exc:  # noqa: BLE001 - answered and logged, never raised
            self.log.error(
                "control_request_crashed", project=project.slug, action=action, error=repr(exc)
            )
            answer = {
                "ok": False,
                "error": {"code": "INTERNAL_ERROR", "message": f"{action} failed: {exc!r}"},
            }
        await in_worker(lambda: control.answer_unowned(path, answer))
        self.log.info(
            "control_request",
            project=project.slug,
            request=path.stem,
            action=action,
            ok=answer["ok"],
            error_code=(answer.get("error") or {}).get("code"),
        )

    async def _lifecycle(self, project: Project, action: str) -> dict:
        async with self.admission_only(project):
            if action == "unload":
                return await in_worker(lambda: self._unload(project))
            if action == "load":
                return await in_worker(lambda: self._load(project))
            if action == "reload":
                await in_worker(lambda: self._unload(project))
                return await in_worker(lambda: self._load(project))
            if action == "doctor":
                return await in_worker(lambda: self._doctor(project))
        raise OpError("VALIDATION_ERROR", f"unsupported control action {action!r}")

    def _unload(self, project: Project) -> dict:
        with project.work:
            project.unload()
        # Answer only once the lease is provably free (SPEC §8.2).
        if not admin.try_owner_flock_free(project.board):
            raise OpError(
                "BOARD_BUSY", f"project {project.slug} was unloaded, but its lease is still held"
            )
        return {"project": project.slug, "state": project.state, "lease": "released"}

    def _load(self, project: Project) -> dict:
        project.held_unloaded = False
        if not (project.state == LOADED and project.holds_lease):
            project.load()
        if project.state != LOADED:
            raise OpError(
                "BOARD_UNAVAILABLE",
                f"project {project.slug} did not load: {project.reason}",
                {"reason": project.reason},
            )
        head = project.head()
        return {"project": project.slug, "state": project.state, **head}

    def _doctor(self, project: Project) -> dict:
        if project.holds_lease:
            with project.locked():
                data = admin.run_doctor(project.board)
        else:
            fd = try_owner_flock(project.board)
            if fd is None:
                raise OpError(
                    "BOARD_BUSY",
                    f"another process holds project {project.slug} (offline maintenance?)",
                )
            try:
                with project.work:
                    data = admin.run_doctor(project.board)
            finally:
                release_owner_flock(fd)
        return {"project": project.slug, **data}

    def write_status(self, *, stopped: bool = False) -> None:
        """Publish project states for ``lattice server project list``.

        Called on every project state change (from worker threads) and at start
        and stop; serialized so the newest state is the one left on disk.
        """
        with self._status_lock:
            self._write_status(stopped)

    def _write_status(self, stopped: bool) -> None:
        projects = list(self._projects.items())
        status = {
            "pid": os.getpid(),
            "server_id": self.server_id,
            "updated_at": now_ms(),
            "stopped": stopped,
            "projects": {slug: {"state": p.state, "reason": p.reason} for slug, p in projects},
        }
        try:
            atomic_write(self.root / STATUS_JSON, json.dumps(status, sort_keys=True) + "\n")
        except OSError as exc:
            self.log.warning("status_write_failed", error=str(exc))


def final_audit(registry: ProjectRegistry, project: Project) -> None:
    """The final audit commit (SPEC §8.10): H-16 connects its committer here."""


def clean_shutdown(registry: ProjectRegistry, project: Project) -> None:
    project.write_clean_shutdown()
    if project.journal is not None and project.state == "loaded":
        registry.log.info(
            "clean_shutdown", project=project.slug, head_seq=project.journal.head_seq
        )


def release_lease(registry: ProjectRegistry, project: Project) -> None:
    project.release()


#: The per-project shutdown phases, in order, run under the work lock after drain.
SHUTDOWN_PHASES: tuple[Callable[[ProjectRegistry, Project], None], ...] = (
    final_audit,
    clean_shutdown,
    release_lease,
)
