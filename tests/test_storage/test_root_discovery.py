"""Tests for root discovery logic."""

from __future__ import annotations

from pathlib import Path

import pytest

from lattice.storage.fs import LATTICE_DIR, LatticeRootError, _detect_worktree, find_root


class TestFindRootWalkUp:
    """find_root() walks up from a starting path to find .lattice/."""

    def test_finds_lattice_in_current_dir(self, tmp_path: Path) -> None:
        (tmp_path / LATTICE_DIR).mkdir()
        result = find_root(start=tmp_path)
        assert result == tmp_path

    def test_finds_lattice_in_parent_dir(self, tmp_path: Path) -> None:
        (tmp_path / LATTICE_DIR).mkdir()
        nested = tmp_path / "a" / "b" / "c"
        nested.mkdir(parents=True)

        result = find_root(start=nested)
        assert result == tmp_path

    def test_returns_none_when_not_found(self, tmp_path: Path) -> None:
        # tmp_path has no .lattice/ — walk up should eventually hit root and return None
        # Use a nested dir to avoid accidentally finding a real .lattice/ on the system
        isolated = tmp_path / "isolated"
        isolated.mkdir()
        result = find_root(start=isolated)
        assert result is None


class TestFindRootEnvVar:
    """LATTICE_ROOT env var overrides walk-up discovery."""

    def test_env_var_overrides_walk_up(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Create .lattice/ in the env var target
        env_target = tmp_path / "env_root"
        env_target.mkdir()
        (env_target / LATTICE_DIR).mkdir()

        monkeypatch.setenv("LATTICE_ROOT", str(env_target))

        # Even when starting from a different path, env var wins
        other = tmp_path / "other"
        other.mkdir()
        result = find_root(start=other)
        assert result == env_target

    def test_env_var_nonexistent_path_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LATTICE_ROOT", str(tmp_path / "does_not_exist"))

        with pytest.raises(LatticeRootError, match="does not exist"):
            find_root(start=tmp_path)

    def test_env_var_no_lattice_dir_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Directory exists but has no .lattice/ inside
        env_target = tmp_path / "empty_root"
        env_target.mkdir()

        monkeypatch.setenv("LATTICE_ROOT", str(env_target))

        with pytest.raises(LatticeRootError, match="no .lattice/"):
            find_root(start=tmp_path)

    def test_env_var_invalid_does_not_fall_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When LATTICE_ROOT is set but invalid, do NOT fall back to walk-up."""
        # Create .lattice/ that walk-up would find
        (tmp_path / LATTICE_DIR).mkdir()

        # But set env var to a bad path
        monkeypatch.setenv("LATTICE_ROOT", str(tmp_path / "bad"))

        with pytest.raises(LatticeRootError):
            find_root(start=tmp_path)

    def test_env_var_empty_string_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Empty LATTICE_ROOT is an error, not a silent cwd fallback."""
        monkeypatch.setenv("LATTICE_ROOT", "")

        with pytest.raises(LatticeRootError, match="empty"):
            find_root(start=tmp_path)


class TestFindRootWorktreeTransparent:
    """find_root() jumps to the primary worktree when called from a git linked
    worktree, so ``lattice`` resolves to the canonical .lattice/ rather than
    a stale snapshot copied into the worktree at creation time.
    """

    @staticmethod
    def _build_primary(tmp_path: Path) -> Path:
        primary = tmp_path / "primary"
        primary.mkdir()
        (primary / ".git").mkdir()
        return primary

    @staticmethod
    def _build_linked_worktree(primary: Path, name: str, location: Path) -> Path:
        worktree_meta = primary / ".git" / "worktrees" / name
        worktree_meta.mkdir(parents=True)
        location.mkdir(parents=True, exist_ok=True)
        (location / ".git").write_text(f"gitdir: {worktree_meta}\n", encoding="utf-8")
        return location

    def test_worktree_resolves_to_primary_lattice(self, tmp_path: Path) -> None:
        """A linked worktree finds the primary's .lattice/, even when the
        worktree also has its own (stale) copy."""
        primary = self._build_primary(tmp_path)
        (primary / LATTICE_DIR).mkdir()

        worktree = self._build_linked_worktree(primary, "wt1", tmp_path / "worktrees" / "wt1")
        (worktree / LATTICE_DIR).mkdir()  # stale snapshot — must be skipped

        result = find_root(start=worktree)
        assert result == primary

    def test_worktree_resolves_to_primary_from_subdir(self, tmp_path: Path) -> None:
        primary = self._build_primary(tmp_path)
        (primary / LATTICE_DIR).mkdir()

        worktree = self._build_linked_worktree(primary, "wt1", tmp_path / "worktrees" / "wt1")
        deep = worktree / "src" / "deep"
        deep.mkdir(parents=True)

        result = find_root(start=deep)
        assert result == primary

    def test_worktree_walks_up_past_primary_when_lattice_higher(self, tmp_path: Path) -> None:
        """If the primary worktree has no .lattice/, the walk continues up
        from the primary root — not from the worktree dir."""
        outer = tmp_path / "outer"
        outer.mkdir()
        (outer / LATTICE_DIR).mkdir()

        primary = outer / "primary"
        primary.mkdir()
        (primary / ".git").mkdir()

        worktree = self._build_linked_worktree(primary, "wt1", tmp_path / "worktrees" / "wt1")

        result = find_root(start=worktree)
        assert result == outer

    def test_primary_worktree_unchanged(self, tmp_path: Path) -> None:
        """When start is inside the primary worktree itself (.git is a dir),
        behavior is the existing walk-up — no special-case redirect."""
        primary = self._build_primary(tmp_path)
        (primary / LATTICE_DIR).mkdir()
        subdir = primary / "src"
        subdir.mkdir()

        result = find_root(start=subdir)
        assert result == primary

    def test_non_git_tree_unchanged(self, tmp_path: Path) -> None:
        """When start isn't inside any git tree, behavior is plain walk-up."""
        (tmp_path / LATTICE_DIR).mkdir()
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)

        result = find_root(start=nested)
        assert result == tmp_path

    def test_malformed_worktree_pointer_falls_back(self, tmp_path: Path) -> None:
        """A .git file with garbage contents falls back to walk-up from start.
        This protects against weird user states without dropping into an
        unrelated lattice install up the tree."""
        primary = self._build_primary(tmp_path)
        (primary / LATTICE_DIR).mkdir()

        worktree = tmp_path / "worktrees" / "wt1"
        worktree.mkdir(parents=True)
        (worktree / ".git").write_text("not a gitdir pointer\n", encoding="utf-8")
        (worktree / LATTICE_DIR).mkdir()

        result = find_root(start=worktree)
        assert result == worktree


