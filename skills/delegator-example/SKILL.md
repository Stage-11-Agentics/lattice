---
name: delegator-example
description: Worked example of a high-powered Lattice delegator flow, as practiced in Stage 11's c11 project. Drives a single Lattice ticket end-to-end through plan → implement → review → validate → handoff using an orchestrator + delegator + phase-sub-agents pattern, with a sibling pane per phase, an isolated git worktree per delegation, and Lattice as the shared comms bus. Read this when you want to see what a fully developed agent-native delegation pattern looks like end-to-end. The c11 surface and pane mechanics will need adaptation for other harnesses, but the orchestrator/delegator architecture, worktree blast-radius discipline, and phase-by-phase Lattice status hygiene all transfer.
homepage: https://github.com/Stage-11-Agentics/lattice
metadata: {"openclaw":{"emoji":"performing_arts","requires":{"bins":["lattice"]},"install":[{"id":"uv","kind":"command","command":"uv tool install lattice-tracker","bins":["lattice"],"label":"Install Lattice (uv)"},{"id":"pipx","kind":"command","command":"pipx install lattice-tracker","bins":["lattice"],"label":"Install Lattice (pipx)"},{"id":"pip","kind":"command","command":"pip install lattice-tracker","bins":["lattice"],"label":"Install Lattice (pip)"}]}}
---

# Example Delegator Flow (Stage 11 / c11)

> **This is an example skill, not a generic one.** It documents how Stage 11 drives a single Lattice ticket end-to-end inside the **c11** terminal multiplexer. It uses c11-specific commands (`c11 new-split`, `c11 new-surface`, `c11 set-metadata`, etc.) to compose panes, surfaces, and sidebar telemetry around the work. If you are running a different harness, treat the orchestrator/delegator/phase architecture and the worktree+Lattice discipline as the transferable lesson; the surface mechanics will need a rewrite.
>
> Stage 11 publishes this as a worked example so other teams can see what a fully developed agent-native delegation pattern looks like before designing their own.

## Prerequisites

1. **Install the `lattice` CLI** if it is not already on your PATH (`lattice --help` should resolve). Pick whichever installer fits your environment:
   - `uv tool install lattice-tracker` (recommended)
   - `pipx install lattice-tracker`
   - `pip install lattice-tracker`
2. **Initialize Lattice in the target repo** if it has not been initialized yet:
   ```bash
   lattice init --project-code PROJ
   ```
   (Replace `PROJ` with a short project code, e.g. `APP`, `API`, `WEB`.) This creates the project's `.lattice/` directory.
3. **Load the companion `lattice` skill** (in this same `skills/` directory) for the core CLI reference. This skill builds on top of it.
4. **You are running inside c11** (`CMUX_SHELL_INTEGRATION=1`). If you are not on c11, do not try to follow the surface/pane commands literally — read the pattern, then adapt. The Stage 11 c11 project lives at <https://github.com/Stage-11-Agentics/c11>.

# Lattice Delegate Pattern

A pattern for driving a single Lattice ticket through its full lifecycle with dedicated panes, an isolated git worktree, and sub-agents per phase. The delegator is the single point of contact for the human; the ticket is the shared comms bus; the worktree is the blast-radius boundary.

## Roles

- **Orchestrator** — the chat that sets up the delegation and then runs an active polling loop. Creates the worktree, splits a pane, seeds the delegator surface, posts a framing Lattice comment. After setup, the orchestrator schedules periodic wake-ups (`ScheduleWakeup`) and surfaces meaningful ticket transitions to the operator — silent watching is a documented failure mode (see *Active orchestrator watch* below). The work itself happens in the delegator pane; the orchestrator's job is to make sure the operator hears about it.
- **Delegator** — a new Claude Code session spawned in a sibling pane. Walks the ticket's lifecycle, spawning one sibling surface per phase inside its own pane. **The delegator is the primary human interface** — when the human scrubs the delegation, they read the delegator pane; when the human has a question or needs to intervene, they talk to the delegator; when the work escalates, the delegator is the one who surfaces the escalation.
- **Phase sub-agents** — spawned by the delegator. One per phase: **Plan**, **Impl**, **Translator** (only if user-facing strings change and the repo is localized), **Review**, **Validate**. Each lives as a tab in the delegator's pane so the operator can scrub any of them in one vertical slice.
- **The ticket** — the shared comms bus. Every agent posts `lattice comment` at meaningful milestones. The orchestrator reads the trail via `lattice show <id>` or `lattice watch --task <id>`.

## Core principles

These shape every decision downstream. If a section below seems to conflict with one of these, the principle wins.

### The delegator is the human interface

The human watches and interacts with exactly one pane: the delegator's. Everything else is internal machinery. This has real consequences:

- **Sub-agents never message the human directly.** They write to the ticket, set their surface status, and stop. If they have a question or recommendation, it's a Lattice comment addressed to the delegator — not chat text addressed to the human.
- **Only the delegator escalates.** Needs-human flags, operator decisions, pauses for clarification — all of these flow through the delegator. A Plan or Review sibling that discovers an ambiguity flags it to the delegator (via the ticket), and the delegator decides whether to resolve, spawn another sub-agent, or escalate to the human.
- **The delegator summarizes.** When the human checks in, they read the delegator pane and get a coherent picture of where the ticket is and what's next. The delegator owns that picture — it's not assembled from scraping 9 sibling tabs.
- **The delegator is also the last-mile communicator.** When the PR is ready, when a decision is needed, when something has gone sideways — the delegator's most recent message is what the human sees.

Sub-agents are *internal*. The delegator is the membrane between the human and the work.

### Prefer cohesive tickets over spree tickets

When a review surfaces additional work, the default is to **absorb it into the current ticket's PR as additional commits**, not to spawn a follow-up ticket. A single fatter ticket with a cleanup commit is almost always better than a ticket + a followup ticket. Reasons: less coordination overhead, one review cycle, one merge, one audit trail, one reader has to load the context once.

Create a follow-up ticket only when:
- The item is explicitly out of scope per the plan's `Do NOT ship` list.
- The item would block current-ticket merge (e.g., unrelated CI/infra fix).
- The item needs its own planning cycle (not a trivial absorb).

The delegator is free to file follow-up tickets when the above applies, but it should feel like an exception, not a default.

### Review sub-agents are read-only; reviewers fix minor, escalate major

See the Review phase below — this is important enough to call out as a principle. Review **sub-agents** (the ones that only read code and emit findings) never mutate the codebase, never create Lattice tasks, and never commit. But the **Review phase** itself is not purely read-only — see the contract below for how minor findings get absorbed and major ones escalate.

### Worktrees are the blast-radius boundary — for code, not for Lattice

Every delegation runs in its own git worktree. The delegator and every sub-agent do their *code* work inside it: edits, builds, commits, branch pushes. The orchestrator's repo's working tree stays untouched. Parallel delegations don't collide. Build artifacts don't cross-contaminate. The diff on the worktree is, by definition, the delegation's entire code output.

**Lattice writes are the exception** — see the next principle. Lattice is the coordination surface; it must be visible to every reader (dashboard, board, orchestrator, sibling delegations, the operator scanning their normal workflow), not buried inside one worktree's `.lattice/` that nobody else looks at.

