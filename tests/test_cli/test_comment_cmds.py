"""Tests for enhanced comment CLI commands: comment (with --reply-to),
comment-edit, comment-delete, react, unreact, and comments (read)."""

from __future__ import annotations

import json


# ---------------------------------------------------------------------------
# Helper to add a comment and get its event ID
# ---------------------------------------------------------------------------


def _add_comment(invoke, task_id: str, body: str, reply_to: str | None = None) -> str:
    """Add a comment and return the event (comment) ID."""
    args = ["comment", task_id, body, "--actor", "human:test", "--json"]
    if reply_to:
        args.extend(["--reply-to", reply_to])
    result = invoke(*args)
    assert result.exit_code == 0, f"comment failed: {result.output}"
    # The event ID is in the events list — get the last comment_added event
    parsed = json.loads(result.output)
    snapshot = parsed["data"]
    # Read events to find the comment ID
    return snapshot["last_event_id"]


# ---------------------------------------------------------------------------
# lattice comment --reply-to
# ---------------------------------------------------------------------------


class TestCommentReplyTo:
    def test_reply_to_top_level(self, invoke, create_task) -> None:
        task = create_task("Comment test")
        task_id = task["id"]
        # Add a top-level comment
        comment_id = _add_comment(invoke, task_id, "top-level comment")
        # Reply to it
        result = invoke(
            "comment", task_id, "this is a reply",
            "--reply-to", comment_id,
            "--actor", "human:test",
        )
        assert result.exit_code == 0
        assert "Reply added" in result.output

    def test_reply_to_nonexistent(self, invoke, create_task) -> None:
        task = create_task("Comment test")
        result = invoke(
            "comment", task["id"], "orphan reply",
            "--reply-to", "ev_nonexistent",
            "--actor", "human:test",
        )
        assert result.exit_code != 0
        assert "not found" in result.output.lower()

    def test_reply_to_reply_rejected(self, invoke, create_task) -> None:
        task = create_task("Comment test")
        task_id = task["id"]
        comment_id = _add_comment(invoke, task_id, "top-level")
        reply_id = _add_comment(invoke, task_id, "reply", reply_to=comment_id)
        result = invoke(
            "comment", task_id, "nested reply",
            "--reply-to", reply_id,
            "--actor", "human:test",
        )
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# lattice comment-edit
# ---------------------------------------------------------------------------


class TestCommentEdit:
    def test_edit_comment(self, invoke, create_task) -> None:
        task = create_task("Edit test")
        task_id = task["id"]
        comment_id = _add_comment(invoke, task_id, "original text")
        result = invoke(
            "comment-edit", task_id, comment_id, "edited text",
            "--actor", "human:test",
        )
        assert result.exit_code == 0
        assert "edited" in result.output.lower() or "Comment edited" in result.output

    def test_edit_nonexistent(self, invoke, create_task) -> None:
        task = create_task("Edit test")
        result = invoke(
            "comment-edit", task["id"], "ev_nonexistent", "new text",
            "--actor", "human:test",
        )
        assert result.exit_code != 0

    def test_edit_deleted_comment(self, invoke, create_task) -> None:
        task = create_task("Edit test")
        task_id = task["id"]
        comment_id = _add_comment(invoke, task_id, "to be deleted")
        invoke("comment-delete", task_id, comment_id, "--actor", "human:test")
        result = invoke(
            "comment-edit", task_id, comment_id, "should fail",
            "--actor", "human:test",
        )
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# lattice comment-delete
# ---------------------------------------------------------------------------


class TestCommentDelete:
    def test_delete_comment(self, invoke, create_task) -> None:
        task = create_task("Delete test")
        task_id = task["id"]
        comment_id = _add_comment(invoke, task_id, "to be deleted")
        result = invoke(
            "comment-delete", task_id, comment_id,
            "--actor", "human:test",
        )
        assert result.exit_code == 0

    def test_delete_already_deleted(self, invoke, create_task) -> None:
        task = create_task("Delete test")
        task_id = task["id"]
        comment_id = _add_comment(invoke, task_id, "to be deleted")
        invoke("comment-delete", task_id, comment_id, "--actor", "human:test")
        result = invoke(
            "comment-delete", task_id, comment_id,
            "--actor", "human:test",
        )
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# lattice react / unreact
# ---------------------------------------------------------------------------


