"""MCP tool registrations for Lattice — write and read operations."""

from __future__ import annotations

import json
import logging
import mimetypes
import shutil
from pathlib import Path
from typing import Annotated

from pydantic import Field

from lattice.core.artifacts import ARTIFACT_TYPES, create_artifact_metadata, serialize_artifact
from lattice.core.acceptance_criteria import (
    allocate_criterion_id,
    criterion_without_history,
    find_criterion,
    normalize_criterion_ids,
    normalize_outcome,
    validate_criterion_id,
)
from lattice.core.comments import (
    materialize_comments,
    validate_comment_body,
    validate_comment_for_delete,
    validate_comment_for_edit,
    validate_comment_for_react,
    validate_comment_for_reply,
    validate_emoji,
)
from lattice.core.config import (
    VALID_PRIORITIES,
    VALID_URGENCIES,
    configured_event_prefix,
    get_configured_roles,
    validate_completion_policy,
    validate_status,
    validate_task_type,
    validate_transition,
)
from lattice.core.events import (
    BUILTIN_EVENT_TYPES,
    create_event,
    get_actor_display,
    validate_custom_event_type,
)
from lattice.core.ids import (
    generate_artifact_id,
    generate_task_id,
    is_short_id,
    validate_actor,
    validate_id,
)
from lattice.core.relationships import RELATIONSHIP_TYPES, validate_relationship_type
from lattice.core.tasks import apply_event_to_snapshot
from lattice.mcp.server import mcp
from lattice.storage.fs import (
    atomic_write,
    ensure_artifact_dirs,
    find_root,
)
from lattice.storage.operations import (
    AuthoritativeLogError,
    TaskMutationDecision,
    discover_task_authorities,
    mutate_task,
    read_task_authority,
    scaffold_plan,
)
from lattice.storage.readers import read_task_events
from lattice.storage.short_ids import resolve_short_id

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _find_root(lattice_root: str | None = None) -> Path:
    """Resolve the lattice root directory, returning the .lattice/ path."""
    if lattice_root:
        root = Path(lattice_root)
        lattice_dir = root / ".lattice"
        if not lattice_dir.is_dir():
            raise ValueError(f"No .lattice/ directory found at {root}")
        return lattice_dir

    root = find_root()
    if root is None:
        raise ValueError("No .lattice/ directory found. Run 'lattice init' first.")
    return root / ".lattice"


def _load_config(lattice_dir: Path) -> dict:
    """Load config.json from the lattice directory."""
    return json.loads((lattice_dir / "config.json").read_text())


def _resolve_task_id(lattice_dir: Path, raw_id: str) -> str:
    """Resolve a short ID or ULID to the canonical task ULID."""
    if validate_id(raw_id, "task"):
        return raw_id

    if is_short_id(raw_id):
        normalized = raw_id.upper()
        ulid = resolve_short_id(lattice_dir, normalized)
        if ulid is not None:
            return ulid
        raise ValueError(f"Short ID '{normalized}' not found.")

    raise ValueError(f"Invalid task ID format: '{raw_id}'.")


def _read_snapshot(lattice_dir: Path, task_id: str) -> dict | None:
    """Read an active task's event-authoritative snapshot."""
    try:
        authority = read_task_authority(lattice_dir, task_id, allow_missing=True)
    except AuthoritativeLogError:
        return None
    if authority is None or authority.location != "active":
        return None
    return authority.snapshot


def _read_snapshot_or_error(lattice_dir: Path, task_id: str) -> dict:
    """Read a task snapshot or raise ValueError."""
    snapshot = _read_snapshot(lattice_dir, task_id)
    if snapshot is None:
        raise ValueError(f"Task {task_id} not found.")
    return snapshot


def _validate_actor(actor: str) -> None:
    """Validate actor format or raise ValueError."""
    if not validate_actor(actor):
        raise ValueError(
            f"Invalid actor format: '{actor}'. "
            "Expected prefix:identifier (e.g., human:atin, agent:claude)."
        )


def _read_events(lattice_dir: Path, task_id: str, is_archived: bool = False) -> list[dict]:
    """Read all events for a task from the JSONL log."""
    return read_task_events(lattice_dir, task_id, is_archived=is_archived)


# ---------------------------------------------------------------------------
# Write tools
# ---------------------------------------------------------------------------


@mcp.tool()
def lattice_create(
    title: Annotated[str, Field(description="Task title")],
    actor: Annotated[str, Field(description="Actor ID (e.g., agent:claude-opus-4, human:atin)")],
    task_type: Annotated[str, Field(description="Task type")] = "task",
    priority: Annotated[str, Field(description="Priority level")] = "medium",
    status: Annotated[
        str | None, Field(description="Initial status (default: from config)")
    ] = None,
    description: Annotated[str | None, Field(description="Task description")] = None,
    tags: Annotated[str | None, Field(description="Comma-separated tags")] = None,
    assigned_to: Annotated[str | None, Field(description="Assignee actor ID")] = None,
    task_id: Annotated[
        str | None, Field(description="Caller-supplied task ID for idempotency")
    ] = None,
    lattice_root: Annotated[
        str | None, Field(description="Path to project directory containing .lattice/")
    ] = None,
) -> dict:
    """Create a new Lattice task. Returns the task snapshot."""
    lattice_dir = _find_root(lattice_root)
    config = _load_config(lattice_dir)
    _validate_actor(actor)

    # Apply defaults
    if status is None:
        status = config.get("default_status", "backlog")
    if priority is None:
        priority = config.get("default_priority", "medium")

    # Validate inputs
    if not validate_status(config, status):
        valid = ", ".join(config.get("workflow", {}).get("statuses", []))
        raise ValueError(f"Invalid status: '{status}'. Valid statuses: {valid}.")
    if not validate_task_type(config, task_type):
        valid = ", ".join(config.get("task_types", []))
        raise ValueError(f"Invalid task type: '{task_type}'. Valid types: {valid}.")
    if priority not in VALID_PRIORITIES:
        valid = ", ".join(VALID_PRIORITIES)
        raise ValueError(f"Invalid priority: '{priority}'. Valid priorities: {valid}.")
    if assigned_to is not None and not validate_actor(assigned_to):
        raise ValueError(f"Invalid assigned-to format: '{assigned_to}'.")

    # Parse tags
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []

    # Generate or validate task ID
    if task_id is not None:
        if not validate_id(task_id, "task"):
            raise ValueError(f"Invalid task ID format: '{task_id}'.")
    else:
        task_id = generate_task_id()

    prefix = configured_event_prefix(config)

    requested_data: dict = {
        "title": title,
        "status": status,
        "type": task_type,
        "priority": priority,
    }
    if description is not None:
        requested_data["description"] = description
    if tag_list:
        requested_data["tags"] = tag_list
    if assigned_to is not None:
        requested_data["assigned_to"] = assigned_to

    compare_fields = (
        "title",
        "type",
        "priority",
        "status",
        "description",
        "tags",
        "assigned_to",
    )

    def decide(context):  # noqa: ANN001, ANN202
        if context.snapshot is not None:
            existing_data = {name: context.events[0]["data"].get(name) for name in compare_fields}
            new_data = {name: requested_data.get(name) for name in compare_fields}
            existing_data["tags"] = existing_data.get("tags") or []
            new_data["tags"] = new_data.get("tags") or []
            if existing_data != new_data:
                raise ValueError(f"Conflict: task {task_id} exists with different data.")
            return TaskMutationDecision(idempotent=True)
        event_data = dict(requested_data)
        if context.reserved_short_id is not None:
            event_data["short_id"] = context.reserved_short_id
        event = create_event(type="task_created", task_id=task_id, actor=actor, data=event_data)
        return TaskMutationDecision(events=[event])

    snapshot = mutate_task(
        lattice_dir,
        task_id,
        decide,
        config,
        source="absent",
        may_emit_lifecycle=True,
        project_prefix=prefix,
    ).snapshot
    short_id = snapshot.get("short_id")

    # Scaffold plan file
    scaffold_plan(lattice_dir, task_id, title, short_id, description)

    return snapshot