### Lattice writes target the parent repo, not the worktree

Lattice state — task status, comments, attachments, completions — must be written to the parent repo's `.lattice/`, not the worktree's. The worktree's `.lattice/` is a hydrated snapshot that exists so `lattice show <TICKET>` resolves during phase work; it is **read-only** for the delegation. All write operations target `$REPO_ROOT`.

```bash
# Code work lives in the worktree:
cd $WT_DIR
git diff
git commit -am "..."
git push

# Lattice writes live in the parent repo. Use a subshell so cwd doesn't drift:
(cd $REPO_ROOT && lattice status $TICKET review --actor $ACTOR)
(cd $REPO_ROOT && lattice comment $TICKET "..." --actor $ACTOR)
(cd $REPO_ROOT && lattice attach $TICKET <pr-url> --type reference --title "PR" --actor $ACTOR)
(cd $REPO_ROOT && lattice complete $TICKET --review "..." --actor $ACTOR)
```

**Why:** the dashboard, the board UI, the orchestrator's polling, sibling delegations, and any other agents reading the project's Lattice state all read from `$REPO_ROOT/.lattice/`. A status change written only to `$WT_DIR/.lattice/` is invisible to all of them — including the operator looking at their normal Lattice surfaces. The worktree may have its `.lattice/` deltas merged eventually (when the branch lands), but "eventually" is not "right now," and needs-human flags / `blocked` / `review` transitions need to be visible *right now*.

Failure mode this prevents: delegator runs an audit, completes work, flags the ticket needs-human in the worktree, then sits silent. The flag lands only in the worktree's `.lattice/`; the Lattice dashboard, board, and orchestrator never see it. Operator has no way to know their input is needed. (Observed 2026-05-03 on C11-1; led to this principle.)

**Reading is fine from either location.** `lattice show` in the worktree shows the worktree's view (which may be slightly behind the parent if the parent is being mutated by something else). When the orchestrator polls, it reads from `$REPO_ROOT` because that's where writes land — see *Active orchestrator watch* below.

### Verify the prior phase's claims before acting on them

Every phase inherits the prior phase's claimed work. Before executing what the prior phase said it did, ground in the actual artifacts: read the diff, grep the source, list the commits. Plan claims a localization sweep — Impl checks the codebase pattern before wrapping every string. Impl reports completion — Translator greps the diff for `String(localized:)` calls before translating. Review reads `synthesis-action.md` — but verifies cited file:line refs against the actual diff before applying. The pattern generalizes: a downstream phase trusts no upstream claim without grounding. Catching a Plan-vs-codebase or Plan-vs-Impl gap at the next phase is cheap; catching it post-merge is not.

### Lattice status is the durable record — bump it as you go, not at the end

Surface metadata (`c11 set-metadata --key status`) is local and ephemeral — it tells the operator what the *pane* is doing right now. The Lattice ticket status is a different thing: it's what the operator, the orchestrator, sibling delegators, and every future reader use to know where the *work* is. **Bump the ticket status at every phase transition, immediately, before you spawn the next sub-agent.** Never batch status updates for the end. Never assume opening a PR implies `review`.

Concrete invariant: before the Plan sub-agent starts, status must be `in_planning`. Before Impl, `planned`. Before Review, `review` is already correct (Impl ends by transitioning there). Before opening the PR, verify `lattice show <ticket>` reports the expected status — if it doesn't, you missed a transition; fix it before pushing further.

The orchestrator polls on these transitions to sequence dependent work (e.g., "spawn C11-4 when C11-7 hits review"). If you skip a transition, downstream delegations stall silently and the operator has to babysit. Status discipline is the contract that makes parallel delegations work.

Failure mode this prevents: delegator drives 14 commits + opens PR, but ticket status never moves past `planned`. Operator looking at the board has no idea the work is real, dependent tickets don't fire, audit trail breaks. (Observed 2026-04-25 on CMUX-37; the orchestrator had to backfill status by hand.)

## Two-line sidebar convention

Every surface in the delegation — orchestrator, delegator, every phase sub-agent — uses a two-line read in the c11 sidebar:

- **Tab title (first line)**: `<TICKET> <ROLE>` or `<TICKET> <ROLE> :: <PHASE>`. e.g. `<TICKET> Orchestrator`, `<TICKET> Delegator`, `<TICKET> Delegator :: Plan`.
- **Description (second line)**: the ticket's **purpose** — a one-line restatement of what the ticket is *for* (the `title` field from `lattice show`). The same purpose string across every surface in the delegation, so scrubbing the sidebar tells you what the work is *about*, not just who's doing what role.

The first line answers *who am I and where am I in the dance*. The second line answers *what are we actually trying to do*. Never conflate them. Never use the description to narrate orchestration meta-state — that lives in metadata (`role`, `status`, `progress`) and is already visible elsewhere.

## When to invoke

Operator cues:
- "Execute this ticket" / "run this ticket end-to-end" / "delegate it"
- "Create an orchestrator pane for <TICKET>" / any setup that mentions an orchestrator+delegator split
- "Walk this through the Lattice process"

Pre-flight:
- `CMUX_SHELL_INTEGRATION=1` (you are in c11). If not, bail — this pattern depends on c11 panes.
- The target ticket exists: `lattice show <TICKET>` returns a task.
- Load the `c11` and `lattice` skills if not already loaded.
- The target repo has a `CLAUDE.md` the delegator can read for project-specific conventions (typing-latency paths, localization policy, testing policy, release flow, validation flow, etc.). Don't duplicate those rules here — point at CLAUDE.md.

## Setup playbook

Run these in order from the orchestrator pane. All c11 flags follow the "always pass `--surface`/`--workspace` explicitly on surface writes" convention from the c11 skill.

