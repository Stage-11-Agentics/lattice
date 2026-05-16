"""Tests for the Layer F worktree guard on `lattice init`.

When `lattice init` runs inside a git linked worktree whose primary repo
already has a `.lattice/`, the CLI refuses to create a divergent worktree-
local `.lattice/` unless `--force --reason` is passed.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from lattice.cli.main import cli

# Non-interactive: --actor and --project-code together skip every prompt.
_NON_INTERACTIVE = ["--actor", "human:atin", "--project-code", "TEST"]


def _build_primary(tmp_path: Path) -> Path:
    primary = tmp_path / "primary"
    primary.mkdir()
    (primary / ".git").mkdir()
    return primary


def _build_linked_worktree(primary: Path, name: str, location: Path) -> Path:
    worktree_meta = primary / ".git" / "worktrees" / name
    worktree_meta.mkdir(parents=True)
    location.mkdir(parents=True, exist_ok=True)
    (location / ".git").write_text(f"gitdir: {worktree_meta}\n", encoding="utf-8")
    return location


class TestInitWorktreeGuard:
    def test_refuses_init_in_worktree_when_primary_has_lattice(self, tmp_path: Path) -> None:
        primary = _build_primary(tmp_path)
        (primary / ".lattice").mkdir()
        worktree = _build_linked_worktree(primary, "wt1", tmp_path / "worktrees" / "wt1")

        runner = CliRunner()
        result = runner.invoke(cli, ["init", "--path", str(worktree), *_NON_INTERACTIVE])

        assert result.exit_code != 0
        assert "worktree" in result.output.lower()
        assert "Refusing to create" in result.output
        assert str(primary / ".lattice") in result.output
        # And no .lattice/ was created in the worktree.
        assert not (worktree / ".lattice").exists()

    def test_allows_init_in_worktree_when_primary_has_no_lattice(self, tmp_path: Path) -> None:
        """Primary lacks .lattice/ → no divergence risk → guard does not fire."""
        primary = _build_primary(tmp_path)
        worktree = _build_linked_worktree(primary, "wt1", tmp_path / "worktrees" / "wt1")

        runner = CliRunner()
        result = runner.invoke(cli, ["init", "--path", str(worktree), *_NON_INTERACTIVE])

        assert result.exit_code == 0, result.output
        assert (worktree / ".lattice").is_dir()

    def test_force_without_reason_errors(self, tmp_path: Path) -> None:
        primary = _build_primary(tmp_path)
        (primary / ".lattice").mkdir()
        worktree = _build_linked_worktree(primary, "wt1", tmp_path / "worktrees" / "wt1")

        runner = CliRunner()
        result = runner.invoke(
            cli, ["init", "--path", str(worktree), "--force", *_NON_INTERACTIVE]
        )

        assert result.exit_code != 0
        assert "--reason is required" in result.output
        assert not (worktree / ".lattice").exists()

    def test_force_with_reason_creates_worktree_local_lattice(self, tmp_path: Path) -> None:
        primary = _build_primary(tmp_path)
        (primary / ".lattice").mkdir()
        worktree = _build_linked_worktree(primary, "wt1", tmp_path / "worktrees" / "wt1")

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "init",
                "--path",
                str(worktree),
                "--force",
                "--reason",
                "demoing the override",
                *_NON_INTERACTIVE,
            ],
        )

        assert result.exit_code == 0, result.output
        assert (worktree / ".lattice").is_dir()
        # Stderr (Click mixes them in CliRunner) carries the audit line.
        assert "demoing the override" in result.output

    def test_outside_any_worktree_is_unaffected(self, tmp_path: Path) -> None:
        """Plain directory (no .git anywhere) — guard is a no-op."""
        target = tmp_path / "fresh"
        target.mkdir()

        runner = CliRunner()
        result = runner.invoke(cli, ["init", "--path", str(target), *_NON_INTERACTIVE])

        assert result.exit_code == 0, result.output
        assert (target / ".lattice").is_dir()

    def test_in_primary_worktree_is_unaffected(self, tmp_path: Path) -> None:
        """Standing in the primary worktree (real .git directory) is not a
        linked worktree — guard does not fire even if .lattice/ exists nearby."""
        primary = _build_primary(tmp_path)

        runner = CliRunner()
        result = runner.invoke(cli, ["init", "--path", str(primary), *_NON_INTERACTIVE])

        assert result.exit_code == 0, result.output
        assert (primary / ".lattice").is_dir()
