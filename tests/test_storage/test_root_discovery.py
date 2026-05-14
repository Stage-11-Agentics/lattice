"""Tests for root discovery logic."""

from __future__ import annotations

from pathlib import Path

import pytest

from lattice.storage.fs import LATTICE_DIR, LatticeRootError, find_root


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