```bash
TICKET="<TICKET>"                   # short ID or ULID; both resolve. ULID is canonical, prefer it in sub-agent prompts.
TICKET_LOWER="$(echo "$TICKET" | tr '[:upper:]' '[:lower:]')"   # used as a filename prefix so prompts self-identify
ACTOR="agent:<model>-<ticket>"      # unique per delegation so actor trails don't collide

# Fetch the ticket's title plus its parent ticket (if any), composed into a
# multi-line description — used on every surface in this delegation
# (orchestrator, delegator, every phase sub-agent) so the c11 sidebar grounds
# the operator in the parent context, the immediate ticket, and the phase role.
# See the c11 skill's "Multi-line descriptions for ticket-flow surfaces" section.
TITLE="$(lattice show "$TICKET" --json | jq -r '.data.title // empty')"
PARENT_ID="$(lattice show "$TICKET" --json | jq -r '.data.relationships_out[]? | select(.type=="related_to" or .type=="subtask_of") | .target_task_id' | head -1)"
PARENT_TITLE="$([ -n "$PARENT_ID" ] && lattice show "$PARENT_ID" --json 2>/dev/null | jq -r '.data.title // empty')"
PURPOSE="$([ -n "$PARENT_TITLE" ] && printf '%s\n' "$PARENT_TITLE.")$TICKET — $TITLE."
# When you set surface descriptions, append the phase role on a third line:
#   "$PURPOSE\nDelegator: orchestrating Plan → Impl → Review → PR."

# 1. Create an isolated worktree for the delegation.
#    Naming: <ticket-slug>-<short-purpose-slug> or whatever the project convention is.
#    Worktree lives outside the repo (sibling directory) so the orchestrator's repo is untouched.
REPO_ROOT="<path-to-repo>"          # the repo the ticket targets
WT_SLUG="<ticket-lowercase-slug>"   # e.g. "<ticket>-<short-branch-slug>"
WT_DIR="$REPO_ROOT-worktrees/$WT_SLUG"   # or whatever the project's worktree convention is
BRANCH="$WT_SLUG"

git -C "$REPO_ROOT" fetch origin main
git -C "$REPO_ROOT" worktree add -b "$BRANCH" "$WT_DIR" origin/main

# 1a. Hydrate the worktree's .lattice/ as a READ-ONLY snapshot for sub-agent
#     resolution (so `lattice show $TICKET` works inside the worktree during
#     phase work). Writes still target $REPO_ROOT — see *Lattice writes target
#     the parent repo* in Core principles. Worktrees inherit .lattice/ from
#     the base commit; a ticket created in $REPO_ROOT after the base SHA won't
#     be in the worktree's view without this copy.
( cd "$WT_DIR"
  if ! lattice show "$TICKET" >/dev/null 2>&1; then
    ULID="$(jq -r ".map[\"$TICKET\"] // empty" "$REPO_ROOT/.lattice/ids.json")"
    [ -z "$ULID" ] && ULID="$TICKET"   # caller passed a ULID directly
    cp "$REPO_ROOT/.lattice/tasks/$ULID.json"   .lattice/tasks/
    cp "$REPO_ROOT/.lattice/events/$ULID.jsonl" .lattice/events/ 2>/dev/null || true
    cp "$REPO_ROOT/.lattice/plans/$ULID.md"     .lattice/plans/ 2>/dev/null || true
    jq ".map[\"$TICKET\"] = \"$ULID\"" .lattice/ids.json > .lattice/ids.json.new && mv .lattice/ids.json.new .lattice/ids.json
  fi
)

# 2. Orient the orchestrator (you).
ORCH_SURF="$CMUX_SURFACE_ID"
WS="$CMUX_WORKSPACE_ID"
c11 set-agent       --surface "$ORCH_SURF" --type claude-code --model claude-opus-4-7
c11 rename-tab      --surface "$ORCH_SURF" "$TICKET Orchestrator"
c11 set-metadata    --surface "$ORCH_SURF" --key role   --value "orchestrator"
c11 set-metadata    --surface "$ORCH_SURF" --key task   --value "$TICKET"
c11 set-metadata    --surface "$ORCH_SURF" --key status --value "delegating"
c11 set-description --surface "$ORCH_SURF" "$(printf '%s\nOrchestrator: handing off to delegator in sibling pane.' "$PURPOSE")"

# 3. Split a new pane directly below and discover its refs.
c11 new-split down
#    → "OK surface:<N> workspace:<M>" (new-split does NOT return the pane ref)
c11 tree --no-layout
#    Read the new pane from the tree output. Capture DELEG_PANE and DELEG_SURF.

# 4. Seed the delegator pane's metadata — SAME purpose string so the
#    sidebar reads consistently across the delegation.
c11 set-agent       --surface "$DELEG_SURF" --type claude-code --model claude-opus-4-7
c11 rename-tab      --surface "$DELEG_SURF" "$TICKET Delegator"
c11 set-metadata    --surface "$DELEG_SURF" --key role   --value "delegator"
c11 set-metadata    --surface "$DELEG_SURF" --key task   --value "$TICKET"
c11 set-metadata    --surface "$DELEG_SURF" --key status --value "starting"
c11 set-description --surface "$DELEG_SURF" "$(printf '%s\nDelegator: orchestrating Plan → Impl → Review → PR.' "$PURPOSE")"

# 5. Write the delegator prompt to the working root's .lattice/prompts/
#    directory. Co-locating prompts with the rest of the ticket's state keeps
#    them inspectable in place and tied to the orchestration's lifetime.
#    $WT_DIR is the worktree when you're using one (the default in this
#    skill); the same relative `.lattice/prompts/<ticket>-<phase>.md` path
#    resolves cleanly when running without a worktree because it's relative to
#    whatever the launching shell `cd`s into. Parameterize the template
#    with $TICKET, the ULID, $DELEG_PANE, $DELEG_SURF, $WT_DIR, $BRANCH, etc.
#
#    Filenames are **ticket-prefixed** (`<ticket-lower>-delegator.md`,
#    `<ticket-lower>-plan.md`, etc.) — never bare `delegator.md` / `plan.md`.
#    Reasons: (a) `grep -r delegator.md` and `git log -- '*delegator.md*'`
#    light up across every worktree if the names collide; (b) `lattice
#    comment` and PR descriptions reference these paths, and a self-identifying
#    filename survives copy-paste; (c) when multiple delegations are in
#    flight the operator can scan a flat listing of prompt files and tell
#    them apart. Convention is enforcement-light but the cost of forgetting
#    grows linearly with parallel delegations.
mkdir -p $WT_DIR/.lattice/prompts
cat > $WT_DIR/.lattice/prompts/${TICKET_LOWER}-delegator.md <<'EOF'
[templated prompt — see next section]
EOF

# 6. One-shot launch. `claude --dangerously-skip-permissions` resolves to the
#    PATH-scoped wrapper and inherits the auth chain. No ready-state polling.
#
#    If the operator's shell auto-starts a TUI on terminal init (e.g. `cc` for
#    Claude Code), the launch text would land inside the running TUI rather
#    than the shell. Read first; send the right shape:
SCREEN="$(c11 read-screen --workspace "$WS" --surface "$DELEG_SURF" --lines 5 2>&1)"
if echo "$SCREEN" | grep -qE 'Claude Code v|^❯ '; then
  # TUI already running — send only the prompt content as a user message.
  # Use the ABSOLUTE path so the agent doesn't have to guess at cwd
  # (the TUI's cwd may not be $WT_DIR).
  c11 send --workspace "$WS" --surface "$DELEG_SURF" \
    "Read $WT_DIR/.lattice/prompts/${TICKET_LOWER}-delegator.md and follow the instructions."
else
  # Clean shell — one-shot launch.
  c11 send --workspace "$WS" --surface "$DELEG_SURF" \
    "cd $WT_DIR && claude --dangerously-skip-permissions --model opus \"Read .lattice/prompts/${TICKET_LOWER}-delegator.md and follow the instructions.\""
fi
c11 send-key --workspace "$WS" --surface "$DELEG_SURF" enter

# 7. Record the setup on the ticket so the trail is complete from first event.
#    All lattice writes target $REPO_ROOT — the worktree's .lattice/ is for
#    sub-agent reads only.
( cd "$REPO_ROOT" && lattice comment "$TICKET" \
    "Orchestration started. Orchestrator: surface:$ORCH_SURF. Delegator: pane:$DELEG_PANE / surface:$DELEG_SURF. Worktree: $WT_DIR (branch $BRANCH). Parent repo (canonical Lattice store): $REPO_ROOT. Prompt at $WT_DIR/.lattice/prompts/${TICKET_LOWER}-delegator.md. Phase sub-agents will be sibling surfaces inside the delegator's pane." \
    --actor human:<operator> )

