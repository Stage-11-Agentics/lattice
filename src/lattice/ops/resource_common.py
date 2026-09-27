"""What the ``resource.*`` operations share: their name parameter and the write.

Not an operation module; ``resource_create``, ``resource_acquire``,
``resource_release`` and ``resource_heartbeat`` are.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from lattice.ops.base import CommonParams, OpContext, OpResult
from lattice.storage.operations import write_resource_event


@dataclass(frozen=True, kw_only=True)
class ResourceParams(CommonParams):
    """``name``: a resource name or ``res_`` ID, as the caller gave it."""

    path_params: ClassVar[dict[str, str]] = {"name": "resource name"}

    name: str


def write(
    ctx: OpContext, resource_id: str, resource_name: str, events: list[dict], snapshot: dict
) -> None:
    """Append *events* and write *snapshot*; the caller holds the resource lock."""
    write_resource_event(
        ctx.lattice_dir,
        resource_id,
        resource_name,
        events,
        snapshot,
        ctx.config,
        _caller_holds_lock=True,
        run_hooks=ctx.run_hooks,
    )


def result(
    resource_id: str,
    resource_name: str,
    snapshot: dict,
    events: list[dict],
    *,
    idempotent: bool = False,
) -> OpResult:
    """The ``OpResult`` of a resource operation: the snapshot is its ``--json`` data."""
    return OpResult(
        events=events,
        value=snapshot,
        idempotent=idempotent,
        resource_id=resource_id,
        resource_name=resource_name,
    )
