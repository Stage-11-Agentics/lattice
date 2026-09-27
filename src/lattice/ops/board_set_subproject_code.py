"""``board.set_subproject_code``: the ``lattice set-subproject-code`` command's rules.

Takes no actor (SPEC §3.7) and changes only ``subproject_code``.
"""

from __future__ import annotations

from dataclasses import dataclass

from lattice.core.config import validate_subproject_code
from lattice.ops.base import OpContext, OpError, OpResult, operation
from lattice.storage.board_config import UNCHANGED, update_config_key


@dataclass(frozen=True, kw_only=True)
class SetSubprojectCodeParams:
    code: str
    force: bool = False


@operation("board.set_subproject_code")
class SetSubprojectCode:
    Params = SetSubprojectCodeParams
    no_actor = True

    def run(self, ctx: OpContext, p: SetSubprojectCodeParams) -> OpResult:
        code = p.code.upper()
        if not validate_subproject_code(code):
            raise OpError(
                "VALIDATION_ERROR",
                f"Invalid subproject code: '{code}'. "
                "Must be 1-5 uppercase ASCII letters/digits, starting with a letter.",
            )
        previous: list[str | None] = [None]

        def decide(config: dict) -> object:
            if not config.get("project_code"):
                raise OpError(
                    "VALIDATION_ERROR",
                    "Cannot set subproject code without a project code. "
                    "Run 'lattice set-project-code' first.",
                )
            existing = config.get("subproject_code")
            previous[0] = existing
            if existing:
                if existing == code:
                    return UNCHANGED
                if not p.force:
                    raise OpError(
                        "CONFLICT",
                        f"Subproject code is already set to '{existing}'. "
                        "Use --force to change it.",
                    )
            return code

        _, written = update_config_key(ctx.lattice_dir, "subproject_code", decide)
        return OpResult(
            value={"subproject_code": code, "previous": previous[0]}, idempotent=not written
        )
