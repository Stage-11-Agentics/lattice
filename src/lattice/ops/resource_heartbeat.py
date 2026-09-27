"""``resource.heartbeat``: the ``lattice resource heartbeat`` command's rules."""

from __future__ import annotations

from dataclasses import dataclass

from lattice.core import events as core_events
from lattice.core.events import create_resource_event
from lattice.core.resources import (
    apply_resource_event_to_snapshot,
    compute_expires_at,
    find_holder,
    is_holder_stale,
)
from lattice.ops import resource_common
from lattice.ops.base import OpContext, OpError, OpResult, operation
from lattice.ops.resource_common import ResourceParams
from lattice.storage.operations import resource_write_context
from lattice.storage.resources import find_resource


@dataclass(frozen=True, kw_only=True)
class ResourceHeartbeatParams(ResourceParams):
    pass


@operation("resource.heartbeat")
class ResourceHeartbeat:
    Params = ResourceHeartbeatParams

    def run(self, ctx: OpContext, p: ResourceHeartbeatParams) -> OpResult:
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

            now = core_events.utc_now()
            # An expired hold is not extended: the holder must re-acquire.
            if is_holder_stale(holder, now):
                raise OpError(
                    "EXPIRED",
                    f"Your hold on '{resource_name}' has expired. "
                    "Use 'lattice resource acquire' to re-acquire.",
                    {"resource": snapshot},
                )

            event = create_resource_event(
                "resource_heartbeat",
                resource_id,
                actor,
                {"holder": actor, "expires_at": compute_expires_at(snapshot["ttl_seconds"], now)},
                ts=now,
                **p.provenance(),
            )
            snapshot = apply_resource_event_to_snapshot(snapshot, event)
            resource_common.write(ctx, resource_id, resource_name, [event], snapshot)

        return resource_common.result(resource_id, resource_name, snapshot, [event])
