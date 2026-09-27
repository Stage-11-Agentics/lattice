"""``xtest.parity_fixture``: the hosted replay's fixture writes, as an operation.

A corpus ``WriteFile`` / ``DeleteFile`` under a durable path is test setup, not
a command under test (``hosted.py``). On a server it runs as this operation,
registered only in a test process (G-11: the server needs no change), so the
write is one journaled transaction that sync carries to every cache. It writes
through the storage primitives and appends no event.
"""

from __future__ import annotations

from dataclasses import dataclass

from lattice.ops.base import CommonParams, OpContext, OpResult, operation
from lattice.storage.fs import atomic_write, ensure_dir, unlink_path

FIXTURE_OP = "xtest.parity_fixture"


@dataclass(frozen=True, kw_only=True)
class FixtureParams(CommonParams):
    path: str  # relative to .lattice/
    text: str | None = None  # None: delete the file


@operation(FIXTURE_OP)
class ParityFixture:
    Params = FixtureParams
    no_actor = True

    def run(self, ctx: OpContext, p: FixtureParams) -> OpResult:
        target = ctx.lattice_dir / p.path
        if p.text is None:
            unlink_path(target)
        else:
            ensure_dir(target.parent)
            atomic_write(target, p.text)
        return OpResult(value={"path": p.path})
