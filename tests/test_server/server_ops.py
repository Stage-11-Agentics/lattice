"""Operations registered only inside the test process (G-11: no server change needed).

Imported by the server tests' conftest; ``lattice.ops`` discovery never sees
this module.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from lattice.ops.base import CommonParams, OpContext, OpResult, operation
from lattice.storage.fs import atomic_write, jsonl_append, unlink_path
from lattice.storage.operations import TaskMutationDecision


@dataclass(frozen=True, kw_only=True)
class SleepParams(CommonParams):
    ms: int = 0


@operation("xtest.sleep")
class Sleep:
    """Holds the project's work lock for *ms* milliseconds, writes nothing."""

    Params = SleepParams

    def run(self, ctx: OpContext, p: SleepParams) -> OpResult:
        time.sleep(p.ms / 1000)
        return OpResult(value={"slept_ms": p.ms}, idempotent=True)


@dataclass(frozen=True, kw_only=True)
class RaiseParams(CommonParams):
    kind: str


@operation("xtest.raise")
class Raise:
    """Raises SystemExit, KeyboardInterrupt, or RuntimeError from inside an operation."""

    Params = RaiseParams

    def run(self, ctx: OpContext, p: RaiseParams) -> OpResult:
        if p.kind == "exit":
            raise SystemExit(3)
        if p.kind == "interrupt":
            raise KeyboardInterrupt
        raise RuntimeError("boom")


@dataclass(frozen=True, kw_only=True)
class ConfigParams(CommonParams):
    kind: str


@operation("xtest.touch_config")
class TouchConfig:
    """Mutates config.json with the given kind (append, replace, unlink)."""

    Params = ConfigParams

    def run(self, ctx: OpContext, p: ConfigParams) -> OpResult:
        path = ctx.lattice_dir / "config.json"
        if p.kind == "append":
            jsonl_append(path, "{}\n")
        elif p.kind == "replace":
            atomic_write(path, path.read_bytes().replace(b'"review_mode"', b'"review_mode"'))
        elif p.kind == "unlink":
            unlink_path(path)
        return OpResult(value={})


@dataclass(frozen=True, kw_only=True)
class NoteParams(CommonParams):
    task: str
    note: str = "hi"


@operation("xtest.note")
class Note:
    """Appends a custom ``x_`` event to a task: a plugin-style event family."""

    Params = NoteParams

    def run(self, ctx: OpContext, p: NoteParams) -> OpResult:
        task_id = ctx.resolve_task(p.task)
        ctx.require_active(task_id)

        def decide(_context):  # noqa: ANN001, ANN202
            return TaskMutationDecision(events=[ctx.event("x_note", task_id, {"note": p.note}, p)])

        result = ctx.mutate(task_id, decide)
        return OpResult(task=result.snapshot, events=result.appended_events, value=p.note)


@dataclass(frozen=True, kw_only=True)
class ExtraParams(CommonParams):
    task: str
    color: str = "blue"


@operation("xtest.defaulted")
class Defaulted:
    """An op whose params the 'older server' in the version tests lacks one of."""

    Params = ExtraParams

    def run(self, ctx: OpContext, p: ExtraParams) -> OpResult:
        return OpResult(value={"color": p.color}, idempotent=True)