class TestFindRootPreferWorktree:
    """find_root(prefer_worktree=True) prefers the worktree-local .lattice/
    when one exists, and falls back to the primary's .lattice/ otherwise.
    Used by branch-link, branch-unlink, code-review.
    """

    @staticmethod
    def _build_primary(tmp_path: Path) -> Path:
        primary = tmp_path / "primary"
        primary.mkdir()
        (primary / ".git").mkdir()
        return primary

    @staticmethod
    def _build_linked_worktree(primary: Path, name: str, location: Path) -> Path:
        worktree_meta = primary / ".git" / "worktrees" / name
        worktree_meta.mkdir(parents=True)
        location.mkdir(parents=True, exist_ok=True)
        (location / ".git").write_text(f"gitdir: {worktree_meta}\n", encoding="utf-8")
        return location

    def test_prefers_worktree_lattice_when_present(self, tmp_path: Path) -> None:
        """Tracked-.lattice/ project (e.g. c11): worktree has its own
        .lattice/, prefer it over the primary's."""
        primary = self._build_primary(tmp_path)
        (primary / LATTICE_DIR).mkdir()

        worktree = self._build_linked_worktree(primary, "wt1", tmp_path / "worktrees" / "wt1")
        (worktree / LATTICE_DIR).mkdir()

        result = find_root(start=worktree, prefer_worktree=True)
        assert result == worktree

    def test_falls_back_to_primary_when_worktree_has_no_lattice(self, tmp_path: Path) -> None:
        """Gitignored-.lattice/ project (e.g. Lattice itself): worktree
        starts empty, fall back to the primary's .lattice/."""
        primary = self._build_primary(tmp_path)
        (primary / LATTICE_DIR).mkdir()

        worktree = self._build_linked_worktree(primary, "wt1", tmp_path / "worktrees" / "wt1")
        # No .lattice/ in the worktree.

        result = find_root(start=worktree, prefer_worktree=True)
        assert result == primary

    def test_falls_back_from_worktree_subdir(self, tmp_path: Path) -> None:
        """Same as above, called from a nested subdir of the worktree."""
        primary = self._build_primary(tmp_path)
        (primary / LATTICE_DIR).mkdir()

        worktree = self._build_linked_worktree(primary, "wt1", tmp_path / "worktrees" / "wt1")
        deep = worktree / "src" / "deep"
        deep.mkdir(parents=True)

        result = find_root(start=deep, prefer_worktree=True)
        assert result == primary

    def test_worktree_subtree_search_does_not_escape_to_outer(self, tmp_path: Path) -> None:
        """The prefer-worktree search is bounded to the worktree subtree.
        A .lattice/ that lives *above* the worktree (in some unrelated outer
        directory) must NOT be picked by the prefer step — that would route
        writes into the wrong project. The fallback then takes over and
        either finds the primary or returns None."""
        outer = tmp_path / "outer"
        outer.mkdir()
        (outer / LATTICE_DIR).mkdir()  # decoy .lattice/ outside the worktree

        primary = outer / "primary"
        primary.mkdir()
        (primary / ".git").mkdir()
        # primary has NO .lattice/ of its own.

        worktree = self._build_linked_worktree(primary, "wt1", tmp_path / "worktrees" / "wt1")
        # worktree has NO .lattice/ of its own.

        # The prefer step should not grab outer/.lattice/. The fallback
        # walks up from primary; primary has no .lattice/, so it climbs to
        # outer and finds the .lattice/ there — but that's the explicit
        # auto-route behavior (LAT-216), not the prefer step.
        result = find_root(start=worktree, prefer_worktree=True)
        assert result == outer  # found via fallback auto-route, not prefer step

    def test_env_var_still_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """LATTICE_ROOT overrides everything, including prefer_worktree."""
        primary = self._build_primary(tmp_path)
        (primary / LATTICE_DIR).mkdir()

        worktree = self._build_linked_worktree(primary, "wt1", tmp_path / "worktrees" / "wt1")
        (worktree / LATTICE_DIR).mkdir()

        env_target = tmp_path / "env_root"
        env_target.mkdir()
        (env_target / LATTICE_DIR).mkdir()
        monkeypatch.setenv("LATTICE_ROOT", str(env_target))

        result = find_root(start=worktree, prefer_worktree=True)
        assert result == env_target

    def test_non_worktree_unchanged(self, tmp_path: Path) -> None:
        """When start isn't in a worktree, prefer_worktree=True is a no-op."""
        (tmp_path / LATTICE_DIR).mkdir()
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)

        result = find_root(start=nested, prefer_worktree=True)
        assert result == tmp_path

    def test_default_prefer_worktree_false_routes_to_primary(self, tmp_path: Path) -> None:
        """Default prefer_worktree=False is the LAT-216 auto-route: even when
        the worktree has its own (stale) .lattice/, jump to primary."""
        primary = self._build_primary(tmp_path)
        (primary / LATTICE_DIR).mkdir()

        worktree = self._build_linked_worktree(primary, "wt1", tmp_path / "worktrees" / "wt1")
        (worktree / LATTICE_DIR).mkdir()

        result = find_root(start=worktree)  # default
        assert result == primary

        result_explicit = find_root(start=worktree, prefer_worktree=False)
        assert result_explicit == primary