@mcp.tool()
def lattice_criterion_add(
    task_id: Annotated[str, Field(description="Task ID (ULID or short ID)")],
    outcome: Annotated[str, Field(description="Observable outcome prose")],
    actor: Annotated[str, Field(description="Actor ID")],
    criterion_id: Annotated[
        str | None, Field(description="Optional explicit task-local criterion ID")
    ] = None,
    lattice_root: Annotated[
        str | None, Field(description="Path to project directory containing .lattice/")
    ] = None,
) -> dict:
    """Add an optional task-local acceptance criterion."""
    lattice_dir = _find_root(lattice_root)
    config = _load_config(lattice_dir)
    _validate_actor(actor)
    task_id = _resolve_task_id(lattice_dir, task_id)
    outcome = normalize_outcome(outcome)
    if criterion_id is not None:
        validate_criterion_id(criterion_id)

    def decide(context):  # noqa: ANN001, ANN202
        snapshot = context.snapshot
        assert snapshot is not None
        chosen_id = criterion_id or allocate_criterion_id(snapshot.get("acceptance_criteria", []))
        existing = find_criterion(snapshot, chosen_id)
        if existing is not None:
            if criterion_id is not None and existing["revisions"][0]["outcome"] == outcome:
                return TaskMutationDecision(value=chosen_id, idempotent=True)
            raise ValueError(
                f"Acceptance criterion {chosen_id} already exists with different initial prose."
            )
        event = create_event(
            "acceptance_criterion_added",
            task_id,
            actor,
            {"criterion_id": chosen_id, "outcome": outcome, "revision": 1},
        )
        return TaskMutationDecision(events=[event], value=chosen_id)

    result = mutate_task(lattice_dir, task_id, decide, config)
    criterion = find_criterion(result.snapshot, result.callback_value)
    return {"task_id": task_id, "criterion": criterion, "snapshot": result.snapshot}


@mcp.tool()
def lattice_criterion_edit(
    task_id: Annotated[str, Field(description="Task ID (ULID or short ID)")],
    criterion_id: Annotated[str, Field(description="Task-local criterion ID")],
    outcome: Annotated[str, Field(description="Revised observable outcome prose")],
    actor: Annotated[str, Field(description="Actor ID")],
    lattice_root: Annotated[
        str | None, Field(description="Path to project directory containing .lattice/")
    ] = None,
) -> dict:
    """Revise an active task-local acceptance criterion."""
    lattice_dir = _find_root(lattice_root)
    config = _load_config(lattice_dir)
    _validate_actor(actor)
    task_id = _resolve_task_id(lattice_dir, task_id)
    validate_criterion_id(criterion_id)
    outcome = normalize_outcome(outcome)

    def decide(context):  # noqa: ANN001, ANN202
        snapshot = context.snapshot
        assert snapshot is not None
        criterion = find_criterion(snapshot, criterion_id)
        if criterion is None:
            raise ValueError(f"Acceptance criterion {criterion_id} not found.")
        if criterion["retired"]:
            raise ValueError(f"Acceptance criterion {criterion_id} is retired.")
        if criterion["outcome"] == outcome:
            return TaskMutationDecision(idempotent=True)
        event = create_event(
            "acceptance_criterion_edited",
            task_id,
            actor,
            {
                "criterion_id": criterion_id,
                "from_outcome": criterion["outcome"],
                "outcome": outcome,
                "revision": criterion["revision"] + 1,
            },
        )
        return TaskMutationDecision(events=[event])

    result = mutate_task(lattice_dir, task_id, decide, config)
    return {
        "task_id": task_id,
        "criterion": find_criterion(result.snapshot, criterion_id),
        "snapshot": result.snapshot,
    }


@mcp.tool()
def lattice_criterion_retire(
    task_id: Annotated[str, Field(description="Task ID (ULID or short ID)")],
    criterion_id: Annotated[str, Field(description="Task-local criterion ID")],
    actor: Annotated[str, Field(description="Actor ID")],
    lattice_root: Annotated[
        str | None, Field(description="Path to project directory containing .lattice/")
    ] = None,
) -> dict:
    """Retire a criterion without deleting its immutable history."""
    lattice_dir = _find_root(lattice_root)
    config = _load_config(lattice_dir)
    _validate_actor(actor)
    task_id = _resolve_task_id(lattice_dir, task_id)
    validate_criterion_id(criterion_id)

    def decide(context):  # noqa: ANN001, ANN202
        snapshot = context.snapshot
        assert snapshot is not None
        criterion = find_criterion(snapshot, criterion_id)
        if criterion is None:
            raise ValueError(f"Acceptance criterion {criterion_id} not found.")
        if criterion["retired"]:
            raise ValueError(f"Acceptance criterion {criterion_id} is already retired.")
        event = create_event(
            "acceptance_criterion_retired",
            task_id,
            actor,
            {"criterion_id": criterion_id, "revision": criterion["revision"]},
        )
        return TaskMutationDecision(events=[event])

    result = mutate_task(lattice_dir, task_id, decide, config)
    return {
        "task_id": task_id,
        "criterion": find_criterion(result.snapshot, criterion_id),
        "snapshot": result.snapshot,
    }


