"""Tests for MCP branch-link and branch-unlink tool functions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lattice.mcp.tools import (
    lattice_branch_link,
    lattice_branch_unlink,
    lattice_create,
)


# ---------------------------------------------------------------------------
# lattice_branch_link
# ---------------------------------------------------------------------------


class TestBranchLink:
    """Tests for lattice_branch_link MCP tool."""

    def test_branch_link_basic(self, lattice_env: Path, lattice_dir: Path):
        task = lattice_create(title="Branch test", actor="human:test")
        result = lattice_branch_link(task_id=task["id"], branch="feat/LAT-42", actor="human:test")
        assert len(result["branch_links"]) == 1
        bl = result["branch_links"][0]
        assert bl["branch"] == "feat/LAT-42"
        assert bl["repo"] is None
        assert bl["linked_by"] == "human:test"

    def test_branch_link_with_repo(self, lattice_env: Path):
        task = lattice_create(title="Repo test", actor="human:test")
        result = lattice_branch_link(
            task_id=task["id"], branch="feat/login", actor="human:test", repo="lattice"
        )
        assert result["branch_links"][0]["branch"] == "feat/login"
        assert result["branch_links"][0]["repo"] == "lattice"

    def test_branch_link_writes_event(self, lattice_env: Path, lattice_dir: Path):
        task = lattice_create(title="Event test", actor="human:test")
        lattice_branch_link(task_id=task["id"], branch="feat/test", actor="human:test")

        event_path = lattice_dir / "events" / f"{task['id']}.jsonl"
        lines = event_path.read_text().strip().split("\n")
        last_event = json.loads(lines[-1])
        assert last_event["type"] == "branch_linked"
        assert last_event["data"]["branch"] == "feat/test"

    def test_branch_link_multiple(self, lattice_env: Path):
        task = lattice_create(title="Multi branch", actor="human:test")
        lattice_branch_link(task_id=task["id"], branch="feat/a", actor="human:test")
        result = lattice_branch_link(task_id=task["id"], branch="feat/b", actor="human:test")
        assert len(result["branch_links"]) == 2

    def test_branch_link_duplicate_rejected(self, lattice_env: Path):
        task = lattice_create(title="Dup test", actor="human:test")
        lattice_branch_link(task_id=task["id"], branch="feat/test", actor="human:test")
        with pytest.raises(ValueError, match="Duplicate"):
            lattice_branch_link(task_id=task["id"], branch="feat/test", actor="human:test")

    def test_branch_link_duplicate_with_repo_rejected(self, lattice_env: Path):
        task = lattice_create(title="Dup repo", actor="human:test")
        lattice_branch_link(task_id=task["id"], branch="main", actor="human:test", repo="lattice")
        with pytest.raises(ValueError, match="Duplicate"):
            lattice_branch_link(
                task_id=task["id"], branch="main", actor="human:test", repo="lattice"
            )

    def test_branch_link_same_branch_different_repo_allowed(self, lattice_env: Path):
        task = lattice_create(title="Diff repo", actor="human:test")
        lattice_branch_link(task_id=task["id"], branch="main", actor="human:test", repo="frontend")
        result = lattice_branch_link(
            task_id=task["id"], branch="main", actor="human:test", repo="backend"
        )
        assert len(result["branch_links"]) == 2

    def test_branch_link_task_not_found(self, lattice_env: Path):
        with pytest.raises(ValueError, match="not found"):
            lattice_branch_link(
                task_id="task_00000000000000000000000099",
                branch="feat/test",
                actor="human:test",
            )

    def test_branch_link_with_short_id(self, lattice_env: Path):
        task = lattice_create(title="Short ID test", actor="human:test")
        short_id = task["short_id"]
        result = lattice_branch_link(task_id=short_id, branch="feat/short", actor="human:test")
        assert result["id"] == task["id"]
        assert len(result["branch_links"]) == 1


# ---------------------------------------------------------------------------
# lattice_branch_unlink
# ---------------------------------------------------------------------------


class TestBranchUnlink:
    """Tests for lattice_branch_unlink MCP tool."""

    def test_branch_unlink_basic(self, lattice_env: Path):
        task = lattice_create(title="Unlink test", actor="human:test")
        lattice_branch_link(task_id=task["id"], branch="feat/test", actor="human:test")
        result = lattice_branch_unlink(task_id=task["id"], branch="feat/test", actor="human:test")
        assert len(result["branch_links"]) == 0

    def test_branch_unlink_writes_event(self, lattice_env: Path, lattice_dir: Path):
        task = lattice_create(title="Unlink event", actor="human:test")
        lattice_branch_link(task_id=task["id"], branch="feat/test", actor="human:test")
        lattice_branch_unlink(task_id=task["id"], branch="feat/test", actor="human:test")

        event_path = lattice_dir / "events" / f"{task['id']}.jsonl"
        lines = event_path.read_text().strip().split("\n")
        last_event = json.loads(lines[-1])
        assert last_event["type"] == "branch_unlinked"
        assert last_event["data"]["branch"] == "feat/test"

    def test_branch_unlink_not_found(self, lattice_env: Path):
        task = lattice_create(title="Not linked", actor="human:test")
        with pytest.raises(ValueError, match="No branch link"):
            lattice_branch_unlink(task_id=task["id"], branch="feat/test", actor="human:test")

    def test_branch_unlink_with_repo(self, lattice_env: Path):
        task = lattice_create(title="Unlink repo", actor="human:test")
        lattice_branch_link(task_id=task["id"], branch="main", actor="human:test", repo="lattice")
        result = lattice_branch_unlink(
            task_id=task["id"], branch="main", actor="human:test", repo="lattice"
        )
        assert len(result["branch_links"]) == 0

    def test_branch_unlink_wrong_repo_fails(self, lattice_env: Path):
        task = lattice_create(title="Wrong repo", actor="human:test")
        lattice_branch_link(task_id=task["id"], branch="main", actor="human:test", repo="frontend")
        with pytest.raises(ValueError, match="No branch link"):
            lattice_branch_unlink(
                task_id=task["id"], branch="main", actor="human:test", repo="backend"
            )

    def test_branch_unlink_preserves_other_links(self, lattice_env: Path):
        task = lattice_create(title="Preserve test", actor="human:test")
        lattice_branch_link(task_id=task["id"], branch="feat/a", actor="human:test")
        lattice_branch_link(task_id=task["id"], branch="feat/b", actor="human:test")
        result = lattice_branch_unlink(task_id=task["id"], branch="feat/a", actor="human:test")
        assert len(result["branch_links"]) == 1
        assert result["branch_links"][0]["branch"] == "feat/b"


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


class TestBranchNameValidation:
    """Tests for branch name input validation in MCP tools."""

    def test_empty_branch_rejected(self, lattice_env: Path):
        task = lattice_create(title="Empty branch", actor="human:test")
        with pytest.raises(ValueError, match="empty"):
            lattice_branch_link(task_id=task["id"], branch="", actor="human:test")

    def test_whitespace_branch_rejected(self, lattice_env: Path):
        task = lattice_create(title="Whitespace branch", actor="human:test")
        with pytest.raises(ValueError, match="empty"):
            lattice_branch_link(task_id=task["id"], branch="   ", actor="human:test")

    def test_dash_prefix_branch_rejected(self, lattice_env: Path):
        task = lattice_create(title="Dash branch", actor="human:test")
        with pytest.raises(ValueError, match="must not start with '-'"):
            lattice_branch_link(task_id=task["id"], branch="-evil-flag", actor="human:test")

    def test_control_char_branch_rejected(self, lattice_env: Path):
        task = lattice_create(title="Control char", actor="human:test")
        with pytest.raises(ValueError, match="control characters"):
            lattice_branch_link(task_id=task["id"], branch="feat/\x00bad", actor="human:test")

    def test_tab_in_branch_rejected(self, lattice_env: Path):
        task = lattice_create(title="Tab branch", actor="human:test")
        with pytest.raises(ValueError, match="control characters"):
            lattice_branch_link(task_id=task["id"], branch="feat/\tbad", actor="human:test")

    def test_empty_branch_rejected_unlink(self, lattice_env: Path):
        task = lattice_create(title="Empty unlink", actor="human:test")
        with pytest.raises(ValueError, match="empty"):
            lattice_branch_unlink(task_id=task["id"], branch="", actor="human:test")

    def test_dash_prefix_rejected_unlink(self, lattice_env: Path):
        task = lattice_create(title="Dash unlink", actor="human:test")
        with pytest.raises(ValueError, match="must not start with '-'"):
            lattice_branch_unlink(task_id=task["id"], branch="--delete", actor="human:test")

    def test_empty_repo_normalized_to_none(self, lattice_env: Path):
        """Empty string repo should be normalized to None (no repo)."""
        task = lattice_create(title="Empty repo", actor="human:test")
        result = lattice_branch_link(
            task_id=task["id"], branch="feat/test", actor="human:test", repo=""
        )
        assert result["branch_links"][0]["repo"] is None

    def test_whitespace_repo_normalized_to_none(self, lattice_env: Path):
        """Whitespace-only repo should be normalized to None."""
        task = lattice_create(title="WS repo", actor="human:test")
        result = lattice_branch_link(
            task_id=task["id"], branch="feat/test", actor="human:test", repo="   "
        )
        assert result["branch_links"][0]["repo"] is None
