"""Real-git tests for ``resolve_diff`` (LAT-271 / KWB-217).

These bite because they use real git. The unit tests that patch ``_git_diff``
are why the bugs these cover shipped: a review that diffs the wrong tree looks
identical to a correct one from inside a mock.

The failure modes reproduced here, all of which produced confident PASS
verdicts on code nobody read:

* a stale *local* ``main`` as the base, dragging every sibling ticket merged
  since the last pull into the diff (KWB-222: 7820 lines for a 642-line change);
* a linked branch that does not resolve, silently answered with some other
  ticket's commits (KWB-201);
* evidence headers describing the caller's cwd rather than the diffed head.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from lattice.core import review as review_mod
from tests.conftest import git

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _snapshot(branch: str | None, **extra) -> dict:
    snap: dict = {"short_id": "KWB-222", **extra}
    if branch is not None:
        snap["branch_links"] = [{"branch": branch}]
    return snap


class TestBaseResolution:
    def test_stale_local_main_is_not_the_base(self, worktree_repo):
        """The KWB-222 regression: reviewed from the board checkout, the diff
        must contain the ticket's own change and none of the sibling tickets'."""
        res = review_mod.resolve_diff(
            worktree_repo.lattice_dir,
            "task_01",
            _snapshot(worktree_repo.branch),
        )
        assert res.success is True, res.error
        assert "feature.py" in res.diff
        assert "sibling_" not in res.diff
        assert "uncommitted local edit" not in res.diff

    def test_base_ref_is_remote_default(self, worktree_repo):
        res = review_mod.resolve_diff(
            worktree_repo.lattice_dir, "task_01", _snapshot(worktree_repo.branch)
        )
        assert res.base_ref == "origin/main"
        assert res.base_sha == git(worktree_repo.main, "rev-parse", "origin/main").strip()
        assert res.head_sha == git(worktree_repo.main, "rev-parse", worktree_repo.branch).strip()
        assert res.source == "linked_branch"

    def test_origin_head_symbolic_ref_preferred(self, worktree_repo):
        """``origin/HEAD`` names the remote default branch — honor it over a
        hardcoded ``origin/main``."""
        main = worktree_repo.main
        # Give the remote a non-'main' default and point origin/HEAD at it.
        git(worktree_repo.sib, "checkout", "-b", "trunk")
        git(worktree_repo.sib, "push", "origin", "trunk")
        git(main, "fetch", "origin")
        git(main, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")

        res = review_mod.resolve_diff(
            worktree_repo.lattice_dir, "task_01", _snapshot(worktree_repo.branch)
        )
        assert res.success is True, res.error
        assert res.base_ref == "origin/trunk"

    def test_local_ahead_of_unfetched_origin_picks_descendant_merge_base(self, worktree_repo):
        """When the local default branch is *fresher* than the remote ref, the
        tighter (descendant) merge-base wins — an unfetched remote degrades
        gracefully instead of over-including."""
        main = worktree_repo.main
        branch = worktree_repo.branch
        # Local main catches up to the branch point and then some.
        git(main, "reset", "--hard", branch)
        (main / "local_only.txt").write_text("local main moved on\n")
        git(main, "add", "-A")
        git(main, "commit", "-m", "local-only commit")

        res = review_mod.resolve_diff(worktree_repo.lattice_dir, "task_01", _snapshot(branch))
        # merge-base(local main, branch) == branch tip, which is a descendant of
        # merge-base(origin/main, branch). The tighter range wins and is empty —
        # honestly empty, and reported as a failure rather than a silent pass.
        assert res.base_ref == "main"
        assert res.success is False
        assert "empty" in (res.error or "").lower()

    def test_no_remote_falls_back_to_local_main(self, worktree_repo):
        main = worktree_repo.main
        git(main, "remote", "remove", "origin")
        git(main, "update-ref", "-d", "refs/remotes/origin/main")
        res = review_mod.resolve_diff(
            worktree_repo.lattice_dir, "task_01", _snapshot(worktree_repo.branch)
        )
        assert res.base_ref == "main"
        assert res.success is True, res.error
        assert "feature.py" in res.diff

    def test_stale_remote_warns_without_failing(self, worktree_repo):
        """origin/main behind local main is a fetch-overdue signal, not an error."""
        main = worktree_repo.main
        git(main, "reset", "--hard", "origin/main")
        (main / "ahead.txt").write_text("local is ahead\n")
        git(main, "add", "-A")
        git(main, "commit", "-m", "local ahead of origin")
        res = review_mod.resolve_diff(
            worktree_repo.lattice_dir, "task_01", _snapshot(worktree_repo.branch)
        )
        assert res.warning is not None
        assert "origin/main" in res.warning
        assert "behind local main" in res.warning
        assert res.success is True, res.error

    def test_diverged_remote_warns(self, worktree_repo):
        """The topology on a real board: local main carries commits origin never
        saw while origin moved on. Neither ref is an ancestor of the other, and
        that is exactly when a reader wants to know how old the base ref is."""
        main = worktree_repo.main
        (main / "local_only.txt").write_text("never pushed\n")
        git(main, "add", "-A")
        git(main, "commit", "-m", "local-only commit on main")

        res = review_mod.resolve_diff(
            worktree_repo.lattice_dir, "task_01", _snapshot(worktree_repo.branch)
        )
        assert res.success is True, res.error
        assert res.warning is not None
        assert "diverged" in res.warning
        assert "'git fetch' refreshes it" in res.warning

    def test_fresh_remote_does_not_warn(self, worktree_repo):
        """Local main strictly behind the remote is the ordinary, healthy state
        of a board checkout — the base ref is current, so say nothing."""
        res = review_mod.resolve_diff(
            worktree_repo.lattice_dir, "task_01", _snapshot(worktree_repo.branch)
        )
        assert res.success is True, res.error
        assert res.warning is None


class TestHeadResolution:
    def test_unresolvable_linked_branch_fails_loudly(self, worktree_repo):
        """The KWB-201 regression: a branch link that doesn't resolve must be a
        named error, never a fall-through to somebody else's commits."""
        res = review_mod.resolve_diff(
            worktree_repo.lattice_dir, "task_01", _snapshot("feat/does-not-exist")
        )
        assert res.success is False
        assert res.error_code == "HEAD_REF_UNRESOLVABLE"
        assert "feat/does-not-exist" in (res.error or "")
        assert "--head" in (res.error or "")
        assert res.diff == ""

    def test_author_fallback_is_gone(self, worktree_repo):
        """A sibling commit by the same author in the same window is not an
        answer to 'what did this ticket change'."""
        sib_authored = git(worktree_repo.main, "log", "--all", "--author=Tester", "--format=%H")
        assert sib_authored.strip(), "fixture should have same-author sibling commits"

        res = review_mod.resolve_diff(
            worktree_repo.lattice_dir,
            "task_01",
            _snapshot(
                "feat/does-not-exist",
                assigned_to="agent:Tester",
                updated_at="2000-01-01T00:00:00Z",
            ),
        )
        assert res.success is False
        assert "sibling_" not in res.diff

    def test_commit_message_scan_fallback_is_gone(self, worktree_repo):
        """No branch link at all: the ambient HEAD, said out loud — not a
        ``git log --all --grep <short id>`` guess."""
        res = review_mod.resolve_diff(worktree_repo.lattice_dir, "task_01", _snapshot(None))
        assert res.source == "head"
        assert res.head_ref == "HEAD"
        assert res.success is False  # HEAD is stale local main; honestly empty

    def test_no_branch_link_uses_ambient_head_and_says_so(self, worktree_repo):
        """Same rung, from the feature worktree, where HEAD is the branch."""
        res = review_mod.resolve_diff(
            worktree_repo.lattice_dir,
            "task_01",
            _snapshot(None),
            worktree=worktree_repo.worktree,
        )
        assert res.source == "head"
        assert res.success is True, res.error
        assert "feature.py" in res.diff

    def test_explicit_base_and_head_win(self, worktree_repo):
        res = review_mod.resolve_diff(
            worktree_repo.lattice_dir,
            "task_01",
            _snapshot("feat/does-not-exist"),
            base="main",
            head=worktree_repo.branch,
        )
        assert res.success is True, res.error
        assert res.source == "explicit"
        assert res.base_ref == "main"
        assert res.head_ref == worktree_repo.branch
        # ``main`` is the stale local branch, so this range *does* include the
        # siblings — the operator asked for it explicitly.
        assert "sibling_" in res.diff

    def test_worktree_is_the_tree_git_ran_in(self, worktree_repo):
        res = review_mod.resolve_diff(
            worktree_repo.lattice_dir,
            "task_01",
            _snapshot(worktree_repo.branch),
            worktree=worktree_repo.worktree,
        )
        assert res.worktree == Path(worktree_repo.worktree)


class TestTruncationNamesTheRange:
    def test_line_cap_marker_names_the_range(self, worktree_repo):
        res = review_mod.resolve_diff(
            worktree_repo.lattice_dir, "task_01", _snapshot(worktree_repo.branch)
        )
        capped, was_capped, _ = review_mod.cap_diff(res.diff, 1, range_desc=res.range_desc)
        assert was_capped is True
        assert f"range: origin/main...{worktree_repo.branch}" in capped

    def test_char_cap_marker_names_the_range(self, worktree_repo):
        res = review_mod.resolve_diff(
            worktree_repo.lattice_dir, "task_01", _snapshot(worktree_repo.branch)
        )
        capped, was_capped, _ = review_mod.cap_diff_chars(res.diff, 40, range_desc=res.range_desc)
        assert was_capped is True
        assert f"range: origin/main...{worktree_repo.branch}" in capped