@mcp.tool()
def lattice_criteria(
    task_id: Annotated[str, Field(description="Task ID (ULID or short ID)")],
    include_retired: Annotated[bool, Field(description="Include retired criteria")] = False,
    include_history: Annotated[bool, Field(description="Include revision histories")] = False,
    lattice_root: Annotated[
        str | None, Field(description="Path to project directory containing .lattice/")
    ] = None,
) -> dict:
    """List criteria for an active or archived task."""
    lattice_dir = _find_root(lattice_root)
    task_id = _resolve_task_id(lattice_dir, task_id)
    authority = read_task_authority(lattice_dir, task_id, allow_missing=True)
    if authority is None:
        raise ValueError(f"Task {task_id} not found.")
    archived = authority.location == "archived"
    snapshot = authority.snapshot
    criteria = [
        criterion
        for criterion in snapshot.get("acceptance_criteria", [])
        if include_retired or not criterion["retired"]
    ]
    if not include_history:
        criteria = [criterion_without_history(criterion) for criterion in criteria]
    return {"task_id": task_id, "archived": archived, "criteria": criteria}


@mcp.tool()
def lattice_update(
    task_id: Annotated[str, Field(description="Task ID (ULID or short ID like LAT-42)")],
    actor: Annotated[str, Field(description="Actor ID")],
    fields: Annotated[
        dict,
        Field(
            description="Dict of field=value pairs to update (e.g., {'title': 'New title', 'priority': 'high'})"
        ),
    ],
    lattice_root: Annotated[
        str | None, Field(description="Path to project directory containing .lattice/")
    ] = None,
) -> dict:
    """Update task fields. Returns the updated snapshot."""
    lattice_dir = _find_root(lattice_root)
    config = _load_config(lattice_dir)
    _validate_actor(actor)
    task_id = _resolve_task_id(lattice_dir, task_id)
    if not fields:
        raise ValueError("No fields provided to update.")

    updatable = {"title", "description", "priority", "urgency", "type", "tags"}
    redirect = {
        "status": "Use lattice_status to change status.",
        "assigned_to": "Use lattice_assign to change assignment.",
    }

    from lattice.core.events import utc_now

    shared_ts = utc_now()
    normalized: list[tuple[str, object]] = []
    for field, value in fields.items():
        if field in redirect:
            raise ValueError(redirect[field])

        if field.startswith("custom_fields."):
            key = field[len("custom_fields.") :]
            if not key:
                raise ValueError("Invalid custom field: 'custom_fields.' requires a key name.")
            normalized.append((field, value))
            continue

        if field not in updatable:
            valid = ", ".join(sorted(updatable))
            raise ValueError(
                f"Unknown or non-updatable field: '{field}'. Updatable fields: {valid}. "
                "Use custom_fields.<key> for custom data."
            )

        # Validate enum fields
        if field == "priority" and value not in VALID_PRIORITIES:
            raise ValueError(f"Invalid priority: '{value}'. Valid: {', '.join(VALID_PRIORITIES)}.")
        if field == "urgency" and value not in VALID_URGENCIES:
            raise ValueError(f"Invalid urgency: '{value}'. Valid: {', '.join(VALID_URGENCIES)}.")
        if field == "type" and not validate_task_type(config, value):
            raise ValueError(
                f"Invalid task type: '{value}'. Valid: {', '.join(config.get('task_types', []))}."
            )

        if field == "tags":
            if isinstance(value, str):
                new_value = [t.strip() for t in value.split(",") if t.strip()]
            else:
                new_value = value
        else:
            new_value = value
        normalized.append((field, new_value))

    def decide(context):  # noqa: ANN001, ANN202
        snapshot = context.snapshot
        assert snapshot is not None
        events: list[dict] = []
        for field, new_value in normalized:
            if field.startswith("custom_fields."):
                old_value = (snapshot.get("custom_fields") or {}).get(
                    field[len("custom_fields.") :]
                )
            else:
                old_value = snapshot.get(field)
            comparable_old = old_value or [] if field == "tags" else old_value
            if comparable_old == new_value:
                continue
            event = create_event(
                type="field_updated",
                task_id=task_id,
                actor=actor,
                data={"field": field, "from": old_value, "to": new_value},
                ts=shared_ts,
            )
            events.append(event)
            snapshot = apply_event_to_snapshot(snapshot, event)
        return TaskMutationDecision(events=events, idempotent=not events)

    result = mutate_task(lattice_dir, task_id, decide, config)
    if result.idempotent:
        return {"message": "No changes", "snapshot": result.snapshot}
    return result.snapshot


@mcp.tool()
def lattice_status(
    task_id: Annotated[str, Field(description="Task ID (ULID or short ID)")],
    new_status: Annotated[str, Field(description="New status value")],
    actor: Annotated[str, Field(description="Actor ID")],
    force: Annotated[bool, Field(description="Force an invalid transition")] = False,
    reason: Annotated[str | None, Field(description="Reason for forced transition")] = None,
    lattice_root: Annotated[
        str | None, Field(description="Path to project directory containing .lattice/")
    ] = None,
) -> dict:
    """Change a task's status. Returns the updated snapshot."""
    lattice_dir = _find_root(lattice_root)
    config = _load_config(lattice_dir)
    _validate_actor(actor)
    task_id = _resolve_task_id(lattice_dir, task_id)
    if not validate_status(config, new_status):
        valid = ", ".join(config.get("workflow", {}).get("statuses", []))
        raise ValueError(f"Invalid status: '{new_status}'. Valid statuses: {valid}.")

    def decide(context):  # noqa: ANN001, ANN202
        snapshot = context.snapshot
        assert snapshot is not None
        current_status = snapshot["status"]
        if current_status == new_status:
            return TaskMutationDecision(idempotent=True)
        if not validate_transition(config, current_status, new_status):
            if not force:
                raise ValueError(
                    f"Invalid transition from {current_status} to {new_status}. "
                    "Set force=True and provide a reason to override."
                )
            if not reason:
                raise ValueError("reason is required when force=True.")
        policy_ok, policy_failures = validate_completion_policy(config, snapshot, new_status)
        if not policy_ok:
            if not force:
                raise ValueError(
                    f"Completion policy not satisfied: {'; '.join(policy_failures)}. "
                    "Set force=True and provide a reason to override."
                )
            if not reason:
                raise ValueError("reason is required when force=True.")
        event_data: dict = {"from": current_status, "to": new_status}
        if force:
            event_data["force"] = True
            event_data["reason"] = reason
        event = create_event(type="status_changed", task_id=task_id, actor=actor, data=event_data)
        return TaskMutationDecision(events=[event])

    result = mutate_task(lattice_dir, task_id, decide, config)
    if result.idempotent:
        return {"message": f"Already at status {new_status}", "snapshot": result.snapshot}
    return result.snapshot


