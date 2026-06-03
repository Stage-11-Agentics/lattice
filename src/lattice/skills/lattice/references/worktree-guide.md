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
LATTICE_ROOT=/absolute/path/to/.lattice <agent-command>
```

Each sub-agent inherits the env var and operates against the shared Lattice state.

## CLI worktree↔root bridge footguns (`code-review` and `plan-review`)

LAT-219 added directory-walking auto-detection so most `lattice` calls route correctly from a worktree (read/write tasks, comments, events, plan files all land in the root repo's `.lattice/`). **Two commands still have known worktree↔root bridge bugs even with `LATTICE_ROOT` set.**

### `lattice code-review` — empty-diff failure

- **Symptom:** `lattice code-review <TICKET> --base <remote>/main` returns an empty diff or a vacuous artifact when run from a worktree, even with `LATTICE_ROOT=$PWD` set. The reviewer sees no changes and writes a useless review. The auto-fired review (`review_mode: single`) can also just **die without attaching any artifact** — `review-status` keeps ticking, the spawned pid is dead, no `--role review` artifact exists.
- **Also:** new files are invisible to the diff until committed. **Commit before transitioning to `review`** so the reviewer (and the diff) see the whole change.
- **Why:** The diff-resolution path doesn't fully honor the worktree's HEAD; it falls back to the primary checkout's refs in some configurations.
- **Cheap mitigation:** Always pass `--base <remote>/main` (NEVER bare `main` — they look identical but the local ref may be behind the remote). Set `export LATTICE_ROOT=$PWD` at session start.
- **Fallback (small tickets):** review the committed diff yourself and complete with `lattice complete <TICKET> --review "<verdict + findings>"` — the review text satisfies the `done` policy without a CLI-spawned artifact.
- **Fallback when cheap mitigation fails:** Spawn an own-reviewer sub-agent on the delegator's own pane that computes the diff itself (`git log <remote>/main..HEAD --stat` + per-file `git diff`), writes a custom artifact at `notes/.tmp/<TICKET>-codereview-custom.md`, and attaches it via `lattice attach <TICKET> --type note --role review --inline "<markdown>" --actor agent:<id>-reviewer`. The `--role review` attachment satisfies the `done` completion policy — the orchestrator can't tell the difference from a CLI-generated review. See the `lattice-orchestrator` skill's `references/orchestrator.md` `## Own-reviewer-tab fallback` section for the full pattern.
- **Observed:** Every Wave 2 delegator on the EC v1.2.1 run hit this independently and converged on the fallback.

### `lattice plan-review` — wrong-file silent read

- **Symptom:** `lattice plan-review <TICKET> --headless` silently reads the empty 30-line plan scaffold (from `.lattice/plans/<task_id>.md` in the wrong location) instead of the authored plan, and reports a vacuous FAIL with no findings against the actual plan content.
- **Why:** LAT-219 routes plan-file *writes* to the root repo but the plan-review *read path* doesn't always resolve to the same location, depending on how the plan was authored (lattice CLI vs direct file write).
- **Cheap mitigation:** Before `lattice plan-review`, verify `$REPO_ROOT/.lattice/plans/<task_id>.md` has the authored content via `wc -l`. If it's the 30-line scaffold but the worktree has the real plan, copy worktree→root: `cp <worktree>/.lattice/plans/<task_id>.md $REPO_ROOT/.lattice/plans/`.
- **Observed:** EC v1.2.1 run, PSY-47 delegator. First plan-review pass returned vacuous FAIL; worked around by the copy. File a Lattice ticket if you hit this — it's an upstream defect that should follow LAT-219's fix to the same conclusion.

Both bugs are upstream defects in Lattice, not workflow issues. Document a hit in your run's closeout audit and consider filing a Lattice ticket so the maintainer can apply the LAT-219 fix to the review path.