# 8. Seed the orchestrator's watch state file (used by the active polling loop)
#    and schedule the first wake-up. See "Active orchestrator watch" below for
#    what to do on each wake. The state file lives in /tmp keyed by ticket so
#    parallel delegations don't collide. Polling reads from $REPO_ROOT.
cat > /tmp/${TICKET_LOWER}-orch-state.json <<EOF
{"ticket":"$TICKET","wt_dir":"$WT_DIR","repo_root":"$REPO_ROOT","ws":"$WS","deleg_surf":"$DELEG_SURF","last_status":"$(cd $REPO_ROOT && lattice show $TICKET --json | jq -r '.data.status')","last_comment_count":$(cd $REPO_ROOT && lattice show $TICKET --json | jq '.data.comment_count // 0'),"started_at":"$(date -u +%FT%TZ)"}
EOF
# Then call ScheduleWakeup with delaySeconds=1500 (default) and a prompt that
# re-enters this skill's polling routine. ScheduleWakeup is a Claude Code tool
# call, not a shell command — invoke it from the orchestrator's tool surface.
```

After step 8 the setup is complete. The orchestrator must keep itself alive via `ScheduleWakeup` (see *Active orchestrator watch* below) — a chat that returns without scheduling another wake-up is a chat that goes silent.

## Delegator prompt template

The delegator is a full Claude Code session launched with one prompt. Give it everything it needs to run autonomously; the orchestrator only reads ticket state and surface metadata afterward (it is not a participant in the work itself). Template below — substitute the `{{var}}` placeholders before writing. Standard placeholders: `{{TICKET}}`, `{{TASK_ULID}}`, `{{TASK_TITLE}}`, `{{PURPOSE}}` (the multi-line parent + ticket string built in the setup playbook), `{{DELEG_PANE}}`, `{{DELEG_SURF}}`, `{{ORCH_SURF}}`, `{{WS}}`, `{{WT_DIR}}`, `{{REPO_ROOT}}` (parent repo — the canonical Lattice store), `{{BRANCH}}`, `{{ACTOR}}`.

```markdown
# {{TICKET}} Delegator

You are the **delegator** for Lattice ticket **{{TICKET}}** (`{{TASK_ULID}}`): *"{{TASK_TITLE}}"*. Drive this ticket end-to-end through the full Lattice lifecycle, spawning sub-agents in sibling surfaces (tabs in your own pane) for each phase.

You are the **primary human interface for this ticket**. The operator scrubs your pane, not your sub-agents'. All escalations, decisions, and status-to-human communication flow through you. Your sub-agents post to Lattice and stop; they do not address the human.

## Context

- You live in pane **{{DELEG_PANE}}**, surface **{{DELEG_SURF}}**, workspace **{{WS}}**.
- Orchestrator: surface **{{ORCH_SURF}}** (tab "{{TICKET}} Orchestrator"). Do not message the orchestrator directly; report via Lattice comments on {{TICKET}}.
- Worktree: **{{WT_DIR}}** on branch **{{BRANCH}}**. **All code work stays inside this worktree.** The main repo's working tree is off-limits for code edits, builds, and commits.
- Parent repo: **{{REPO_ROOT}}**. **All `lattice` writes target this repo, not the worktree** — see the *Lattice writes* discipline below. Use `(cd {{REPO_ROOT}} && lattice ...)`.
- Actor: `{{ACTOR}}` for your own writes. Sub-agents you spawn should tag themselves with a phase-specific actor (e.g., `{{ACTOR}}-plan`, `{{ACTOR}}-impl`, `{{ACTOR}}-review`, `{{ACTOR}}-translator`).

## Lattice writes go to the parent repo

Lattice is the project's coordination surface. The dashboard, the board UI, the orchestrator, sibling delegations, and any other agents on the project all read from `{{REPO_ROOT}}/.lattice/`. **A `lattice status`, `lattice needs-human`, or `lattice comment` from `{{WT_DIR}}` lands in the worktree's `.lattice/` and nowhere else** — invisible to every reader. That's a documented failure mode (the audit on C11-1 raised the needs-human flag in its worktree and the operator had no way to know).

Discipline:

```bash
# Code work — in the worktree.
cd {{WT_DIR}}
git diff
git commit -am "..."
git push

# Lattice writes — through the parent repo. Always.
(cd {{REPO_ROOT}} && lattice status {{TICKET}} review --actor {{ACTOR}})
(cd {{REPO_ROOT}} && lattice comment {{TICKET}} "..." --actor {{ACTOR}})
(cd {{REPO_ROOT}} && lattice attach {{TICKET}} <pr-url> --type reference --title "PR" --actor {{ACTOR}})
(cd {{REPO_ROOT}} && lattice complete {{TICKET}} --review "..." --actor {{ACTOR}})
```

Reads can come from either side; the worktree's `.lattice/` is a snapshot for resolution during phase work. But every write goes through `{{REPO_ROOT}}`. Sub-agents you spawn must inherit this discipline — bake the `(cd {{REPO_ROOT}} && lattice ...)` form into every phase prompt.

## Load these skills first

1. **c11** — pane splits, surface creation, metadata, send/send-key, launching sub-agents.
2. **lattice** — Advance loop, statuses, `complete` ceremony, plan file at `.lattice/notes/<ulid>.md`.
3. **delegator-example** (this skill) — the pattern you're implementing.
4. Any review/PR/release skill the project ships with (e.g., `trident-code-review`, `compushar`, `release-local`, etc.).
5. **Read the project's `CLAUDE.md`.** It holds the project-specific rules — hot paths, localization, testing, validation, release. Do not improvise these; CLAUDE.md is authoritative.

## Orient immediately

```bash
cd {{WT_DIR}}                                       # code work happens here
export REPO_ROOT={{REPO_ROOT}}                      # Lattice writes go here
c11 identify
c11 set-agent       --surface "$CMUX_SURFACE_ID" --type claude-code --model claude-opus-4-7
c11 set-metadata    --surface "$CMUX_SURFACE_ID" --key status --value "orienting"
# Description = three lines, per the c11 skill's "Multi-line descriptions for
# ticket-flow surfaces" section: parent context, this ticket, this phase. Keeps
# the sidebar groundable for the operator scanning a dozen surfaces.
c11 set-description --surface "$CMUX_SURFACE_ID" "$(printf '%s\nDelegator: orchestrating Plan → Impl → Review → PR.' "{{PURPOSE}}")"
(cd $REPO_ROOT && lattice show {{TICKET}})
cat $REPO_ROOT/.lattice/notes/{{TASK_ULID}}.md 2>/dev/null || echo "(no plan yet)"
```

## Phase model — one sibling surface per phase

Each phase = a new tab in **pane {{DELEG_PANE}}** so the operator can scrub all of them from one vertical slice. Every phase sibling inherits the same two-line sidebar convention — title carries role+phase, description carries the multi-line purpose plus the phase role.

```bash
c11 new-surface --pane {{DELEG_PANE}}
# → captures new surface ref; use `::` lineage in its tab name:
#   "{{TICKET}} Delegator :: Plan"    …:: Impl    …:: Translator    …:: Review    …:: Validate    …:: Fix

# Each phase sibling sets its description to {{PURPOSE}} + the phase-specific role line:
c11 set-description --surface "<phase-surface-ref>" "$(printf '%s\nPlan phase: drafting commit grouping and parallelization.' "{{PURPOSE}}")"
```

