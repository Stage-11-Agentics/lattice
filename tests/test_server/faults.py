"""Deterministic fault injection for server transactions (AC-4, SPEC §8.6).

An :class:`Injector` stands in for every seam a transaction passes through:

- ``transactions._fault(point, **ctx)``: the server-control steps
  (``undo.write``, ``undo.fsync``, ``board.mutation``, ``receipt.write``,
  ``receipt.fsync``, ``journal.write``, ``journal.fsync``, ``finish.index``,
  ``finish.undo_delete``, ``recover.*``). A ``*.write`` point passes ``fd`` and
  ``data``, so a ``short`` fault writes a real torn prefix before it raises.
- ``operations._mutation_boundary(name, ...)``: the placement boundaries, as
  ``placement.<name>`` (for example ``placement.source_event_removed``).
- ``fs._fsync_directory``: as ``dir_fsync``, counted only for a strictly
  durable write (a server transaction or a ``hosted/`` path), where a failure
  propagates.

A counting pass (no fault) lists every point in order; a test then fails the
same operation at each ``(point, occurrence)`` in turn.
"""

from __future__ import annotations

import errno
import os
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from lattice.core.errors import OpError
from lattice.ops.base import Caller, get_operation, parse_params
from lattice.server.journal import fingerprint
from lattice.server.log import ServerLog
from lattice.server.project import Project, WriteOutcome, WriteRequest


class InjectedFault(OSError):
    def __init__(self, point: str) -> None:
        super().__init__(errno.EIO, f"injected fault at {point}")
        self.point = point


@dataclass
class Fault:
    point: str
    occurrence: int = 1
    #: Write a torn prefix first (``*.write`` points only).
    short: bool = False
    #: Keep failing at every later occurrence too (a persistent fault).
    sticky: bool = False
    fired: bool = False


