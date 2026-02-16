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
from lattice.core.config import (
    VALID_PRIORITIES,
    VALID_URGENCIES,
    validate_status,
    validate_task_type,
    validate_transition,
)
from lattice.core.events import (
    BUILTIN_EVENT_TYPES,
    create_event,
    serialize_event,
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
from lattice.core.tasks import apply_event_to_snapshot, serialize_snapshot
from lattice.mcp.server import mcp
from lattice.storage.fs import atomic_write, find_root, jsonl_append
from lattice.storage.hooks import execute_hooks
from lattice.storage.locks import multi_lock
from lattice.storage.operations import scaffold_notes, write_task_event
from lattice.storage.short_ids import allocate_short_id, resolve_short_id

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
    """Read a task snapshot, returning None if not found."""
    path = lattice_dir / "tasks" / f"{task_id}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


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
    if is_archived:
        event_path = lattice_dir / "archive" / "events" / f"{task_id}.jsonl"
    else:
        event_path = lattice_dir / "events" / f"{task_id}.jsonl"

    events: list[dict] = []
    if event_path.exists():
        for line in event_path.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return events


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
        # Idempotency check with payload comparison (matches CLI behavior)
        existing_path = lattice_dir / "tasks" / f"{task_id}.json"
        if existing_path.exists():
            existing = json.loads(existing_path.read_text())
            _compare_fields = (
                "title",
                "type",
                "priority",
                "status",
                "description",
                "tags",
                "assigned_to",
            )
            new_data = {
                "title": title,
                "type": task_type,
                "priority": priority,
                "status": status,
                "description": description,
                "tags": tag_list,
                "assigned_to": assigned_to,
            }
            existing_data = {f: existing.get(f) for f in _compare_fields}
            if existing_data.get("tags") is None:
                existing_data["tags"] = []
            if new_data == existing_data:
                return existing
            raise ValueError(f"Conflict: task {task_id} exists with different data.")
    else:
        task_id = generate_task_id()

    # Allocate short ID if project code is configured
    project_code = config.get("project_code")
    subproject_code = config.get("subproject_code")
    short_id: str | None = None
    if project_code:
        prefix = f"{project_code}-{subproject_code}" if subproject_code else project_code
        short_id, _idx = allocate_short_id(lattice_dir, prefix, task_ulid=task_id)

    # Build event data
    event_data: dict = {
        "title": title,
        "status": status,
        "type": task_type,
        "priority": priority,
    }
    if description is not None:
        event_data["description"] = description
    if tag_list:
        event_data["tags"] = tag_list
    if assigned_to is not None:
        event_data["assigned_to"] = assigned_to
    if short_id is not None:
        event_data["short_id"] = short_id

    # Build event and snapshot
    event = create_event(type="task_created", task_id=task_id, actor=actor, data=event_data)
    snapshot = apply_event_to_snapshot(None, event)

    # Write (event-first, then snapshot, under lock)
    write_task_event(lattice_dir, task_id, [event], snapshot, config)

    # Scaffold notes file
    scaffold_notes(lattice_dir, task_id, title, short_id, description)

    return snapshot


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
    snapshot = _read_snapshot_or_error(lattice_dir, task_id)

    if not fields:
        raise ValueError("No fields provided to update.")

    updatable = {"title", "description", "priority", "urgency", "type", "tags"}
    redirect = {
        "status": "Use lattice_status to change status.",
        "assigned_to": "Use lattice_assign to change assignment.",
    }

    from lattice.core.events import utc_now

    shared_ts = utc_now()
    events: list[dict] = []

    for field, value in fields.items():
        if field in redirect:
            raise ValueError(redirect[field])

        if field.startswith("custom_fields."):
            key = field[len("custom_fields.") :]
            if not key:
                raise ValueError("Invalid custom field: 'custom_fields.' requires a key name.")
            old_value = (snapshot.get("custom_fields") or {}).get(key)
            if old_value == value:
                continue
            events.append(
                create_event(
                    type="field_updated",
                    task_id=task_id,
                    actor=actor,
                    data={"field": field, "from": old_value, "to": value},
                    ts=shared_ts,
                )
            )
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
            old_value = snapshot.get("tags") or []
        else:
            new_value = value
            old_value = snapshot.get(field)

        if old_value == new_value:
            continue

        events.append(
            create_event(
                type="field_updated",
                task_id=task_id,
                actor=actor,
                data={"field": field, "from": old_value, "to": new_value},
                ts=shared_ts,
            )
        )

    if not events:
        return {"message": "No changes", "snapshot": snapshot}

    updated_snapshot = snapshot
    for event in events:
        updated_snapshot = apply_event_to_snapshot(updated_snapshot, event)

    write_task_event(lattice_dir, task_id, events, updated_snapshot, config)
    return updated_snapshot


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
    snapshot = _read_snapshot_or_error(lattice_dir, task_id)
    current_status = snapshot["status"]

    if not validate_status(config, new_status):
        valid = ", ".join(config.get("workflow", {}).get("statuses", []))
        raise ValueError(f"Invalid status: '{new_status}'. Valid statuses: {valid}.")

    if current_status == new_status:
        return {"message": f"Already at status {new_status}", "snapshot": snapshot}

    if not validate_transition(config, current_status, new_status):
        if not force:
            raise ValueError(
                f"Invalid transition from {current_status} to {new_status}. "
                "Set force=True and provide a reason to override."
            )
        if not reason:
            raise ValueError("reason is required when force=True.")

    event_data: dict = {"from": current_status, "to": new_status}
    if force:
        event_data["force"] = True
        event_data["reason"] = reason

    event = create_event(type="status_changed", task_id=task_id, actor=actor, data=event_data)
    updated_snapshot = apply_event_to_snapshot(snapshot, event)
    write_task_event(lattice_dir, task_id, [event], updated_snapshot, config)
    return updated_snapshot


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
    snapshot = _read_snapshot_or_error(lattice_dir, task_id)
    current_assigned = snapshot.get("assigned_to")

    if current_assigned == assignee:
        return {"message": f"Already assigned to {assignee}", "snapshot": snapshot}

    event = create_event(
        type="assignment_changed",
        task_id=task_id,
        actor=actor,
        data={"from": current_assigned, "to": assignee},
    )
    updated_snapshot = apply_event_to_snapshot(snapshot, event)
    write_task_event(lattice_dir, task_id, [event], updated_snapshot, config)
    return updated_snapshot


@mcp.tool()
def lattice_comment(
    task_id: Annotated[str, Field(description="Task ID (ULID or short ID)")],
    text: Annotated[str, Field(description="Comment text")],
    actor: Annotated[str, Field(description="Actor ID")],
    lattice_root: Annotated[
        str | None, Field(description="Path to project directory containing .lattice/")
    ] = None,
) -> dict:
    """Add a comment to a task. Returns the updated snapshot."""
    lattice_dir = _find_root(lattice_root)
    config = _load_config(lattice_dir)
    _validate_actor(actor)
    task_id = _resolve_task_id(lattice_dir, task_id)
    snapshot = _read_snapshot_or_error(lattice_dir, task_id)

    event = create_event(type="comment_added", task_id=task_id, actor=actor, data={"body": text})
    updated_snapshot = apply_event_to_snapshot(snapshot, event)
    write_task_event(lattice_dir, task_id, [event], updated_snapshot, config)
    return updated_snapshot


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

    snapshot = _read_snapshot_or_error(lattice_dir, source_id)

    # Check target exists
    if not (lattice_dir / "tasks" / f"{target_id}.json").exists():
        raise ValueError(f"Target task {target_id} not found.")

    # Reject duplicates
    for rel in snapshot.get("relationships_out", []):
        if rel["type"] == relationship_type and rel["target_task_id"] == target_id:
            raise ValueError(
                f"Duplicate: {relationship_type} relationship to {target_id} already exists."
            )

    event_data: dict = {"type": relationship_type, "target_task_id": target_id}
    if note is not None:
        event_data["note"] = note

    event = create_event(
        type="relationship_added", task_id=source_id, actor=actor, data=event_data
    )
    updated_snapshot = apply_event_to_snapshot(snapshot, event)
    write_task_event(lattice_dir, source_id, [event], updated_snapshot, config)
    return updated_snapshot


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

    snapshot = _read_snapshot_or_error(lattice_dir, source_id)

    found = False
    for rel in snapshot.get("relationships_out", []):
        if rel["type"] == relationship_type and rel["target_task_id"] == target_id:
            found = True
            break

    if not found:
        raise ValueError(f"No {relationship_type} relationship to {target_id}.")

    event_data: dict = {"type": relationship_type, "target_task_id": target_id}
    event = create_event(
        type="relationship_removed", task_id=source_id, actor=actor, data=event_data
    )
    updated_snapshot = apply_event_to_snapshot(snapshot, event)
    write_task_event(lattice_dir, source_id, [event], updated_snapshot, config)
    return updated_snapshot


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
    lattice_root: Annotated[
        str | None, Field(description="Path to project directory containing .lattice/")
    ] = None,
) -> dict:
    """Attach a file or URL to a task as an artifact. Returns the artifact metadata."""
    lattice_dir = _find_root(lattice_root)
    config = _load_config(lattice_dir)
    _validate_actor(actor)
    task_id = _resolve_task_id(lattice_dir, task_id)
    snapshot = _read_snapshot_or_error(lattice_dir, task_id)

    is_url = source.startswith("http://") or source.startswith("https://")

    if art_type is None:
        art_type = "reference" if is_url else "file"
    if art_type not in ARTIFACT_TYPES:
        raise ValueError(
            f"Invalid artifact type: '{art_type}'. Valid: {', '.join(sorted(ARTIFACT_TYPES))}."
        )

    art_id = generate_artifact_id()

    if title is None:
        title = source if is_url else Path(source).name

    # File handling
    content_type: str | None = None
    size_bytes: int | None = None
    payload_file: str | None = None
    custom_fields: dict | None = None

    if is_url:
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

    event_data: dict = {"artifact_id": art_id}
    event = create_event(type="artifact_attached", task_id=task_id, actor=actor, data=event_data)

    metadata = create_artifact_metadata(
        art_id,
        art_type,
        title,
        created_by=actor,
        created_at=event["ts"],
        summary=summary,
        payload_file=payload_file,
        content_type=content_type,
        size_bytes=size_bytes,
        custom_fields=custom_fields,
    )

    # Write artifact metadata
    meta_path = lattice_dir / "artifacts" / "meta" / f"{art_id}.json"
    atomic_write(meta_path, serialize_artifact(metadata))

    # Apply event and write
    updated_snapshot = apply_event_to_snapshot(snapshot, event)
    write_task_event(lattice_dir, task_id, [event], updated_snapshot, config)
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

    snapshot = _read_snapshot(lattice_dir, task_id)
    if snapshot is None:
        archive_path = lattice_dir / "archive" / "tasks" / f"{task_id}.json"
        if archive_path.exists():
            raise ValueError(f"Task {task_id} is already archived.")
        raise ValueError(f"Task {task_id} not found.")

    event = create_event(type="task_archived", task_id=task_id, actor=actor, data={})
    updated_snapshot = apply_event_to_snapshot(snapshot, event)

    locks_dir = lattice_dir / "locks"
    lock_keys = sorted([f"events_{task_id}", f"tasks_{task_id}", "events__lifecycle"])

    with multi_lock(locks_dir, lock_keys):
        event_path = lattice_dir / "events" / f"{task_id}.jsonl"
        jsonl_append(event_path, serialize_event(event))

        lifecycle_path = lattice_dir / "events" / "_lifecycle.jsonl"
        jsonl_append(lifecycle_path, serialize_event(event))

        atomic_write(
            lattice_dir / "archive" / "tasks" / f"{task_id}.json",
            serialize_snapshot(updated_snapshot),
        )

        snapshot_path = lattice_dir / "tasks" / f"{task_id}.json"
        if snapshot_path.exists():
            snapshot_path.unlink()

        shutil.move(
            str(event_path),
            str(lattice_dir / "archive" / "events" / f"{task_id}.jsonl"),
        )

        notes_path = lattice_dir / "notes" / f"{task_id}.md"
        if notes_path.exists():
            shutil.move(
                str(notes_path),
                str(lattice_dir / "archive" / "notes" / f"{task_id}.md"),
            )

    # Fire hooks after locks released
    execute_hooks(config, lattice_dir, task_id, event)

    return event


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

    active_path = lattice_dir / "tasks" / f"{task_id}.json"
    if active_path.exists():
        raise ValueError(f"Task {task_id} is already active.")

    archive_snapshot_path = lattice_dir / "archive" / "tasks" / f"{task_id}.json"
    if not archive_snapshot_path.exists():
        raise ValueError(f"Task {task_id} not found in archive.")

    snapshot = json.loads(archive_snapshot_path.read_text())
    event = create_event(type="task_unarchived", task_id=task_id, actor=actor, data={})
    updated_snapshot = apply_event_to_snapshot(snapshot, event)

    locks_dir = lattice_dir / "locks"
    lock_keys = sorted([f"events_{task_id}", f"tasks_{task_id}", "events__lifecycle"])

    with multi_lock(locks_dir, lock_keys):
        archive_event_path = lattice_dir / "archive" / "events" / f"{task_id}.jsonl"
        jsonl_append(archive_event_path, serialize_event(event))

        lifecycle_path = lattice_dir / "events" / "_lifecycle.jsonl"
        jsonl_append(lifecycle_path, serialize_event(event))

        shutil.move(
            str(archive_event_path),
            str(lattice_dir / "events" / f"{task_id}.jsonl"),
        )

        atomic_write(
            lattice_dir / "tasks" / f"{task_id}.json",
            serialize_snapshot(updated_snapshot),
        )

        archive_snapshot_path.unlink()

        archive_notes_path = lattice_dir / "archive" / "notes" / f"{task_id}.md"
        if archive_notes_path.exists():
            shutil.move(
                str(archive_notes_path),
                str(lattice_dir / "notes" / f"{task_id}.md"),
            )

    # Fire hooks after locks released
    execute_hooks(config, lattice_dir, task_id, event)

    return event


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

    event_data: dict = {"branch": branch}
    if repo is not None:
        event_data["repo"] = repo

    event = create_event(type="branch_linked", task_id=task_id, actor=actor, data=event_data)

    # Acquire lock, then read snapshot + check + write atomically
    locks_dir = lattice_dir / "locks"
    lock_keys = sorted([f"events_{task_id}", f"tasks_{task_id}"])

    with multi_lock(locks_dir, lock_keys):
        snapshot = _read_snapshot_or_error(lattice_dir, task_id)

        # Reject duplicates: same (branch, repo) pair
        for bl in snapshot.get("branch_links", []):
            if bl["branch"] == branch and bl.get("repo") == repo:
                repo_display = f" (repo: {repo})" if repo else ""
                raise ValueError(
                    f"Duplicate: branch '{branch}'{repo_display} already linked to {task_id}."
                )

        updated_snapshot = apply_event_to_snapshot(snapshot, event)

        # Event-first write
        event_path = lattice_dir / "events" / f"{task_id}.jsonl"
        jsonl_append(event_path, serialize_event(event))

        snapshot_path = lattice_dir / "tasks" / f"{task_id}.json"
        atomic_write(snapshot_path, serialize_snapshot(updated_snapshot))

    # Fire hooks after locks released
    if config:
        execute_hooks(config, lattice_dir, task_id, event)

    return updated_snapshot


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

    event_data: dict = {"branch": branch}
    if repo is not None:
        event_data["repo"] = repo

    event = create_event(type="branch_unlinked", task_id=task_id, actor=actor, data=event_data)

    # Acquire lock, then read snapshot + check + write atomically
    locks_dir = lattice_dir / "locks"
    lock_keys = sorted([f"events_{task_id}", f"tasks_{task_id}"])

    with multi_lock(locks_dir, lock_keys):
        snapshot = _read_snapshot_or_error(lattice_dir, task_id)

        # Check the branch link exists
        found = False
        for bl in snapshot.get("branch_links", []):
            if bl["branch"] == branch and bl.get("repo") == repo:
                found = True
                break

        if not found:
            repo_display = f" (repo: {repo})" if repo else ""
            raise ValueError(f"No branch link '{branch}'{repo_display} on {task_id}.")

        updated_snapshot = apply_event_to_snapshot(snapshot, event)

        # Event-first write
        event_path = lattice_dir / "events" / f"{task_id}.jsonl"
        jsonl_append(event_path, serialize_event(event))

        snapshot_path = lattice_dir / "tasks" / f"{task_id}.json"
        atomic_write(snapshot_path, serialize_snapshot(updated_snapshot))

    # Fire hooks after locks released
    if config:
        execute_hooks(config, lattice_dir, task_id, event)

    return updated_snapshot


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
    snapshot = _read_snapshot_or_error(lattice_dir, task_id)

    event = create_event(type=event_type, task_id=task_id, actor=actor, data=event_data)
    updated_snapshot = apply_event_to_snapshot(snapshot, event)
    write_task_event(lattice_dir, task_id, [event], updated_snapshot, config)
    return event


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------


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
    tasks_dir = lattice_dir / "tasks"
    snapshots: list[dict] = []

    if tasks_dir.is_dir():
        for task_file in sorted(tasks_dir.glob("*.json")):
            try:
                snap = json.loads(task_file.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            snapshots.append(snap)

    filtered: list[dict] = []
    for snap in snapshots:
        if status is not None and snap.get("status") != status:
            continue
        if assigned is not None and snap.get("assigned_to") != assigned:
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

    snapshot = _read_snapshot(lattice_dir, task_id)
    is_archived = False

    if snapshot is None:
        archive_path = lattice_dir / "archive" / "tasks" / f"{task_id}.json"
        if archive_path.exists():
            snapshot = json.loads(archive_path.read_text())
            is_archived = True

    if snapshot is None:
        raise ValueError(f"Task {task_id} not found.")

    result: dict = dict(snapshot)
    if is_archived:
        result["archived"] = True

    if include_events:
        result["events"] = _read_events(lattice_dir, task_id, is_archived)

    # Check for notes
    if is_archived:
        notes_path = lattice_dir / "archive" / "notes" / f"{task_id}.md"
    else:
        notes_path = lattice_dir / "notes" / f"{task_id}.md"
    if notes_path.exists():
        result["notes_path"] = f"notes/{task_id}.md"

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
            if fix:
                (lattice_dir / subdir).mkdir(parents=True, exist_ok=True)
                msg += " (created)"
            issues.append({"level": "warning", "message": msg})

    # Check snapshots have matching event logs
    tasks_dir = lattice_dir / "tasks"
    if tasks_dir.is_dir():
        for snap_file in tasks_dir.glob("*.json"):
            tid = snap_file.stem
            event_file = lattice_dir / "events" / f"{tid}.jsonl"
            if not event_file.exists():
                issues.append(
                    {
                        "level": "warning",
                        "message": f"Task {tid} has snapshot but no event log",
                    }
                )

    # Check event logs have matching snapshots
    events_dir = lattice_dir / "events"
    if events_dir.is_dir():
        for event_file in events_dir.glob("*.jsonl"):
            if event_file.name.startswith("_"):
                continue
            tid = event_file.stem
            snap_file = lattice_dir / "tasks" / f"{tid}.json"
            if not snap_file.exists():
                issues.append(
                    {
                        "level": "warning",
                        "message": f"Event log {tid} has no matching snapshot (orphaned)",
                    }
                )

    return {
        "ok": len([i for i in issues if i["level"] == "error"]) == 0,
        "issues": issues,
        "task_count": len(list(tasks_dir.glob("*.json"))) if tasks_dir.is_dir() else 0,
        "archived_count": len(list((lattice_dir / "archive" / "tasks").glob("*.json")))
        if (lattice_dir / "archive" / "tasks").is_dir()
        else 0,
    }
