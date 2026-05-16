"""CLAUDE.md integration template for Lattice.

This is the single source of truth for the Lattice agent integration block.
Edit this file to update what `lattice init` and `lattice setup-claude` write.
"""

CLAUDE_MD_BLOCK = """\

## Lattice

> **MANDATORY: This project has Lattice initialized (`.lattice/` exists). You MUST use Lattice to track all work. Creating tasks, updating statuses, and following the workflow below is not optional — it is a hard requirement. Failure to track work in Lattice is a coordination failure: other agents and humans cannot see, build on, or trust untracked work. If you are about to write code and no Lattice task exists for it, stop and create one first.**

Lattice is file-based, event-sourced task tracking built for minds that think in tokens and act in tool calls. The `.lattice/` directory is the coordination state — it lives alongside the code, not behind an API.

### Creating Tasks (Non-Negotiable)

Before you plan, implement, or touch a single file — the task must exist in Lattice. This is the first thing you do when work arrives.

```
lattice create "<title>" --actor agent:<your-id>
```

**Create a task for:** Any work that will produce commits — features, bugs, refactors, cleanup, pivots.

**Skip task creation only when:** The work is a sub-step of a task you're already tracking (lint fixes within your feature, test adjustments from your change), pure research with no deliverable, or work explicitly scoped under an existing task.

When in doubt, create the task. A small task costs nothing. Lost visibility costs everything.

**Recurring observations become tasks.** If you observe the same issue in 2+ consecutive sessions or advances (e.g., a failing test, a lint warning, a flaky behavior), create a task for it. Agents are disciplined about tracking assigned work but not discovered work — this convention closes that gap. Create discovered issues at `needs_human` if they need scoping, or `backlog` if they're well-understood.

### Descriptions Carry Context

Descriptions tell *what* and *why*. Plan files tell *how*.

- **Fully specified** (bug located, fix named, files identified): still go through `in_planning`, but the plan can be a single line (e.g., "Fix the typo on line 77"). Mark `complexity: low`.
- **Clear goal, open implementation**: go through `in_planning`. The agent figures out the approach and writes a substantive plan.
- **Decision context from conversations**: bake decisions and rationale into the description — without it, the next agent re-derives what was already decided.

### Status Transitions

Every transition is an immutable, attributed event. **The cardinal rule: update status BEFORE you start the work, not after.** If the board says `backlog` but you're actively working, the board is lying and every mind reading it makes decisions on false information.

```
lattice status <task> <status> --actor agent:<your-id>
```

```
backlog → in_planning → planned → in_progress → review → done
                                       ↕            ↕
                                    blocked      needs_human
```

**Transition discipline:**
- `in_planning` — before you open the first file to read. Then write the plan.
- `planned` — only after the plan file has real content.
- `in_progress` — before you write the first line of code.
- `review` — when implementation is complete, before review starts. Then actually review.
- `done` — only after a review has been performed and recorded.
- Spawning a sub-agent? Update status in the parent context first.

### Sub-Agent Execution Model

Each lifecycle stage gets its own sub-agent with fresh context. This is the default execution pattern — not a suggestion, not complexity-gated. Every task, every time.

**Why this matters:** When a planning agent writes a plan and a separate implementation agent reads it, the plan *must* be clear and complete — there's no shared context to fall back on. This forces better plans. When a review agent reads the diff cold, it catches things the implementer's context-polluted mind would miss. The plan file and git diff are the handoff artifacts.

**The three sub-agents:**

| Stage | Sub-agent does | Reads | Produces |
|-------|---------------|-------|----------|
| **Plan** | Explore codebase, write plan, move to `planned` | Task description | Plan file |
| **Implement** | Read plan, build it, test, commit, move to `review` | Plan file | Committed code |
| **Review** | Read diff cold, review against acceptance criteria, record findings | Git diff + plan | Review artifact (`--role review`), move to `done` |

**The parent orchestrator** (the main agent session) manages the lifecycle:
1. Move the task to `in_planning` before spawning the planning sub-agent.
2. After the planner finishes, move to `in_progress` and spawn the implementation sub-agent.
3. After the implementer finishes, the review sub-agent runs independently.

Each sub-agent should use a distinct actor ID (e.g., `agent:claude-opus-4-planner`, `agent:claude-opus-4-impl`, `agent:claude-opus-4-reviewer`) so the event log shows who did what.

**Prompt guidance for sub-agents building streaming/realtime features:** When writing implementation prompts for features involving event streams, fswatch, or background process coordination (e.g., `lattice watch`, `lattice wait`), explicitly tell the sub-agent to skip integration tests that require concurrent processes. Test parsing and filtering logic with unit tests. Trust the I/O core from existing proven commands. Agents will otherwise thrash on launching background processes, sleeping, and debugging timing issues in a single-agent sandbox — a known failure mode that wastes significant context.

### The Planning Gate

The plan file lives at `.lattice/plans/<task_id>.md` — scaffolded on creation, empty until you fill it.

This is the **planning sub-agent's** job. Spawn a sub-agent whose sole purpose is to explore the codebase, understand the problem, and write the plan. It should:
1. Read the task description and any linked context.
2. Explore the relevant source files — understand existing patterns and constraints.
3. Write the plan to `.lattice/plans/<task_id>.md` — scope, approach, key files, acceptance criteria. For trivial tasks, a single sentence is fine. For substantial work, be thorough.
4. Move to `planned` only when the plan file reflects what it intends to build.

**The test:** If you moved to `planned` and the plan file is still empty scaffold, you didn't plan. Every task gets a plan — even trivial tasks get a one-line plan. The CLI enforces this: transitioning to `in_progress` is blocked when the plan is still scaffold.

**Plan review (default: single, fires automatically).** Moving the task to `planned` automatically spawns a detached `lattice plan-review <task>` in the background. Tail progress with `lattice review-status <task>` or `.lattice/.daemon/auto-plan-review-<task>.log`. Disable per-call with `--no-auto-review` on `lattice status`, or project-wide with `auto_plan_review_on_transition: false`. **If you opt into `plan_review_mode: triple`, every transition into `planned` spends three agent runs plus a merge — disable auto-fire or use `--no-auto-review` when cost matters.**

The mode controls *how* the review runs:

| `plan_review_mode` value | What the planner does |
|--------------------------|----------------------|
| `single` (default) | One headless `claude -p` subprocess runs the plan review. No c11 surface. |
| `triple` | Splits one new pane in the caller's c11 workspace and runs `/trident-plan-review` there. The pane owns trident and the task advance. CLI returns immediately. Requires c11. |
| `inline` | Reviews the plan in-session (use when codex/gemini aren't available, or for small/throwaway projects). Auto-fire is a no-op for inline. |

When `plan_approval` is `human`, the CLI automatically moves the task to `needs_human` after `lattice plan-review` completes. Wait for human approval before proceeding to `in_progress`.

### Plan Review Triage

When the plan review returns (trident or otherwise), the orchestrator sorts every finding into one of three buckets before moving to `in_progress`. This triage is the default ritual — it is what "taking on a ticket" means in a Lattice project.

| Bucket | What it looks like | Default action |
|--------|--------------------|----------------|
| **Obvious** | Missing acceptance criteria, contradictions, plan bugs, trivial clarifications, concrete omissions | Fix directly — amend the plan file, record a short `lattice comment` noting what was resolved. |
| **Evolutionary** | Speculative additions, "while we're at it" scope creep, refactor suggestions not tied to the ticket's goal, nice-to-haves | Be skeptical. Default to skip. If worth tracking, create a new Lattice task (`lattice create ...`) and link it — do not fold into this ticket. Record a comment explaining why it was deferred. |
| **Complex** | Genuine design decisions, ambiguity the agent can't resolve alone, trade-offs with real stakes, requirement questions | Bring to the human. Move to `needs_human` with a short comment stating the open question(s). |

Every finding must be explicitly triaged — no silent drops. If triage produces no complex questions, advance to `in_progress`. Otherwise, wait for the human answer, fold it into the plan, then advance.

**Why these three buckets:** Obvious findings improve the plan at zero cost — apply them. Evolutionary findings are often well-intentioned but scope-creep the ticket; the cost of folding them in compounds. Complex findings are where human judgment actually adds value — surface them, don't guess.

### The Review Gate

Moving to `review` is a commitment to actually review the work.

**The review fires automatically by default.** When you transition the task to `review`, the CLI spawns a detached `lattice code-review <task>` in the background — the orchestrator does not need to remember to run it. Tail with `lattice review-status <task>` (covers both manual and auto-fired reviews) or `.lattice/.daemon/auto-code-review-<task>.log`. Disable per-call with `--no-auto-review`, or project-wide with `auto_code_review_on_transition: false`. **If `review_mode` is `triple`, every `→ review` transition (including rework cycles) spends three agent runs by default.**

This is the **review sub-agent's** job. Spawn a sub-agent with fresh context — it did NOT write the code and comes in cold.

**Step 1: Check review_mode.** Before reviewing, check the project config:

```
cat .lattice/config.json | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('review_mode','single'))"
```

| `review_mode` value | What the reviewer does |
|---------------------|----------------------|
| `inline` | Review the diff yourself in-session. Run `lattice code-review <task> --mode inline` to acknowledge. (Auto-fire is a no-op for inline.) |
| `single` (default) | One headless `claude -p` subprocess runs the review and stores the artifact. No c11 surface, no terminal window. Manual fallback: `lattice code-review <task>`. |
| `triple` | Splits one new pane in the caller's c11 workspace and runs `/trident-code-review` there. The pane owns trident, finding triage, and the task-status advance. CLI returns immediately. Requires c11 — outside c11, the command exits non-zero with a clear error. Manual fallback: `lattice code-review <task> --mode triple`. |

**Step 2: Perform the review.** The review sub-agent should:
1. Read the plan file to understand what was supposed to be built.
2. Read the git diff to see what was actually built.
3. Run tests and linting to verify nothing is broken.
4. Compare the implementation against the plan's acceptance criteria.
5. Use the artifact produced by the auto-fired review (or run `lattice code-review <task>` manually if you opted out / are inline).

**When moving to `done`:** If the completion policy blocks you for a missing review artifact, do the review. Do not `--force` past it. `--force --reason` is for genuinely exceptional cases, not a convenience shortcut.

**The test:** If the same agent that wrote the code also reviewed it without a fresh context boundary, the review gate is not doing its job. The whole point is independent verification.

**Review content validation:** Before trusting a review artifact and moving to `done`, the orchestrator must sanity-check that the content is an actual review — not an error message, stack trace, agent crash output, or empty boilerplate. A valid review references the code (files, sections, or acceptance criteria), contains a verdict (pass/fail), and reads like a human wrote it. If the review content looks like agent failure output, treat it as a failed review and re-run. This is a 5-second gut check, not a deep analysis.

### Review Verdict Routing

When the orchestrator reads a completed review (from the review artifact or inline review output), it follows a **three-way routing protocol**:

1. **Fix** (primary path) — Address the finding. Route the task back and spawn a rework sub-agent, or fix inline if trivial. Record what was fixed in a follow-up comment.

2. **Ignore with justification** — Low-severity findings, style preferences, or findings that would change the ticket's scope can be skipped. The orchestrator must note why in a comment: `lattice comment <task> "Skipping [finding]: [reason]" --actor agent:<id>`. Don't silently ignore — always record the decision.

3. **Create new task** — Legitimate findings that are out of scope for the current ticket. Create a new Lattice task to track the work: `lattice create "<finding title>" --actor agent:<id>`. The insight is captured without blocking the current task.

Every finding must be explicitly routed. No finding may be silently dropped.

### Review Rework Loop

When a review agent evaluates work, it produces one of three outcomes:

1. **Pass (with optional minor fix):** The review agent uses vibes-based judgment. If the only issues are trivial (obvious typos, missing semicolons, etc.), fix them inline, record what was changed in the review comment, and move to `done`. No strict line-count threshold — the review agent decides.

2. **Fail — implementation-level:** The plan was sound but the implementation has issues. The review agent explicitly states "implementation-level rework needed" in its comment. The orchestrator transitions the task `review -> in_progress`. Critical findings from the review are appended to the plan file under a new `## Review Cycle N Findings` section. A fresh sub-agent is encouraged (but not mandated) for the rework.

3. **Fail — plan-level:** The original plan was flawed — wrong approach, missing requirements, etc. The review agent explicitly states "plan-level rework needed" in its comment. The orchestrator transitions the task `review -> in_planning`. The plan gets reworked (not just amended), then back through the full lifecycle.

**Who decides what:**

| Decision | Who | How |
|----------|-----|-----|
| Fix inline vs send back | Review agent | Vibes-based judgment, recorded in review comment |
| Implementation-level vs plan-level | Review agent | Explicitly stated in review comment |
| Route to in_progress vs in_planning | Orchestrator | Follows review agent's recommendation |
| Whether to spawn fresh sub-agent | Orchestrator | Encouraged by convention, not enforced |

**3-cycle safety valve:** After 3 review-to-rework transitions (any combination of `review -> in_progress` and `review -> in_planning`), the CLI blocks the 4th attempt. The error message instructs the agent to move the task to `needs_human` with a comment explaining the situation. The limit is configurable via `review_cycle_limit` in the workflow config (default: 3). Override with `--force --reason` for genuinely exceptional cases.

**Allowed lifecycle paths:**

```
Normal:       in_progress -> review -> done
Minor fix:    in_progress -> review -> (fix inline) -> done
1 impl rework: in_progress -> review -> in_progress -> review -> done
1 plan rework: in_progress -> review -> in_planning -> planned -> in_progress -> review -> done
Max cycles:   3 review->rework transitions, then CLI blocks -> needs_human
```

### Review Config Reference

Five settings in `.lattice/config.json` control review behavior:

| Setting | Values | Default | Meaning |
|---------|--------|---------|---------|
| `review_mode` | `inline`, `single`, `triple` | `single` | How code review is performed at the review gate |
| `plan_review_mode` | `inline`, `single`, `triple` | `single` | How plan review is performed after the plan is written |
| `plan_approval` | `auto`, `human` | `auto` | After plan-review: `auto` proceeds, `human` moves to `needs_human` for approval |
| `auto_code_review_on_transition` | `true`, `false` | `true` | Auto-spawn `lattice code-review` when a task transitions to `review` |
| `auto_plan_review_on_transition` | `true`, `false` | `true` | Auto-spawn `lattice plan-review` when a task transitions to `planned` |

**`inline`** — review happens in the same agent session (no subprocess spawned).
**`single`** — one headless review agent is spawned; result stored as a `review` or `plan-review` artifact. No c11 surface.
**`triple`** — one new c11 pane sibling to the caller is spawned; the pane runs `/trident-{code|plan}-review`, which fans out to multiple agents, merges findings, stores the artifact, and advances the task. Requires c11 (the command errors cleanly otherwise).

### Auto-fire Conventions

When a task transitions to `review` or `planned`, `lattice status` automatically spawns a detached `lattice code-review` / `plan-review` subprocess. The transition itself never blocks on the spawn.

- **Coordination** lives in `.lattice/review_state/<task_id>.json` (extended with `started_by_pid` and `auto_fired` fields). First-writer-wins.
- **Logs** at `.lattice/.daemon/auto-{code,plan}-review-<task_id>.log`, overwritten per spawn. Header records the spawn timestamp.
- **Monitor** with `lattice review-status <task_id>` (covers both manual and auto-fired reviews).
- **Audit** via the `auto_review_spawned` event in the per-task event log.
- **Per-call opt-out**: `--no-auto-review` on `lattice status`.
- **Project-wide opt-out**: `auto_code_review_on_transition: false` and/or `auto_plan_review_on_transition: false`.

### When You're Stuck

Use `needs_human` when you need human decision, approval, or input **right now**. This is distinct from `blocked` (generic external dependency) — it creates a scannable queue. **`needs_human` means actionable NOW** — future checkpoints (quality gates, review gates, approval milestones) stay at `planned` or `backlog` until the preceding work is complete. The orchestrator flips them to `needs_human` at the moment they become actionable.

```
lattice status <task> needs_human --actor agent:<your-id>
lattice comment <task> "Need: <what you need, in one line>" --actor agent:<your-id>
```

Use for: design decisions requiring human judgment, missing access/credentials, ambiguous requirements, approval gates. The comment is mandatory — explain what you need in seconds, not minutes. The human's queue should be scannable.

### Actor Attribution

Every operation requires `--actor`. Attribution follows authorship of the *decision*, not the keystroke.

- Agent decided autonomously → `agent:<id>`
- Human typed it directly → `human:<id>`
- Human meaningfully shaped the outcome → `human:<id>` (agent was the instrument)

When in doubt, credit the human.

### Branch Linking

Link feature branches to tasks: `lattice branch-link <task> <branch-name> --actor agent:<your-id>`. Auto-detection works when the branch contains the short code (e.g., `feat/LAT-42-login`), but explicit linking is preferred.

### File-Decision Links

When a task involves a meaningful architectural or design decision about specific files, link them:

```
lattice file-link <task> <filepath> [<filepath>...] --actor agent:<your-id> [--reason "why this file matters"]
```

This records decision provenance — later, `lattice explain <filepath>` shows what decisions shaped a file. Use `--reason` to annotate why the file is linked so the explanation is self-contained. Link files that embody **decisions**, not every file touched. A task that refactors 50 files doesn't need 50 links.

`lattice explain` also supports directory queries (`src/auth/`) and globs (`src/auth/*.ts`).

### Leave Breadcrumbs

You are not the last mind that will touch this work. Use `lattice comment` for what you tried, chose, and left undone. Use `plans/<task_id>.md` for structured plans and `notes/<task_id>.md` for working notes and context dumps. The record you leave is the only bridge to the next agent's context.

### Shared Worktree Discipline

Multiple agents may work in the same repository concurrently on different tasks. The `git status` snapshot from your session start goes stale the moment another agent commits.

**When you encounter unfamiliar changes** (unexpected files, diffs you didn't make, new commits on HEAD):
1. **Investigate first.** Check `git log` and `lattice list` to see if another task/agent is responsible.
2. **Ask "who made this?" before "this shouldn't be here."** The change is almost certainly another agent's legitimate work.
3. **Never revert, reset, or delete changes you can't attribute.** If you're unsure, leave them alone and ask the human.

This applies to uncommitted changes in the working tree, unexpected commits on the branch, and new files that weren't there when your session started. The instinct to "clean up" unfamiliar state is exactly wrong in a multi-agent worktree — it destroys a sibling agent's work.

### Where Learnings Go

When you discover something important about how this project works — a pattern, a gotcha, a convention — **do not save it to auto-memory**. Memory is per-session and per-user; future Lattice agents in other installations will never see it. Instead, add it to this project's `CLAUDE.md` (for project-specific conventions) or propose updating the Lattice template (for universal patterns that should ship with every `lattice init`). The goal: every future agent, in every future installation, benefits from what you learned.

### Quick Reference

```
lattice create "<title>" --actor agent:<id>
lattice status <task> <status> --actor agent:<id>
lattice assign <task> <actor> --actor agent:<id>
lattice comment <task> "<text>" --actor agent:<id>
lattice link <task> <type> <target> --actor agent:<id>
lattice branch-link <task> <branch> --actor agent:<id>
lattice file-link <task> <path>... --actor agent:<id> [--reason "why"]
lattice file-unlink <task> <path> --actor agent:<id>
lattice explain <path>                           # also supports directory/ and globs
lattice next [--actor agent:<id>] [--claim]
lattice show <task>
lattice list
```

**Useful flags:**
- `--quiet` — prints only the task ID (scripting: `TASK=$(lattice create "..." --quiet)`)
- `--json` — structured output: `{"ok": true, "data": ...}` or `{"ok": false, "error": ...}`
- `lattice list --status in_progress` / `--assigned agent:<id>` / `--tag <tag>` — filters
- `lattice link <task> subtask_of|depends_on|blocks <target>` — task relationships

For the full CLI reference, see the `/lattice` skill.
"""

# Marker comment used to detect if the block was already added
CLAUDE_MD_MARKER = "## Lattice"