class Injector:
    """Records every seam it passes; raises :class:`InjectedFault` where told.

    ``Injector()`` only counts; ``Injector("journal.fsync")`` fails the first
    journal fsync; :meth:`also` adds more faults (for example a failed recovery).
    """

    def __init__(
        self,
        point: str | None = None,
        occurrence: int = 1,
        *,
        short: bool = False,
        sticky: bool = False,
    ) -> None:
        self.faults: list[Fault] = []
        self.calls: list[str] = []
        self._seen: Counter = Counter()
        if point is not None:
            self.also(point, occurrence, short=short, sticky=sticky)

    def also(
        self, point: str, occurrence: int = 1, *, short: bool = False, sticky: bool = False
    ) -> Injector:
        self.faults.append(Fault(point, occurrence, short, sticky))
        return self

    def disarm(self) -> None:
        self.faults.clear()

    @property
    def fired(self) -> bool:
        return any(f.fired for f in self.faults)

    def __call__(self, point: str, **ctx: Any) -> None:
        self.calls.append(point)
        self._seen[point] += 1
        for fault in self.faults:
            if fault.point != point or self._seen[point] < fault.occurrence:
                continue
            if fault.fired and not fault.sticky:
                continue
            fault.fired = True
            if fault.short and "fd" in ctx:
                data = ctx["data"]
                os.write(ctx["fd"], data[: max(1, len(data) // 2)])
            raise InjectedFault(point)

    def occurrences(self) -> list[tuple[str, int]]:
        """Every ``(point, occurrence)`` the counting pass saw, in order."""
        seen: Counter = Counter()
        out = []
        for point in self.calls:
            seen[point] += 1
            out.append((point, seen[point]))
        return out


def install(monkeypatch: pytest.MonkeyPatch, injector: Injector) -> Injector:
    """Route every transaction seam through *injector*."""
    import lattice.server.transactions as transactions
    import lattice.storage.fs as fs
    import lattice.storage.operations as operations

    monkeypatch.setattr(transactions, "_fault", injector)
    monkeypatch.setattr(
        operations,
        "_mutation_boundary",
        lambda name, *_args: injector(f"placement.{name}"),
    )
    real_fsync_directory = fs._fsync_directory

    def fsync_directory(path: Path, *, strict: bool = False) -> None:
        if strict or fs._STRICT_DURABILITY.get():
            injector("dir_fsync")
        real_fsync_directory(path, strict=strict)

    monkeypatch.setattr(fs, "_fsync_directory", fsync_directory)
    return injector


# ---------------------------------------------------------------------------
# Driving a Project directly (no HTTP), as the op endpoint's worker does
# ---------------------------------------------------------------------------


def load_project(root: Path, slug: str, log: ServerLog | None = None) -> Project:
    project = Project(slug, root / "projects" / slug, log or ServerLog("debug"), "srv_test")
    project.load()
    assert project.state == "loaded", project.reason
    return project


def request(
    op: str,
    params: dict | None = None,
    *,
    token_id: str = "tok_alice",
    op_id: str | None = None,
    actor: str | None = "human:alice",
    actor_name: str | None = None,
    minted: bool = False,
) -> WriteRequest:
    """A :class:`WriteRequest` built the way ``parse_envelope`` builds one. Like the
    Lattice client, it always carries an ``op_id`` (a fresh one unless given);
    ``minted`` builds a request that sent none."""
    from lattice.core.ids import generate_op_id

    params = params or {}
    op_id = op_id or generate_op_id()
    op_cls = get_operation(op)
    if actor_name is not None:
        actor = None
    caller = Caller(
        actor=actor,
        actor_name=actor_name,
        origin={"op_id": op_id, "reported": {}, "authenticated": {"token_id": token_id}},
        attestations={},
        expect_last_event_id=None,
    )
    return WriteRequest(
        op=op,
        params=parse_params(op_cls.Params, params, op_name=op),
        caller=caller,
        token_id=token_id,
        fp=fingerprint(op, params, actor, actor_name, {}, None),
        minted=minted,
    )


def run(project: Project, write: WriteRequest) -> WriteOutcome:
    """Admit and run one write under the project's work lock."""
    with project.locked():
        project.admit()
        return project.run_write(write)


def run_expecting_error(project: Project, write: WriteRequest) -> BaseException:
    try:
        run(project, write)
    except (OpError, OSError, Exception) as exc:  # noqa: BLE001 - the test inspects it
        return exc
    raise AssertionError(f"{write.op} unexpectedly succeeded")


# ---------------------------------------------------------------------------
# Crashes: the disk as a killed process leaves it (SPEC §8.7, AC-4)
# ---------------------------------------------------------------------------


class CrashSnapshots:
    """Copies the server root at every seam, as if the process died right there.

    Installed like an :class:`Injector` (it raises nothing), it records, for each
    ``(point, occurrence)``, a copy of the whole root taken *before* that step
    runs. At a ``*.write`` point it also records the torn variant: the copy with
    the first half of the line already appended to the file being written. A
    test then loads each copy with a fresh :class:`Project` and checks that
    startup recovery leaves the operation wholly present or wholly absent.
    """

    def __init__(self, root: Path, base: Path) -> None:
        self.root = Path(root)
        self.base = Path(base)
        self.snapshots: list[tuple[str, Path]] = []
        self._seen: Counter = Counter()

    def __call__(self, point: str, **ctx: Any) -> None:
        import shutil

        self._seen[point] += 1
        label = f"{point}#{self._seen[point]}"
        target = self.base / f"crash-{len(self.snapshots):03d}"
        shutil.copytree(self.root, target, symlinks=True)
        self.snapshots.append((label, target))
        if "data" in ctx and "path" in ctx:
            torn = self.base / f"crash-{len(self.snapshots):03d}"
            shutil.copytree(self.root, torn, symlinks=True)
            copied = torn / Path(ctx["path"]).resolve().relative_to(self.root.resolve())
            data = ctx["data"]
            with open(copied, "ab") as fh:
                fh.write(data[: max(1, len(data) // 2)])
            self.snapshots.append((f"{label} torn", torn))
