"""``board.set_project_code``: the ``lattice set-project-code`` command's rules.

Takes no actor (SPEC §3.7) and changes only ``project_code``.
"""

from __future__ import annotations

from dataclasses import dataclass

from lattice.core.config import validate_project_code
from lattice.ops.base import OpContext, OpError, OpResult, operation
from lattice.storage.board_config import UNCHANGED, update_config_key
from lattice.storage.short_ids import load_id_index, save_id_index


@dataclass(frozen=True, kw_only=True)
class SetProjectCodeParams:
    code: str
    force: bool = False


@operation("board.set_project_code")
class SetProjectCode:
    Params = SetProjectCodeParams
    no_actor = True

    def run(self, ctx: OpContext, p: SetProjectCodeParams) -> OpResult:
        code = p.code.upper()
        if not validate_project_code(code):
            raise OpError(
                "VALIDATION_ERROR",
                f"Invalid project code: '{code}'. "
                "Must be 1-5 uppercase ASCII letters/digits, starting with a letter.",
            )
        previous: list[str | None] = [None]

        def decide(config: dict) -> object:
            existing = config.get("project_code")
            previous[0] = existing
            if existing:
                if existing == code:
                    return UNCHANGED
                if not p.force:
                    raise OpError(
                        "CONFLICT",
                        f"Project code is already set to '{existing}'. Use --force to change it.",
                    )
            return code

        _, written = update_config_key(ctx.lattice_dir, "project_code", decide)
        value = {"project_code": code, "previous": previous[0]}
        if not written:
            return OpResult(value=value, idempotent=True)
        if not (ctx.lattice_dir / "ids.json").exists():
            save_id_index(ctx.lattice_dir, load_id_index(ctx.lattice_dir))
        return OpResult(value=value)
