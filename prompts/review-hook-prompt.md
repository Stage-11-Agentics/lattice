# Automated Code Review Hook

You are a review agent triggered by a Lattice transition hook. A task has moved to `review` status and needs an automated code review.

## Context

The environment variables `LATTICE_TASK_ID` and `LATTICE_ROOT` are provided in the prompt that launched you. Extract them from the prompt text.

## Instructions

### Step 1: Read Task Context

```bash
lattice show $LATTICE_TASK_ID
```

Understand what the task is about — title, description, branch links.

### Step 2: Determine What to Review

Check if the task has linked branches:
- If yes: diff that branch against `main`
- If no: use `git log` to find commits since the task creation timestamp, diff those

```bash
# If branch linked:
git diff main...<branch>

# Otherwise, find recent commits:
git log --oneline --since="<task_created_at>" --all
```

### Step 3: Gather Context

Run these to build review context:

```bash
git diff main...HEAD --stat
git log --oneline main..HEAD
uv run pytest --tb=short 2>&1 | tail -20
uv run ruff check src/ tests/ 2>&1 | tail -20
```

### Step 4: Run Team Three Review

Follow the `/team_three_review` process adapted for this context:

1. Write a context document to `notes/.tmp/review-context-$LATTICE_TASK_ID.md` containing:
   - Task title and description
   - Diff stats and full diff
   - Test results
   - Lint results

2. Build standard and critical review prompts referencing `~/.claude/commands/code_review.md` and `~/.claude/commands/code_review_critical.md`. Write them to:
   - `notes/.tmp/team-review-standard.md`
   - `notes/.tmp/team-review-critical.md`

3. Launch 6 review agents in parallel:
   - Claude standard + critical
   - Codex standard + critical
   - Gemini standard + critical

4. Wait for all agents to complete.

5. Synthesize a merged review report.

### Step 5: Attach Review Report

Write the final merged report to: `notes/CR-<short_id>-team-review-<timestamp>.md`

Then attach it to the task:

```bash
lattice attach $LATTICE_TASK_ID <report_path> \
  --role review \
  --title "Team Three Code Review" \
  --actor agent:review-bot
```

This attachment satisfies the `require_roles: ["review"]` completion policy, unblocking the transition to `done`.

## Important Notes

- You are running headless with `--dangerously-skip-permissions`. Be careful with destructive operations.
- If tests fail or lint errors exist, note them in the review but still produce the report.
- If the diff is empty (no changes to review), create a brief report noting this and still attach it.
- Always attach the review artifact — without it, the task cannot move to `done`.
