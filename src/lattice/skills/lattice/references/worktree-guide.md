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

## CLI worktree↔root bridge footguns (`code-review` and `plan-review`)

LAT-219 added directory-walking auto-detection so most `lattice` calls route correctly from a worktree (read/write tasks, comments, events, plan files all land in the root repo's `.lattice/`). **Two commands still have known worktree↔root bridge bugs even with `LATTICE_ROOT` set.**

### `lattice code-review` — empty-diff failure

- **Symptom:** `lattice code-review <TICKET> --base <remote>/main` returns an empty diff or a vacuous artifact when run from a worktree, even with `LATTICE_ROOT=$PWD` set. The reviewer sees no changes and writes a useless review.
- **Why:** The diff-resolution path doesn't fully honor the worktree's HEAD; it falls back to the primary checkout's refs in some configurations.
- **Cheap mitigation:** Always pass `--base <remote>/main` (NEVER bare `main` — they look identical but the local ref may be behind the remote). Set `export LATTICE_ROOT=$PWD` at session start.
- **Fallback when cheap mitigation fails:** Spawn an own-reviewer sub-agent on the delegator's own pane that computes the diff itself (`git log <remote>/main..HEAD --stat` + per-file `git diff`), writes a custom artifact at `notes/.tmp/<TICKET>-codereview-custom.md`, and attaches it via `lattice attach <TICKET> --type note --role review --inline "<markdown>" --actor agent:<id>-reviewer`. The `--role review` attachment satisfies the `done` completion policy — the orchestrator can't tell the difference from a CLI-generated review. See the `lattice-orchestrator` skill's `references/orchestrator.md` `## Own-reviewer-tab fallback` section for the full pattern.
- **Observed:** Every Wave 2 delegator on the EC v1.2.1 run hit this independently and converged on the fallback.

### `lattice plan-review` — wrong-file silent read

- **Symptom:** `lattice plan-review <TICKET> --headless` silently reads the empty 30-line plan scaffold (from `.lattice/plans/<task_id>.md` in the wrong location) instead of the authored plan, and reports a vacuous FAIL with no findings against the actual plan content.
- **Why:** LAT-219 routes plan-file *writes* to the root repo but the plan-review *read path* doesn't always resolve to the same location, depending on how the plan was authored (lattice CLI vs direct file write).
- **Cheap mitigation:** Before `lattice plan-review`, verify `$REPO_ROOT/.lattice/plans/<task_id>.md` has the authored content via `wc -l`. If it's the 30-line scaffold but the worktree has the real plan, copy worktree→root: `cp <worktree>/.lattice/plans/<task_id>.md $REPO_ROOT/.lattice/plans/`.
- **Observed:** EC v1.2.1 run, PSY-47 delegator. First plan-review pass returned vacuous FAIL; worked around by the copy. File a Lattice ticket if you hit this — it's an upstream defect that should follow LAT-219's fix to the same conclusion.

Both bugs are upstream defects in Lattice, not workflow issues (tracked as LAT-214 / LAT-225). Document a hit in your run's closeout audit and consider filing a Lattice ticket so the maintainer can apply the LAT-219 fix to the review path.
