# Git Worktree Protocol for Lattice Projects

This guide covers creating, configuring, and tearing down git worktrees in projects that use Lattice for coordination.

## The Critical Invariant

All worktrees MUST share a single `.lattice/` directory via the `LATTICE_ROOT` environment variable. `LATTICE_ROOT` points at the primary checkout's **root directory** — the directory that *contains* `.lattice/`, never the `.lattice/` directory itself (the CLI rejects a path ending in `.lattice/` with `LATTICE_ROOT points to a directory with no .lattice/ inside`). Lattice is the real-time coordination state for all agents. If a worktree runs Lattice commands without `LATTICE_ROOT` set to the shared root, it creates divergent state — tasks, events, and plans invisible to every other agent, and a lagging short-ID counter that later reissues in-use IDs. This is unrecoverable without manual intervention.

## Creating a Worktree

1. Identify the task (e.g., LAT-42) and determine a branch name:
   ```bash
   git worktree add ../worktree-LAT-42 -b feat/LAT-42-<slug>
   ```
   Use sibling directories (`../worktree-*`), not subdirectories of the primary checkout.

2. Set `LATTICE_ROOT` to the primary checkout's **root** absolute path (the directory containing `.lattice/`, not `.lattice/` itself):
   ```bash
   export LATTICE_ROOT=$(cd /path/to/primary-checkout && pwd)
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
- **Author plan/notes files into the primary checkout's `.lattice/`, not the worktree's copy.** The CLI's plan reads — including the `in_progress` scaffold gate — resolve against the root repo. A plan you `Write` to `<worktree>/.lattice/plans/<task_id>.md` is invisible to it, so the gate blocks with "plan is still scaffold". Write to `$REPO_ROOT/.lattice/plans/` (or write anywhere then copy it there).
- Branch awareness checks still apply — verify your worktree is on the expected branch before commits and status transitions.
- Commits happen on the worktree's branch, fully isolated from other worktrees and the primary checkout.
- Push your branch regularly so other agents and CI can see your work.
- **Never `git add` the `.lattice/` board from a worktree.** The board (`tasks/`, `events/`, `plans/`, `artifacts/`, `ids.json`, `config.json`) is owned and committed by the **primary checkout**. Your CLI writes already land there (root discovery redirects them), so the worktree's own checked-out copy is vestigial — committing it onto your branch creates a stale snapshot that collides with the primary's live state on merge. Only commit your actual code/doc deliverable. The one legitimate exception is a ticket whose *deliverable is itself a tracked `.lattice/` doc* (e.g. editing `.lattice/orchestration/*.md`); commit only that file, nothing else under `.lattice/`.
- Ephemeral runtime state (`review_state/`, `tmp-prompts/`, `.daemon/`, `locks/`) is excluded by the scaffolded `.lattice/.gitignore`, so it can never be accidentally committed. The durable board stays tracked by design.

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
LATTICE_ROOT=/absolute/path/to/primary-checkout <agent-command>
```

`LATTICE_ROOT` is the primary checkout's **root** (the directory containing `.lattice/`), never `.lattice/` itself. Each sub-agent inherits the env var and operates against the shared Lattice state.

## Reviewing from a worktree (`code-review` / `plan-review`)

Most `lattice` calls auto-detect the root repo and route correctly from a worktree (tasks, comments, events, and plan-file writes all land in the root repo's `.lattice/`). Two review paths need extra care when run from a worktree.

### `lattice code-review` — make sure the diff is real

The diff a worktree review sees can be empty or partial if it resolves against the wrong refs, which yields a vacuous review. Defend against it:

- **Commit before transitioning to `review`.** New/uncommitted files are invisible to the diff — commit so the reviewer sees the whole change.
- **Always pass `--base <remote>/main`** (e.g. `origin/main`), never bare `main` — they look identical but the local ref may lag the remote, producing a stale or empty diff. Set `export LATTICE_ROOT=<primary-checkout>` at session start.
- If the auto-review fails outright, `lattice review-status <TICKET>` reports it as `FAILED` (with no `review`-role artifact, so the `done` gate stays blocked). Re-run, or use a fallback below.
- **Fallback (small tickets):** review the committed diff yourself and close with `lattice complete <TICKET> --review "<verdict + findings>"` — the review text satisfies the `done` policy without a CLI-spawned artifact.
- **Fallback (richer):** spawn an own-reviewer sub-agent that computes the diff itself (`git log <remote>/main..HEAD --stat` + per-file `git diff`), writes an artifact, and attaches it with `lattice attach <TICKET> --type note --role review --inline "<markdown>" --actor agent:<id>-reviewer`. The `--role review` attachment satisfies the `done` policy. See the `lattice-orchestrator` skill's `references/orchestrator.md` (`## Own-reviewer-tab fallback`).

### `lattice plan-review` — confirm it reads the authored plan

A worktree plan-review can read the empty plan scaffold instead of the authored plan (depending on where the plan was written) and return a vacuous FAIL. Before running it, verify the root holds the real plan:

- `wc -l $REPO_ROOT/.lattice/plans/<task_id>.md` — if it's still the short scaffold but the worktree has the authored plan, copy it across: `cp <worktree>/.lattice/plans/<task_id>.md $REPO_ROOT/.lattice/plans/`.
