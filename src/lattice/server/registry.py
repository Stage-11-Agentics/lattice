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
from lattice.server.project import CURRENT_PROJECT, LOADING, STATES, UNLOADED, Project
from lattice.storage.fs import atomic_write

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

    # -- lookup --------------------------------------------------------------

    def slugs(self) -> list[str]:
        return admin.project_slugs(self.root)

    def get(self, slug: str) -> Project | None:
        project = self._projects.get(slug)
        if project is not None:
            return project
        if not admin.SLUG_RE.fullmatch(slug) or slug not in self.slugs():
            return None
        project = Project(slug, self.root / "projects" / slug, self.log, self.server_id)
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
    async def admitted(self, project: Project) -> AsyncIterator[Project]:
        """Hold *project*'s admission lock (``BOARD_BUSY`` after ``lock_timeout_seconds``),
        loading it first if it has never loaded."""
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
            if project.state in (UNLOADED, LOADING):
                await in_worker(project.load)
                self.write_status()
            yield project
        finally:
            project.admission.release()

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

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks.clear()
        for project in list(self._projects.values()):
            async with project.admission:
                await in_worker(lambda p=project: _release(p))
        set_unknown_type_reporter(None)
        self.write_status(stopped=True)

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
            await asyncio.sleep(CONTROL_POLL_SECONDS)
            await self.run_pending_control()

    async def run_pending_control(self) -> None:
        """Run the control requests waiting in any project directory."""
        for slug in self.slugs():
            board = self.root / "projects" / slug / ".lattice"
            if not control.pending_requests(board):
                continue
            project = self.get(slug)
            if project is None:
                continue
            try:
                await self.run_locked(project, project.run_control_requests, admit=False)
            except OpError as exc:
                self.log.warning("control_deferred", project=slug, error_code=exc.code)
            except Exception as exc:  # noqa: BLE001
                self.log.error("control_failed", project=slug, error=repr(exc))

    def write_status(self, *, stopped: bool = False) -> None:
        """Publish project states for ``lattice server project list``."""
        status = {
            "pid": os.getpid(),
            "server_id": self.server_id,
            "updated_at": now_ms(),
            "stopped": stopped,
            "projects": {
                slug: {"state": p.state, "reason": p.reason} for slug, p in self._projects.items()
            },
        }
        try:
            atomic_write(self.root / STATUS_JSON, json.dumps(status, sort_keys=True) + "\n")
        except OSError as exc:
            self.log.warning("status_write_failed", error=str(exc))


def _release(project: Project) -> None:
    with project.work:
        project.release()
