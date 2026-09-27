"""``task.notes_write``: ``lattice notes write`` (SPEC §3.9)."""

from __future__ import annotations

from dataclasses import dataclass

from lattice.ops.base import CommonParams, OpContext, OpResult, operation
from lattice.ops.prose_common import ContentParams, write_task_prose


@dataclass(frozen=True, kw_only=True)
class NotesWriteParams(CommonParams, ContentParams):
    task: str
    expect_sha256: str | None = None

    what = "the notes"


@operation("task.notes_write")
class NotesWrite:
    Params = NotesWriteParams

    def run(self, ctx: OpContext, p: NotesWriteParams) -> OpResult:
        return write_task_prose(ctx, p, "notes")
