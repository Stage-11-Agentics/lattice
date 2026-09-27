"""``session.end``: the ``lattice session end`` command's rules.

Takes no actor (SPEC §3.7). The archived session file records this
operation's ``origin``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from lattice.ops.base import OpContext, OpError, OpResult, operation
from lattice.storage.sessions import end_session


@dataclass(frozen=True, kw_only=True)
class SessionEndParams:
    path_params: ClassVar[dict[str, str]] = {"name": "session name"}

    name: str
    reason: str | None = None


@operation("session.end")
class SessionEnd:
    Params = SessionEndParams
    no_actor = True

    def run(self, ctx: OpContext, p: SessionEndParams) -> OpResult:
        if not end_session(ctx.lattice_dir, p.name, reason=p.reason):
            raise OpError("NOT_FOUND", f"No active session named '{p.name}'.")
        return OpResult(value={"name": p.name, "status": "ended"})