class TestDetectWorktree:
    """_detect_worktree(start) returns (primary, worktree) for linked worktrees,
    (None, None) otherwise. Used by require_root for the sharpened error."""

    @staticmethod
    def _build_primary(tmp_path: Path) -> Path:
        primary = tmp_path / "primary"
        primary.mkdir()
        (primary / ".git").mkdir()
        return primary

    @staticmethod
    def _build_linked_worktree(primary: Path, name: str, location: Path) -> Path:
        worktree_meta = primary / ".git" / "worktrees" / name
        worktree_meta.mkdir(parents=True)
        location.mkdir(parents=True, exist_ok=True)
        (location / ".git").write_text(f"gitdir: {worktree_meta}\n", encoding="utf-8")
        return location

    def test_returns_both_paths_for_worktree(self, tmp_path: Path) -> None:
        primary = self._build_primary(tmp_path)
        worktree = self._build_linked_worktree(primary, "wt1", tmp_path / "worktrees" / "wt1")

        result_primary, result_worktree = _detect_worktree(worktree)
        assert result_primary == primary
        assert result_worktree == worktree

    def test_returns_both_paths_from_worktree_subdir(self, tmp_path: Path) -> None:
        primary = self._build_primary(tmp_path)
        worktree = self._build_linked_worktree(primary, "wt1", tmp_path / "worktrees" / "wt1")
        sub = worktree / "src" / "deep"
        sub.mkdir(parents=True)

        result_primary, result_worktree = _detect_worktree(sub)
        assert result_primary == primary
        assert result_worktree == worktree

    def test_returns_none_in_primary_worktree(self, tmp_path: Path) -> None:
        primary = self._build_primary(tmp_path)
        sub = primary / "src"
        sub.mkdir()

        result = _detect_worktree(sub)
        assert result == (None, None)

    def test_returns_none_outside_any_git_tree(self, tmp_path: Path) -> None:
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)

        result = _detect_worktree(nested)
        assert result == (None, None)

    def test_returns_none_for_malformed_pointer(self, tmp_path: Path) -> None:
        worktree = tmp_path / "worktrees" / "wt1"
        worktree.mkdir(parents=True)
        (worktree / ".git").write_text("garbage\n", encoding="utf-8")

        result = _detect_worktree(worktree)
        assert result == (None, None)
