# Lattice vs. Harness Engineering: Pattern Comparison

**Purpose:** Map the patterns from OpenAI's Harness Engineering and Carson's Code Factory against Lattice's existing primitives. Identify what Lattice already provides, what the gap is, and where the opportunity lies.

**Sources:**
- [Harness Engineering (Lopopolo/OpenAI)](./harness-engineering-lopopolo-openai.md)
- [Code Factory (Carson)](./code-factory-ryan-carson.md)
- Lattice project: `CLAUDE.md`, `ProjectRequirements_v1.md`, `Philosophy.md`

---

## The Core Insight

OpenAI's team built a bespoke, markdown-based coordination system inside their repo because nothing like Lattice existed for them to use. Their `docs/exec-plans/active/` and `docs/exec-plans/completed/` are tasks with status. Their `tech-debt-tracker.md` is a backlog. Their `QUALITY_SCORE.md` is a dashboard. Their `design-docs/` with verification status is task metadata.

They reinvented a worse version of what Lattice provides — worse because it lacks event sourcing, actor attribution, status machines, relationship graphs, and mechanical enforcement. But the *instinct* was right: agent-driven work needs a structured, in-repo coordination layer.

Carson's Code Factory has the same gap from the CI side — he bolted together five GitHub Actions workflows with SHA-matching and comment-dedup hacks because no coordination layer exists upstream of the merge gate.

**Lattice is the missing piece in both architectures.**

---

## Pattern-by-Pattern Mapping

### 1. Repository Knowledge as System of Record

| Their Pattern | Lattice Equivalent | Gap |
|---|---|---|
| Everything must live in-repo | `.lattice/` lives in-repo alongside code | **None — direct alignment.** Lattice's file-based design was built for exactly this reason. |
| AGENTS.md as table of contents (~100 lines) | `lattice setup-claude` injects a coordination block into CLAUDE.md | **Aligned.** Lattice's approach is even more principled — the block is templated from a single source of truth (`claude_md_block.py`). |
| Slack threads are illegible to agents | Lattice events + comments = the durable record | **Lattice solves this directly.** Decisions made in conversation can be captured as Lattice events and comments rather than lost in ephemeral channels. |

### 2. Structured Knowledge Directory

| Their Pattern | Lattice Equivalent | Gap |
|---|---|---|
| `docs/exec-plans/active/` | `lattice list --status in_progress` | **Lattice is strictly better.** Their "active plans" are static markdown files. Lattice tasks are event-sourced, queryable, and have status machines. |
| `docs/exec-plans/completed/` | `lattice list --status done` | Same — Lattice provides this with attribution and history. |
| `docs/exec-plans/tech-debt-tracker.md` | `lattice list --status backlog` | A static markdown file vs. a queryable backlog with priority and relationships. |
| `docs/design-docs/` with verification status | `.lattice/notes/<task_id>.md` + task metadata | **Partial gap.** Lattice notes exist but "verification status" on design docs isn't a first-class concept. Could be modeled as task status or custom metadata. |
| `docs/references/` (LLM-optimized .txt files) | Repo-level `research/` folder | **Equivalent.** Different name, same purpose. |
| `QUALITY_SCORE.md` grading domains | Lattice dashboard | **Gap.** Lattice dashboard shows task state, not quality grades. Quality scoring is a potential extension. |
| Progressive disclosure | CLAUDE.md block → `.lattice/` → task details | **Aligned.** Lattice already follows this pattern — the CLAUDE.md block is the entry point, `lattice show` provides depth. |

### 3. Plans as First-Class Artifacts

| Their Pattern | Lattice Equivalent | Gap |
|---|---|---|
| Execution plans checked into repo | Lattice tasks + `.lattice/notes/` | **Lattice is better structured.** Their plans are markdown files in a folder. Lattice tasks have status, events, relationships, and notes. |
| Active vs. completed plans | Status machine: `in_planning` → `planned` → `in_progress` → `done` | **Direct mapping.** Lattice makes this a state machine rather than a folder convention. |
| Progress and decision logs | `lattice comment` + event log | **Lattice provides this with immutability and attribution.** Their progress logs are editable markdown; Lattice events are append-only. |

### 4. Architecture Enforcement

| Their Pattern | Lattice Equivalent | Gap |
|---|---|---|
| Custom linters enforce invariants | Not Lattice's scope — but Lattice tasks could track linter coverage | **By design.** Lattice is the coordination layer, not the enforcement layer. Linters are a CI concern. |
| Taste-to-code feedback pipeline (review → docs → lints) | Could be modeled as Lattice task relationships: `review-finding → doc-update → lint-rule` | **Opportunity.** Lattice's relationship graph could track the lineage of how human taste becomes mechanical enforcement. |
| Remediation instructions in linter errors | Not Lattice's scope | N/A |

### 5. Risk Tiers and Policy Gates (Carson)

| Their Pattern | Lattice Equivalent | Gap |
|---|---|---|
| Risk tiers by file path | Not in Lattice today | **Not Lattice's scope** — this is CI infrastructure. But Lattice tasks could carry risk-tier metadata as custom fields. |
| Single JSON merge policy contract | Lattice `config.json` serves a similar role for task workflow | **Analogous philosophy.** Both are single-source-of-truth config files, but for different domains. |
| Preflight gates before expensive CI | N/A | CI concern, not coordination. |

### 6. SHA Discipline and Evidence

