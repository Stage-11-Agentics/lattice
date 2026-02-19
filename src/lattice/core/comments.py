"""Comment materialization and validation — pure functions, no I/O."""

from __future__ import annotations

import re

# Emoji validation: alphanumeric, underscores, hyphens, 1-50 chars.
_EMOJI_RE = re.compile(r"^[a-zA-Z0-9_-]{1,50}$")


def validate_emoji(emoji: str) -> bool:
    """Return ``True`` if *emoji* is a valid reaction emoji string."""
    return bool(_EMOJI_RE.match(emoji))


def validate_comment_body(body: str) -> str:
    """Validate and normalize a comment body.

    Strips whitespace and rejects empty/whitespace-only bodies.
    Returns the stripped body on success.
    Raises ``ValueError`` if the body is empty or whitespace-only.
    """
    if not isinstance(body, str) or not body.strip():
        raise ValueError("Comment body must be a non-empty string.")
    return body.strip()


# ---------------------------------------------------------------------------
# Shared event-processing core
# ---------------------------------------------------------------------------


def _build_comments_map(events: list[dict]) -> dict[str, dict]:
    """Process events into a ``{comment_id: comment_dict}`` map.

    Single-pass over events.  Each comment dict contains:
    ``id``, ``body``, ``author``, ``created_at``, ``edited``, ``edited_at``,
    ``edit_history``, ``deleted``, ``deleted_by``, ``deleted_at``,
    ``parent_id``, ``reactions``.

    Note: comment IDs are the event IDs from ``comment_added`` events.
    This avoids a separate comment ID namespace — every ``comment_id``
    parameter in edit/delete/reaction events refers to an event ID.
    """
    comments_by_id: dict[str, dict] = {}

    for ev in events:
        etype = ev.get("type")
        data = ev.get("data", {})

        if etype == "comment_added":
            comment_id = ev["id"]
            comments_by_id[comment_id] = {
                "id": comment_id,
                "body": data.get("body", ""),
                "role": data.get("role"),
                "author": ev.get("actor", ""),
                "created_at": ev.get("ts", ""),
                "edited": False,
                "edited_at": None,
                "edit_history": [],
                "deleted": False,
                "deleted_by": None,
                "deleted_at": None,
                "parent_id": data.get("parent_id"),
                "reactions": {},
            }

        elif etype == "comment_edited":
            target_id = data.get("comment_id")
            comment = comments_by_id.get(target_id)
            if comment is not None and not comment["deleted"]:
                comment["edit_history"].append(
                    {
                        "body": comment["body"],
                        "edited_at": ev.get("ts", ""),
                        "edited_by": ev.get("actor", ""),
                    }
                )
                comment["body"] = data.get("body", comment["body"])
                if "role" in data:
                    comment["role"] = data["role"]
                comment["edited"] = True
                comment["edited_at"] = ev.get("ts", "")

        elif etype == "comment_deleted":
            target_id = data.get("comment_id")
            comment = comments_by_id.get(target_id)
            if comment is not None and not comment["deleted"]:
                comment["deleted"] = True
                comment["deleted_by"] = ev.get("actor", "")
                comment["deleted_at"] = ev.get("ts", "")

        elif etype == "reaction_added":
            target_id = data.get("comment_id")
            emoji = data.get("emoji", "")
            actor = ev.get("actor", "")
            comment = comments_by_id.get(target_id)
            if comment is not None and not comment["deleted"]:
                reactions = comment["reactions"]
                if emoji not in reactions:
                    reactions[emoji] = []
                if actor not in reactions[emoji]:
                    reactions[emoji].append(actor)

        elif etype == "reaction_removed":
            target_id = data.get("comment_id")
            emoji = data.get("emoji", "")
            actor = ev.get("actor", "")
            comment = comments_by_id.get(target_id)
            # Intentionally does NOT check comment["deleted"] here:
            # if a reaction was added before deletion and then removed,
            # the removal should still apply to keep materialized state clean.
            if comment is not None:
                reactions = comment["reactions"]
                if emoji in reactions and actor in reactions[emoji]:
                    reactions[emoji].remove(actor)
                    if not reactions[emoji]:
                        del reactions[emoji]

    return comments_by_id


def materialize_comments(events: list[dict]) -> list[dict]:
    """Reconstruct current comment state from a task's event list.

    Returns a list of top-level comment dicts with nested ``replies``.
    Deleted comments are included (with ``deleted=True``) so that threading
    structure is preserved and callers can render ``[deleted]`` placeholders.
    """
    comments_by_id = _build_comments_map(events)

    # Add replies list and build threaded structure
    for comment in comments_by_id.values():
        comment["replies"] = []

    top_level: list[dict] = []
    for comment in comments_by_id.values():
        parent_id = comment.get("parent_id")
        if parent_id and parent_id in comments_by_id:
            comments_by_id[parent_id]["replies"].append(comment)
        else:
            top_level.append(comment)

    return top_level


def _flat_comments(events: list[dict]) -> list[dict]:
    """Materialize comments as a flat list (no nesting in replies)."""
    return list(_build_comments_map(events).values())


def validate_comment_for_reply(events: list[dict], parent_id: str) -> None:
    """Validate that *parent_id* is a valid reply target.

    Raises ``ValueError`` if the parent doesn't exist, is itself a reply,
    or is deleted.
    """
    comments = {c["id"]: c for c in _flat_comments(events)}
    parent = comments.get(parent_id)
    if parent is None:
        raise ValueError(f"Comment {parent_id} not found.")
    if parent["deleted"]:
        raise ValueError(f"Cannot reply to deleted comment {parent_id}.")
    if parent["parent_id"] is not None:
        raise ValueError(
            f"Cannot reply to a reply ({parent_id}). Only top-level comments accept replies."
        )


def validate_comment_for_edit(events: list[dict], comment_id: str) -> tuple[str, str | None]:
    """Validate that *comment_id* can be edited.

    Returns ``(previous_body, previous_role)`` tuple.
    Raises ``ValueError`` if the comment doesn't exist or is deleted.
    """
    comments = {c["id"]: c for c in _flat_comments(events)}
    comment = comments.get(comment_id)
    if comment is None:
        raise ValueError(f"Comment {comment_id} not found.")
    if comment["deleted"]:
        raise ValueError(f"Cannot edit deleted comment {comment_id}.")
    return comment["body"], comment.get("role")


def validate_comment_for_delete(events: list[dict], comment_id: str) -> None:
    """Validate that *comment_id* can be deleted.

    Raises ``ValueError`` if the comment doesn't exist or is already deleted.
    """
    comments = {c["id"]: c for c in _flat_comments(events)}
    comment = comments.get(comment_id)
    if comment is None:
        raise ValueError(f"Comment {comment_id} not found.")
    if comment["deleted"]:
        raise ValueError(f"Comment {comment_id} is already deleted.")


def validate_comment_for_react(events: list[dict], comment_id: str) -> None:
    """Validate that *comment_id* can receive reactions.

    Raises ``ValueError`` if the comment doesn't exist or is deleted.
    """
    comments = {c["id"]: c for c in _flat_comments(events)}
    comment = comments.get(comment_id)
    if comment is None:
        raise ValueError(f"Comment {comment_id} not found.")
    if comment["deleted"]:
        raise ValueError(f"Cannot react to deleted comment {comment_id}.")
