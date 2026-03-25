"""Bidirectional bridge between Lattice event-sourced state and Automerge CRDTs.

Handles two flows:
1. Local write → update CRDT document → sync propagates to peers
2. Remote CRDT change arrives → synthesize Lattice events → write locally
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from lattice.core.events import create_event
from lattice.core.tasks import apply_event_to_snapshot
from lattice.storage.operations import write_task_event
from lattice.sync.documents import (
    SCALAR_FIELDS,
    TEXT_FIELDS,
    apply_field_update_to_doc,
    snapshot_to_automerge,
)
from lattice.sync.store import AutomergeStore


class SyncBridge:
    """Bidirectional bridge between Lattice event-sourced state and Automerge CRDTs."""

    def __init__(
        self,
        lattice_dir: Path,
        store: AutomergeStore,
        config: dict | None = None,
    ) -> None:
        self.lattice_dir = lattice_dir
        self.store = store
        self.config = config
        self._applying_remote = False  # Guard against feedback loops

    def on_local_write(self, task_id: str, events: list[dict], snapshot: dict) -> None:
        """Called after write_task_event().  Updates the Automerge document.

        Registered as a bus listener.  Skips if the write originated from
        a remote CRDT change (feedback loop guard).
        """
        if self._applying_remote:
            return

        try:
            doc = self.store.get_or_create(task_id)

            for event in events:
                _apply_lattice_event_to_crdt(doc, event, snapshot)

            self.store.save(task_id, doc)
        except Exception as exc:
            print(f"lattice: sync bridge error (local→crdt): {exc}", file=sys.stderr)

    def on_crdt_change(self, task_id: str, old_state: dict, new_state: dict) -> None:
        """Called when Automerge sync delivers remote changes.

        Compares old and new CRDT state, synthesizes Lattice events,
        and writes them through the normal write path.

        Args:
            task_id: The task ID.
            old_state: The previous ``doc.to_py()`` dict.
            new_state: The current ``doc.to_py()`` dict.
        """
        diff = _diff_states(old_state, new_state)
        if not diff:
            return

        self._applying_remote = True
        try:
            snapshot = self._read_snapshot(task_id)
            if snapshot is None:
                # New task from remote — create it
                events = _synthesize_create_event(task_id, new_state)
            else:
                events = _synthesize_events_from_diff(task_id, snapshot, diff)

            if not events:
                return

            for event in events:
                snapshot = apply_event_to_snapshot(snapshot, event)

            write_task_event(self.lattice_dir, task_id, events, snapshot, self.config)
        except Exception as exc:
            print(f"lattice: sync bridge error (crdt→local): {exc}", file=sys.stderr)
        finally:
            self._applying_remote = False

    def bootstrap_task(self, task_id: str) -> None:
        """One-time: convert an existing task snapshot into an Automerge doc."""
        snapshot = self._read_snapshot(task_id)
        if snapshot is None:
            return

        doc = self.store.get_or_create(task_id)
        snapshot_to_automerge(doc, snapshot)
        self.store.save(task_id, doc)

    def _read_snapshot(self, task_id: str) -> dict | None:
        """Read a task snapshot from disk."""
        path = self.lattice_dir / "tasks" / f"{task_id}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text())


# ---------------------------------------------------------------------------
# Local event → CRDT mutation
# ---------------------------------------------------------------------------


def _apply_lattice_event_to_crdt(doc, event: dict, snapshot: dict) -> None:
    """Apply a single Lattice event to an Automerge document."""
    etype = event["type"]
    data = event.get("data", {})

    if etype == "task_created":
        snapshot_to_automerge(doc, snapshot)

    elif etype == "status_changed":
        apply_field_update_to_doc(doc, "status", data.get("to"))

    elif etype == "assignment_changed":
        apply_field_update_to_doc(doc, "assigned_to", data.get("to"))

    elif etype == "field_updated":
        field = data.get("field")
        value = data.get("to", data.get("value"))
        if field:
            apply_field_update_to_doc(doc, field, value)

    elif etype in ("task_archived", "task_unarchived"):
        # Sync the full snapshot state
        snapshot_to_automerge(doc, snapshot)

    else:
        # For events that modify snapshot state indirectly (comments,
        # relationships, etc.), update metadata to track last_event_id.
        from automerge import ImmutableString

        with doc.change() as d:
            if "_meta" not in _doc_keys(doc):
                d["_meta"] = {}
            d["_meta"]["last_event_id"] = ImmutableString(event["id"])


# ---------------------------------------------------------------------------
# CRDT diff → Lattice events
# ---------------------------------------------------------------------------


def _diff_states(old: dict, new: dict) -> list[dict]:
    """Compare two to_py() dicts and produce a list of field changes."""
    changes: list[dict] = []
    all_keys = set(old.keys()) | set(new.keys())

    for key in all_keys:
        if key == "_meta":
            continue  # Meta changes don't generate events
        old_val = old.get(key)
        new_val = new.get(key)
        if old_val != new_val:
            changes.append({"field": key, "from": old_val, "to": new_val})

    return changes


def _synthesize_events_from_diff(
    task_id: str,
    snapshot: dict,
    diff: list[dict],
) -> list[dict]:
    """Generate Lattice events from a list of field changes."""
    events: list[dict] = []
    actor = "sync:remote"

    for change in diff:
        field = change["field"]
        from_val = change["from"]
        to_val = change["to"]

        if field == "status" and to_val is not None:
            events.append(
                create_event(
                    type="status_changed",
                    task_id=task_id,
                    actor=actor,
                    data={
                        "from": snapshot.get("status", ""),
                        "to": str(to_val),
                    },
                    triggered_by="crdt_sync",
                    reason="Remote change via Automerge sync",
                )
            )
        elif field == "assigned_to":
            events.append(
                create_event(
                    type="assignment_changed",
                    task_id=task_id,
                    actor=actor,
                    data={
                        "from": snapshot.get("assigned_to"),
                        "to": str(to_val) if to_val else None,
                    },
                    triggered_by="crdt_sync",
                    reason="Remote change via Automerge sync",
                )
            )
        elif field in TEXT_FIELDS or field in SCALAR_FIELDS:
            events.append(
                create_event(
                    type="field_updated",
                    task_id=task_id,
                    actor=actor,
                    data={
                        "field": field,
                        "from": from_val,
                        "to": to_val,
                    },
                    triggered_by="crdt_sync",
                    reason="Remote change via Automerge sync",
                )
            )
        elif field == "tags" and isinstance(to_val, list):
            events.append(
                create_event(
                    type="field_updated",
                    task_id=task_id,
                    actor=actor,
                    data={
                        "field": "tags",
                        "from": from_val,
                        "to": to_val,
                    },
                    triggered_by="crdt_sync",
                    reason="Remote change via Automerge sync",
                )
            )

    return events


def _synthesize_create_event(task_id: str, state: dict) -> list[dict]:
    """Generate a task_created event from a remote CRDT state."""
    meta = state.get("_meta", {})
    return [
        create_event(
            type="task_created",
            task_id=meta.get("id", task_id),
            actor="sync:remote",
            data={
                "title": str(state.get("title", "")),
                "status": str(state.get("status", "backlog")),
                "type": str(state.get("type", "task")),
                "priority": str(state.get("priority", "medium")),
                "description": str(state.get("description", "")),
                "tags": list(state.get("tags", [])),
            },
            triggered_by="crdt_sync",
            reason="New task from remote Automerge peer",
        )
    ]


def _doc_keys(doc) -> list[str]:
    """Get top-level keys from a Document."""
    try:
        return list(doc.keys())
    except Exception:
        return []