| Their Pattern | Lattice Equivalent | Gap |
|---|---|---|
| Current-head SHA discipline | Lattice events are timestamped and ordered | **Different domain, same principle.** Both are about ensuring you're looking at current state, not stale state. |
| Browser evidence as first-class proof | Lattice artifact system (`artifacts/meta/`, `artifacts/payload/`) | **Lattice has the infrastructure.** Artifacts exist but aren't used for CI evidence yet. This is a natural extension. |
| Evidence manifests | Could be artifact metadata with verification assertions | **Opportunity.** The artifact system is underutilized. |

### 7. Entropy Management

| Their Pattern | Lattice Equivalent | Gap |
|---|---|---|
| "Golden principles" encoded in repo | Lattice `config.json` + CLAUDE.md conventions | **Partial match.** Lattice encodes coordination principles; they encode code quality principles. Same pattern, different domain. |
| Recurring "garbage collection" agent runs | Could be Lattice recurring tasks with SLA tracking | **Opportunity.** Lattice doesn't have recurring/scheduled tasks today, but the task primitive could support this. |
| Doc-gardening agent | Same — a Lattice task type for documentation maintenance | **Natural fit.** Every gardening pass could be a Lattice task, making the history queryable. |
| Quality grades tracked over time | Dashboard extension opportunity | **Gap.** Lattice tracks task state over time via events, but not quality metrics. |

### 8. Agent Autonomy Loop

| Their Pattern | Lattice Equivalent | Gap |
|---|---|---|
| Full end-to-end agent loop (reproduce → fix → validate → PR → merge) | Lattice `sweep` command processes backlog tasks autonomously | **Aligned.** `lattice sweep` is Lattice's version of this — agents pick up tasks, work them, move them through status. |
| "Ralph Wiggum Loop" (iterate until reviewers satisfied) | Global CLAUDE.md defines `ralph` as autonomous iteration loop | **Identical term and concept.** Our `ralph` command and their reference are the same pattern — iterate autonomously until complete. |
| Escalate to human only when judgment required | Lattice `needs_human` status | **Direct mapping.** This is exactly what `needs_human` was designed for. |

### 9. Actor Attribution

| Their Pattern | Lattice Equivalent | Gap |
|---|---|---|
| Not implemented — unclear who approved what | Every Lattice operation requires `--actor` | **Lattice is ahead.** This is one of Lattice's strongest differentiators. OpenAI's system has no attribution model — if a PR was opened by Codex and reviewed by another Codex instance, there's no structured record of the decision chain. |
| Provenance of changes | Lattice provenance fields: `triggered_by`, `on_behalf_of`, `reason` | **Lattice provides this.** OpenAI would benefit from knowing *why* an agent made a decision, not just *that* it did. |

---

## Summary: Where Each System Excels

### Lattice Strengths (Things OpenAI Would Benefit From)

1. **Event sourcing** — immutable, append-only history. Their markdown files can be edited and history is lost.
2. **Actor attribution** — every operation is attributed. They have no equivalent.
3. **Status machine** — formal state transitions with validation. They use folder conventions (`active/` vs `completed/`).
4. **Relationship graph** — tasks can block, relate to, and depend on each other. They track this in prose.
5. **Provenance** — `triggered_by`, `on_behalf_of`, `reason` on every event. Deep attribution chain.
6. **`needs_human`** — explicit escalation signal. They mention escalation but have no structured mechanism.
7. **Artifact system** — metadata + payload storage for evidence. Underutilized but architecturally sound.

### OpenAI/Carson Strengths (Things Lattice Could Learn From)

1. **Quality scoring** — tracking quality grades per domain over time. Lattice tracks task state, not quality metrics.
2. **Mechanical enforcement** — linters with remediation instructions. Lattice coordinates; it doesn't enforce.
3. **Evidence requirements** — completion means proof exists. Lattice's `done` status doesn't require evidence.
4. **Recurring agent patterns** — scheduled cleanup/gardening. Lattice tasks are one-shot, not recurring.
5. **Risk tiering** — different requirements for different code paths. Lattice tasks don't carry risk metadata.
6. **The taste-to-code pipeline** — systematic progression from review comment → documentation → lint rule. Lattice could track this lineage via relationships.
7. **Application inspectability tooling** — worktree isolation, CDP integration, ephemeral observability. This is infrastructure, not coordination, but Lattice tasks could track which agent ran against which worktree.

---

## The Strategic Takeaway

OpenAI spent five months building ad-hoc coordination primitives (markdown folders, naming conventions, manual tracking) because no structured alternative existed. Carson built ad-hoc CI coordination (SHA dedup, comment writers, rerun workflows) for the same reason.

**Lattice IS the structured alternative.** The opportunity is positioning Lattice as the coordination layer that sits underneath both patterns:

- **For teams like OpenAI's:** Replace `docs/exec-plans/` with Lattice tasks. Replace manual progress tracking with event-sourced status. Replace folder conventions with queryable state machines. Gain attribution, provenance, and relationship graphs for free.
- **For teams like Carson's:** Lattice tasks upstream of the CI pipeline provide the "what is being worked on" context that his policy gates and review agents need. Evidence artifacts on Lattice tasks create the proof chain his SHA discipline is trying to establish.

The early adopter persona is clear: **teams already running agent-first development loops who built their own coordination scaffolding and are hitting its limits.** Lattice replaces the scaffolding with something principled.

---

*Captured 2026-02-16 for Stage 11 Agentics research. See LAT-68.*
