"""Tests for enhanced comment CLI commands: comment (with --reply-to),
comment-edit, comment-delete, react, unreact, and comments (read)."""

from __future__ import annotations

import json

import pytest


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
            "comment",
            task_id,
            "this is a reply",
            "--reply-to",
            comment_id,
            "--actor",
            "human:test",
        )
        assert result.exit_code == 0
        assert "Reply added" in result.output

    def test_reply_to_nonexistent(self, invoke, create_task) -> None:
        task = create_task("Comment test")
        result = invoke(
            "comment",
            task["id"],
            "orphan reply",
            "--reply-to",
            "ev_nonexistent",
            "--actor",
            "human:test",
        )
        assert result.exit_code != 0
        assert "not found" in result.output.lower()

    def test_reply_to_reply_rejected(self, invoke, create_task) -> None:
        task = create_task("Comment test")
        task_id = task["id"]
        comment_id = _add_comment(invoke, task_id, "top-level")
        reply_id = _add_comment(invoke, task_id, "reply", reply_to=comment_id)
        result = invoke(
            "comment",
            task_id,
            "nested reply",
            "--reply-to",
            reply_id,
            "--actor",
            "human:test",
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
            "comment-edit",
            task_id,
            comment_id,
            "edited text",
            "--actor",
            "human:test",
        )
        assert result.exit_code == 0
        assert "edited" in result.output.lower() or "Comment edited" in result.output

    def test_edit_nonexistent(self, invoke, create_task) -> None:
        task = create_task("Edit test")
        result = invoke(
            "comment-edit",
            task["id"],
            "ev_nonexistent",
            "new text",
            "--actor",
            "human:test",
        )
        assert result.exit_code != 0

    def test_edit_deleted_comment(self, invoke, create_task) -> None:
        task = create_task("Edit test")
        task_id = task["id"]
        comment_id = _add_comment(invoke, task_id, "to be deleted")
        invoke("comment-delete", task_id, comment_id, "--actor", "human:test")
        result = invoke(
            "comment-edit",
            task_id,
            comment_id,
            "should fail",
            "--actor",
            "human:test",
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
            "comment-delete",
            task_id,
            comment_id,
            "--actor",
            "human:test",
        )
        assert result.exit_code == 0

    def test_delete_already_deleted(self, invoke, create_task) -> None:
        task = create_task("Delete test")
        task_id = task["id"]
        comment_id = _add_comment(invoke, task_id, "to be deleted")
        invoke("comment-delete", task_id, comment_id, "--actor", "human:test")
        result = invoke(
            "comment-delete",
            task_id,
            comment_id,
            "--actor",
            "human:test",
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
            "react",
            task_id,
            comment_id,
            "thumbsup",
            "--actor",
            "human:test",
        )
        assert result.exit_code == 0

    def test_idempotent_reaction(self, invoke, create_task) -> None:
        task = create_task("React test")
        task_id = task["id"]
        comment_id = _add_comment(invoke, task_id, "react twice")
        invoke("react", task_id, comment_id, "thumbsup", "--actor", "human:test")
        result = invoke(
            "react",
            task_id,
            comment_id,
            "thumbsup",
            "--actor",
            "human:test",
        )
        assert result.exit_code == 0  # no-op, should succeed

    def test_invalid_emoji(self, invoke, create_task) -> None:
        task = create_task("React test")
        task_id = task["id"]
        comment_id = _add_comment(invoke, task_id, "bad emoji")
        result = invoke(
            "react",
            task_id,
            comment_id,
            ":bad:",
            "--actor",
            "human:test",
        )
        assert result.exit_code != 0