@mcp.tool()
def lattice_assign(
    task_id: Annotated[str, Field(description="Task ID (ULID or short ID)")],
    assignee: Annotated[str, Field(description="Assignee actor ID (e.g., agent:claude-opus-4)")],
    actor: Annotated[str, Field(description="Actor performing the assignment")],
    lattice_root: Annotated[
        str | None, Field(description="Path to project directory containing .lattice/")
    ] = None,
) -> dict:
    """Assign a task to an actor. Returns the updated snapshot."""
    lattice_dir = _find_root(lattice_root)
    config = _load_config(lattice_dir)
    _validate_actor(actor)
    _validate_actor(assignee)
    task_id = _resolve_task_id(lattice_dir, task_id)

    def decide(context):  # noqa: ANN001, ANN202
        snapshot = context.snapshot
        assert snapshot is not None
        current_assigned = snapshot.get("assigned_to")
        if current_assigned == assignee:
            return TaskMutationDecision(idempotent=True)
        event = create_event(
            type="assignment_changed",
            task_id=task_id,
            actor=actor,
            data={"from": current_assigned, "to": assignee},
        )
        return TaskMutationDecision(events=[event])

    result = mutate_task(lattice_dir, task_id, decide, config)
    if result.idempotent:
        return {"message": f"Already assigned to {assignee}", "snapshot": result.snapshot}
    return result.snapshot


@mcp.tool()
def lattice_comment(
    task_id: Annotated[str, Field(description="Task ID (ULID or short ID)")],
    text: Annotated[str, Field(description="Comment text")],
    actor: Annotated[str, Field(description="Actor ID")],
    parent_id: Annotated[
        str | None,
        Field(description="Event ID of parent comment for threading (one-level only)"),
    ] = None,
    role: Annotated[
        str | None,
        Field(description="Role of this comment (e.g., 'review'). Satisfies completion policies."),
    ] = None,
    criterion_ids: Annotated[
        list[str] | None,
        Field(description="Optional task-local acceptance criterion IDs"),
    ] = None,
    lattice_root: Annotated[
        str | None, Field(description="Path to project directory containing .lattice/")
    ] = None,
) -> dict:
    """Add a comment to a task. Returns the updated snapshot."""
    lattice_dir = _find_root(lattice_root)
    config = _load_config(lattice_dir)
    _validate_actor(actor)
    task_id = _resolve_task_id(lattice_dir, task_id)
    text = validate_comment_body(text)

    # Validate role against configured completion policy roles
    if role is not None:
        configured_roles = get_configured_roles(config)
        if configured_roles and role not in configured_roles:
            raise ValueError(
                f"Unknown role: '{role}'. Valid roles: {', '.join(sorted(configured_roles))}."
            )

    def decide(context):  # noqa: ANN001, ANN202
        snapshot = context.snapshot
        assert snapshot is not None
        normalized_ids = normalize_criterion_ids(criterion_ids, snapshot=snapshot)
        event_data: dict = {"body": text}
        if parent_id is not None:
            validate_comment_for_reply(list(context.events), parent_id)
            event_data["parent_id"] = parent_id
        if role is not None:
            event_data["role"] = role
        if normalized_ids:
            event_data["criterion_ids"] = normalized_ids
        event = create_event(type="comment_added", task_id=task_id, actor=actor, data=event_data)
        return TaskMutationDecision(events=[event])

    return mutate_task(lattice_dir, task_id, decide, config).snapshot


@mcp.tool()
def lattice_link(
    source_id: Annotated[str, Field(description="Source task ID (ULID or short ID)")],
    relationship_type: Annotated[
        str,
        Field(
            description="Relationship type (blocks, depends_on, subtask_of, related_to, spawned_by, duplicate_of, supersedes)"
        ),
    ],
    target_id: Annotated[str, Field(description="Target task ID (ULID or short ID)")],
    actor: Annotated[str, Field(description="Actor ID")],
    note: Annotated[str | None, Field(description="Optional note for the relationship")] = None,
    lattice_root: Annotated[
        str | None, Field(description="Path to project directory containing .lattice/")
    ] = None,
) -> dict:
    """Create a relationship between two tasks. Returns the updated source snapshot."""
    lattice_dir = _find_root(lattice_root)
    config = _load_config(lattice_dir)
    _validate_actor(actor)
    source_id = _resolve_task_id(lattice_dir, source_id)
    target_id = _resolve_task_id(lattice_dir, target_id)

    if not validate_relationship_type(relationship_type):
        raise ValueError(
            f"Invalid relationship type: '{relationship_type}'. "
            f"Valid: {', '.join(sorted(RELATIONSHIP_TYPES))}."
        )

    if source_id == target_id:
        raise ValueError("Cannot create a relationship from a task to itself.")

    target_authority = read_task_authority(lattice_dir, target_id, allow_missing=True)
    if target_authority is None or target_authority.location != "active":
        raise ValueError(f"Target task {target_id} not found.")

    event_data: dict = {"type": relationship_type, "target_task_id": target_id}
    if note is not None:
        event_data["note"] = note

    def decide(context):  # noqa: ANN001, ANN202
        snapshot = context.snapshot
        assert snapshot is not None
        if any(
            rel["type"] == relationship_type and rel["target_task_id"] == target_id
            for rel in snapshot.get("relationships_out", [])
        ):
            raise ValueError(
                f"Duplicate: {relationship_type} relationship to {target_id} already exists."
            )
        event = create_event(
            type="relationship_added", task_id=source_id, actor=actor, data=event_data
        )
        return TaskMutationDecision(events=[event])

    return mutate_task(lattice_dir, source_id, decide, config).snapshot


