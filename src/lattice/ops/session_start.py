"""``session.start``: the ``lattice session start`` command's rules.

``name`` is the new session's base name, a creation parameter, not the
``--name`` actor selector. The operation takes no actor (SPEC §3.7). Each
start allocates a new serial under the ``sessions_index`` lock, and the
session file records the operation's ``origin``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from lattice.core.actors import validate_base_name, validate_session_creation
from lattice.ops.base import OpContext, OpError, OpResult, check_path_component, operation
from lattice.storage.sessions import create_session


@dataclass(frozen=True, kw_only=True)
class SessionStartParams:
    path_params: ClassVar[dict[str, str]] = {"name": "session name"}

    model: str
    name: str | None = None
    framework: str | None = None
    agent_type: str | None = None
    prompt: str | None = None
    parent: str | None = None

    def base_name(self) -> str | None:
        """The base name the session gets: ``name``, else the capitalized
        ``agent_type``, else ``None`` (one is picked from the word list)."""
        if self.name is None and self.agent_type is not None:
            return self.agent_type.capitalize()
        return self.name

    def check(self) -> None:
        # Today's messages first, so the path check only catches what they allow.
        base_name = self.base_name()
        if base_name is not None:
            err = validate_base_name(base_name)
            if err:
                raise OpError("VALIDATION_ERROR", err)
        err = validate_session_creation(model=self.model, framework=self.framework)
        if err:
            raise OpError("VALIDATION_ERROR", err)
        if base_name is not None:
            check_path_component(base_name, "session name")


@operation("session.start")
class SessionStart:
    Params = SessionStartParams
    no_actor = True

    def run(self, ctx: OpContext, p: SessionStartParams) -> OpResult:
        try:
            identity = create_session(
                ctx.lattice_dir,
                base_name=p.base_name(),
                agent_type=p.agent_type,
                model=p.model,
                framework=p.framework,
                prompt=p.prompt,
                parent=p.parent,
            )
        except ValueError as exc:
            raise OpError("VALIDATION_ERROR", str(exc)) from exc
        return OpResult(
            value={
                "name": identity.name,
                "base_name": identity.base_name,
                "serial": identity.serial,
                "session": identity.session,
                "model": identity.model,
                "framework": identity.framework,
            }
        )
