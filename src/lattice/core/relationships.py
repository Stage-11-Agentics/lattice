"""Relationship types and validation."""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Relationship types (section 8.3 of ProjectRequirements_v1)
# ---------------------------------------------------------------------------

RELATIONSHIP_TYPES: frozenset[str] = frozenset(
    {
        "blocks",
        "depends_on",
        "subtask_of",
        "related_to",
        "spawned_by",
        "duplicate_of",
        "supersedes",
    }
)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_relationship_type(rel_type: str) -> bool:
    """Return ``True`` if *rel_type* is a recognised relationship type."""
    return isinstance(rel_type, str) and rel_type in RELATIONSHIP_TYPES


# ---------------------------------------------------------------------------
# Record building
# ---------------------------------------------------------------------------


def build_relationship_record(
    rel_type: str,
    target_task_id: str,
    created_by: str,
    created_at: str,
    note: str | None = None,
) -> dict:
    """Build a relationship record dict.

    This mirrors the shape stored in a task snapshot's ``relationships_out``
    list.  All fields are taken explicitly -- the caller is responsible for
    sourcing ``created_at`` from the event timestamp.
    """
    return {
        "type": rel_type,
        "target_task_id": target_task_id,
        "created_at": created_at,
        "created_by": created_by,
        "note": note,
    }