Launch each sub-agent with the c11 one-shot pattern: write the phase prompt to `$WT_DIR/.lattice/prompts/${TICKET_LOWER}-<phase>.md`, then `cd $WT_DIR && claude --dangerously-skip-permissions --model opus "Read .lattice/prompts/${TICKET_LOWER}-<phase>.md and follow the instructions."`. No ready-state polling. The relative path resolves against the launching `cd` — `$WT_DIR` is the worktree when you're using one, or the repo root if you're not.

**Prompt filenames are ticket-prefixed** (e.g. `c11-6-plan.md`, not bare `plan.md`). See the rationale in step 5 of the setup playbook above; the short version is that bare names collide across parallel delegations in greps, git history, and Lattice references. Prefer ticket-prefixed always, even when the worktree is supposedly isolated — copy-paste of paths between agents is a constant.

Prompt files accumulate alongside the rest of the ticket's `.lattice/` state, one per phase. For ticket `C11-6` (`TICKET_LOWER=c11-6`):

```
$WT_DIR/.lattice/prompts/
├── c11-6-delegator.md   # orchestrator writes this in step 5 of setup
├── c11-6-plan.md        # delegator writes this before spawning the Plan sibling
├── c11-6-impl.md        # delegator writes this before spawning the Impl sibling
├── c11-6-review.md      # delegator writes this before spawning the Review sibling
├── c11-6-validate.md    # delegator writes this before spawning the Validate sibling
└── c11-6-fix.md         # only if a Fix phase is needed
```

Apply the same auto-launch-detection pattern from setup playbook step 6 to each phase sub-agent launch (read the target surface for an existing TUI prompt before sending the launch line).

**Every sub-agent prompt must end with an explicit stop instruction:**
> "After you post your completion comment, stop. Do not address the human directly — the delegator is the human's interface for this ticket. Another agent will evaluate your work and continue the process."

Without this, sub-agents continue doing work after their phase is done — renaming tabs, exploring unrelated issues, chatting with the human. The stop line is the boundary.

**Every sub-agent prompt must wrap `lattice` writes in `(cd {{REPO_ROOT}} && lattice ...)` and `lattice` reads in `(cd {{REPO_ROOT}} && lattice show ...)`.** This keeps writes visible to the dashboard and the orchestrator, and avoids short-ID resolution failures from cwd drift during builds. ULIDs always resolve, but the wrapper is more robust.

## Lifecycle

Every phase below begins with a **status bump** as its first action. This is non-negotiable — see *Lattice status is the durable record* and *Lattice writes target the parent repo* in Core principles. After bumping, verify with `(cd {{REPO_ROOT}} && lattice show {{TICKET}} | grep "^Status:")` before doing anything else.

### 1. Plan
- **Status bump (first action):** `(cd {{REPO_ROOT}} && lattice status {{TICKET}} in_planning --actor {{ACTOR}})`. Verify with `(cd {{REPO_ROOT}} && lattice show {{TICKET}} | grep "^Status:")`.
- Spawn **Plan** sibling. Prompt it to: read the ticket, survey the relevant code area, write `{{REPO_ROOT}}/.lattice/notes/{{TASK_ULID}}.md` (or `.lattice/plans/...`), comment on the ticket when done. Plan notes are part of the Lattice trail and live in the parent repo's `.lattice/` so the dashboard and other agents can find them. Plan should **flag** operator decisions with a recommendation, not raise needs-human unless the decision genuinely blocks progress.
- Sanity-check the plan yourself. If the plan surfaced a decision, resolve it (pick the recommended option, or escalate to the human via Lattice comment on the ticket if the decision is genuinely theirs).
- **Status bump on exit:** `(cd {{REPO_ROOT}} && lattice status {{TICKET}} planned --actor {{ACTOR}})`. Verify before continuing.

