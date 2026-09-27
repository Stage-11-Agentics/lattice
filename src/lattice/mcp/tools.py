"""MCP tool registrations for Lattice — write and read operations.

Every write tool is a thin wrapper over a named operation (``lattice.ops``),
exactly like the CLI command it mirrors: it resolves its board with
``lattice.boards.resolve_board`` starting from the call's ``lattice_root``,
builds the operation's params and ``Caller``, and returns the ``OpResult`` in
the shape this tool has always returned. The rules (transition graph, plan
gate, review-cycle limit, completion policies, validation) live in the
operation only, so an MCP call is refused wherever the CLI is (SPEC §12,
§14 G-6). Read tools catch the board up first (a no-op on a local board).

Each call's ``lattice_root`` is its operation's starting directory, so the
origin of every event names that call's own worktree and branch (SPEC §4),
even when one server process writes to several checkouts.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field

from lattice.boards import LocalBoard, git_worktree, resolve_board
from lattice.core.acceptance_criteria import criterion_without_history
from lattice.core.comments import materialize_comments
from lattice.core.events import get_actor_display
from lattice.core.ids import is_short_id, validate_id
from lattice.mcp.server import mcp
from lattice.ops import Caller, OpError, OpResult
from lattice.storage.operations import discover_task_authorities, read_task_authority
from lattice.storage.short_ids import resolve_short_id

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


class LatticeToolError(ValueError):
    """A refused tool call: the operation's error code, message, and details.

    A ``ValueError``, as every MCP tool error has always been; the message
    leads with the code so an agent can tell ``PLAN_REQUIRED`` from
    ``INVALID_TRANSITION`` without parsing prose.
    """

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = details or {}


@contextmanager
def _tool_errors() -> Iterator[None]:
    """Report an ``OpError`` as a ``LatticeToolError``."""
    try:
        yield
    except OpError as exc:
        raise LatticeToolError(exc.code, exc.message, exc.details) from exc


def _board(lattice_root: str | None) -> LocalBoard:
    """The board this call's starting directory belongs to.

    ``lattice_root`` names the starting directory and wins over the server
    process's ``LATTICE_ROOT``; without it the call starts in the cwd, where
    ``LATTICE_ROOT`` applies as it does for the CLI.
    """
    with _tool_errors():
        if lattice_root:
            return resolve_board(Path(lattice_root), honor_env=False)
        return resolve_board()


def _execute(
    lattice_root: str | None,
    op_name: str,
    params: dict,
    actor: str,
    *,
    board: LocalBoard | None = None,
    attestations: dict | None = None,
    config: dict | None = None,
) -> OpResult:
    """Run *op_name* on the call's board as *actor*."""
    board = board if board is not None else _board(lattice_root)
    caller = Caller(actor=actor, attestations=attestations or {})
    with _tool_errors():
        return board.execute(op_name, params, caller, config=config)


def _read_dir(lattice_root: str | None) -> Path:
    """The call's board directory, caught up before it is read (SPEC §9.5)."""
    board = _board(lattice_root)
    with _tool_errors():
        board.refresh()
    return board.lattice_dir


def _load_config(lattice_dir: Path) -> dict:
    """Load config.json from the lattice directory."""
    return json.loads((lattice_dir / "config.json").read_text())


def _resolve_task_id(lattice_dir: Path, raw_id: str) -> str:
    """Resolve a short ID or ULID to the canonical task ULID (read tools)."""
    if validate_id(raw_id, "task"):
        return raw_id

    if is_short_id(raw_id):
        normalized = raw_id.upper()
        ulid = resolve_short_id(lattice_dir, normalized)
        if ulid is not None:
            return ulid
        raise ValueError(f"Short ID '{normalized}' not found.")

    raise ValueError(f"Invalid task ID format: '{raw_id}'.")


