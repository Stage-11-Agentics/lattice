# Git Worktree Protocol for Lattice Projects

This guide covers creating, configuring, and tearing down git worktrees in projects that use Lattice for coordination.

## The CLI is worktree-transparent

Lattice's CLI auto-detects when it's running inside a git linked worktree and routes `.lattice/` reads and writes to the **primary repo's** `.lattice/` automatically. You no longer need to set `LATTICE_ROOT` or wrap calls in `(cd $REPO_ROOT && lattice ...)`. This is the same way `git` itself behaves — `git status` in a worktree just works, talking to the primary.

Detection uses the `.git` *file* (gitdir pointer) that linked worktrees carry. Bare repos, submodules, and non-git directories are unaffected.

### Two exceptions

Two commands intentionally write to the **worktree's** `.lattice/` instead of the primary, because their output should ride the feature branch into the PR:

- `lattice branch-link` (and `branch-unlink`) — records the `branch_linked` / `branch_unlinked` event on the same branch the link refers to.
- `lattice code-review` — writes review artifacts under `.lattice/artifacts/` so they land in commit 1 / the PR.

These commands fall back to the primary's `.lattice/` when the worktree has none (e.g., in projects where `.lattice/` is gitignored, like Lattice itself). So you get the right behavior in both tracked-`.lattice/` and gitignored-`.lattice/` projects without thinking about it.

### Precedence

`LATTICE_ROOT` (environment variable) > auto-detect > walk-up from cwd. Setting `LATTICE_ROOT` overrides everything else, including the worktree exceptions, and is still useful for unusual layouts.

## Creating a Worktree

```bash
git worktree add ../worktree-LAT-42 -b feat/LAT-42-<slug>
cd ../worktree-LAT-42
lattice list                                              # auto-routes to primary
lattice branch-link LAT-42 feat/LAT-42-<slug> \           # writes to worktree if .lattice/ tracked
    --actor agent:<your-id>
```

Use sibling directories (`../worktree-*`), not subdirectories of the primary checkout.

## Working in a Worktree

- All Lattice commands work without special setup. The CLI handles routing.
- Commits happen on the worktree's branch, fully isolated from other worktrees and the primary checkout.
- Push your branch regularly so other agents and CI can see your work.

## Tearing Down a Worktree

```bash
cd /path/to/primary-checkout
git worktree remove ../worktree-LAT-42
git branch -d feat/LAT-42-<slug>      # if merged
```

## Do NOT

- **Run `lattice init` in a worktree** when the primary already has `.lattice/`. The CLI now refuses this explicitly — it would create a divergent worktree-local `.lattice/` that splits coordination state. If you genuinely need a separate Lattice instance per worktree (rare), pass `--force --reason "<why>"`.
- **Create worktrees inside the primary checkout.** Use sibling directories (`../worktree-*`) to keep the filesystem clean.
- **Leave stale worktrees.** They hold branch refs and can cause confusion. Clean up when work is merged.

## Historical context: the old `LATTICE_ROOT` workflow

Before the CLI gained worktree-transparency, this guide required users to:

```bash
export LATTICE_ROOT=$(cd /path/to/primary-checkout/.lattice && pwd)
```

That step is no longer necessary — the CLI does it for you. `LATTICE_ROOT` is still respected (and overrides auto-detection) for unusual layouts and for tests.

## Spawning Sub-Agents in Worktrees

Sub-agents run from within a worktree the same way you do — Lattice auto-routes for them too. Just `cd` into the worktree and start the agent; no `LATTICE_ROOT` plumbing required. If you do choose to set `LATTICE_ROOT` in the agent's environment for testing or special layouts, the agent honors it as usual.