@mcp.tool()
def lattice_unlink(
    source_id: Annotated[str, Field(description="Source task ID (ULID or short ID)")],
    relationship_type: Annotated[str, Field(description="Relationship type to remove")],
    target_id: Annotated[str, Field(description="Target task ID (ULID or short ID)")],
    actor: Annotated[str, Field(description="Actor ID")],
    lattice_root: Annotated[
        str | None, Field(description="Path to project directory containing .lattice/")
    ] = None,
) -> dict:
    """Remove a relationship between two tasks. Returns the updated source snapshot."""
    lattice_dir = _find_root(lattice_root)
    config = _load_config(lattice_dir)
    _validate_actor(actor)
    source_id = _resolve_task_id(lattice_dir, source_id)
    target_id = _resolve_task_id(lattice_dir, target_id)

    if not validate_relationship_type(relationship_type):
        raise ValueError(
            f"Invalid relationship type: '{relationship_type}'. "
            f"Valid: {', '.join(sorted(RELATIONSHIP_TYPES))}."
        )

    event_data: dict = {"type": relationship_type, "target_task_id": target_id}

    def decide(context):  # noqa: ANN001, ANN202
        snapshot = context.snapshot
        assert snapshot is not None
        found = any(
            rel["type"] == relationship_type and rel["target_task_id"] == target_id
            for rel in snapshot.get("relationships_out", [])
        )
        if not found:
            raise ValueError(f"No {relationship_type} relationship to {target_id}.")
        event = create_event(
            type="relationship_removed", task_id=source_id, actor=actor, data=event_data
        )
        return TaskMutationDecision(events=[event])

    return mutate_task(lattice_dir, source_id, decide, config).snapshot


@mcp.tool()
def lattice_attach(
    task_id: Annotated[str, Field(description="Task ID (ULID or short ID)")],
    source: Annotated[str, Field(description="File path or URL to attach")],
    actor: Annotated[str, Field(description="Actor ID")],
    title: Annotated[str | None, Field(description="Artifact title")] = None,
    art_type: Annotated[
        str | None, Field(description="Artifact type (file, reference, conversation, prompt, log)")
    ] = None,
    summary: Annotated[str | None, Field(description="Short summary")] = None,
    role: Annotated[str | None, Field(description="Optional evidence role")] = None,
    criterion_ids: Annotated[
        list[str] | None,
        Field(description="Optional task-local acceptance criterion IDs"),
    ] = None,
    artifact_id: Annotated[
        str | None, Field(description="Caller-supplied artifact ID for retry")
    ] = None,
    lattice_root: Annotated[
        str | None, Field(description="Path to project directory containing .lattice/")
    ] = None,
) -> dict:
    """Attach a file or URL to a task as an artifact. Returns the artifact metadata."""
    lattice_dir = _find_root(lattice_root)
    config = _load_config(lattice_dir)
    _validate_actor(actor)
    task_id = _resolve_task_id(lattice_dir, task_id)

    def validate_target(context):  # noqa: ANN001, ANN202
        snapshot = context.snapshot
        assert snapshot is not None
        normalize_criterion_ids(criterion_ids, snapshot=snapshot)
        return TaskMutationDecision(idempotent=True)

    mutate_task(lattice_dir, task_id, validate_target, config)
    is_url = source.startswith("http://") or source.startswith("https://")

    if role is not None:
        configured_roles = get_configured_roles(config)
        if configured_roles and role not in configured_roles:
            raise ValueError(
                f"Unknown role: '{role}'. Valid roles: {', '.join(sorted(configured_roles))}."
            )

    if art_type is None:
        art_type = "reference" if is_url else "file"
    if art_type not in ARTIFACT_TYPES:
        raise ValueError(
            f"Invalid artifact type: '{art_type}'. Valid: {', '.join(sorted(ARTIFACT_TYPES))}."
        )

    art_id = artifact_id or generate_artifact_id()
    if not validate_id(art_id, "art"):
        raise ValueError(f"Invalid artifact ID format: '{art_id}'.")

    if title is None:
        title = source if is_url else Path(source).name

    # meta/ and payload/ are scaffolded at init but empty dirs aren't
    # git-tracked, so cloned installs may lack them (LAT-239).
    ensure_artifact_dirs(lattice_dir)

    # File handling
    content_type: str | None = None
    size_bytes: int | None = None
    payload_file: str | None = None
    custom_fields: dict | None = None

    meta_path = lattice_dir / "artifacts" / "meta" / f"{art_id}.json"
    existing_metadata = (
        json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else None
    )

    if existing_metadata is not None:
        conflict = (
            existing_metadata.get("type") != art_type or existing_metadata.get("title") != title
        )
        if is_url:
            conflict = conflict or (
                (existing_metadata.get("custom_fields") or {}).get("url") != source
            )
        else:
            expected_payload = f"{art_id}{Path(source).suffix}"
            conflict = conflict or (
                (existing_metadata.get("payload") or {}).get("file") != expected_payload
            )
        if conflict:
            raise ValueError(f"Conflict: artifact {art_id} exists with different data.")
    elif is_url:
        custom_fields = {"url": source}
    else:
        src_path = Path(source)
        if not src_path.is_file():
            raise ValueError(f"Source file not found: '{source}'.")
        dest_path = lattice_dir / "artifacts" / "payload" / f"{art_id}{src_path.suffix}"
        shutil.copy2(str(src_path), str(dest_path))
        guessed_type, _ = mimetypes.guess_type(src_path.name)
        content_type = guessed_type
        size_bytes = src_path.stat().st_size
        payload_file = f"{art_id}{src_path.suffix}"

    event_for_metadata = create_event(
        type="artifact_attached",
        task_id=task_id,
        actor=actor,
        data={"artifact_id": art_id},
    )

    metadata = existing_metadata or create_artifact_metadata(
        art_id,
        art_type,
        title,
        created_by=actor,
        created_at=event_for_metadata["ts"],
        summary=summary,
        payload_file=payload_file,
        content_type=content_type,
        size_bytes=size_bytes,
        custom_fields=custom_fields,
    )

    # Write artifact metadata
    if existing_metadata is None:
        atomic_write(meta_path, serialize_artifact(metadata))

    def decide(context):  # noqa: ANN001, ANN202
        snapshot = context.snapshot
        assert snapshot is not None
        normalized_ids = normalize_criterion_ids(criterion_ids, snapshot=snapshot)
        requested_key = (role, normalized_ids)
        for existing_event in context.events:
            if existing_event.get("type") != "artifact_attached":
                continue
            data = existing_event.get("data", {})
            if data.get("artifact_id") != art_id:
                continue
            if (data.get("role"), data.get("criterion_ids", [])) != requested_key:
                raise ValueError(
                    f"Artifact {art_id} is already attached with different task-local linkage."
                )
            return TaskMutationDecision(idempotent=True)
        event_data: dict = {"artifact_id": art_id}
        if role is not None:
            event_data["role"] = role
        if normalized_ids:
            event_data["criterion_ids"] = normalized_ids
        event = create_event(
            type="artifact_attached", task_id=task_id, actor=actor, data=event_data
        )
        return TaskMutationDecision(events=[event])

    mutate_task(lattice_dir, task_id, decide, config)
    return metadata


