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
    get_artifact_roles,
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
        assert snap["artifact_refs"] == []
        assert snap["branch_links"] == []
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
    def test_only_bookkeeping_changes(self) -> None:
        snap = _make_snapshot()
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
        assert snap["status"] == original_status
        assert snap["title"] == original_title
        assert snap["last_event_id"] == _EV_2
        assert snap["updated_at"] == _TS_2


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
    def test_appended_to_artifact_refs(self) -> None:
        snap = _make_snapshot()
        assert snap["artifact_refs"] == []
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
        assert snap["artifact_refs"] == [{"id": "art_01ARTIFACT00000000000000000", "role": None}]

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
        assert snap["artifact_refs"] == [
            {"id": "art_01ARTIFACT00000000000000000", "role": "review"}
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
        assert len(snap["artifact_refs"]) == 1

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
        assert len(snap["artifact_refs"]) == 2


# ---------------------------------------------------------------------------
# get_artifact_roles helper
# ---------------------------------------------------------------------------


class TestGetArtifactRoles:
    def test_empty_refs(self) -> None:
        snap = _make_snapshot()
        assert get_artifact_roles(snap) == {}

    def test_new_format(self) -> None:
        snap = _make_snapshot()
        snap["artifact_refs"] = [
            {"id": "art_A", "role": "review"},
            {"id": "art_B", "role": None},
        ]
        assert get_artifact_roles(snap) == {"art_A": "review", "art_B": None}

    def test_old_format(self) -> None:
        """Backward compat: bare string IDs map to None role."""
        snap = _make_snapshot()
        snap["artifact_refs"] = ["art_A", "art_B"]
        assert get_artifact_roles(snap) == {"art_A": None, "art_B": None}

    def test_mixed_format(self) -> None:
        """Handle a mix of old and new format refs."""
        snap = _make_snapshot()
        snap["artifact_refs"] = [
            "art_A",
            {"id": "art_B", "role": "review"},
        ]
        roles = get_artifact_roles(snap)
        assert roles == {"art_A": None, "art_B": "review"}


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
            "relationships_out_count",
            "artifact_ref_count",
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
        snap["artifact_refs"] = [
            {"id": "art_A", "role": None},
            {"id": "art_B", "role": "review"},
        ]

        compact = compact_snapshot(snap)
        assert compact["relationships_out_count"] == 1
        assert compact["artifact_ref_count"] == 2

    def test_empty_collections(self) -> None:
        snap = _make_snapshot()
        compact = compact_snapshot(snap)
        assert compact["relationships_out_count"] == 0
        assert compact["artifact_ref_count"] == 0
        assert compact["branch_link_count"] == 0

    def test_excludes_large_fields(self) -> None:
        snap = _make_snapshot()
        compact = compact_snapshot(snap)
        assert "description" not in compact
        assert "created_by" not in compact
        assert "created_at" not in compact
        assert "updated_at" not in compact
        assert "relationships_out" not in compact
        assert "artifact_refs" not in compact
        assert "branch_links" not in compact
        assert "custom_fields" not in compact
        assert "last_event_id" not in compact


# ---------------------------------------------------------------------------
# Mutation registry completeness
# ---------------------------------------------------------------------------


class TestMutationRegistryCompleteness:
    """Every BUILTIN_EVENT_TYPES entry is either in the handler registry,
    the noop set, or is ``task_created`` (handled in the main switch)."""

    def test_all_builtin_types_covered(self) -> None:
        handled = set(_MUTATION_HANDLERS.keys())
        noop = set(_NOOP_EVENT_TYPES)
        init_type = {"task_created"}  # handled separately in apply_event_to_snapshot

        covered = handled | noop | init_type
        # Resource event types are handled by a separate materialization path
        # (core/resources.py), not by the task snapshot materializer.
        task_event_types = BUILTIN_EVENT_TYPES - RESOURCE_EVENT_TYPES
        missing = task_event_types - covered
        assert not missing, f"Unhandled builtin event types: {missing}"

    def test_no_handler_also_in_noop(self) -> None:
        """A type should not be in both the handler registry and the noop set."""
        overlap = set(_MUTATION_HANDLERS.keys()) & _NOOP_EVENT_TYPES
        assert not overlap, f"Types in both handler and noop: {overlap}"