class TestReact:
    def test_add_reaction(self, invoke, create_task) -> None:
        task = create_task("React test")
        task_id = task["id"]
        comment_id = _add_comment(invoke, task_id, "react to this")
        result = invoke(
            "react", task_id, comment_id, "thumbsup",
            "--actor", "human:test",
        )
        assert result.exit_code == 0

    def test_idempotent_reaction(self, invoke, create_task) -> None:
        task = create_task("React test")
        task_id = task["id"]
        comment_id = _add_comment(invoke, task_id, "react twice")
        invoke("react", task_id, comment_id, "thumbsup", "--actor", "human:test")
        result = invoke(
            "react", task_id, comment_id, "thumbsup",
            "--actor", "human:test",
        )
        assert result.exit_code == 0  # no-op, should succeed

    def test_invalid_emoji(self, invoke, create_task) -> None:
        task = create_task("React test")
        task_id = task["id"]
        comment_id = _add_comment(invoke, task_id, "bad emoji")
        result = invoke(
            "react", task_id, comment_id, ":bad:",
            "--actor", "human:test",
        )
        assert result.exit_code != 0


class TestUnreact:
    def test_remove_reaction(self, invoke, create_task) -> None:
        task = create_task("Unreact test")
        task_id = task["id"]
        comment_id = _add_comment(invoke, task_id, "unreact from this")
        invoke("react", task_id, comment_id, "thumbsup", "--actor", "human:test")
        result = invoke(
            "unreact", task_id, comment_id, "thumbsup",
            "--actor", "human:test",
        )
        assert result.exit_code == 0

    def test_unreact_nonexistent(self, invoke, create_task) -> None:
        task = create_task("Unreact test")
        task_id = task["id"]
        comment_id = _add_comment(invoke, task_id, "no reactions")
        result = invoke(
            "unreact", task_id, comment_id, "thumbsup",
            "--actor", "human:test",
        )
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# lattice comments (read command)
# ---------------------------------------------------------------------------


class TestCommentsReadCommand:
    def test_empty_comments(self, invoke, create_task) -> None:
        task = create_task("No comments")
        result = invoke("comments", task["id"])
        assert result.exit_code == 0
        assert "No comments" in result.output

    def test_shows_comments(self, invoke, create_task) -> None:
        task = create_task("With comments")
        task_id = task["id"]
        _add_comment(invoke, task_id, "hello world")
        result = invoke("comments", task_id)
        assert result.exit_code == 0
        assert "hello world" in result.output

    def test_json_output(self, invoke, create_task) -> None:
        task = create_task("JSON comments")
        task_id = task["id"]
        _add_comment(invoke, task_id, "json test")
        result = invoke("comments", task_id, "--json")
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["ok"] is True
        assert len(parsed["data"]) == 1
        assert parsed["data"][0]["body"] == "json test"

    def test_shows_threaded(self, invoke, create_task) -> None:
        task = create_task("Threaded")
        task_id = task["id"]
        comment_id = _add_comment(invoke, task_id, "parent comment")
        _add_comment(invoke, task_id, "child reply", reply_to=comment_id)
        result = invoke("comments", task_id)
        assert result.exit_code == 0
        assert "parent comment" in result.output
        assert "child reply" in result.output

    def test_shows_deleted_placeholder(self, invoke, create_task) -> None:
        task = create_task("Deleted")
        task_id = task["id"]
        comment_id = _add_comment(invoke, task_id, "will be deleted")
        invoke("comment-delete", task_id, comment_id, "--actor", "human:test")
        result = invoke("comments", task_id)
        assert result.exit_code == 0
        assert "[deleted]" in result.output.lower()


# ---------------------------------------------------------------------------
# lattice comment --role
# ---------------------------------------------------------------------------


class TestCommentRole:
    def test_comment_with_role_stored_in_snapshot(self, invoke, create_task) -> None:
        """--role stores role in snapshot's comment_role_refs."""
        task = create_task("Role test")
        task_id = task["id"]

        result = invoke(
            "comment", task_id, "LGTM — all good",
            "--role", "review",
            "--actor", "human:test",
            "--json",
        )
        assert result.exit_code == 0, result.output
        snapshot = json.loads(result.output)["data"]
        refs = snapshot.get("comment_role_refs", [])
        assert len(refs) == 1
        assert refs[0]["role"] == "review"

    def test_comment_without_role_not_tracked(self, invoke, create_task) -> None:
        """Comment without --role does not populate comment_role_refs."""
        task = create_task("No role")
        task_id = task["id"]

        result = invoke(
            "comment", task_id, "Just a note",
            "--actor", "human:test",
            "--json",
        )
        assert result.exit_code == 0
        snapshot = json.loads(result.output)["data"]
        assert snapshot.get("comment_role_refs", []) == []

    def test_deleted_role_comment_removed_from_refs(self, invoke, create_task) -> None:
        """Deleting a role comment removes it from comment_role_refs."""
        task = create_task("Delete role")
        task_id = task["id"]

        result = invoke(
            "comment", task_id, "review findings",
            "--role", "review",
            "--actor", "human:test",
            "--json",
        )
        comment_id = json.loads(result.output)["data"]["last_event_id"]

        result = invoke("comment-delete", task_id, comment_id, "--actor", "human:test", "--json")
        assert result.exit_code == 0
        snapshot = json.loads(result.output)["data"]
        assert snapshot.get("comment_role_refs", []) == []