@mcp.tool()
def lattice_archive(
    task_id: Annotated[str, Field(description="Task ID (ULID or short ID)")],
    actor: Annotated[str, Field(description="Actor ID")],
    lattice_root: Annotated[
        str | None, Field(description="Path to project directory containing .lattice/")
    ] = None,
) -> dict:
    """Archive a task. Returns the archive event."""
    lattice_dir = _find_root(lattice_root)
    config = _load_config(lattice_dir)
    _validate_actor(actor)
    task_id = _resolve_task_id(lattice_dir, task_id)

    def decide(context):  # noqa: ANN001, ANN202
        if context.location == "archived":
            existing = next(
                event for event in reversed(context.events) if event["type"] == "task_archived"
            )
            return TaskMutationDecision(value=existing, idempotent=True)
        event = create_event(type="task_archived", task_id=task_id, actor=actor, data={})
        return TaskMutationDecision(events=[event], value=event)

    return mutate_task(
        lattice_dir,
        task_id,
        decide,
        config,
        source="either",
        destination="archived",
        may_emit_lifecycle=True,
    ).callback_value


@mcp.tool()
def lattice_unarchive(
    task_id: Annotated[str, Field(description="Task ID (ULID or short ID)")],
    actor: Annotated[str, Field(description="Actor ID")],
    lattice_root: Annotated[
        str | None, Field(description="Path to project directory containing .lattice/")
    ] = None,
) -> dict:
    """Restore an archived task to active status. Returns the unarchive event."""
    lattice_dir = _find_root(lattice_root)
    config = _load_config(lattice_dir)
    _validate_actor(actor)
    task_id = _resolve_task_id(lattice_dir, task_id)

    def decide(context):  # noqa: ANN001, ANN202
        if context.location == "active":
            existing = next(
                (
                    event
                    for event in reversed(context.events)
                    if event["type"] == "task_unarchived"
                ),
                context.events[0],
            )
            return TaskMutationDecision(value=existing, idempotent=True)
        event = create_event(type="task_unarchived", task_id=task_id, actor=actor, data={})
        return TaskMutationDecision(events=[event], value=event)

    return mutate_task(
        lattice_dir,
        task_id,
        decide,
        config,
        source="either",
        destination="active",
        may_emit_lifecycle=True,
    ).callback_value


def _validate_branch_name(branch: str) -> None:
    """Validate a branch name for safety.

    Rejects empty/whitespace-only names, names starting with ``-``
    (git flag injection), and names containing ASCII control characters.
    """
    if not branch or not branch.strip():
        raise ValueError("Branch name must not be empty or whitespace-only.")
    if branch.startswith("-"):
        raise ValueError(f"Branch name must not start with '-': '{branch}'.")
    if any(0 <= ord(c) <= 31 for c in branch):
        raise ValueError(f"Branch name must not contain control characters: '{branch!r}'.")


@mcp.tool()
def lattice_branch_link(
    task_id: Annotated[str, Field(description="Task ID (ULID or short ID)")],
    branch: Annotated[str, Field(description="Git branch name")],
    actor: Annotated[str, Field(description="Actor ID")],
    repo: Annotated[str | None, Field(description="Optional repository identifier")] = None,
    lattice_root: Annotated[
        str | None, Field(description="Path to project directory containing .lattice/")
    ] = None,
) -> dict:
    """Link a git branch to a task. Returns the updated snapshot."""
    # Input validation
    _validate_branch_name(branch)
    # Normalize empty repo to None
    if repo is not None and not repo.strip():
        repo = None

    lattice_dir = _find_root(lattice_root)
    config = _load_config(lattice_dir)
    _validate_actor(actor)
    task_id = _resolve_task_id(lattice_dir, task_id)
    _read_snapshot_or_error(lattice_dir, task_id)

    event_data: dict = {"branch": branch}
    if repo is not None:
        event_data["repo"] = repo

    def decide(context):  # noqa: ANN001, ANN202
        snapshot = context.snapshot
        assert snapshot is not None
        # Reject duplicates: same (branch, repo) pair
        for bl in snapshot.get("branch_links", []):
            if bl["branch"] == branch and bl.get("repo") == repo:
                repo_display = f" (repo: {repo})" if repo else ""
                raise ValueError(
                    f"Duplicate: branch '{branch}'{repo_display} already linked to {task_id}."
                )

        event = create_event(type="branch_linked", task_id=task_id, actor=actor, data=event_data)
        return TaskMutationDecision(events=[event])

    return mutate_task(lattice_dir, task_id, decide, config).snapshot


@mcp.tool()
def lattice_branch_unlink(
    task_id: Annotated[str, Field(description="Task ID (ULID or short ID)")],
    branch: Annotated[str, Field(description="Git branch name")],
    actor: Annotated[str, Field(description="Actor ID")],
    repo: Annotated[str | None, Field(description="Optional repository identifier")] = None,
    lattice_root: Annotated[
        str | None, Field(description="Path to project directory containing .lattice/")
    ] = None,
) -> dict:
    """Unlink a git branch from a task. Returns the updated snapshot."""
    # Input validation
    _validate_branch_name(branch)
    # Normalize empty repo to None
    if repo is not None and not repo.strip():
        repo = None

    lattice_dir = _find_root(lattice_root)
    config = _load_config(lattice_dir)
    _validate_actor(actor)
    task_id = _resolve_task_id(lattice_dir, task_id)
    _read_snapshot_or_error(lattice_dir, task_id)

    event_data: dict = {"branch": branch}
    if repo is not None:
        event_data["repo"] = repo

    def decide(context):  # noqa: ANN001, ANN202
        snapshot = context.snapshot
        assert snapshot is not None
        # Check the branch link exists
        found = False
        for bl in snapshot.get("branch_links", []):
            if bl["branch"] == branch and bl.get("repo") == repo:
                found = True
                break

        if not found:
            repo_display = f" (repo: {repo})" if repo else ""
            raise ValueError(f"No branch link '{branch}'{repo_display} on {task_id}.")

        event = create_event(type="branch_unlinked", task_id=task_id, actor=actor, data=event_data)
        return TaskMutationDecision(events=[event])

    return mutate_task(lattice_dir, task_id, decide, config).snapshot


