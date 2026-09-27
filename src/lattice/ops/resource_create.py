"""``resource.create``: the ``lattice resource create`` command's rules."""

from __future__ import annotations

from dataclasses import dataclass

from lattice.core.events import create_resource_event
from lattice.core.ids import generate_resource_id, validate_id
from lattice.core.resources import apply_resource_event_to_snapshot
from lattice.ops import resource_common
from lattice.ops.base import OpContext, OpError, OpResult, operation
from lattice.ops.resource_common import ResourceParams
from lattice.storage.operations import resource_write_context
from lattice.storage.resources import list_all_resources, read_resource_snapshot


@dataclass(frozen=True, kw_only=True)
class ResourceCreateParams(ResourceParams):
    description: str | None = None
    max_holders: int = 1
    ttl: int = 300
    id: str | None = None


@operation("resource.create")
class ResourceCreate:
    Params = ResourceCreateParams

    def run(self, ctx: OpContext, p: ResourceCreateParams) -> OpResult:
        # Today's order: the ID format, then the limits, then the board.
        if p.id and not validate_id(p.id, "res"):
            raise OpError("INVALID_ID", f"Invalid resource ID format: '{p.id}'.")
        if p.max_holders < 1:
            raise OpError("VALIDATION_ERROR", "--max-holders must be at least 1.")
        if p.ttl < 1:
            raise OpError("VALIDATION_ERROR", "--ttl must be at least 1 second.")

        name = p.name
        with resource_write_context(ctx.lattice_dir, name):
            existing = read_resource_snapshot(ctx.lattice_dir, name)
            if existing is not None:
                if p.id and existing.get("id") == p.id:
                    # Same ID, same resource: nothing to do.
                    return resource_common.result(
                        existing["id"], name, existing, [], idempotent=True
                    )
                raise OpError(
                    "CONFLICT",
                    f"Resource '{name}' already exists ({existing['id']}).",
                    {"resource": existing},
                )

            if p.id:
                for other in list_all_resources(ctx.lattice_dir):
                    if other.get("id") == p.id:
                        raise OpError(
                            "CONFLICT",
                            f"Resource ID '{p.id}' is already used by resource "
                            f"'{other.get('name')}'.",
                            {"resource": other},
                        )
            resource_id = p.id or generate_resource_id()

            data: dict = {"name": name, "max_holders": p.max_holders, "ttl_seconds": p.ttl}
            if p.description:
                data["description"] = p.description
            event = create_resource_event(
                "resource_created", resource_id, ctx.actor, data, **p.provenance()
            )
            snapshot = apply_resource_event_to_snapshot(None, event)
            resource_common.write(ctx, resource_id, name, [event], snapshot)

        return resource_common.result(resource_id, name, snapshot, [event])