def _update_pairs(fields: dict) -> list[str]:
    """``lattice_update``'s ``fields`` as ``task.update``'s ``field=value`` pairs.

    Values are text, as on the command line: a list of tags is joined with
    commas, a number or boolean is written as JSON, and ``None``, a list for
    any other field, or an object is refused, as is a name holding ``=``
    (it would split into another field).
    """
    pairs: list[str] = []
    for name, value in fields.items():
        if "=" in name:
            raise LatticeToolError("VALIDATION_ERROR", f"Invalid field name: '{name}'.")
        if name == "tags" and isinstance(value, list) and all(isinstance(t, str) for t in value):
            value = ",".join(value)
        elif isinstance(value, bool | int | float):
            value = json.dumps(value)
        elif not isinstance(value, str):
            raise LatticeToolError(
                "VALIDATION_ERROR",
                f"Invalid value for '{name}': expected text (as with 'lattice update').",
            )
        pairs.append(f"{name}={value}")
    return pairs


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
    params = {
        "title": title,
        "type": task_type,
        "priority": priority,
        "status": status,
        "description": description,
        "tags": tags,
        "assigned_to": assigned_to,
        "id": task_id,
    }
    return _execute(lattice_root, "task.create", params, actor).task


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
    params = {"task": task_id, "outcome": outcome, "id": criterion_id}
    return _execute(lattice_root, "task.criterion_add", params, actor).value


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
    params = {"task": task_id, "criterion_id": criterion_id, "outcome": outcome}
    return _execute(lattice_root, "task.criterion_edit", params, actor).value


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
    params = {"task": task_id, "criterion_id": criterion_id}
    return _execute(lattice_root, "task.criterion_retire", params, actor).value


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
    lattice_dir = _read_dir(lattice_root)
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
    params = {"task": task_id, "pairs": _update_pairs(fields)}
    result = _execute(lattice_root, "task.update", params, actor)
    if result.idempotent:
        return {"message": "No changes", "snapshot": result.task}
    return result.task


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
    """Change a task's status with the CLI's rules. Returns the updated snapshot.

    The transition graph, the plan gate, the review-cycle limit, and the
    completion policies apply as they do for ``lattice status``; ``force``
    with a ``reason`` overrides them as ``--force --reason`` does.
    """
    from lattice.cli.attestations import completion_attestations
    from lattice.core.attestations import STALE_ATTESTATION
    from lattice.core.config import resolve_status_input

    board = _board(lattice_root)
    config = board.load_config()
    target_status = resolve_status_input(config, new_status)
    worktree = git_worktree(board.start)
    params = {"task": task_id, "new_status": new_status, "force": force, "reason": reason}

    # The same attestation and single stale-retry as the CLI (SPEC §3.4),
    # computed in this call's worktree rather than the server's cwd.
    for attempt in range(2):
        if attempt:
            with _tool_errors():
                board.refresh()
        attestations = (
            completion_attestations(board, config, task_id, target_status, worktree=worktree)
            if worktree is not None
            else {}
        )
        try:
            result = _execute(
                lattice_root,
                "task.status",
                params,
                actor,
                board=board,
                attestations=attestations,
                config=config,
            )
        except LatticeToolError as exc:
            stale = exc.code == "COMPLETION_BLOCKED" and (
                exc.details.get("reason") == STALE_ATTESTATION
            )
            if stale and attempt == 0:
                continue
            raise
        break
    if result.idempotent:
        return {"message": f"Already at status {result.task['status']}", "snapshot": result.task}
    return result.task


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
    params = {"task": task_id, "actor_id": assignee}
    result = _execute(lattice_root, "task.assign", params, actor)
    if result.idempotent:
        return {"message": f"Already assigned to {assignee}", "snapshot": result.task}
    return result.task


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
    params = {
        "task": task_id,
        "text": text,
        "reply_to": parent_id,
        "role": role,
        "criterion": list(criterion_ids or []),
    }
    return _execute(lattice_root, "task.comment", params, actor).task


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
    params = {
        "task": source_id,
        "type": relationship_type,
        "target_task": target_id,
        "note": note,
    }
    return _execute(lattice_root, "task.link", params, actor).task


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
    params = {"task": source_id, "type": relationship_type, "target_task": target_id}
    return _execute(lattice_root, "task.unlink", params, actor).task


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
    from lattice.ops.task_attach import SOURCE_NOT_FOUND, encode_payload

    params: dict[str, Any] = {
        "task": task_id,
        "source": source,
        "type": art_type,
        "title": title,
        "summary": summary,
        "role": role,
        "criterion": list(criterion_ids or []),
        "id": artifact_id,
    }
    board = _board(lattice_root)
    # A readable file travels as its content (SPEC §3.8), read only after
    # every rule that comes before it has passed, as the CLI does: a call with
    # the bare path writes nothing and is refused for want of the content.
    if not source.startswith(("http://", "https://")):
        src_path = Path(source)
        if src_path.is_file():
            try:
                _execute(lattice_root, "task.attach", params, actor, board=board)
            except LatticeToolError as exc:
                if exc.details.get("reason") != SOURCE_NOT_FOUND:
                    raise
            try:
                content = src_path.read_bytes()
            except OSError as exc:
                raise LatticeToolError(
                    "VALIDATION_ERROR",
                    f"Cannot read source file '{source}': {exc.strerror or exc}.",
                ) from exc
            params["source"] = None
            params["payload"] = encode_payload(src_path.name, content)
    return _execute(lattice_root, "task.attach", params, actor, board=board).value


