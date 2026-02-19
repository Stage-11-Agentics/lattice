"""Tests for lattice.core.tasks."""

from __future__ import annotations

import json

import pytest

from lattice.core.events import BUILTIN_EVENT_TYPES, RESOURCE_EVENT_TYPES
from lattice.core.tasks import (
    PROTECTED_FIELDS,
    _MUTATION_HANDLERS,
    _NOOP_EVENT_TYPES,
    apply_event_to_snapshot,
    compact_snapshot,
    compute_epic_derived_status,
    get_artifact_roles,
    get_comment_role_refs,
    serialize_snapshot,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TS_1 = "2026-02-15T03:45:00Z"
_TS_2 = "2026-02-15T04:00:00Z"
_TS_3 = "2026-02-15T04:15:00Z"

_TASK_ID = "task_01EXAMPLE0000000000000000"
_EV_1 = "ev_01AAAAAAAAAAAAAAAAAAAAAAAAAA"
_EV_2 = "ev_01BBBBBBBBBBBBBBBBBBBBBBBBBB"
_EV_3 = "ev_01CCCCCCCCCCCCCCCCCCCCCCCCCC"
_ACTOR = "human:atin"


def _created_event(
    *,
    task_id: str = _TASK_ID,
    actor: str = _ACTOR,
    ev_id: str = _EV_1,
    ts: str = _TS_1,
    data: dict | None = None,
) -> dict:
    """Build a minimal ``task_created`` event for testing."""
    if data is None:
        data = {
            "title": "Fix login bug",
            "status": "backlog",
            "priority": "high",
            "urgency": "normal",
            "type": "bug",
            "description": "OAuth redirect broken",
            "tags": ["auth", "urgent"],
            "assigned_to": "agent:claude",
            "custom_fields": {"sprint": 12},
        }
    return {
        "schema_version": 1,
        "id": ev_id,
        "ts": ts,
        "type": "task_created",
        "task_id": task_id,
        "actor": actor,
        "data": data,
    }


def _make_snapshot() -> dict:
    """Create a snapshot via a ``task_created`` event."""
    return apply_event_to_snapshot(None, _created_event())


# ---------------------------------------------------------------------------
# apply_event_to_snapshot: task_created
# ---------------------------------------------------------------------------


class TestTaskCreated:
    """Snapshot initialisation from a task_created event."""

    def test_all_expected_fields(self) -> None:
        snap = _make_snapshot()
        assert snap["schema_version"] == 1
        assert snap["id"] == _TASK_ID
        assert snap["title"] == "Fix login bug"
        assert snap["status"] == "backlog"
        assert snap["priority"] == "high"
        assert snap["urgency"] == "normal"
        assert snap["type"] == "bug"
        assert snap["description"] == "OAuth redirect broken"
        assert snap["tags"] == ["auth", "urgent"]
        assert snap["assigned_to"] == "agent:claude"
        assert snap["created_by"] == _ACTOR
        assert snap["relationships_out"] == []
        assert snap["evidence_refs"] == []
        assert snap["branch_links"] == []
        assert snap["reopened_count"] == 0
        assert snap["custom_fields"] == {"sprint": 12}

    def test_timestamps_from_event(self) -> None:
        snap = _make_snapshot()
        assert snap["created_at"] == _TS_1
        assert snap["updated_at"] == _TS_1

    def test_last_event_id_set(self) -> None:
        snap = _make_snapshot()
        assert snap["last_event_id"] == _EV_1

    def test_minimal_data(self) -> None:
        """task_created with only a title; other fields default to None."""
        ev = _created_event(data={"title": "Bare minimum"})
        snap = apply_event_to_snapshot(None, ev)
        assert snap["title"] == "Bare minimum"
        assert snap["priority"] is None
        assert snap["custom_fields"] == {}

    def test_done_at_none_by_default(self) -> None:
        snap = _make_snapshot()
        assert snap["done_at"] is None

    def test_done_at_set_if_created_as_done(self) -> None:
        ev = _created_event(data={"title": "Already done", "status": "done"})
        snap = apply_event_to_snapshot(None, ev)
        assert snap["done_at"] == _TS_1


# ---------------------------------------------------------------------------
# apply_event_to_snapshot: status_changed
# ---------------------------------------------------------------------------


class TestStatusChanged:
    def test_status_updated(self) -> None:
        snap = _make_snapshot()
        ev = {
            "schema_version": 1,
            "id": _EV_2,
            "ts": _TS_2,
            "type": "status_changed",
            "task_id": _TASK_ID,
            "actor": "agent:claude",
            "data": {"from": "backlog", "to": "in_planning"},
        }
        snap = apply_event_to_snapshot(snap, ev)
        assert snap["status"] == "in_planning"
        assert snap["last_event_id"] == _EV_2
        assert snap["updated_at"] == _TS_2

    def test_done_at_set_when_transitioning_to_done(self) -> None:
        snap = _make_snapshot()
        assert snap["done_at"] is None
        ev = {
            "schema_version": 1,
            "id": _EV_2,
            "ts": _TS_2,
            "type": "status_changed",
            "task_id": _TASK_ID,
            "actor": _ACTOR,
            "data": {"from": "backlog", "to": "done"},
        }
        snap = apply_event_to_snapshot(snap, ev)
        assert snap["status"] == "done"
        assert snap["done_at"] == _TS_2

    def test_done_at_cleared_when_leaving_done(self) -> None:
        snap = _make_snapshot()
        # First move to done
        ev1 = {
            "schema_version": 1,
            "id": _EV_2,
            "ts": _TS_2,
            "type": "status_changed",
            "task_id": _TASK_ID,
            "actor": _ACTOR,
            "data": {"from": "backlog", "to": "done"},
        }
        snap = apply_event_to_snapshot(snap, ev1)
        assert snap["done_at"] == _TS_2
        # Now reopen — move back to in_progress
        ev2 = {
            "schema_version": 1,
            "id": _EV_3,
            "ts": _TS_3,
            "type": "status_changed",
            "task_id": _TASK_ID,
            "actor": _ACTOR,
            "data": {"from": "done", "to": "in_progress"},
        }
        snap = apply_event_to_snapshot(snap, ev2)
        assert snap["status"] == "in_progress"
        assert snap["done_at"] is None

    def test_done_at_not_set_for_non_done_transitions(self) -> None:
        snap = _make_snapshot()
        ev = {
            "schema_version": 1,
            "id": _EV_2,
            "ts": _TS_2,
            "type": "status_changed",
            "task_id": _TASK_ID,
            "actor": _ACTOR,
            "data": {"from": "backlog", "to": "in_progress"},
        }
        snap = apply_event_to_snapshot(snap, ev)
        assert snap["done_at"] is None

    def test_reopened_count_increments_on_backward_transition(self) -> None:
        snap = _make_snapshot()
        assert snap["reopened_count"] == 0
        ev1 = {
            "schema_version": 1,
            "id": _EV_2,
            "ts": _TS_2,
            "type": "status_changed",
            "task_id": _TASK_ID,
            "actor": _ACTOR,
            "data": {"from": "backlog", "to": "done"},
        }
        snap = apply_event_to_snapshot(snap, ev1)
        ev2 = {
            "schema_version": 1,
            "id": _EV_3,
            "ts": _TS_3,
            "type": "status_changed",
            "task_id": _TASK_ID,
            "actor": _ACTOR,
            "data": {"from": "done", "to": "planned"},
        }
        snap = apply_event_to_snapshot(snap, ev2)
        assert snap["reopened_count"] == 1

    def test_reopened_count_not_incremented_on_forward_transition(self) -> None:
        snap = _make_snapshot()
        ev = {
            "schema_version": 1,
            "id": _EV_2,
            "ts": _TS_2,
            "type": "status_changed",
            "task_id": _TASK_ID,
            "actor": _ACTOR,
            "data": {"from": "backlog", "to": "in_progress"},
        }
        snap = apply_event_to_snapshot(snap, ev)
        assert snap["reopened_count"] == 0


# ---------------------------------------------------------------------------
# apply_event_to_snapshot: assignment_changed
# ---------------------------------------------------------------------------


class TestAssignmentChanged:
    def test_assigned_to_updated(self) -> None:
        snap = _make_snapshot()
        ev = {
            "schema_version": 1,
            "id": _EV_2,
            "ts": _TS_2,
            "type": "assignment_changed",
            "task_id": _TASK_ID,
            "actor": _ACTOR,
            "data": {"from": "agent:claude", "to": "agent:codex"},
        }
        snap = apply_event_to_snapshot(snap, ev)
        assert snap["assigned_to"] == "agent:codex"

    def test_assigned_to_null(self) -> None:
        snap = _make_snapshot()
        ev = {
            "schema_version": 1,
            "id": _EV_2,
            "ts": _TS_2,
            "type": "assignment_changed",
            "task_id": _TASK_ID,
            "actor": _ACTOR,
            "data": {"from": "agent:claude", "to": None},
        }
        snap = apply_event_to_snapshot(snap, ev)
        assert snap["assigned_to"] is None


# ---------------------------------------------------------------------------
# apply_event_to_snapshot: field_updated
# ---------------------------------------------------------------------------


class TestFieldUpdated:
    def test_named_field_updated(self) -> None:
        snap = _make_snapshot()
        ev = {
            "schema_version": 1,
            "id": _EV_2,
            "ts": _TS_2,
            "type": "field_updated",
            "task_id": _TASK_ID,
            "actor": _ACTOR,
            "data": {"field": "title", "from": "Fix login bug", "to": "Fix OAuth bug"},
        }
        snap = apply_event_to_snapshot(snap, ev)
        assert snap["title"] == "Fix OAuth bug"

    def test_custom_fields_dot_notation(self) -> None:
        snap = _make_snapshot()
        ev = {
            "schema_version": 1,
            "id": _EV_2,
            "ts": _TS_2,
            "type": "field_updated",
            "task_id": _TASK_ID,
            "actor": _ACTOR,
            "data": {
                "field": "custom_fields.estimate",
                "from": None,
                "to": 5,
            },
        }
        snap = apply_event_to_snapshot(snap, ev)
        assert snap["custom_fields"]["estimate"] == 5
        # Original custom field still present.
        assert snap["custom_fields"]["sprint"] == 12

    def test_custom_fields_does_not_mutate_original(self) -> None:
        """Updating custom_fields must not mutate the caller's original snapshot."""
        original = _make_snapshot()
        assert original["custom_fields"] == {"sprint": 12}
        original_cf = original["custom_fields"]
        ev = {
            "schema_version": 1,
            "id": _EV_2,
            "ts": _TS_2,
            "type": "field_updated",
            "task_id": _TASK_ID,
            "actor": _ACTOR,
            "data": {"field": "custom_fields.estimate", "from": None, "to": 5},
        }
        updated = apply_event_to_snapshot(original, ev)
        # Updated snapshot has the new field.
        assert updated["custom_fields"]["estimate"] == 5
        # Original snapshot's custom_fields must be untouched.
        assert "estimate" not in original_cf
        assert original_cf == {"sprint": 12}

    def test_custom_fields_creates_dict_if_missing(self) -> None:
        """Ensure custom_fields dict is created when snapshot lacks it."""
        snap = _make_snapshot()
        snap.pop("custom_fields", None)
        ev = {
            "schema_version": 1,
            "id": _EV_2,
            "ts": _TS_2,
            "type": "field_updated",
            "task_id": _TASK_ID,
            "actor": _ACTOR,
            "data": {"field": "custom_fields.key", "from": None, "to": "val"},
        }
        snap = apply_event_to_snapshot(snap, ev)
        assert snap["custom_fields"]["key"] == "val"

    @pytest.mark.parametrize("field", sorted(PROTECTED_FIELDS))
    def test_protected_field_rejected(self, field: str) -> None:
        """Protected fields raise ValueError when targeted by field_updated."""
        snap = _make_snapshot()
        ev = {
            "schema_version": 1,
            "id": _EV_2,
            "ts": _TS_2,
            "type": "field_updated",
            "task_id": _TASK_ID,
            "actor": _ACTOR,
            "data": {"field": field, "from": "old", "to": "new"},
        }
        with pytest.raises(ValueError, match="protected field"):
            apply_event_to_snapshot(snap, ev)


# ---------------------------------------------------------------------------
# apply_event_to_snapshot: comment_added
# ---------------------------------------------------------------------------


class TestCommentAdded:
    def test_increments_comment_count(self) -> None:
        snap = _make_snapshot()
        assert snap.get("comment_count", 0) == 0
        original_status = snap["status"]
        original_title = snap["title"]
        ev = {
            "schema_version": 1,
            "id": _EV_2,
            "ts": _TS_2,
            "type": "comment_added",
            "task_id": _TASK_ID,
            "actor": "agent:claude",
            "data": {"body": "Starting work now"},
        }
        snap = apply_event_to_snapshot(snap, ev)
        assert snap["comment_count"] == 1
        assert snap["status"] == original_status
        assert snap["title"] == original_title
        assert snap["last_event_id"] == _EV_2
        assert snap["updated_at"] == _TS_2

    def test_multiple_comments_increment(self) -> None:
        snap = _make_snapshot()
        for i in range(3):
            ev = {
                "schema_version": 1,
                "id": f"ev_0000000000000000000000000{i + 2}",
                "ts": _TS_2,
                "type": "comment_added",
                "task_id": _TASK_ID,
                "actor": "agent:claude",
                "data": {"body": f"Comment {i + 1}"},
            }
            snap = apply_event_to_snapshot(snap, ev)
        assert snap["comment_count"] == 3

    def test_comment_deleted_decrements(self) -> None:
        snap = _make_snapshot()
        # Add two comments
        for i in range(2):
            ev = {
                "schema_version": 1,
                "id": f"ev_0000000000000000000000000{i + 2}",
                "ts": _TS_2,
                "type": "comment_added",
                "task_id": _TASK_ID,
                "actor": "agent:claude",
                "data": {"body": f"Comment {i + 1}"},
            }
            snap = apply_event_to_snapshot(snap, ev)
        assert snap["comment_count"] == 2

        # Delete one
        del_ev = {
            "schema_version": 1,
            "id": "ev_00000000000000000000000005",
            "ts": _TS_2,
            "type": "comment_deleted",
            "task_id": _TASK_ID,
            "actor": "agent:claude",
            "data": {"comment_id": "ev_00000000000000000000000002"},
        }
        snap = apply_event_to_snapshot(snap, del_ev)
        assert snap["comment_count"] == 1

    def test_comment_deleted_floors_at_zero(self) -> None:
        snap = _make_snapshot()
        assert snap.get("comment_count", 0) == 0
        del_ev = {
            "schema_version": 1,
            "id": _EV_2,
            "ts": _TS_2,
            "type": "comment_deleted",
            "task_id": _TASK_ID,
            "actor": "agent:claude",
            "data": {"comment_id": "ev_doesnotexist"},
        }
        snap = apply_event_to_snapshot(snap, del_ev)
        assert snap["comment_count"] == 0


# ---------------------------------------------------------------------------
# apply_event_to_snapshot: relationship_added / relationship_removed
# ---------------------------------------------------------------------------


class TestRelationshipAdded:
    def test_appended_to_relationships_out(self) -> None:
        snap = _make_snapshot()
        assert snap["relationships_out"] == []
        ev = {
            "schema_version": 1,
            "id": _EV_2,
            "ts": _TS_2,
            "type": "relationship_added",
            "task_id": _TASK_ID,
            "actor": _ACTOR,
            "data": {
                "type": "blocks",
                "target_task_id": "task_01TARGET000000000000000000",
                "note": "Blocking deploy",
            },
        }
        snap = apply_event_to_snapshot(snap, ev)
        assert len(snap["relationships_out"]) == 1
        rel = snap["relationships_out"][0]
        assert rel["type"] == "blocks"
        assert rel["target_task_id"] == "task_01TARGET000000000000000000"
        assert rel["created_at"] == _TS_2
        assert rel["created_by"] == _ACTOR
        assert rel["note"] == "Blocking deploy"

    def test_note_defaults_to_none(self) -> None:
        snap = _make_snapshot()
        ev = {
            "schema_version": 1,
            "id": _EV_2,
            "ts": _TS_2,
            "type": "relationship_added",
            "task_id": _TASK_ID,
            "actor": _ACTOR,
            "data": {
                "type": "depends_on",
                "target_task_id": "task_01TARGET000000000000000000",
            },
        }
        snap = apply_event_to_snapshot(snap, ev)
        assert snap["relationships_out"][0]["note"] is None


class TestRelationshipRemoved:
    def test_removed_from_relationships_out(self) -> None:
        snap = _make_snapshot()
        target = "task_01TARGET000000000000000000"
        # Add two relationships.
        for ev_id, rel_type in [(_EV_2, "blocks"), (_EV_3, "depends_on")]:
            ev = {
                "schema_version": 1,
                "id": ev_id,
                "ts": _TS_2,
                "type": "relationship_added",
                "task_id": _TASK_ID,
                "actor": _ACTOR,
                "data": {"type": rel_type, "target_task_id": target},
            }
            snap = apply_event_to_snapshot(snap, ev)
        assert len(snap["relationships_out"]) == 2

        # Remove the "blocks" relationship.
        ev_remove = {
            "schema_version": 1,
            "id": "ev_01DDDDDDDDDDDDDDDDDDDDDDDD",
            "ts": _TS_3,
            "type": "relationship_removed",
            "task_id": _TASK_ID,
            "actor": _ACTOR,
            "data": {"type": "blocks", "target_task_id": target},
        }
        snap = apply_event_to_snapshot(snap, ev_remove)
        assert len(snap["relationships_out"]) == 1
        assert snap["relationships_out"][0]["type"] == "depends_on"


# ---------------------------------------------------------------------------
# apply_event_to_snapshot: artifact_attached
# ---------------------------------------------------------------------------


class TestArtifactAttached:
    def test_appended_to_evidence_refs(self) -> None:
        snap = _make_snapshot()
        assert snap["evidence_refs"] == []
        ev = {
            "schema_version": 1,
            "id": _EV_2,
            "ts": _TS_2,
            "type": "artifact_attached",
            "task_id": _TASK_ID,
            "actor": _ACTOR,
            "data": {"artifact_id": "art_01ARTIFACT00000000000000000"},
        }
        snap = apply_event_to_snapshot(snap, ev)
        assert snap["evidence_refs"] == [
            {"id": "art_01ARTIFACT00000000000000000", "role": None, "source_type": "artifact"}
        ]

    def test_stores_role(self) -> None:
        snap = _make_snapshot()
        ev = {
            "schema_version": 1,
            "id": _EV_2,
            "ts": _TS_2,
            "type": "artifact_attached",
            "task_id": _TASK_ID,
            "actor": _ACTOR,
            "data": {"artifact_id": "art_01ARTIFACT00000000000000000", "role": "review"},
        }
        snap = apply_event_to_snapshot(snap, ev)
        assert snap["evidence_refs"] == [
            {"id": "art_01ARTIFACT00000000000000000", "role": "review", "source_type": "artifact"}
        ]

    def test_deduplicates_by_artifact_id(self) -> None:
        snap = _make_snapshot()
        art_id = "art_01ARTIFACT00000000000000000"
        for ev_id in [_EV_2, _EV_3]:
            ev = {
                "schema_version": 1,
                "id": ev_id,
                "ts": _TS_2,
                "type": "artifact_attached",
                "task_id": _TASK_ID,
                "actor": _ACTOR,
                "data": {"artifact_id": art_id},
            }
            snap = apply_event_to_snapshot(snap, ev)
        assert len(snap["evidence_refs"]) == 1

    def test_multiple_artifacts(self) -> None:
        snap = _make_snapshot()
        for idx, ev_id in enumerate([_EV_2, _EV_3]):
            ev = {
                "schema_version": 1,
                "id": ev_id,
                "ts": _TS_2,
                "type": "artifact_attached",
                "task_id": _TASK_ID,
                "actor": _ACTOR,
                "data": {"artifact_id": f"art_{idx:026d}"},
            }
            snap = apply_event_to_snapshot(snap, ev)
        assert len(snap["evidence_refs"]) == 2


# ---------------------------------------------------------------------------
# get_artifact_roles helper
# ---------------------------------------------------------------------------


class TestGetArtifactRoles:
    def test_empty_refs(self) -> None:
        snap = _make_snapshot()
        assert get_artifact_roles(snap) == {}

    def test_evidence_refs_format(self) -> None:
        snap = _make_snapshot()
        snap["evidence_refs"] = [
            {"id": "art_A", "role": "review", "source_type": "artifact"},
            {"id": "art_B", "role": None, "source_type": "artifact"},
            {"id": "ev_C", "role": "review", "source_type": "comment"},
        ]
        assert get_artifact_roles(snap) == {"art_A": "review", "art_B": None}

    def test_legacy_artifact_refs_format(self) -> None:
        """Backward compat: old artifact_refs field still works."""
        snap = _make_snapshot()
        del snap["evidence_refs"]
        snap["artifact_refs"] = [
            {"id": "art_A", "role": "review"},
            {"id": "art_B", "role": None},
        ]
        assert get_artifact_roles(snap) == {"art_A": "review", "art_B": None}

    def test_legacy_bare_string_format(self) -> None:
        """Backward compat: bare string IDs map to None role."""
        snap = _make_snapshot()
        del snap["evidence_refs"]
        snap["artifact_refs"] = ["art_A", "art_B"]
        assert get_artifact_roles(snap) == {"art_A": None, "art_B": None}


# ---------------------------------------------------------------------------
# get_comment_role_refs helper
# ---------------------------------------------------------------------------


class TestGetCommentRoleRefs:
    def test_rebuild_from_created_event_initializes_empty_evidence_refs(self) -> None:
        """Replaying legacy task_created events always materializes evidence_refs."""
        created = _created_event()

        # Simulate an on-disk legacy snapshot from before evidence_refs existed.
        legacy_snapshot = apply_event_to_snapshot(None, created)
        del legacy_snapshot["evidence_refs"]
        assert "evidence_refs" not in legacy_snapshot

        rebuilt = apply_event_to_snapshot(None, created)
        assert rebuilt["evidence_refs"] == []
        assert get_comment_role_refs(rebuilt) == {}

    def test_rebuild_with_role_comment_preserves_comment_role_mapping(self) -> None:
        """Replay with role comments reconstructs comment role evidence deterministically."""
        created = _created_event()
        comment = {
            "schema_version": 1,
            "id": _EV_2,
            "ts": _TS_2,
            "type": "comment_added",
            "task_id": _TASK_ID,
            "actor": _ACTOR,
            "data": {"body": "Review complete", "role": "review"},
        }

        rebuilt = apply_event_to_snapshot(None, created)
        rebuilt = apply_event_to_snapshot(rebuilt, comment)

        assert get_comment_role_refs(rebuilt) == {_EV_2: "review"}
        assert rebuilt["evidence_refs"] == [
            {"id": _EV_2, "role": "review", "source_type": "comment"}
        ]

    def test_legacy_comment_role_refs_fallback(self) -> None:
        """Backward compat: old comment_role_refs field still works when evidence_refs absent."""
        snap = _make_snapshot()
        del snap["evidence_refs"]
        snap["comment_role_refs"] = [
            {"id": "ev_A", "role": "review"},
            {"id": "ev_B", "role": None},
        ]
        assert get_comment_role_refs(snap) == {"ev_A": "review", "ev_B": None}


# ---------------------------------------------------------------------------
# apply_event_to_snapshot: branch_linked / branch_unlinked
# ---------------------------------------------------------------------------


class TestBranchLinked:
    def test_appended_to_branch_links(self) -> None:
        snap = _make_snapshot()
        assert snap["branch_links"] == []
        ev = {
            "schema_version": 1,
            "id": _EV_2,
            "ts": _TS_2,
            "type": "branch_linked",
            "task_id": _TASK_ID,
            "actor": _ACTOR,
            "data": {"branch": "feat/LAT-42-login-fix", "repo": "lattice"},
        }
        snap = apply_event_to_snapshot(snap, ev)
        assert len(snap["branch_links"]) == 1
        bl = snap["branch_links"][0]
        assert bl["branch"] == "feat/LAT-42-login-fix"
        assert bl["repo"] == "lattice"
        assert bl["linked_at"] == _TS_2
        assert bl["linked_by"] == _ACTOR

    def test_repo_defaults_to_none(self) -> None:
        snap = _make_snapshot()
        ev = {
            "schema_version": 1,
            "id": _EV_2,
            "ts": _TS_2,
            "type": "branch_linked",
            "task_id": _TASK_ID,
            "actor": _ACTOR,
            "data": {"branch": "main"},
        }
        snap = apply_event_to_snapshot(snap, ev)
        assert snap["branch_links"][0]["repo"] is None

    def test_multiple_branches(self) -> None:
        snap = _make_snapshot()
        for idx, (ev_id, branch) in enumerate([(_EV_2, "feat/a"), (_EV_3, "feat/b")]):
            ev = {
                "schema_version": 1,
                "id": ev_id,
                "ts": _TS_2,
                "type": "branch_linked",
                "task_id": _TASK_ID,
                "actor": _ACTOR,
                "data": {"branch": branch},
            }
            snap = apply_event_to_snapshot(snap, ev)
        assert len(snap["branch_links"]) == 2
        assert snap["branch_links"][0]["branch"] == "feat/a"
        assert snap["branch_links"][1]["branch"] == "feat/b"


class TestBranchUnlinked:
    def test_removed_from_branch_links(self) -> None:
        snap = _make_snapshot()
        # Add two branch links
        for ev_id, branch in [(_EV_2, "feat/a"), (_EV_3, "feat/b")]:
            ev = {
                "schema_version": 1,
                "id": ev_id,
                "ts": _TS_2,
                "type": "branch_linked",
                "task_id": _TASK_ID,
                "actor": _ACTOR,
                "data": {"branch": branch, "repo": "lattice"},
            }
            snap = apply_event_to_snapshot(snap, ev)
        assert len(snap["branch_links"]) == 2

        # Remove "feat/a"
        ev_remove = {
            "schema_version": 1,
            "id": "ev_01DDDDDDDDDDDDDDDDDDDDDDDD",
            "ts": _TS_3,
            "type": "branch_unlinked",
            "task_id": _TASK_ID,
            "actor": _ACTOR,
            "data": {"branch": "feat/a", "repo": "lattice"},
        }
        snap = apply_event_to_snapshot(snap, ev_remove)
        assert len(snap["branch_links"]) == 1
        assert snap["branch_links"][0]["branch"] == "feat/b"

    def test_repo_matching(self) -> None:
        """Unlink must match both branch and repo."""
        snap = _make_snapshot()
        # Add branch with repo
        ev = {
            "schema_version": 1,
            "id": _EV_2,
            "ts": _TS_2,
            "type": "branch_linked",
            "task_id": _TASK_ID,
            "actor": _ACTOR,
            "data": {"branch": "main", "repo": "lattice"},
        }
        snap = apply_event_to_snapshot(snap, ev)
        # Add same branch without repo
        ev2 = {
            "schema_version": 1,
            "id": _EV_3,
            "ts": _TS_2,
            "type": "branch_linked",
            "task_id": _TASK_ID,
            "actor": _ACTOR,
            "data": {"branch": "main"},
        }
        snap = apply_event_to_snapshot(snap, ev2)
        assert len(snap["branch_links"]) == 2

        # Remove only the one with repo=None
        ev_remove = {
            "schema_version": 1,
            "id": "ev_01DDDDDDDDDDDDDDDDDDDDDDDD",
            "ts": _TS_3,
            "type": "branch_unlinked",
            "task_id": _TASK_ID,
            "actor": _ACTOR,
            "data": {"branch": "main"},
        }
        snap = apply_event_to_snapshot(snap, ev_remove)
        assert len(snap["branch_links"]) == 1
        assert snap["branch_links"][0]["repo"] == "lattice"


# ---------------------------------------------------------------------------
# apply_event_to_snapshot: git_event (no-op beyond bookkeeping)
# ---------------------------------------------------------------------------


class TestGitEvent:
    def test_recognised_no_field_changes(self) -> None:
        snap = _make_snapshot()
        original_title = snap["title"]
        ev = {
            "schema_version": 1,
            "id": _EV_2,
            "ts": _TS_2,
            "type": "git_event",
            "task_id": _TASK_ID,
            "actor": "agent:ci",
            "data": {"action": "commit", "sha": "abc123", "ref": "main"},
        }
        snap = apply_event_to_snapshot(snap, ev)
        assert snap["title"] == original_title
        assert snap["last_event_id"] == _EV_2
        assert snap["updated_at"] == _TS_2


# ---------------------------------------------------------------------------
# apply_event_to_snapshot: task_archived (no-op beyond bookkeeping)
# ---------------------------------------------------------------------------


class TestTaskArchived:
    def test_no_field_changes(self) -> None:
        snap = _make_snapshot()
        original_status = snap["status"]
        ev = {
            "schema_version": 1,
            "id": _EV_2,
            "ts": _TS_2,
            "type": "task_archived",
            "task_id": _TASK_ID,
            "actor": _ACTOR,
            "data": {},
        }
        snap = apply_event_to_snapshot(snap, ev)
        assert snap["status"] == original_status
        assert snap["last_event_id"] == _EV_2
        assert snap["updated_at"] == _TS_2


# ---------------------------------------------------------------------------
# apply_event_to_snapshot: custom x_ type
# ---------------------------------------------------------------------------


class TestCustomEventType:
    def test_x_event_no_field_changes(self) -> None:
        snap = _make_snapshot()
        original_title = snap["title"]
        ev = {
            "schema_version": 1,
            "id": _EV_2,
            "ts": _TS_2,
            "type": "x_deployment_started",
            "task_id": _TASK_ID,
            "actor": "agent:deploy",
            "data": {"env": "staging"},
        }
        snap = apply_event_to_snapshot(snap, ev)
        assert snap["title"] == original_title
        assert snap["last_event_id"] == _EV_2
        assert snap["updated_at"] == _TS_2


# ---------------------------------------------------------------------------
# Bookkeeping: every event type updates last_event_id and updated_at
# ---------------------------------------------------------------------------


class TestBookkeepingAlwaysUpdated:
    """Verify that last_event_id and updated_at change for every event."""

    @pytest.fixture()
    def base_snapshot(self) -> dict:
        return _make_snapshot()

    _EVENT_TYPES_AND_DATA: list[tuple[str, dict]] = [
        ("status_changed", {"from": "backlog", "to": "in_planning"}),
        ("assignment_changed", {"from": "agent:claude", "to": "agent:codex"}),
        ("field_updated", {"field": "title", "from": "old", "to": "new"}),
        ("comment_added", {"body": "test"}),
        ("comment_deleted", {"comment_id": "ev_01AAAAAAAAAAAAAAAAAAAAAAAAAA"}),
        (
            "relationship_added",
            {"type": "blocks", "target_task_id": "task_01TARGET000000000000000000"},
        ),
        (
            "relationship_removed",
            {"type": "blocks", "target_task_id": "task_01NONEXISTENT000000000000"},
        ),
        ("artifact_attached", {"artifact_id": "art_01ARTIFACT00000000000000000"}),
        ("git_event", {"action": "commit", "sha": "abc", "ref": "main"}),
        ("task_archived", {}),
        ("branch_linked", {"branch": "feat/test"}),
        ("branch_unlinked", {"branch": "feat/test"}),
        ("x_custom_thing", {"key": "value"}),
    ]

    @pytest.mark.parametrize(
        ("etype", "data"),
        _EVENT_TYPES_AND_DATA,
        ids=[t[0] for t in _EVENT_TYPES_AND_DATA],
    )
    def test_updates_bookkeeping(self, base_snapshot: dict, etype: str, data: dict) -> None:
        ev = {
            "schema_version": 1,
            "id": _EV_2,
            "ts": _TS_2,
            "type": etype,
            "task_id": _TASK_ID,
            "actor": _ACTOR,
            "data": data,
        }
        snap = apply_event_to_snapshot(base_snapshot, ev)
        assert snap["last_event_id"] == _EV_2
        assert snap["updated_at"] == _TS_2


# ---------------------------------------------------------------------------
# apply_event_to_snapshot: error on missing snapshot for non-create
# ---------------------------------------------------------------------------


class TestNonCreateWithoutSnapshot:
    def test_raises_value_error(self) -> None:
        ev = {
            "schema_version": 1,
            "id": _EV_2,
            "ts": _TS_2,
            "type": "status_changed",
            "task_id": _TASK_ID,
            "actor": _ACTOR,
            "data": {"from": "a", "to": "b"},
        }
        with pytest.raises(ValueError, match="Cannot apply event type"):
            apply_event_to_snapshot(None, ev)


# ---------------------------------------------------------------------------
# serialize_snapshot
# ---------------------------------------------------------------------------


class TestSerializeSnapshot:
    def test_pretty_json(self) -> None:
        snap = _make_snapshot()
        output = serialize_snapshot(snap)
        parsed = json.loads(output)
        assert parsed == snap

    def test_sorted_keys(self) -> None:
        snap = {"z_field": 1, "a_field": 2}
        output = serialize_snapshot(snap)
        assert output.index('"a_field"') < output.index('"z_field"')

    def test_trailing_newline(self) -> None:
        output = serialize_snapshot({"x": 1})
        assert output.endswith("\n")
        assert not output.endswith("\n\n")

    def test_two_space_indent(self) -> None:
        snap = {"key": {"nested": True}}
        output = serialize_snapshot(snap)
        # "nested" should be indented by 4 spaces (2 levels of 2-space indent)
        assert '    "nested"' in output


# ---------------------------------------------------------------------------
# compact_snapshot
# ---------------------------------------------------------------------------


class TestCompactSnapshot:
    def test_expected_fields_only(self) -> None:
        snap = _make_snapshot()
        compact = compact_snapshot(snap)
        expected_keys = {
            "id",
            "title",
            "status",
            "priority",
            "urgency",
            "complexity",
            "type",
            "assigned_to",
            "tags",
            "done_at",
            "comment_count",
            "reopened_count",
            "relationships_out_count",
            "evidence_ref_count",
            "branch_link_count",
        }
        assert set(compact.keys()) == expected_keys

    def test_field_values(self) -> None:
        snap = _make_snapshot()
        compact = compact_snapshot(snap)
        assert compact["id"] == _TASK_ID
        assert compact["title"] == "Fix login bug"
        assert compact["status"] == "backlog"
        assert compact["priority"] == "high"
        assert compact["urgency"] == "normal"
        assert compact["type"] == "bug"
        assert compact["assigned_to"] == "agent:claude"
        assert compact["tags"] == ["auth", "urgent"]

    def test_counts_computed_correctly(self) -> None:
        snap = _make_snapshot()
        # Add one relationship and two artifacts.
        snap["relationships_out"] = [
            {
                "type": "blocks",
                "target_task_id": "task_T",
                "created_at": _TS_1,
                "created_by": _ACTOR,
                "note": None,
            },
        ]
        snap["evidence_refs"] = [
            {"id": "art_A", "role": None, "source_type": "artifact"},
            {"id": "art_B", "role": "review", "source_type": "artifact"},
        ]

        compact = compact_snapshot(snap)
        assert compact["relationships_out_count"] == 1
        assert compact["evidence_ref_count"] == 2

    def test_empty_collections(self) -> None:
        snap = _make_snapshot()
        compact = compact_snapshot(snap)
        assert compact["comment_count"] == 0
        assert compact["relationships_out_count"] == 0
        assert compact["evidence_ref_count"] == 0
        assert compact["branch_link_count"] == 0

    def test_done_at_included(self) -> None:
        snap = _make_snapshot()
        compact = compact_snapshot(snap)
        assert "done_at" in compact
        assert compact["done_at"] is None

    def test_excludes_large_fields(self) -> None:
        snap = _make_snapshot()
        compact = compact_snapshot(snap)
        assert "description" not in compact
        assert "created_by" not in compact
        assert "created_at" not in compact
        assert "updated_at" not in compact
        assert "relationships_out" not in compact
        assert "evidence_refs" not in compact
        assert "branch_links" not in compact
        assert "custom_fields" not in compact
        assert "last_event_id" not in compact


# ---------------------------------------------------------------------------
# Mutation registry completeness
# ---------------------------------------------------------------------------


_TASK_EVENT_TYPES = sorted(BUILTIN_EVENT_TYPES - RESOURCE_EVENT_TYPES)
_COVERED_TYPES = set(_MUTATION_HANDLERS.keys()) | set(_NOOP_EVENT_TYPES) | {"task_created"}


class TestMutationRegistryCompleteness:
    """Every BUILTIN_EVENT_TYPES entry is either in the handler registry,
    the noop set, or is ``task_created`` (handled in the main switch)."""

    @pytest.mark.parametrize("etype", _TASK_EVENT_TYPES)
    def test_builtin_type_is_handled(self, etype: str) -> None:
        """Each built-in task event type has a handler, is noop, or is task_created."""
        assert etype in _COVERED_TYPES, (
            f"Event type '{etype}' has no mutation handler and is not in _NOOP_EVENT_TYPES"
        )

    @pytest.mark.parametrize("etype", sorted(_MUTATION_HANDLERS.keys()))
    def test_handler_not_also_noop(self, etype: str) -> None:
        """A type with a mutation handler should not also be in the noop set."""
        assert etype not in _NOOP_EVENT_TYPES, (
            f"Event type '{etype}' is in both handler registry and noop set"
        )


# ---------------------------------------------------------------------------
# compute_epic_derived_status
# ---------------------------------------------------------------------------


class TestComputeEpicDerivedStatus:
    """Tests for epic derived status computation."""

    def _snap(self, task_id: str, status: str = "backlog", task_type: str = "task") -> dict:
        return {"id": task_id, "status": status, "type": task_type}

    def _link(self, child: str, parent: str) -> dict:
        return {"source": child, "target": parent, "type": "subtask_of"}

    def test_no_children(self) -> None:
        epic = self._snap("epic_1", "backlog", "epic")
        result = compute_epic_derived_status("epic_1", [epic], [])
        assert result["derived_status"] is None
        assert result["progress"] == {"done": 0, "total": 0, "cancelled": 0}
        assert result["child_ids"] == []

    def test_all_children_done(self) -> None:
        epic = self._snap("epic_1", "backlog", "epic")
        c1 = self._snap("task_1", "done")
        c2 = self._snap("task_2", "done")
        links = [self._link("task_1", "epic_1"), self._link("task_2", "epic_1")]
        result = compute_epic_derived_status("epic_1", [epic, c1, c2], links)
        assert result["derived_status"] == "done"
        assert result["progress"] == {"done": 2, "total": 2, "cancelled": 0}

    def test_mixed_statuses_picks_highest_urgency(self) -> None:
        epic = self._snap("epic_1", "backlog", "epic")
        c1 = self._snap("task_1", "in_progress")
        c2 = self._snap("task_2", "backlog")
        c3 = self._snap("task_3", "done")
        links = [
            self._link("task_1", "epic_1"),
            self._link("task_2", "epic_1"),
            self._link("task_3", "epic_1"),
        ]
        result = compute_epic_derived_status("epic_1", [epic, c1, c2, c3], links)
        assert result["derived_status"] == "in_progress"
        assert result["progress"] == {"done": 1, "total": 3, "cancelled": 0}

    def test_needs_human_takes_precedence(self) -> None:
        epic = self._snap("epic_1", "backlog", "epic")
        c1 = self._snap("task_1", "in_progress")
        c2 = self._snap("task_2", "needs_human")
        links = [self._link("task_1", "epic_1"), self._link("task_2", "epic_1")]
        result = compute_epic_derived_status("epic_1", [epic, c1, c2], links)
        assert result["derived_status"] == "needs_human"
        assert result["health"] == {"blocked": 0, "needs_human": 1}

    def test_blocked_counted_in_health(self) -> None:
        epic = self._snap("epic_1", "backlog", "epic")
        c1 = self._snap("task_1", "blocked")
        c2 = self._snap("task_2", "backlog")
        links = [self._link("task_1", "epic_1"), self._link("task_2", "epic_1")]
        result = compute_epic_derived_status("epic_1", [epic, c1, c2], links)
        assert result["derived_status"] == "blocked"
        assert result["health"] == {"blocked": 1, "needs_human": 0}

    def test_cancelled_excluded_from_total(self) -> None:
        epic = self._snap("epic_1", "backlog", "epic")
        c1 = self._snap("task_1", "done")
        c2 = self._snap("task_2", "cancelled")
        links = [self._link("task_1", "epic_1"), self._link("task_2", "epic_1")]
        result = compute_epic_derived_status("epic_1", [epic, c1, c2], links)
        assert result["derived_status"] == "done"
        assert result["progress"] == {"done": 1, "total": 1, "cancelled": 1}

    def test_recursive_children(self) -> None:
        """subtask_of chains: task_2 -> task_1 -> epic_1."""
        epic = self._snap("epic_1", "backlog", "epic")
        c1 = self._snap("task_1", "in_progress")
        c2 = self._snap("task_2", "backlog")
        links = [self._link("task_1", "epic_1"), self._link("task_2", "task_1")]
        result = compute_epic_derived_status("epic_1", [epic, c1, c2], links)
        assert set(result["child_ids"]) == {"task_1", "task_2"}
        assert result["derived_status"] == "in_progress"
        assert result["progress"]["total"] == 2

    def test_child_ids_returned(self) -> None:
        epic = self._snap("epic_1", "backlog", "epic")
        c1 = self._snap("task_1", "backlog")
        c2 = self._snap("task_2", "in_progress")
        links = [self._link("task_1", "epic_1"), self._link("task_2", "epic_1")]
        result = compute_epic_derived_status("epic_1", [epic, c1, c2], links)
        assert set(result["child_ids"]) == {"task_1", "task_2"}
