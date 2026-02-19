"""Tests for lattice.core.comments — pure comment materialization and validation."""

from __future__ import annotations

import pytest

from lattice.core.comments import (
    materialize_comments,
    validate_comment_body,
    validate_comment_for_delete,
    validate_comment_for_edit,
    validate_comment_for_react,
    validate_comment_for_reply,
    validate_emoji,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _comment_event(
    event_id: str,
    body: str,
    actor: str = "human:atin",
    parent_id: str | None = None,
    role: str | None = None,
) -> dict:
    data: dict = {"body": body}
    if parent_id is not None:
        data["parent_id"] = parent_id
    if role is not None:
        data["role"] = role
    return {
        "id": event_id,
        "type": "comment_added",
        "ts": "2026-02-17T01:00:00Z",
        "actor": actor,
        "data": data,
    }


def _edit_event(
    comment_id: str,
    body: str,
    previous_body: str,
    actor: str = "human:atin",
    role: str | None = None,
) -> dict:
    data: dict = {"comment_id": comment_id, "body": body, "previous_body": previous_body}
    if role is not None:
        data["role"] = role
    return {
        "id": f"ev_edit_{comment_id}",
        "type": "comment_edited",
        "ts": "2026-02-17T01:01:00Z",
        "actor": actor,
        "data": data,
    }


def _delete_event(comment_id: str, actor: str = "human:atin") -> dict:
    return {
        "id": f"ev_del_{comment_id}",
        "type": "comment_deleted",
        "ts": "2026-02-17T01:02:00Z",
        "actor": actor,
        "data": {"comment_id": comment_id},
    }


def _react_event(comment_id: str, emoji: str, actor: str = "human:atin") -> dict:
    return {
        "id": f"ev_react_{comment_id}_{emoji}",
        "type": "reaction_added",
        "ts": "2026-02-17T01:03:00Z",
        "actor": actor,
        "data": {"comment_id": comment_id, "emoji": emoji},
    }


def _unreact_event(comment_id: str, emoji: str, actor: str = "human:atin") -> dict:
    return {
        "id": f"ev_unreact_{comment_id}_{emoji}",
        "type": "reaction_removed",
        "ts": "2026-02-17T01:04:00Z",
        "actor": actor,
        "data": {"comment_id": comment_id, "emoji": emoji},
    }


# ---------------------------------------------------------------------------
# validate_emoji
# ---------------------------------------------------------------------------


class TestValidateEmoji:
    def test_valid_alphanumeric(self) -> None:
        assert validate_emoji("thumbsup") is True

    def test_valid_with_underscores(self) -> None:
        assert validate_emoji("thumbs_up") is True

    def test_valid_with_hyphens(self) -> None:
        assert validate_emoji("thumbs-up") is True

    def test_empty_string(self) -> None:
        assert validate_emoji("") is False

    def test_too_long(self) -> None:
        assert validate_emoji("a" * 51) is False

    def test_max_length(self) -> None:
        assert validate_emoji("a" * 50) is True

    def test_special_chars_invalid(self) -> None:
        assert validate_emoji(":thumbsup:") is False
        assert validate_emoji("thumbs up") is False


# ---------------------------------------------------------------------------
# materialize_comments
# ---------------------------------------------------------------------------


class TestMaterializeComments:
    def test_empty_events(self) -> None:
        assert materialize_comments([]) == []

    def test_single_comment(self) -> None:
        events = [_comment_event("ev_1", "hello")]
        result = materialize_comments(events)
        assert len(result) == 1
        assert result[0]["id"] == "ev_1"
        assert result[0]["body"] == "hello"
        assert result[0]["deleted"] is False
        assert result[0]["edited"] is False
        assert result[0]["replies"] == []

    def test_threaded_reply(self) -> None:
        events = [
            _comment_event("ev_1", "top-level"),
            _comment_event("ev_2", "reply", parent_id="ev_1"),
        ]
        result = materialize_comments(events)
        assert len(result) == 1
        assert result[0]["id"] == "ev_1"
        assert len(result[0]["replies"]) == 1
        assert result[0]["replies"][0]["id"] == "ev_2"
        assert result[0]["replies"][0]["parent_id"] == "ev_1"

    def test_edit_updates_body(self) -> None:
        events = [
            _comment_event("ev_1", "original"),
            _edit_event("ev_1", "edited text", "original"),
        ]
        result = materialize_comments(events)
        assert result[0]["body"] == "edited text"
        assert result[0]["edited"] is True
        assert len(result[0]["edit_history"]) == 1
        assert result[0]["edit_history"][0]["body"] == "original"

    def test_delete_marks_deleted(self) -> None:
        events = [
            _comment_event("ev_1", "hello"),
            _delete_event("ev_1"),
        ]
        result = materialize_comments(events)
        assert result[0]["deleted"] is True
        assert result[0]["deleted_by"] == "human:atin"

    def test_reaction_added(self) -> None:
        events = [
            _comment_event("ev_1", "hello"),
            _react_event("ev_1", "thumbsup"),
        ]
        result = materialize_comments(events)
        assert result[0]["reactions"] == {"thumbsup": ["human:atin"]}

    def test_reaction_removed(self) -> None:
        events = [
            _comment_event("ev_1", "hello"),
            _react_event("ev_1", "thumbsup"),
            _unreact_event("ev_1", "thumbsup"),
        ]
        result = materialize_comments(events)
        assert result[0]["reactions"] == {}

    def test_duplicate_reaction_idempotent(self) -> None:
        events = [
            _comment_event("ev_1", "hello"),
            _react_event("ev_1", "thumbsup"),
            _react_event("ev_1", "thumbsup"),
        ]
        result = materialize_comments(events)
        assert result[0]["reactions"] == {"thumbsup": ["human:atin"]}

    def test_multiple_reactors(self) -> None:
        events = [
            _comment_event("ev_1", "hello"),
            _react_event("ev_1", "thumbsup", actor="human:atin"),
            _react_event("ev_1", "thumbsup", actor="agent:claude"),
        ]
        result = materialize_comments(events)
        assert result[0]["reactions"] == {"thumbsup": ["human:atin", "agent:claude"]}

    def test_edit_after_delete_ignored(self) -> None:
        events = [
            _comment_event("ev_1", "hello"),
            _delete_event("ev_1"),
            _edit_event("ev_1", "should not apply", "hello"),
        ]
        result = materialize_comments(events)
        assert result[0]["deleted"] is True
        assert result[0]["body"] == "hello"  # unchanged

    def test_reaction_on_deleted_ignored(self) -> None:
        events = [
            _comment_event("ev_1", "hello"),
            _delete_event("ev_1"),
            _react_event("ev_1", "thumbsup"),
        ]
        result = materialize_comments(events)
        assert result[0]["reactions"] == {}

    def test_non_comment_events_ignored(self) -> None:
        events = [
            {
                "id": "ev_status",
                "type": "status_changed",
                "ts": "2026-01-01T00:00:00Z",
                "actor": "human:atin",
                "data": {"from": "backlog", "to": "in_progress"},
            },
            _comment_event("ev_1", "hello"),
        ]
        result = materialize_comments(events)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# Validation functions
# ---------------------------------------------------------------------------


class TestValidateCommentForReply:
    def test_valid_reply(self) -> None:
        events = [_comment_event("ev_1", "top-level")]
        validate_comment_for_reply(events, "ev_1")  # should not raise

    def test_reply_to_nonexistent(self) -> None:
        events = [_comment_event("ev_1", "top-level")]
        with pytest.raises(ValueError, match="not found"):
            validate_comment_for_reply(events, "ev_nonexistent")

    def test_reply_to_deleted(self) -> None:
        events = [_comment_event("ev_1", "top-level"), _delete_event("ev_1")]
        with pytest.raises(ValueError, match="deleted"):
            validate_comment_for_reply(events, "ev_1")

    def test_reply_to_reply(self) -> None:
        events = [
            _comment_event("ev_1", "top-level"),
            _comment_event("ev_2", "reply", parent_id="ev_1"),
        ]
        with pytest.raises(ValueError, match="reply"):
            validate_comment_for_reply(events, "ev_2")


class TestValidateCommentForEdit:
    def test_valid_edit(self) -> None:
        events = [_comment_event("ev_1", "original")]
        body, role = validate_comment_for_edit(events, "ev_1")
        assert body == "original"
        assert role is None

    def test_valid_edit_returns_role(self) -> None:
        events = [_comment_event("ev_1", "LGTM", role="review")]
        body, role = validate_comment_for_edit(events, "ev_1")
        assert body == "LGTM"
        assert role == "review"

    def test_edit_nonexistent(self) -> None:
        with pytest.raises(ValueError, match="not found"):
            validate_comment_for_edit([], "ev_nonexistent")

    def test_edit_deleted(self) -> None:
        events = [_comment_event("ev_1", "hello"), _delete_event("ev_1")]
        with pytest.raises(ValueError, match="deleted"):
            validate_comment_for_edit(events, "ev_1")


class TestValidateCommentForDelete:
    def test_valid_delete(self) -> None:
        events = [_comment_event("ev_1", "hello")]
        validate_comment_for_delete(events, "ev_1")  # should not raise

    def test_delete_nonexistent(self) -> None:
        with pytest.raises(ValueError, match="not found"):
            validate_comment_for_delete([], "ev_nonexistent")

    def test_delete_already_deleted(self) -> None:
        events = [_comment_event("ev_1", "hello"), _delete_event("ev_1")]
        with pytest.raises(ValueError, match="already deleted"):
            validate_comment_for_delete(events, "ev_1")


class TestValidateCommentForReact:
    def test_valid_react(self) -> None:
        events = [_comment_event("ev_1", "hello")]
        validate_comment_for_react(events, "ev_1")  # should not raise

    def test_react_nonexistent(self) -> None:
        with pytest.raises(ValueError, match="not found"):
            validate_comment_for_react([], "ev_nonexistent")

    def test_react_deleted(self) -> None:
        events = [_comment_event("ev_1", "hello"), _delete_event("ev_1")]
        with pytest.raises(ValueError, match="deleted"):
            validate_comment_for_react(events, "ev_1")


# ---------------------------------------------------------------------------
# validate_comment_body
# ---------------------------------------------------------------------------


class TestValidateCommentBody:
    def test_valid_body(self) -> None:
        assert validate_comment_body("hello") == "hello"

    def test_strips_whitespace(self) -> None:
        assert validate_comment_body("  hello  ") == "hello"

    def test_empty_string(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            validate_comment_body("")

    def test_whitespace_only(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            validate_comment_body("   ")

    def test_non_string(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            validate_comment_body(42)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Additional materialization edge cases
# ---------------------------------------------------------------------------


class TestMaterializeCommentsEdgeCases:
    def test_multiple_sequential_edits(self) -> None:
        """Two sequential edits should produce two edit_history entries."""
        events = [
            _comment_event("ev_1", "original"),
            _edit_event("ev_1", "first edit", "original"),
            {
                "id": "ev_edit2_ev_1",
                "type": "comment_edited",
                "ts": "2026-02-17T01:05:00Z",
                "actor": "human:atin",
                "data": {
                    "comment_id": "ev_1",
                    "body": "second edit",
                    "previous_body": "first edit",
                },
            },
        ]
        result = materialize_comments(events)
        assert result[0]["body"] == "second edit"
        assert result[0]["edited"] is True
        assert len(result[0]["edit_history"]) == 2
        assert result[0]["edit_history"][0]["body"] == "original"
        assert result[0]["edit_history"][1]["body"] == "first edit"

    def test_reaction_on_reply(self) -> None:
        """Reactions should work on reply comments (nested)."""
        events = [
            _comment_event("ev_1", "top-level"),
            _comment_event("ev_2", "reply", parent_id="ev_1"),
            _react_event("ev_2", "thumbsup"),
        ]
        result = materialize_comments(events)
        assert len(result) == 1  # only top-level
        reply = result[0]["replies"][0]
        assert reply["id"] == "ev_2"
        assert reply["reactions"] == {"thumbsup": ["human:atin"]}

    def test_edit_updates_role(self) -> None:
        """Editing a comment with --role updates the materialized role."""
        events = [
            _comment_event("ev_1", "looks good"),
            _edit_event("ev_1", "looks good", "looks good", role="review"),
        ]
        result = materialize_comments(events)
        assert result[0]["role"] == "review"

    def test_edit_preserves_role_without_role_in_event(self) -> None:
        """Editing body only preserves the existing role."""
        events = [
            _comment_event("ev_1", "LGTM", role="review"),
            _edit_event("ev_1", "LGTM — no issues found", "LGTM"),
        ]
        result = materialize_comments(events)
        assert result[0]["role"] == "review"
        assert result[0]["body"] == "LGTM — no issues found"
