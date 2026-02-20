# Git Worktree Protocol for Lattice Projects

This guide covers creating, configuring, and tearing down git worktrees in projects that use Lattice for coordination.

## The Critical Invariant

All worktrees MUST share a single `.lattice/` directory via the `LATTICE_ROOT` environment variable. Lattice is the real-time coordination state for all agents. If a worktree runs Lattice commands without `LATTICE_ROOT` pointing to the shared `.lattice/`, it creates divergent state — tasks, events, and plans invisible to every other agent. This is unrecoverable without manual intervention.

## Creating a Worktree

1. Identify the task (e.g., LAT-42) and determine a branch name:
   ```bash
   git worktree add ../worktree-LAT-42 -b feat/LAT-42-<slug>
   ```
   Use sibling directories (`../worktree-*`), not subdirectories of the primary checkout.

2. Set `LATTICE_ROOT` to the primary checkout's `.lattice/` absolute path:
   ```bash
   export LATTICE_ROOT=$(cd /path/to/primary-checkout/.lattice && pwd)
   ```

3. Verify Lattice sees the shared state from within the worktree:
   ```bash
   cd ../worktree-LAT-42
   lattice list
   ```
   You should see the same tasks as in the primary checkout. If you see an empty list or an error, `LATTICE_ROOT` is not set correctly.

4. Link the branch in Lattice:
   ```bash
   lattice branch-link LAT-42 feat/LAT-42-<slug> --actor agent:<your-id>
   ```

## Working in a Worktree

- All Lattice commands work normally as long as `LATTICE_ROOT` is set.
- Branch awareness checks still apply — verify your worktree is on the expected branch before commits and status transitions.
- Commits happen on the worktree's branch, fully isolated from other worktrees and the primary checkout.
- Push your branch regularly so other agents and CI can see your work.

## Tearing Down a Worktree

1. Ensure all work is committed and pushed.
2. Return to the primary checkout:
   ```bash
   cd /path/to/primary-checkout
   ```
3. Remove the worktree:
   ```bash
   git worktree remove ../worktree-LAT-42
   ```
4. If the branch was merged, clean it up:
   ```bash
   git branch -d feat/LAT-42-<slug>
   ```

## Do NOT

- **Run `lattice init` in a worktree.** This creates a separate `.lattice/` directory and splits coordination state.
- **Forget to set `LATTICE_ROOT`.** Lattice's root discovery walks up the directory tree. Without `LATTICE_ROOT`, it will either find nothing (error) or create a new root if someone runs `lattice init`.
- **Create worktrees inside the primary checkout.** Use sibling directories (`../worktree-*`) to keep the filesystem clean.
- **Leave stale worktrees.** They hold branch refs and can cause confusion. Clean up when work is merged.

## Spawning Sub-Agents in Worktrees

When spawning sub-agents that will work in a worktree, ensure `LATTICE_ROOT` is set in their environment:

```bash
LATTICE_ROOT=/absolute/path/to/.lattice <agent-command>
```

Each sub-agent inherits the env var and operates against the shared Lattice state.
