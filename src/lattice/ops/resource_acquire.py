"""``resource.acquire``: one non-blocking attempt of ``lattice resource acquire``.

``RESOURCE_HELD`` when the resource is not available. ``--wait`` is not a
parameter: the client repeats this operation, one call per attempt, holding
no lock between attempts (SPEC §3.3).
"""

from __future__ import annotations

from dataclasses import dataclass

from lattice.core import events as core_events
from lattice.core.events import create_resource_event
from lattice.core.ids import generate_resource_id
from lattice.core.resources import (
    apply_resource_event_to_snapshot,
    compute_expires_at,
    evict_stale_holders,
    find_holder,
    format_duration_ago,
    format_duration_remaining,
    is_resource_available,
)
from lattice.ops import resource_common
from lattice.ops.base import OpContext, OpError, OpResult, operation
from lattice.ops.resource_common import ResourceParams
from lattice.storage.operations import resource_write_context
from lattice.storage.resources import find_resource, read_resource_snapshot


@dataclass(frozen=True, kw_only=True)
class ResourceAcquireParams(ResourceParams):
    task: str | None = None
    force: bool = False


@operation("resource.acquire")
class ResourceAcquire:
    Params = ResourceAcquireParams

    def run(self, ctx: OpContext, p: ResourceAcquireParams) -> OpResult:
        actor = ctx.actor
        task_id = ctx.resolve_task(p.task) if p.task else None
        provenance = p.provenance()
        written: list[dict] = []

        with resource_write_context(ctx.lattice_dir, p.name):
            resource_id, resource_name, snapshot = find_resource(
                ctx.lattice_dir, p.name, ctx.config
            )
            if not resource_id:
                resource_id, snapshot, created = _auto_create(ctx, resource_name, provenance)
                written.extend(created)
            assert snapshot is not None

            now = core_events.utc_now()
            events: list[dict] = []

            def add(type_: str, data: dict) -> None:
                nonlocal snapshot
                event = create_resource_event(
                    type_, resource_id, actor, data, ts=now, **provenance
                )
                snapshot = apply_resource_event_to_snapshot(snapshot, event)
                events.append(event)

            for stale in evict_stale_holders(snapshot, now):
                add(
                    "resource_expired",
                    {
                        "holder": stale["actor"],
                        "expired_at": stale.get("expires_at", now),
                        "reclaimed_by": actor,
                    },
                )

            if find_holder(snapshot, actor) is not None:
                # Already held by this actor: extend the TTL.
                new_expires = compute_expires_at(snapshot["ttl_seconds"], now)
                add("resource_heartbeat", {"holder": actor, "expires_at": new_expires})
                resource_common.write(ctx, resource_id, resource_name, events, snapshot)
                return resource_common.result(
                    resource_id, resource_name, snapshot, written + events
                )

            if p.force:
                for holder in list(snapshot.get("holders", [])):
                    add(
                        "resource_expired",
                        {"holder": holder["actor"], "expired_at": now, "reclaimed_by": actor},
                    )

            if is_resource_available(snapshot, now):
                data: dict = {
                    "holder": actor,
                    "expires_at": compute_expires_at(snapshot["ttl_seconds"], now),
                }
                if task_id:
                    data["task_id"] = task_id
                if p.reason:
                    data["reason"] = p.reason
                add("resource_acquired", data)
                resource_common.write(ctx, resource_id, resource_name, events, snapshot)
                return resource_common.result(
                    resource_id, resource_name, snapshot, written + events
                )

            # Not available. Stale holders evicted on the way are still written.
            if events:
                resource_common.write(ctx, resource_id, resource_name, events, snapshot)

        holder_info = ""
        holders = snapshot.get("holders", [])
        if holders:
            h = holders[0]
            holder_info = f" Held by {h['actor']}"
            if h.get("task_id"):
                holder_info += f" ({h['task_id']})"
            holder_info += f" since {format_duration_ago(h['acquired_at'], now)}"
            holder_info += f", expires {format_duration_remaining(h['expires_at'], now)}"
        raise OpError(
            "RESOURCE_HELD",
            f"Resource '{p.name}' is not available.{holder_info}",
            {"resource": snapshot},
        )


def _auto_create(
    ctx: OpContext, resource_name: str, provenance: dict
) -> tuple[str, dict, list[dict]]:
    """Create a resource the board's config declares, under the caller's lock.

    Re-checks existence first, so two concurrent first acquires create it once.
    Returns ``(resource_id, snapshot, events written)``.
    """
    existing = read_resource_snapshot(ctx.lattice_dir, resource_name)
    if existing is not None:
        return existing["id"], existing, []

    definition = ctx.config.get("resources", {}).get(resource_name, {})
    resource_id = generate_resource_id()
    data: dict = {
        "name": resource_name,
        "max_holders": definition.get("max_holders", 1),
        "ttl_seconds": definition.get("ttl_seconds", 300),
    }
    if definition.get("description"):
        data["description"] = definition["description"]
    event = create_resource_event("resource_created", resource_id, ctx.actor, data, **provenance)
    snapshot = apply_resource_event_to_snapshot(None, event)
    resource_common.write(ctx, resource_id, resource_name, [event], snapshot)
    return resource_id, snapshot, [event]