@mcp.tool()
def lattice_event(
    task_id: Annotated[str, Field(description="Task ID (ULID or short ID)")],
    event_type: Annotated[str, Field(description="Custom event type (must start with x_)")],
    actor: Annotated[str, Field(description="Actor ID")],
    data: Annotated[dict | None, Field(description="Optional event data dict")] = None,
    lattice_root: Annotated[
        str | None, Field(description="Path to project directory containing .lattice/")
    ] = None,
) -> dict:
    """Record a custom event on a task. Event type must start with x_. Returns the event."""
    lattice_dir = _find_root(lattice_root)
    config = _load_config(lattice_dir)
    _validate_actor(actor)
    task_id = _resolve_task_id(lattice_dir, task_id)

    if event_type in BUILTIN_EVENT_TYPES:
        raise ValueError(
            f"Event type '{event_type}' is reserved. Custom types must start with 'x_'."
        )
    if not validate_custom_event_type(event_type):
        raise ValueError(
            f"Invalid custom event type: '{event_type}'. Custom types must start with 'x_'."
        )

    event_data = data if data is not None else {}

    def decide(context):  # noqa: ANN001, ANN202
        event = create_event(type=event_type, task_id=task_id, actor=actor, data=event_data)
        return TaskMutationDecision(events=[event], value=event)

    result = mutate_task(lattice_dir, task_id, decide, config)
    return result.callback_value


@mcp.tool()
def lattice_comment_edit(
    task_id: Annotated[str, Field(description="Task ID (ULID or short ID)")],
    comment_id: Annotated[str, Field(description="Event ID of the comment to edit")],
    new_text: Annotated[str, Field(description="New comment text")],
    actor: Annotated[str, Field(description="Actor ID")],
    role: Annotated[
        str | None,
        Field(description="Set or change the comment's evidence role"),
    ] = None,
    clear_role: Annotated[
        bool,
        Field(description="Remove the comment's role while preserving linked acceptance criteria"),
    ] = False,
    lattice_root: Annotated[
        str | None, Field(description="Path to project directory containing .lattice/")
    ] = None,
) -> dict:
    """Edit an existing comment body or role. Returns the updated snapshot."""
    lattice_dir = _find_root(lattice_root)
    config = _load_config(lattice_dir)
    _validate_actor(actor)
    task_id = _resolve_task_id(lattice_dir, task_id)
    new_text = validate_comment_body(new_text)

    if role is not None and clear_role:
        raise ValueError("role and clear_role are mutually exclusive.")
    if role is not None:
        configured_roles = get_configured_roles(config)
        if configured_roles and role not in configured_roles:
            raise ValueError(
                f"Unknown role: '{role}'. Valid roles: {', '.join(sorted(configured_roles))}."
            )

    def decide(context):  # noqa: ANN001, ANN202
        previous_body, previous_role = validate_comment_for_edit(list(context.events), comment_id)
        role_requested = role is not None or clear_role
        target_role = None if clear_role else role
        if previous_body == new_text and (not role_requested or previous_role == target_role):
            return TaskMutationDecision(idempotent=True)
        event_data: dict = {
            "comment_id": comment_id,
            "body": new_text,
            "previous_body": previous_body,
        }
        if role_requested:
            event_data["role"] = target_role
            if previous_role != target_role:
                event_data["previous_role"] = previous_role
        event = create_event(
            type="comment_edited",
            task_id=task_id,
            actor=actor,
            data=event_data,
        )
        return TaskMutationDecision(events=[event])

    return mutate_task(lattice_dir, task_id, decide, config).snapshot


@mcp.tool()
def lattice_comment_delete(
    task_id: Annotated[str, Field(description="Task ID (ULID or short ID)")],
    comment_id: Annotated[str, Field(description="Event ID of the comment to delete")],
    actor: Annotated[str, Field(description="Actor ID")],
    lattice_root: Annotated[
        str | None, Field(description="Path to project directory containing .lattice/")
    ] = None,
) -> dict:
    """Soft-delete a comment on a task. Returns the updated snapshot."""
    lattice_dir = _find_root(lattice_root)
    config = _load_config(lattice_dir)
    _validate_actor(actor)
    task_id = _resolve_task_id(lattice_dir, task_id)

    def decide(context):  # noqa: ANN001, ANN202
        validate_comment_for_delete(list(context.events), comment_id)
        event = create_event(
            type="comment_deleted",
            task_id=task_id,
            actor=actor,
            data={"comment_id": comment_id},
        )
        return TaskMutationDecision(events=[event])

    return mutate_task(lattice_dir, task_id, decide, config).snapshot


@mcp.tool()
def lattice_react(
    task_id: Annotated[str, Field(description="Task ID (ULID or short ID)")],
    comment_id: Annotated[str, Field(description="Event ID of the comment to react to")],
    emoji: Annotated[
        str, Field(description="Reaction emoji (alphanumeric, underscores, hyphens)")
    ],
    actor: Annotated[str, Field(description="Actor ID")],
    lattice_root: Annotated[
        str | None, Field(description="Path to project directory containing .lattice/")
    ] = None,
) -> dict:
    """Add a reaction to a comment. Idempotent — duplicate reactions are no-ops. Returns the updated snapshot."""
    lattice_dir = _find_root(lattice_root)
    config = _load_config(lattice_dir)
    _validate_actor(actor)
    task_id = _resolve_task_id(lattice_dir, task_id)
    if not validate_emoji(emoji):
        raise ValueError(
            f"Invalid emoji: '{emoji}'. Must be 1-50 alphanumeric, underscore, or hyphen characters."
        )

    def decide(context):  # noqa: ANN001, ANN202
        events = list(context.events)
        validate_comment_for_react(events, comment_id)
        for comment in materialize_comments(events):
            candidates = [comment, *comment.get("replies", [])]
            if any(
                candidate["id"] == comment_id
                and actor in candidate.get("reactions", {}).get(emoji, [])
                for candidate in candidates
            ):
                return TaskMutationDecision(idempotent=True)
        event = create_event(
            type="reaction_added",
            task_id=task_id,
            actor=actor,
            data={"comment_id": comment_id, "emoji": emoji},
        )
        return TaskMutationDecision(events=[event])

    result = mutate_task(lattice_dir, task_id, decide, config)
    if result.idempotent:
        return {"message": "Reaction already exists", "snapshot": result.snapshot}
    return result.snapshot