### 2. Implement
- **Status bump (first action):** `(cd {{REPO_ROOT}} && lattice status {{TICKET}} in_progress --actor {{ACTOR}})`. Verify with `(cd {{REPO_ROOT}} && lattice show {{TICKET}} | grep "^Status:")`.
- Spawn **Impl** sibling. Prompt it to: read the plan, implement commit-by-commit with clear messages in `{{WT_DIR}}`, push after meaningful commits, post `lattice comment` updates on the ticket *via the parent repo* after each meaningful commit. Respect every CLAUDE.md constraint.
- **Impl must surface deviations from the plan explicitly in its completion comment**, even when the deviation is the right judgment call. Format: "Deviated from plan on X — chose Y instead because Z." Skipped items, deferred tests, substituted approaches — list them. A silent right-decision is harder for the next phase to validate than an explicit one (see *Verify the prior phase's claims* in Core principles).
- If user-facing strings change and the repo is localized, spawn a **Translator** sibling after impl. Parallelize one-per-locale for larger batches.
- **Status bump on exit:** `(cd {{REPO_ROOT}} && lattice status {{TICKET}} review --actor {{ACTOR}})`. Verify before continuing.

### 3. Review
- Spawn a **Review** sibling. The Review sibling invokes whatever review flow the project prescribes (typically `trident-code-review` or `trident-plan-review` for Stage 11 projects; a lighter-weight review where appropriate).

**Review sub-agents are READ-ONLY when they are pure review agents.** When the Review sibling is itself invoking a multi-agent framework like trident, that framework's reviewers and synthesizers are read-only; the Review sibling that invoked the framework is *not* read-only and is expected to act on the findings per the framework's contract. Every pure-review sub-agent prompt must include the guardrail:

> **IMPORTANT: You are a READ-ONLY review agent. DO NOT create Lattice tasks, DO NOT run `lattice create`, DO NOT modify the Lattice board in any way, DO NOT commit or push code. Your only job is to read the code and write review output.**

- **Review cycle contract — when the framework produces a `synthesis-action.md` (trident does):**
  - The Review sibling reads `synthesis-action.md` as the action contract. Items under **Apply by default** (Blockers, Important, Straightforward mediums, Evolutionary clear wins) get applied as fix commits on the same branch and pushed. Items under **Surface to user** and **Evolutionary worth considering** get **escalated to the delegator** via a Lattice comment with the synthesizer's rationale plus any annotation the reviewer adds.
  - In other words: the action synthesizer has already done the validation pass and the apply/escalate split. The reviewer executes that contract, then escalates only the deferred items.
  - If the verdict is `rework-then-review` (or the equivalent), the reviewer escalates the entire batch rather than applying piecemeal — the review pack itself is telling you the work needs structural rework, not absorption.

- **Review cycle contract — when the review flow does not produce a `synthesis-action.md`:**
  - Use a minor/major heuristic. **Minor findings** (doc tightening, test nits, trivial refactors, cosmetic fixes, anything the reviewer estimates as under ~20 min of focused work) → the Review sibling applies them directly as a fix commit, pushes, and posts a pass comment noting what was absorbed. **Major findings** (correctness bugs, structural issues, scope violations, anything that needs design thought or touches a hot path) → the Review sibling escalates to the delegator via a Lattice comment with verdict and specific findings.

- **On escalation, the delegator routes (every `lattice` call goes through `(cd {{REPO_ROOT}} && ...)`):**
  - **Focused rework**: spawn a **Fix** sibling with a tight prompt addressing the specific findings. Commits land on the same branch; no new review cycle required unless the fix itself is non-trivial.
  - **Structural rework**: `(cd {{REPO_ROOT}} && lattice status {{TICKET}} in_progress)`; spawn a new **Impl** sibling with the findings appended to the plan. A new review cycle follows.
  - **Plan-level rework**: `(cd {{REPO_ROOT}} && lattice status {{TICKET}} in_planning)`; spawn a new **Plan** sibling.
  - **Genuine human decision**: `(cd {{REPO_ROOT}} && lattice needs-human {{TICKET}} "<what you need>")`, stop. No status transition — the ticket keeps its current status and the needs-human flag is what surfaces it. The orchestrator surfaces the flag to the operator (see *Active orchestrator watch*); the operator finds out via the orchestrator chat AND the dashboard, both of which now see the flag because the write went to the parent.

- **Max 3 rework cycles** before the delegator raises the needs-human flag — pattern means something deeper is off.

### 4. Validate
- Before handoff, the delegator (or a dedicated **Validate** sibling) runs whatever validation the project's CLAUDE.md prescribes. This is typically: build the project in a way the operator can smoke-test (tagged local build, preview deploy, simulator run, headless browser check), confirm it comes up cleanly, post usage instructions on the ticket, and leave the validation artifact in a state the operator can poke at.
- The point is that the human's job when they show up is to *smoke-test*, not *build from scratch*. The delegator has done the building; the human does the last mile.
- If validation reveals issues, treat them like Review major findings: escalate, spawn a Fix sibling, push fixes.

### 5. Handoff
- **Pre-flight invariant check (first action):** `(cd {{REPO_ROOT}} && lattice show {{TICKET}} | grep "^Status:")` must report `review`. If it does not, you missed a transition earlier — fix it now, before opening the PR. Also re-run the check after Validate fixes if any landed, since a Fix sibling may have moved status around.
- Before `gh pr create`, verify the base branch is on origin: `git ls-remote origin <base-branch>`. If not found, push it first — stacked branches (feature on feature) frequently haven't been pushed yet and `gh pr create` fails silently with a blank-SHA error.
- Push the branch, open a PR via `gh pr create`, attach the PR URL: `(cd {{REPO_ROOT}} && lattice attach {{TICKET}} <pr-url> --type reference --title "PR" --actor {{ACTOR}})`.
- `(cd {{REPO_ROOT}} && lattice complete {{TICKET}} --review "..." --actor {{ACTOR}})` — the review text is the audit entry for every future reader, write it like you mean it. **Unless the project convention is to leave merge to the operator**, in which case stay at `review` and let the operator call `complete` after PR merge.
- Final `(cd {{REPO_ROOT}} && lattice comment ...)` summarizing what shipped + pointing to the validation artifact.
- The delegator's last message in its pane should be a human-readable summary of where the ticket landed, because that's what the operator will read when they come back.

## Handling follow-up work

If Review or Validate surfaces items beyond the original scope:

- **Default: absorb into the current ticket's PR.** Add commits, update the plan note, note the absorption in the Lattice comment trail. One fatter PR is almost always better than a PR + followup PR.
- **Create a follow-up ticket only when** the item is explicitly out of scope per the plan's `Do NOT ship` list, or would block current-ticket merge, or needs its own planning cycle. If you do, link with `related_to` and keep the scope tight.

Err on the side of cohesion. Spree tickets age badly.

## Visible status

Keep your own surface honest throughout:

```bash
c11 set-metadata --surface "$CMUX_SURFACE_ID" --key status --value "planning"    # then implementing / reviewing / validating / handing-off
c11 set-progress 0.25                                                              # use set-progress, not set-metadata --key progress
c11 log --source "{{TICKET}} Delegator" "Plan sub-agent launched in surface:<N>"
```

Use `c11 set-progress <float>` — not `c11 set-metadata --key progress --value <n> --type number`. The dedicated command handles the type contract; the metadata form fails with `reserved_key_invalid_type`.

After each sub-agent phase commit lands, run `git show <sha> --stat` to confirm the diff touches only expected files before advancing to the next phase. Catch scope drift here, not in review.

Post a one-line Lattice comment at every phase transition so the orchestrator can follow without screen-scraping.

## Guardrails

- **Don't steamroll human judgment.** Genuine decisions → `lattice needs-human <ticket> "<the question>"`. The operator filed the ticket; open questions are real.
- **Code in the worktree, Lattice in the parent.** All code edits, builds, and commits stay in `{{WT_DIR}}`. Every `lattice` write goes through `(cd {{REPO_ROOT}} && lattice ...)`. Don't write code into the parent repo's working tree; don't write Lattice state into the worktree's `.lattice/`. Two homes, two purposes.
- **Prefer cohesive tickets.** Absorb follow-up work into the current PR when it fits. Create follow-up tickets reluctantly.
- **Don't mix unrelated fixes into this ticket's commits.** Upstream-worthy fixes discovered along the way get their own commits and, if appropriate, their own tickets.
- **Follow CLAUDE.md, not your instincts.** Testing, hot paths, localization, validation — all authoritative there. Deviations break the operator's trust faster than bugs do.
- **Don't address the human directly from a sub-agent.** Sub-agents post to Lattice and stop. The delegator is the human's interface.
- **No `--no-verify` commits** unless the operator explicitly asked for it.
- **Detect TUI auto-launch before sending an agent launch.** If the operator's shell auto-starts a TUI (e.g. Claude Code via `cc`) on terminal init, a `c11 send "cd ... && claude ..."` may arrive after the TUI is up and get typed into it as a user message rather than into the shell — the resulting nested-claude is functional but cosmetically confusing. Step 6 of the setup playbook reads the screen first and adapts. The same pattern applies to phase sub-agent launches.

## Watching sub-agents (delegator)

The delegator owns the responsiveness of the whole delegation. A sub-agent that crashed, hung on a permission prompt, or wandered off-script can sit dead for many minutes if the delegator only polls every few minutes. The operator scrubs *your* pane; if you don't notice the stall, they will, and the trust budget for the pattern erodes.

**Cadence: poll every 30–60 seconds while a sub-agent is the current working item.** Not 90s, not 5min, not "every once in a while." Tight polling is a feature: it bounds the worst-case "how long can this be broken before someone notices" to under a minute. The token cost is real but small relative to the cost of an unnoticed stall in a multi-hour delegation.

**Poll on more than commits.** A `count >= N` predicate misses every failure mode that doesn't manifest as commits — and the sub-agent failure modes that hurt most (silent crash, stuck on input, wandered off-script) all look like "no new commits" to a commit-counter. Each polling cycle should check several signals:

- **Commits on remote** for the expected branch (`git rev-list --count`) — the throughput metric.
- **Sub-agent surface liveness** via `c11 read-screen --workspace $WS --surface $SUB --lines 30 | tail -10` — is there an active prompt waiting? An error trace? A "what should I do?" question to the human that violates the stop contract?
- **Lattice activity on the ticket** — new comments, status changes, status pill on the sub-agent surface (`c11 get-metadata --workspace $WS --surface $SUB --key status`). A sub-agent that posted a meaningful comment 3 minutes ago and went silent is different from one that has produced nothing since launch.
- **Time since last observable progress.** If you've seen no commit, no comment, no status change, and no surface activity for ~5 minutes, treat it as a probable stall and read the surface in full. Don't wait another 20 minutes hoping it recovers.

```bash
# Background poller — 45s cadence, checks throughput AND liveness.
# REPO_ROOT is the parent repo (canonical Lattice store); writes from
# sub-agents land there per the "Lattice writes target the parent repo"
# core principle.
cat <<'BASH' > /tmp/${TICKET_LOWER}-watch-impl.sh
#!/bin/bash
EXPECTED_COMMITS=9
BRANCH=cmux-37/final-push
WS=workspace:3
SUB_A=surface:14
SUB_B=surface:15
REPO_ROOT=/path/to/parent/repo
LAST_PROGRESS_AT=$(date +%s)
TIMEOUT_AT=$(( $(date +%s) + 10800 ))

while :; do
  git -C "$REPO_ROOT" fetch origin "$BRANCH" --quiet 2>/dev/null
  COUNT=$(git -C "$REPO_ROOT" rev-list --count "origin/main..origin/$BRANCH" 2>/dev/null || echo 0)
  COMMENTS=$(cd "$REPO_ROOT" && lattice show "$TICKET" --json 2>/dev/null | jq '.data.comment_count // 0')
  STATUS_A=$(c11 get-metadata --workspace "$WS" --surface "$SUB_A" --key status 2>/dev/null)
  STATUS_B=$(c11 get-metadata --workspace "$WS" --surface "$SUB_B" --key status 2>/dev/null)
  NOW=$(date +%s)

  echo "[$(date -u +%FT%TZ)] commits=$COUNT/$EXPECTED_COMMITS comments=$COMMENTS A=$STATUS_A B=$STATUS_B"

  # Refresh progress timestamp on any signal of forward motion
  # (compare against a stored snapshot — omitted here for brevity)

  if [ "$COUNT" -ge "$EXPECTED_COMMITS" ]; then
    echo "TARGET REACHED"; exit 0
  fi
  if [ "$NOW" -ge "$TIMEOUT_AT" ]; then
    echo "TIMEOUT"; exit 1
  fi
  # Stall heuristic: 5 min since last commit/comment/status change → read screens
  if [ $((NOW - LAST_PROGRESS_AT)) -gt 300 ]; then
    echo "STALL: reading sub-agent screens"
    c11 read-screen --workspace "$WS" --surface "$SUB_A" --lines 50 | tail -30
    c11 read-screen --workspace "$WS" --surface "$SUB_B" --lines 50 | tail -30
    LAST_PROGRESS_AT=$NOW   # reset so we don't re-print every 45s
  fi

  sleep 45
done
BASH
chmod +x /tmp/${TICKET_LOWER}-watch-impl.sh
nohup /tmp/${TICKET_LOWER}-watch-impl.sh > /tmp/${TICKET_LOWER}-watch-impl.log 2>&1 &
disown
```

**Use Monitor when the wake signal can be event-driven.** A long-running tail of the watch log via Monitor (`persistent: true`) wakes the delegator on every line — effectively giving you per-update wake-ups without burning a wakeup call per 45s tick. Pair it with a fallback ScheduleWakeup at 1200–1800s as a safety net.

**Adjust cadence by phase:**

| Phase | Polling cadence | Why |
|-------|-----------------|-----|
| Plan | 30–60s | Plan is fast (single file write); slow polling means the delegator sleeps through completion. |
| Impl (active) | 30–60s | Multiple commits expected; tight cadence catches stalls and posts updates promptly. |
| Translator | 30–60s | Per-locale parallel agents; same reasoning as Impl. |
| Review (trident) | 60–120s | Multi-agent framework runs longer; the action synthesizer phase is the slow part. |
| Validate (build + computer-use) | 60–120s | Build phase is bound by xcodebuild + simulator; little point polling faster. |
| Idle gaps (between phases) | 1200s+ | Nothing actively running; only need to check infrequently. |

**Anti-patterns:**

- `sleep 600` or worse between checks — guarantees a stalled sub-agent sits dead for 10 minutes before the delegator notices.
- Counting commits and nothing else — misses every non-throughput failure mode.
- Manual `lattice show` from the chat thread instead of a background poller — if the delegator is busy doing other work, the operator-visible sidebar status goes stale.
- Letting `until` loops run in the foreground while no other work needs the delegator's attention — fine, but the delegator should be ready to break out and act on what it sees, not just count.

## Active orchestrator watch (mandatory)

After setup, the orchestrator must keep watching until the ticket reaches a terminal state (`done`, `cancelled`) or the operator says stop. **Watching means actively scheduling wake-ups, not assuming the operator will poll.** The skill's prior "passive reader" framing produced exactly the failure mode this section prevents: a delegator finished an audit and raised the needs-human flag, the orchestrator was offline for ~19 minutes, and the operator only learned the audit was done by asking. (Observed 2026-05-03 on C11-1.) Don't repeat it.

### Mechanism: `ScheduleWakeup`

Claude Code's `ScheduleWakeup` tool is the durable wake-up primitive available to the orchestrator. Use it at the end of setup, and again at the end of every wake-up that doesn't surface to the operator. A shell `sleep` loop, a backgrounded `lattice watch`, or a blocking `lattice wait` won't help — the orchestrator chat needs to *return* and then *re-enter*; that's what `ScheduleWakeup` does.

`lattice watch --task` and `lattice wait --status` remain useful for a *human* operator in a side terminal. They are not useful for the orchestrator's own polling.

### Cadence

| Situation | `delaySeconds` | Why |
|-----------|----------------|-----|
| Active phase expected to land soon (e.g. delegator about to wrap a build) | 270 | Stays inside the 5-minute prompt-cache TTL. |
| Long phase (Impl, Validate, big review pack, audit) | 1200–1800 (20–30 min) | One cache miss buys a long, useful wait. Avoid the cache-miss-per-tick of 300s. |
| Idle gap between phases | 1800 | Re-check, but don't burn tokens. |
| Soft cap | After 8 hours of no transition, surface "no progress detected" to the operator regardless. | Even if the ticket is still working, the operator should hear from you. |

Default to **1500s (25 min)** when in doubt. That's a sensible balance for phase-level transitions, which are the orchestrator's beat. The delegator handles the tighter 30–60s sub-agent polling — see *Watching sub-agents (delegator)* above.

### What to do on each wake-up

1. **Poll Lattice from the parent repo.** Per *Lattice writes target the parent repo* in Core principles, the delegator's writes land in `$REPO_ROOT/.lattice/`. That's where the orchestrator reads, where the dashboard reads, where everyone else reads.
   ```bash
   cd $REPO_ROOT
   lattice show $TICKET --json | jq -r '.data.status, .data.comment_count'
   lattice show $TICKET --full | tail -60   # full event/comment trail
   ```
2. **Poll the delegator's surface status** (this is c11-daemon-backed, not file-based, so it's process-independent):
   ```bash
   c11 get-metadata --workspace $WS --surface $DELEG_SURF --key status
   ```