class TestUnreact:
    def test_remove_reaction(self, invoke, create_task) -> None:
        task = create_task("Unreact test")
        task_id = task["id"]
        comment_id = _add_comment(invoke, task_id, "unreact from this")
        invoke("react", task_id, comment_id, "thumbsup", "--actor", "human:test")
        result = invoke(
            "unreact",
            task_id,
            comment_id,
            "thumbsup",
            "--actor",
            "human:test",
        )
        assert result.exit_code == 0

    def test_unreact_nonexistent(self, invoke, create_task) -> None:
        task = create_task("Unreact test")
        task_id = task["id"]
        comment_id = _add_comment(invoke, task_id, "no reactions")
        result = invoke(
            "unreact",
            task_id,
            comment_id,
            "thumbsup",
            "--actor",
            "human:test",
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
        """--role stores role in snapshot's evidence_refs."""
        task = create_task("Role test")
        task_id = task["id"]

        result = invoke(
            "comment",
            task_id,
            "LGTM — all good",
            "--role",
            "review",
            "--actor",
            "human:test",
            "--json",
        )
        assert result.exit_code == 0, result.output
        snapshot = json.loads(result.output)["data"]
        comment_refs = [
            r for r in snapshot.get("evidence_refs", []) if r.get("source_type") == "comment"
        ]
        assert len(comment_refs) == 1
        assert comment_refs[0]["role"] == "review"

    def test_comment_without_role_not_tracked(self, invoke, create_task) -> None:
        """Comment without --role does not add to evidence_refs."""
        task = create_task("No role")
        task_id = task["id"]

        result = invoke(
            "comment",
            task_id,
            "Just a note",
            "--actor",
            "human:test",
            "--json",
        )
        assert result.exit_code == 0
        snapshot = json.loads(result.output)["data"]
        comment_refs = [
            r for r in snapshot.get("evidence_refs", []) if r.get("source_type") == "comment"
        ]
        assert comment_refs == []

    def test_deleted_role_comment_removed_from_refs(self, invoke, create_task) -> None:
        """Deleting a role comment removes it from evidence_refs."""
        task = create_task("Delete role")
        task_id = task["id"]

        result = invoke(
            "comment",
            task_id,
            "review findings",
            "--role",
            "review",
            "--actor",
            "human:test",
            "--json",
        )
        comment_id = json.loads(result.output)["data"]["last_event_id"]

        result = invoke("comment-delete", task_id, comment_id, "--actor", "human:test", "--json")
        assert result.exit_code == 0
        snapshot = json.loads(result.output)["data"]
        comment_refs = [
            r for r in snapshot.get("evidence_refs", []) if r.get("source_type") == "comment"
        ]
        assert comment_refs == []


# ---------------------------------------------------------------------------
# Role validation (LAT-137)
# ---------------------------------------------------------------------------


class TestCommentRoleValidation:
    @pytest.fixture(autouse=True)
    def _with_policies(self, initialized_root_with_policies) -> None:
        """Ensure standard completion policies are active."""

    def test_typo_role_rejected(self, invoke, create_task) -> None:
        """Typo'd role produces an error with valid role list."""
        task = create_task("Role validation")
        result = invoke(
            "comment",
            task["id"],
            "looks good",
            "--role",
            "reveiw",
            "--actor",
            "human:test",
            "--json",
        )
        assert result.exit_code != 0
        parsed = json.loads(result.output)
        assert parsed["ok"] is False
        assert parsed["error"]["code"] == "INVALID_ROLE"
        assert "reveiw" in parsed["error"]["message"]
        assert "review" in parsed["error"]["message"]

    def test_valid_role_accepted(self, invoke, create_task) -> None:
        """Valid role (matching completion policy) succeeds."""
        task = create_task("Role validation OK")
        result = invoke(
            "comment",
            task["id"],
            "all good",
            "--role",
            "review",
            "--actor",
            "human:test",
            "--json",
        )
        assert result.exit_code == 0

    def test_no_role_no_validation(self, invoke, create_task) -> None:
        """Comment without --role skips role validation."""
        task = create_task("No role")
        result = invoke(
            "comment",
            task["id"],
            "just a note",
            "--actor",
            "human:test",
        )
        assert result.exit_code == 0


class TestCommentRoleNoPolicies:
    """When no roles are configured anywhere, any role is accepted (backward compat)."""

    @pytest.fixture(autouse=True)
    def _strip_roles(self, initialized_root) -> None:
        """Remove both workflow.roles and completion_policies so no roles are configured."""
        from lattice.storage.fs import LATTICE_DIR

        config_path = initialized_root / LATTICE_DIR / "config.json"
        config = json.loads(config_path.read_text())
        config["workflow"].pop("roles", None)
        config["workflow"].pop("completion_policies", None)
        config_path.write_text(json.dumps(config, sort_keys=True, indent=2) + "\n")

    def test_any_role_accepted_without_any_config(self, invoke, create_task) -> None:
        task = create_task("No roles configured")
        result = invoke(
            "comment",
            task["id"],
            "all good",
            "--role",
            "anything_goes",
            "--actor",
            "human:test",
        )
        assert result.exit_code == 0


class TestCommentRoleExplicitRoles:
    """When workflow.roles is configured (no completion policies), validation enforces."""

    def test_typo_rejected_by_explicit_roles(self, invoke, create_task) -> None:
        """Default config has workflow.roles: ['review'], so typos are caught."""
        task = create_task("Explicit roles")
        result = invoke(
            "comment",
            task["id"],
            "looks good",
            "--role",
            "reveiw",
            "--actor",
            "human:test",
            "--json",
        )
        assert result.exit_code != 0
        parsed = json.loads(result.output)
        assert parsed["ok"] is False
        assert parsed["error"]["code"] == "INVALID_ROLE"
        assert "reveiw" in parsed["error"]["message"]
        assert "review" in parsed["error"]["message"]

    def test_valid_role_accepted_by_explicit_roles(self, invoke, create_task) -> None:
        """Default config has workflow.roles: ['review'], so 'review' passes."""
        task = create_task("Explicit roles OK")
        result = invoke(
            "comment",
            task["id"],
            "all good",
            "--role",
            "review",
            "--actor",
            "human:test",
        )
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# lattice comment --file
# ---------------------------------------------------------------------------


class TestCommentFile:
    """Tests for --file option on lattice comment."""

    def test_comment_from_file(self, invoke, create_task, tmp_path) -> None:
        """--file reads comment body from a file."""
        task = create_task("File comment")
        body_file = tmp_path / "review.md"
        body_file.write_text("Detailed review findings here.")

        result = invoke(
            "comment",
            task["id"],
            "--file",
            str(body_file),
            "--actor",
            "human:test",
            "--json",
        )
        assert result.exit_code == 0
        snapshot = json.loads(result.output)["data"]
        assert snapshot["comment_count"] == 1

    def test_file_and_text_mutually_exclusive(self, invoke, create_task, tmp_path) -> None:
        """Providing both TEXT and --file is an error."""
        task = create_task("Mutual exclusion")
        body_file = tmp_path / "body.txt"
        body_file.write_text("content")

        result = invoke(
            "comment",
            task["id"],
            "inline text",
            "--file",
            str(body_file),
            "--actor",
            "human:test",
        )
        assert result.exit_code != 0
        assert "not both" in result.output.lower() or "VALIDATION_ERROR" in result.output

    def test_neither_text_nor_file_is_error(self, invoke, create_task) -> None:
        """Providing neither TEXT nor --file is an error."""
        result = invoke(
            "comment",
            create_task("No body")["id"],
            "--actor",
            "human:test",
        )
        assert result.exit_code != 0

    def test_file_with_role(self, invoke, create_task, tmp_path) -> None:
        """--file works with --role."""
        task = create_task("File + role")
        body_file = tmp_path / "review.md"
        body_file.write_text("LGTM — all checks pass.")

        result = invoke(
            "comment",
            task["id"],
            "--file",
            str(body_file),
            "--role",
            "review",
            "--actor",
            "human:test",
            "--json",
        )
        assert result.exit_code == 0
        snapshot = json.loads(result.output)["data"]
        comment_refs = [
            r for r in snapshot.get("evidence_refs", []) if r.get("source_type") == "comment"
        ]
        assert len(comment_refs) == 1
        assert comment_refs[0]["role"] == "review"


# ---------------------------------------------------------------------------
# lattice comment-edit --role (LAT-141)
# ---------------------------------------------------------------------------


class TestCommentEditRole:
    """Tests for --role option on comment-edit."""

    def test_add_role_to_roleless_comment(self, invoke, create_task) -> None:
        """Adding --role to a comment without a role creates an evidence_ref."""
        task = create_task("Edit role test")
        task_id = task["id"]
        comment_id = _add_comment(invoke, task_id, "looks good")

        result = invoke(
            "comment-edit",
            task_id,
            comment_id,
            "looks good",
            "--role",
            "review",
            "--actor",
            "human:test",
            "--json",
        )
        assert result.exit_code == 0, result.output
        snapshot = json.loads(result.output)["data"]
        comment_refs = [
            r for r in snapshot.get("evidence_refs", []) if r.get("source_type") == "comment"
        ]
        assert len(comment_refs) == 1
        assert comment_refs[0]["id"] == comment_id
        assert comment_refs[0]["role"] == "review"

    def test_change_role_on_existing_role_comment(self, invoke, initialized_root) -> None:
        """Changing --role on a comment with an existing role updates evidence_refs."""
        from tests.conftest import _add_policies_to_config

        _add_policies_to_config(
            initialized_root,
            {
                "done": {"require_roles": ["review", "security"]},
            },
        )
        from lattice.storage.fs import LATTICE_DIR

        config_path = initialized_root / LATTICE_DIR / "config.json"
        config = json.loads(config_path.read_text())
        config["workflow"]["roles"] = ["review", "security"]
        config_path.write_text(json.dumps(config, sort_keys=True, indent=2) + "\n")

        r = invoke("create", "Role change test", "--actor", "human:test", "--json")
        task_id = json.loads(r.output)["data"]["id"]

        r = invoke(
            "comment",
            task_id,
            "initial review",
            "--role",
            "review",
            "--actor",
            "human:test",
            "--json",
        )
        comment_id = json.loads(r.output)["data"]["last_event_id"]

        r = invoke(
            "comment-edit",
            task_id,
            comment_id,
            "actually a security review",
            "--role",
            "security",
            "--actor",
            "human:test",
            "--json",
        )
        assert r.exit_code == 0, r.output
        snapshot = json.loads(r.output)["data"]
        comment_refs = [
            ref for ref in snapshot.get("evidence_refs", []) if ref.get("source_type") == "comment"
        ]
        assert len(comment_refs) == 1
        assert comment_refs[0]["role"] == "security"

    def test_body_only_edit_preserves_role(self, invoke, create_task) -> None:
        """Editing body without --role preserves the existing role evidence."""
        task = create_task("Preserve role test")
        task_id = task["id"]

        r = invoke(
            "comment",
            task_id,
            "LGTM",
            "--role",
            "review",
            "--actor",
            "human:test",
            "--json",
        )
        comment_id = json.loads(r.output)["data"]["last_event_id"]

        r = invoke(
            "comment-edit",
            task_id,
            comment_id,
            "LGTM — no issues found",
            "--actor",
            "human:test",
            "--json",
        )
        assert r.exit_code == 0, r.output
        snapshot = json.loads(r.output)["data"]
        comment_refs = [
            ref for ref in snapshot.get("evidence_refs", []) if ref.get("source_type") == "comment"
        ]
        assert len(comment_refs) == 1
        assert comment_refs[0]["role"] == "review"

    def test_completion_policy_satisfied_after_role_edit(
        self,
        invoke_with_policies,
        initialized_root_with_policies,
    ) -> None:
        """Adding a review role via comment-edit satisfies completion policy."""
        r = invoke_with_policies(
            "create",
            "Policy test",
            "--actor",
            "human:test",
            "--json",
        )
        task_id = json.loads(r.output)["data"]["id"]
        invoke_with_policies(
            "status",
            task_id,
            "in_progress",
            "--actor",
            "human:test",
            "--force",
            "--reason",
            "skip",
        )
        invoke_with_policies("status", task_id, "review", "--actor", "human:test")

        r = invoke_with_policies(
            "comment",
            task_id,
            "looks good",
            "--actor",
            "human:test",
            "--json",
        )
        comment_id = json.loads(r.output)["data"]["last_event_id"]

        r = invoke_with_policies(
            "status",
            task_id,
            "done",
            "--actor",
            "human:test",
            "--json",
        )
        assert r.exit_code != 0
        assert json.loads(r.output)["error"]["code"] == "COMPLETION_BLOCKED"

        r = invoke_with_policies(
            "comment-edit",
            task_id,
            comment_id,
            "looks good",
            "--role",
            "review",
            "--actor",
            "human:test",
        )
        assert r.exit_code == 0, r.output

        r = invoke_with_policies(
            "status",
            task_id,
            "done",
            "--actor",
            "human:test",
            "--json",
        )
        assert r.exit_code == 0, r.output
        assert json.loads(r.output)["ok"] is True


class TestCommentEditRoleValidation:
    """Role validation on comment-edit mirrors comment and attach."""

    @pytest.fixture(autouse=True)
    def _with_policies(self, initialized_root_with_policies) -> None:
        """Ensure standard completion policies are active."""

    def test_typo_role_rejected(self, invoke_with_policies) -> None:
        """Typo'd role on comment-edit produces INVALID_ROLE error."""
        r = invoke_with_policies(
            "create",
            "Validation test",
            "--actor",
            "human:test",
            "--json",
        )
        task_id = json.loads(r.output)["data"]["id"]

        r = invoke_with_policies(
            "comment",
            task_id,
            "original",
            "--actor",
            "human:test",
            "--json",
        )
        comment_id = json.loads(r.output)["data"]["last_event_id"]

        r = invoke_with_policies(
            "comment-edit",
            task_id,
            comment_id,
            "updated",
            "--role",
            "reveiw",
            "--actor",
            "human:test",
            "--json",
        )
        assert r.exit_code != 0
        parsed = json.loads(r.output)
        assert parsed["error"]["code"] == "INVALID_ROLE"
        assert "reveiw" in parsed["error"]["message"]
        assert "review" in parsed["error"]["message"]

    def test_valid_role_accepted(self, invoke_with_policies) -> None:
        """Valid role on comment-edit succeeds."""
        r = invoke_with_policies(
            "create",
            "Validation OK",
            "--actor",
            "human:test",
            "--json",
        )
        task_id = json.loads(r.output)["data"]["id"]

        r = invoke_with_policies(
            "comment",
            task_id,
            "original",
            "--actor",
            "human:test",
            "--json",
        )
        comment_id = json.loads(r.output)["data"]["last_event_id"]

        r = invoke_with_policies(
            "comment-edit",
            task_id,
            comment_id,
            "updated",
            "--role",
            "review",
            "--actor",
            "human:test",
        )
        assert r.exit_code == 0