@mcp.tool()
def lattice_archive(
    task_id: Annotated[str, Field(description="Task ID (ULID or short ID)")],
    actor: Annotated[str, Field(description="Actor ID")],
    lattice_root: Annotated[
        str | None, Field(description="Path to project directory containing .lattice/")
    ] = None,
) -> dict:
    """Archive a task. Returns the archive event."""
    return _execute(lattice_root, "task.archive", {"task": task_id}, actor).value


@mcp.tool()
def lattice_unarchive(
    task_id: Annotated[str, Field(description="Task ID (ULID or short ID)")],
    actor: Annotated[str, Field(description="Actor ID")],
    lattice_root: Annotated[
        str | None, Field(description="Path to project directory containing .lattice/")
    ] = None,
) -> dict:
    """Restore an archived task to active status. Returns the unarchive event."""
    return _execute(lattice_root, "task.unarchive", {"task": task_id}, actor).value


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
    params = {"task": task_id, "branch": branch, "repo": repo}
    return _execute(lattice_root, "task.branch_link", params, actor).task


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
    params = {"task": task_id, "branch": branch, "repo": repo}
    return _execute(lattice_root, "task.branch_unlink", params, actor).task


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
    params = {
        "task": task_id,
        "event_type": event_type,
        "data": json.dumps(data) if data is not None else None,
    }
    return _execute(lattice_root, "task.event", params, actor).value


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
    params = {
        "task": task_id,
        "comment_id": comment_id,
        "new_text": new_text,
        "role": role,
        "clear_role": clear_role,
    }
    return _execute(lattice_root, "task.comment_edit", params, actor).task


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
    params = {"task": task_id, "comment_id": comment_id}
    return _execute(lattice_root, "task.comment_delete", params, actor).task


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
    params = {"task": task_id, "comment_id": comment_id, "emoji": emoji}
    result = _execute(lattice_root, "task.react", params, actor)
    if result.idempotent:
        return {"message": "Reaction already exists", "snapshot": result.task}
    return result.task


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
    params = {"task": task_id, "comment_id": comment_id, "emoji": emoji}
    return _execute(lattice_root, "task.unreact", params, actor).task


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
    lattice_dir = _read_dir(lattice_root)
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
    lattice_dir = _read_dir(lattice_root)
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
    lattice_dir = _read_dir(lattice_root)
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
    lattice_dir = _read_dir(lattice_root)
    return _load_config(lattice_dir)


@mcp.tool()
def lattice_doctor(
    fix: Annotated[bool, Field(description="Attempt to fix issues")] = False,
    lattice_root: Annotated[
        str | None, Field(description="Path to project directory containing .lattice/")
    ] = None,
) -> dict:
    """Check Lattice data integrity. Returns a diagnostic report."""
    lattice_dir = _read_dir(lattice_root)
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
