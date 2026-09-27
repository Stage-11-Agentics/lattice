"""``resource.release``: the ``lattice resource release`` command's rules."""

from __future__ import annotations

from dataclasses import dataclass

from lattice.core import events as core_events
from lattice.core.events import create_resource_event
from lattice.core.resources import apply_resource_event_to_snapshot, find_holder
from lattice.ops import resource_common
from lattice.ops.base import OpContext, OpError, OpResult, operation
from lattice.ops.resource_common import ResourceParams
from lattice.storage.operations import resource_write_context
from lattice.storage.resources import find_resource


@dataclass(frozen=True, kw_only=True)
class ResourceReleaseParams(ResourceParams):
    pass


@operation("resource.release")
class ResourceRelease:
    Params = ResourceReleaseParams

    def run(self, ctx: OpContext, p: ResourceReleaseParams) -> OpResult:
        actor = ctx.actor
        with resource_write_context(ctx.lattice_dir, p.name):
            resource_id, resource_name, snapshot = find_resource(
                ctx.lattice_dir, p.name, ctx.config
            )
            if snapshot is None:
                raise OpError("NOT_FOUND", f"Resource '{p.name}' does not exist.")
            holder = find_holder(snapshot, actor)
            if holder is None:
                raise OpError(
                    "NOT_HELD",
                    f"You ({actor}) do not hold resource '{resource_name}'.",
                    {"resource": snapshot},
                )

            data: dict = {"holder": actor}
            if holder.get("task_id"):
                data["task_id"] = holder["task_id"]
            if p.reason:
                data["reason"] = p.reason
            event = create_resource_event(
                "resource_released",
                resource_id,
                actor,
                data,
                ts=core_events.utc_now(),
                **p.provenance(),
            )
            snapshot = apply_resource_event_to_snapshot(snapshot, event)
            resource_common.write(ctx, resource_id, resource_name, [event], snapshot)

        return resource_common.result(resource_id, resource_name, snapshot, [event])