3. **Compare against last-known state** stored at `/tmp/${TICKET_LOWER}-orch-state.json` (seeded in step 8 of setup). Diff against `last_status`, `last_comment_count`, and any new comments since `last_check_at`.
4. **Decide: surface, or re-schedule silently.**
   - **Surface to operator** when the ticket transitions to `review`, `done`, or `blocked`, when the needs-human flag is raised, OR a new Lattice comment lands that contains a recognizable signal phrase (`Audit complete`, `Plan complete`, `Impl complete`, `PR opened`, `Validation`, `BLOCKED`, `escalat`, `recommendation`). Read the new comment(s), summarize for the operator, surface immediately. Update the state file. Do **not** schedule another wake-up until the operator decides what's next — the next wake-up gets scheduled when work resumes.
   - **Re-schedule silently** when nothing meaningful changed. Update the state file's `last_check_at`, call `ScheduleWakeup` with the appropriate cadence, return without operator-visible output.

### Surface format

When you do surface, lead with an unmistakable status header (the operator may be scanning many panes — make it obvious at a glance), then answer three questions, terse:

1. **What changed?** (Status transition, new comment summary.)
2. **What does it mean?** (Done / blocked / decision needed / progress checkpoint.)
3. **What's the next step, and is it on you or them?**

Lead-in conventions by transition target:

