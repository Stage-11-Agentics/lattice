"""Map Lattice task snapshots to/from Automerge CRDT documents.

Automerge documents use the high-level ``automerge.Document`` API.
String fields are stored as collaborative Text (character-level merge).
Scalar fields (status, priority, etc.) use last-writer-wins via
``ImmutableString``.  List fields (tags, relationships) use Automerge
lists for positional merge.
"""

from __future__ import annotations

from automerge import Document, ImmutableString

# Fields stored as collaborative Text (character-level merge).
TEXT_FIELDS: frozenset[str] = frozenset({"title", "description"})

# Fields stored as ImmutableString (last-writer-wins scalar).
SCALAR_FIELDS: frozenset[str] = frozenset(
    {
        "status",
        "priority",
        "urgency",
        "complexity",
        "type",
        "assigned_to",
        "created_by",
        "created_at",
        "updated_at",
        "done_at",
        "last_status_changed_at",
    }
)

# Fields stored as Automerge lists.
LIST_FIELDS: frozenset[str] = frozenset({"tags"})

# Metadata fields stored in a nested _meta map.
META_FIELDS: frozenset[str] = frozenset(
    {"id", "schema_version", "short_id", "last_event_id"}
)

# All fields we sync.  Anything not listed here is skipped.
ALL_SYNCED_FIELDS: frozenset[str] = TEXT_FIELDS | SCALAR_FIELDS | LIST_FIELDS | META_FIELDS


def snapshot_to_automerge(doc: Document, snapshot: dict) -> None:
    """Populate an Automerge Document from a Lattice task snapshot.

    Called inside a ``with doc.change() as d:`` block or as a standalone
    operation that creates its own change.

    Overwrites all synced fields.  Fields not in ALL_SYNCED_FIELDS are
    silently ignored (they live only in the local event-sourced file).
    """
    with doc.change() as d:
        # Meta block
        d["_meta"] = {}
        for field in META_FIELDS:
            val = snapshot.get(field)
            if val is not None:
                d["_meta"][field] = ImmutableString(str(val))

        # Scalar fields (LWW)
        for field in SCALAR_FIELDS:
            val = snapshot.get(field)
            if val is not None:
                d[field] = ImmutableString(str(val))
            elif field in _keys(doc):
                d[field] = ImmutableString("")

        # Text fields (collaborative)
        for field in TEXT_FIELDS:
            val = snapshot.get(field) or ""
            d[field] = str(val)

        # List fields
        for field in LIST_FIELDS:
            val = snapshot.get(field) or []
            d[field] = list(val)


def automerge_to_snapshot_fields(doc: Document) -> dict:
    """Extract snapshot-compatible fields from an Automerge Document.

    Returns a dict of field values that can be compared against or
    merged into a Lattice task snapshot.  Does not include derived
    fields (comment_count, reopened_count, etc.) — those are maintained
    by event replay.
    """
    data = doc.to_py()
    result: dict = {}

    # Meta fields
    meta = data.get("_meta", {})
    for field in META_FIELDS:
        val = meta.get(field)
        if val is not None:
            if field == "schema_version":
                result[field] = int(val)
            else:
                result[field] = str(val)

    # Scalar fields
    for field in SCALAR_FIELDS:
        val = data.get(field)
        if val is not None:
            s = str(val)
            if s:
                result[field] = s

    # Text fields
    for field in TEXT_FIELDS:
        val = data.get(field)
        if val is not None:
            result[field] = str(val)

    # List fields
    for field in LIST_FIELDS:
        val = data.get(field)
        if val is not None:
            result[field] = list(val)

    return result


def apply_field_update_to_doc(doc: Document, field: str, value: object) -> None:
    """Update a single field in an Automerge document.

    Used by the bridge when translating individual Lattice events
    (e.g. status_changed) into CRDT mutations.
    """
    with doc.change() as d:
        if field in TEXT_FIELDS:
            d[field] = str(value) if value else ""
        elif field in SCALAR_FIELDS:
            d[field] = ImmutableString(str(value)) if value is not None else ImmutableString("")
        elif field in LIST_FIELDS:
            d[field] = list(value) if value else []
        elif field in META_FIELDS:
            if "_meta" not in _keys(doc):
                d["_meta"] = {}
            d["_meta"][field] = ImmutableString(str(value))


def _keys(doc: Document) -> list[str]:
    """Get top-level keys from a Document."""
    try:
        return list(doc.keys())
    except Exception:
        return []