@mcp.tool()
def lattice_unreact(
    task_id: Annotated[str, Field(description="Task ID (ULID or short ID)")],
    comment_id: Annotated[
        str, Field(description="Event ID of the comment to remove reaction from")
    ],
    emoji: Annotated[str, Field(description="Reaction emoji to remove")],
    actor: Annotated[str, Field(description="Actor ID")],
    lattice_root: Annotated[
        str | None, Field(description="Path to project directory containing .lattice/")
    ] = None,
) -> dict:
    """Remove a reaction from a comment. Returns the updated snapshot."""
    lattice_dir = _find_root(lattice_root)
    config = _load_config(lattice_dir)
    _validate_actor(actor)
    task_id = _resolve_task_id(lattice_dir, task_id)
    if not validate_emoji(emoji):
        raise ValueError(
            f"Invalid emoji: '{emoji}'. Must be 1-50 alphanumeric, underscore, or hyphen characters."
        )

    def decide(context):  # noqa: ANN001, ANN202
        events = list(context.events)
        validate_comment_for_react(events, comment_id)
        found = any(
            candidate["id"] == comment_id
            and actor in candidate.get("reactions", {}).get(emoji, [])
            for comment in materialize_comments(events)
            for candidate in [comment, *comment.get("replies", [])]
        )
        if not found:
            raise ValueError(f"No '{emoji}' reaction by {actor} on comment {comment_id}.")
        event = create_event(
            type="reaction_removed",
            task_id=task_id,
            actor=actor,
            data={"comment_id": comment_id, "emoji": emoji},
        )
        return TaskMutationDecision(events=[event])

    return mutate_task(lattice_dir, task_id, decide, config).snapshot


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------


@mcp.tool()
def lattice_comments(
    task_id: Annotated[str, Field(description="Task ID (ULID or short ID)")],
    lattice_root: Annotated[
        str | None, Field(description="Path to project directory containing .lattice/")
    ] = None,
) -> list[dict]:
    """List comments on a task with threading, edit history, and reactions. Returns materialized comment tree."""
    lattice_dir = _find_root(lattice_root)
    task_id = _resolve_task_id(lattice_dir, task_id)

    authority = read_task_authority(lattice_dir, task_id, allow_missing=True)
    if authority is None:
        raise ValueError(f"Task {task_id} not found.")
    return materialize_comments(list(authority.events))


@mcp.tool()
def lattice_list(
    status: Annotated[str | None, Field(description="Filter by status")] = None,
    assigned: Annotated[str | None, Field(description="Filter by assignee")] = None,
    tag: Annotated[str | None, Field(description="Filter by tag")] = None,
    task_type: Annotated[str | None, Field(description="Filter by task type")] = None,
    priority: Annotated[str | None, Field(description="Filter by priority")] = None,
    lattice_root: Annotated[
        str | None, Field(description="Path to project directory containing .lattice/")
    ] = None,
) -> list[dict]:
    """List active Lattice tasks with optional filters. Returns list of task snapshots."""
    lattice_dir = _find_root(lattice_root)
    snapshots = [
        authority.snapshot
        for authority in discover_task_authorities(lattice_dir, include_archived=False)
    ]

    filtered: list[dict] = []
    for snap in snapshots:
        if status is not None and snap.get("status") != status:
            continue
        if assigned is not None:
            raw = snap.get("assigned_to")
            if raw is None or get_actor_display(raw) != assigned:
                continue
        if tag is not None and tag not in (snap.get("tags") or []):
            continue
        if task_type is not None and snap.get("type") != task_type:
            continue
        if priority is not None and snap.get("priority") != priority:
            continue
        filtered.append(snap)

    filtered.sort(key=lambda s: s.get("id", ""))
    return filtered


@mcp.tool()
def lattice_show(
    task_id: Annotated[str, Field(description="Task ID (ULID or short ID)")],
    include_events: Annotated[bool, Field(description="Include event history")] = True,
    lattice_root: Annotated[
        str | None, Field(description="Path to project directory containing .lattice/")
    ] = None,
) -> dict:
    """Show detailed task information including events. Returns full task data."""
    lattice_dir = _find_root(lattice_root)
    task_id = _resolve_task_id(lattice_dir, task_id)

    authority = read_task_authority(lattice_dir, task_id, allow_missing=True)
    if authority is None:
        raise ValueError(f"Task {task_id} not found.")
    snapshot = authority.snapshot
    is_archived = authority.location == "archived"

    result: dict = dict(snapshot)
    if is_archived:
        result["archived"] = True

    if include_events:
        result["events"] = list(authority.events)

    # Check for notes
    if is_archived:
        notes_path = lattice_dir / "archive" / "notes" / f"{task_id}.md"
    else:
        notes_path = lattice_dir / "notes" / f"{task_id}.md"
    if notes_path.exists():
        result["notes_path"] = f"notes/{task_id}.md"

    # Check for plan
    if is_archived:
        plan_path = lattice_dir / "archive" / "plans" / f"{task_id}.md"
    else:
        plan_path = lattice_dir / "plans" / f"{task_id}.md"
    if plan_path.exists():
        result["plan_path"] = f"plans/{task_id}.md"

    return result


@mcp.tool()
def lattice_config(
    lattice_root: Annotated[
        str | None, Field(description="Path to project directory containing .lattice/")
    ] = None,
) -> dict:
    """Read the Lattice project configuration. Returns the config.json contents."""
    lattice_dir = _find_root(lattice_root)
    return _load_config(lattice_dir)


@mcp.tool()
def lattice_doctor(
    fix: Annotated[bool, Field(description="Attempt to fix issues")] = False,
    lattice_root: Annotated[
        str | None, Field(description="Path to project directory containing .lattice/")
    ] = None,
) -> dict:
    """Check Lattice data integrity. Returns a diagnostic report."""
    lattice_dir = _find_root(lattice_root)
    issues: list[dict] = []

    # Check config
    config_path = lattice_dir / "config.json"
    if not config_path.exists():
        issues.append({"level": "error", "message": "config.json not found"})
    else:
        try:
            json.loads(config_path.read_text())
        except json.JSONDecodeError as e:
            issues.append({"level": "error", "message": f"config.json is invalid JSON: {e}"})

    # Check required directories
    for subdir in [
        "tasks",
        "events",
        "artifacts/meta",
        "artifacts/payload",
        "notes",
        "archive/tasks",
        "archive/events",
        "archive/notes",
        "locks",
    ]:
        if not (lattice_dir / subdir).is_dir():
            msg = f"Missing directory: {subdir}"
            issues.append({"level": "warning", "message": msg})

    from lattice.cli.integrity_cmds import inspect_task_authority

    authorities, authority_findings = inspect_task_authority(lattice_dir)
    issues.extend(
        {
            "level": finding["level"],
            "message": finding["message"],
            "check": finding["check"],
            "task_id": finding.get("task_id"),
        }
        for finding in authority_findings
    )

    return {
        "ok": len([i for i in issues if i["level"] == "error"]) == 0,
        "issues": issues,
        "task_count": len(authorities),
        "archived_count": sum(
            1 for authority in authorities.values() if authority.location == "archived"
        ),
    }