| Transition | Lead with | Tone |
|------------|-----------|------|
| needs-human flag raised | **🛑 NEEDS YOUR INPUT — `<TICKET>`** | Action required from operator. Don't bury it. |
| `→ blocked` | **⛔ BLOCKED — `<TICKET>`** | External dependency; usually action required but possibly just informational. |
| `→ review` | **✅ READY FOR REVIEW — `<TICKET>`** | PR up, validation done; operator's call to merge. |
| `→ done` | **🎉 DONE — `<TICKET>`** | Final ceremony complete; informational. |
| Signal-phrase comment, no status change | **📋 UPDATE — `<TICKET>`** | Progress checkpoint; usually informational. |

Example:
> **🛑 NEEDS YOUR INPUT — C11-1**
>
> Needs-human flag raised (still `in_progress`). Audit completed; auditor recommends file-a-followup-and-close.
> Two operator decisions on the ticket: (1) what to do with 6 cosmetic stragglers, (2) how to interpret Criterion 3.
> **On you.** The orchestrator will not re-schedule until you direct.

Use the emoji + uppercase header even when the operator is clearly engaged in the chat — the goal is that a glance at the surface tells them "this needs me right now." Restraint elsewhere; loudness here.

### Worktree-vs-main lattice (resolved by the parent-repo-writes principle)

Lattice is file-based; each worktree has its own `.lattice/`. Per *Lattice writes target the parent repo* in Core principles, the delegator and every sub-agent write to `$REPO_ROOT/.lattice/`, so the orchestrator polls there. The worktree's `.lattice/` is a hydrated read-only snapshot — useful for `lattice show` resolution during phase work, but not the source of truth for status transitions or comments. If you find yourself reading from `$WT_DIR/.lattice/` to check status, you're reading the wrong store; if you find yourself writing to it, you're writing to a place no one else looks.

### Operator interrupts

If the operator messages the orchestrator while a wake-up is pending, the wake-up still fires on schedule. That's fine — handle the operator message in the foreground, then either let the wake-up fire normally, or pre-empt it by calling `ScheduleWakeup` again with a different delay, or simply don't re-schedule from the next wake-up if the work is done.

### Polling on artifacts (when you have a specific milestone)

When you know the next meaningful event (e.g. "Impl will push N commits"), you can predicate on the downstream artifact rather than scrape comments. Useful for sequencing dependent delegations. Examples:

```bash
# Inside the wake-up handler, check the artifact directly:
COUNT=$(git -C $WT_DIR rev-list --count origin/main..origin/$BRANCH 2>/dev/null || echo 0)
[ "$COUNT" -ge "$EXPECTED_COMMITS" ] && SURFACE=true

# Or for a Plan that writes a specific section:
grep -q "## Implementation Plan" $WT_DIR/.lattice/plans/$ULID.md 2>/dev/null && SURFACE=true
```

Counters tick on every breadcrumb, so prefer downstream-artifact predicates over comment counts.

If the delegator's surface closes (e.g. the operator closes the pane), the PTY scrollback is unrecoverable — `c11 read-screen` returns `Surface is not a terminal`. That's fine: every meaningful event is already on the ticket. Treat the Lattice audit trail as the durable record and the live panes as transient.

### Anti-patterns

- **"I'll watch the ticket"** without scheduling a wake-up — you won't. The chat goes idle the moment you stop tool-calling. This is half the C11-1 failure verbatim.
- `lattice watch --task $TICKET` or `lattice wait $TICKET --status done --timeout 3600` from inside the orchestrator chat — these block a shell, but the orchestrator isn't a shell. They're for human operators in side terminals.
- **Writing Lattice from the worktree.** A `lattice status` or `lattice comment` from `$WT_DIR` lands in the worktree's `.lattice/` and nowhere else — invisible to the dashboard, the board, the orchestrator, and the operator's normal workflow. The other half of the C11-1 failure. Every Lattice write goes through `(cd $REPO_ROOT && lattice ...)`.
- Surfacing on every wake-up regardless of change — burns the operator's attention budget. Only surface on transitions or signal-phrase comments.
- Surfacing without the action header — a needs-human flag buried in a paragraph reads like progress, not "stop and decide." Lead with the emoji + uppercase header.
- Forgetting to re-schedule from a no-change wake-up — the loop dies after one tick.

## Teardown

- **On `done` — default is leave-open for after-action review.** The delegator and its sibling surfaces stay live so the operator can scrub plan/impl/review transcripts for retrospectives, pattern extraction, or just satisfaction. The delegator may tear them down if explicitly instructed; otherwise leave them.
- **Worktree cleanup is separate** from surface cleanup. Once the PR merges and the branch is deleted, `git worktree remove <dir>` is safe to run. The delegator should leave the worktree alive until the human confirms merge.
- **On the needs-human flag:** leave everything open so the operator can read it directly. Hand control back by posting a clear Lattice comment summarizing what decision is needed.
- **On `blocked`:** same as the needs-human flag but for external dependencies (CI, env, another ticket).

## Start now

1. Orient.
2. Read ticket + any existing plan + project CLAUDE.md.
3. Move to `in_planning`, spawn Plan sibling.
4. Comment on the ticket: "Delegator online. Plan phase starting in sibling surface <ref>."

## Known gaps

- **Pane ref discovery after `new-split`.** `new-split` returns only the new surface ref, not the pane ref; the skill currently works around this by calling `c11 tree --no-layout` and inferring. An upstream change to c11 to emit `OK pane:<P> surface:<N> workspace:<M>` would drop the workaround — file a ticket against c11 when convenient.
- **`new-surface --pane <pane>` needs the pane ref.** If the delegator loses track of its own pane ref, `c11 identify` returns it as `caller.pane_ref`. Keep it in an env var.
- **`c11 new-pane` lacks a `--command` flag.** `c11 new-workspace --command <text>` exists; `c11 new-pane` doesn't. If it did, the auto-launch detection in step 6 would be unnecessary — the orchestrator could create the pane with the launch line as its initial process, bypassing any operator rc-file. File a ticket against c11 when convenient.
