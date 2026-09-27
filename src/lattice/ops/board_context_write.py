"""``board.context_write``: ``lattice context write`` replaces ``context.md`` (SPEC §3.9)."""

from __future__ import annotations

from dataclasses import dataclass

from lattice.ops.base import OpContext, OpResult, operation
from lattice.ops.prose_common import ContentParams, confine, current_sha256, written_value
from lattice.storage.fs import atomic_write
from lattice.storage.locks import lattice_lock


@dataclass(frozen=True, kw_only=True)
class ContextWriteParams(ContentParams):
    what = "the context"


@operation("board.context_write")
class ContextWrite:
    """No actor, like ``set-project-code``: the write is a board file, not a task event."""

    Params = ContextWriteParams
    no_actor = True

    def run(self, ctx: OpContext, p: ContextWriteParams) -> OpResult:
        data = p.content.encode("utf-8")
        path = ctx.lattice_dir / "context.md"
        value = written_value("context.md", data)
        with lattice_lock(ctx.lattice_dir / "locks", "board_files"):
            confine(path)  # before the idempotency read
            if current_sha256(path) == value["sha256"]:
                return OpResult(value=value, idempotent=True)
            atomic_write(path, data)
        return OpResult(value=value)
