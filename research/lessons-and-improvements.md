# Lessons & Improvements: What Harness Engineering and Code Factory Teach Us

**Date:** 2026-02-16
**Context:** Deep reflection after studying OpenAI's Harness Engineering (Lopopolo) and Ryan Carson's Code Factory pattern, compared against Lattice's current architecture, philosophy, and roadmap.
**LAT-68**

---

## The Signal

Two independent teams — one at OpenAI, one at a startup — both arrived at the same conclusion: agent-first development needs structured, in-repo coordination. Both built their own. Both hit the limits of ad-hoc markdown and folder conventions. Neither had access to something like Lattice.

This is the strongest product-market fit signal Lattice has seen. Not because people are asking for it — because they're building it themselves without knowing it exists.

---

## 15 Lessons and Proposed Changes

### 1. Sharpen the Identity: "Coordination Primitive" Not "Task Tracker"

**What we learned:** OpenAI's system isn't just a task tracker. It's a coordination layer for tasks, knowledge, quality, architecture, and entropy management. They needed `exec-plans/`, `design-docs/`, `QUALITY_SCORE.md`, `tech-debt-tracker.md`, and `references/` — all in one coherent system.

**What to change:** Lattice already calls itself "coordination primitives" in the philosophy. But the implementation and messaging lean heavily on "task tracking." The elevator pitch should be: *Lattice is the coordination layer that agent-first development teams are building themselves — we just built it right.*

Stop saying "task tracker." Start saying "coordination infrastructure for agent-first teams." The task is the core primitive, but it's the substrate for coordination, not the end product.

**Concrete action:** Update README, pyproject description, MCP listing, distribution messaging. The word "tracker" appears in the package name (`lattice-tracker`) and that's fine for PyPI — but all other copy should lead with coordination.

---

### 2. Evidence-Gated Completion (The Biggest Single Feature Gap)

**What we learned:** Both Carson and OpenAI treat "done" as "proof exists" — not "someone said it's done." Carson requires browser evidence manifests. OpenAI requires video demonstrations of bug fixes. In both systems, completion without evidence is a process failure.

Lattice's `done` status means: an actor said it's done. Nothing prevents `lattice status LAT-42 done` from running on a task with zero artifacts, zero comments, zero evidence. This makes Lattice a trust-based system in an era where the whole point is trust-but-verify.

**What to change:** Add optional **completion criteria** to task types or priority levels, configurable in `config.json`:

```json
{
  "completion_policy": {
    "high_priority": {
      "require_artifacts": true,
      "require_comment": true
    },
    "default": {
      "require_artifacts": false,
      "require_comment": false
    }
  }
}
```

When `require_artifacts` is true, `lattice status <task> done` fails unless at least one artifact is attached. `--force --reason` overrides, as always.

This turns Lattice from "I said it's done" to "here's the proof it's done" — and it's exactly what would make Lattice valuable in a Code Factory-style CI pipeline. The merge gate checks for Lattice task evidence rather than building its own evidence-tracking scaffolding.

**Priority:** High. This is the single feature that most credibly bridges Lattice from "tracking" to "verification."

---

### 3. Promote Artifacts from Afterthought to First-Class Citizen

**What we learned:** OpenAI requires video evidence of bug fixes. Carson requires browser evidence manifests. Both treat proof artifacts as central to the workflow, not optional metadata.

Lattice has a complete artifact system — `artifacts/meta/`, `artifacts/payload/`, metadata schemas, linkage to tasks. But it's underutilized because the CLI doesn't make attachment easy and `lattice show` doesn't prominently display artifacts.

**What to change:**
- Add `lattice attach <task> <file>` as a convenience command (shortcut for creating an artifact and linking it)
- Make `lattice show` display attached artifacts with type, filename, and size
- Add artifact types: `evidence`, `screenshot`, `test-results`, `review-summary`, `plan`
- Consider: `lattice evidence <task> <file>` as even more semantically clear

When combined with evidence-gated completion (#2), this creates the proof chain: work → evidence → completion. This is what Carson bolted together with SHA-matching and comment-dedup hacks.

---

### 4. Config as Project Contract (Broader Than Workflow)

**What we learned:** Carson's single JSON contract defines risk tiers, required checks, evidence requirements, and merge policy. OpenAI's `core-beliefs.md` defines agent-first operating principles. Both are "project contracts" — machine-readable declarations of how work should flow.

Lattice's `config.json` currently covers workflow state: statuses, transitions, WIP limits, project code. It's the right shape but too narrow in scope.

**What to change:** Extend `config.json` to carry broader coordination policy:

```json
{
  "project_code": "LAT",
  "workflow": { "..." },
  "policy": {
    "completion_policy": { "..." },
    "task_templates": {
      "bug": { "default_priority": "high", "require_evidence": true },
      "chore": { "default_priority": "low" }
    },
    "conventions": {
      "branch_pattern": "{type}/{short_id}-{slug}",
      "commit_prefix": true
    }
  }
}
```

This keeps Lattice's "one source of truth" philosophy but extends it beyond workflow into project-level coordination policy. Agents read `config.json` once and know not just what statuses exist, but what's expected for each type of work.

**Scope warning:** This is extensible territory. Keep it optional and additive. The core workflow config is load-bearing; the policy section is opt-in.

---

### 5. The CLAUDE.md Block Needs a Progressive Disclosure Audit

**What we learned:** OpenAI tried a monolithic AGENTS.md and it failed — context is scarce, too much guidance becomes non-guidance, it rots instantly. Their fix: ~100 lines that serve as a map with pointers to deeper sources.

Lattice's CLAUDE.md block (from `claude_md_block.py`) serves exactly this role. But has it been audited for information density? Is it the right length? Does it teach agents where to look without overwhelming them?

**What to change:**
- Measure the current block length in tokens, not just lines
- Test: does an agent reading only the CLAUDE.md block know enough to start using Lattice? If not, what's missing?
- Test: does the block include information agents never use? If so, cut it.
- Ensure the block follows progressive disclosure: entry point → quick reference → pointer to `lattice show` and `lattice list` for depth
- Consider: should the block teach `lattice next` as the first command (not `lattice list`)? `next` is more opinionated and maps to how agents actually work.

**Priority:** Medium-high. This is the single most-read piece of Lattice content — every agent session starts here. Optimizing it has multiplicative impact.

---

### 6. Recurring Work Patterns (Templates + Cron, Not Recurring Tasks)

**What we learned:** OpenAI runs recurring agents for garbage collection, doc-gardening, and quality scoring. Carson's harness-gap loop converts incidents to test cases on a schedule. Both need recurring work patterns.

Lattice tasks are one-shot by design — and that should stay. Recurring tasks add complexity to the event model and status machine.

**What to change instead:** Task templates + external scheduling.

- `lattice create --template doc-garden --actor agent:gardener` creates a new task from a template with pre-filled fields
- Templates live in `.lattice/templates/` (YAML or JSON)
- A cron job or CI workflow calls `lattice create` periodically
- `lattice sweep` processes the created tasks

This preserves one-shot tasks while enabling recurring patterns through composition. Each run is its own task with its own events, making the history queryable: "show me all doc-gardening runs this month."

**Alternative:** Even simpler — just document the pattern. A bash script that runs `lattice create "Doc-gardening sweep $(date)" --actor agent:gardener` on a cron is 90% of what's needed. Don't build what can be composed.

---

### 7. Quality Metrics as a Dashboard Read-Path

**What we learned:** OpenAI maintains `QUALITY_SCORE.md` grading each product domain and architectural layer. They track gaps over time. This is manual — a human or agent updates the markdown periodically.

Lattice already captures rich data: task throughput, completion rates, time-in-status, blocked-task counts, agent activity. The gap is aggregation and presentation.

**What to change:** Add a `/stats` or `/quality` dashboard tab that computes:
- Tasks completed per week (velocity)
- Average time-in-status (bottleneck detection)
- Open tasks by priority (risk surface)
- Agent contribution breakdown (who's doing what)
- Blocked task count over time (coordination health)

This is purely a read-path feature — no schema changes needed. The event log already contains everything. It just needs a view.

**FutureFeatures.md already has "Analytics / Metrics Aggregation" listed.** This research provides concrete design inspiration from OpenAI's manual quality scoring.

---

### 8. Agent Legibility Audit of Lattice Itself

**What we learned:** OpenAI optimizes their entire codebase for agent legibility. The question they ask: "Can the agent reason about the full domain from the repository alone?"

We should ask the same question about Lattice itself. When an agent runs `lattice list`, is the output optimized for agent consumption? When an error occurs, does the message teach the agent how to fix it?

**What to change:**
- Audit `lattice list` output: is the default format easy for agents to parse and reason about? Or do they need `--json` every time?
- Audit error messages: do they include remediation instructions? (OpenAI puts remediation in linter errors — we should do the same for CLI errors)
- Audit `lattice show`: does it give enough context for an agent to pick up a task and start working?
- Audit `lattice next`: when it recommends a task, does the recommendation include enough context to start, or does the agent need to also run `lattice show`?
- Consider: should `lattice list` default to a more concise, agent-parseable format with `--verbose` for humans? Or vice versa?

**Example of good remediation in an error:**
```
Error: Cannot transition LAT-42 from backlog to in_progress.
Valid transitions from backlog: in_planning
Hint: Use 'lattice status LAT-42 in_planning' first, or use --force --reason "..." to override.
```

---

### 9. Distribution Messaging: Target the Scaffolding Builders

**What we learned:** The early adopter is not "teams that want project management." It's **teams already running agent loops who built their own coordination scaffolding and are hitting its limits.**

OpenAI's team spent months building `docs/exec-plans/`, quality scores, and progress tracking in markdown. Carson bolted together five GitHub Actions workflows. These people are our users — they've already solved the problem badly and would recognize Lattice as the right solution.

**What to change:**
- The README and distribution messaging should lead with: "If you're tracking agent work in markdown files, folder conventions, or ad-hoc GitHub Actions — Lattice replaces all of that."
- Case studies framed as: "Here's what OpenAI built by hand. Here's what Lattice gives you out of the box."
- The distribution strategy currently frames adoption as a funnel (discover → install → init → connect → use). Add a persona at the top: "Who is this for? Teams running agent-first development who've outgrown ad-hoc coordination."

---

### 10. The Taste-to-Code Pipeline as a Relationship Pattern

**What we learned:** OpenAI has an explicit progression: human review comment → documentation update → lint rule. Each step makes the enforcement more mechanical and less dependent on human attention. This is how human judgment compounds in an agent-first system.

**What to change:** This doesn't need a feature — it needs a documented pattern. Show how Lattice relationships can track this lineage:

```
LAT-50 (type: review-finding) "Prefer shared utils over hand-rolled helpers"
  → triggers LAT-51 (type: doc-update) "Add util preference to ARCHITECTURE.md"
  → triggers LAT-52 (type: lint-rule) "Add custom lint: no hand-rolled helpers"
```

Each task tracks one step of the pipeline. The `triggered_by` provenance field records the chain. An agent or human can query: "Where did this lint rule come from?" and trace back to the original review comment.

**Concrete action:** Write this up as a pattern/recipe in docs or Philosophy, not as a feature.

---

### 11. Doc-Gardening as a Positioned Use Case

**What we learned:** OpenAI has a dedicated "doc-gardening agent" — a recurring process that scans for stale docs and opens fix-up PRs. This is one of their most practical patterns.

**What to change:** Position this as a first-class Lattice use case in documentation:

```bash
# Create a gardening task
lattice create "Doc-gardening sweep 2026-02-16" \
  --actor agent:doc-gardener \
  --type chore

# Agent scans, finds stale docs, records findings
lattice comment LAT-70 "Found 3 stale docs: ARCHITECTURE.md, DESIGN.md, QUALITY_SCORE.md" \
  --actor agent:doc-gardener

# Agent opens fix-up PRs and records evidence
lattice attach LAT-70 pr-links.json --type evidence \
  --actor agent:doc-gardener

# Mark complete
lattice status LAT-70 done --actor agent:doc-gardener
```

Over time, the history of gardening sweeps is queryable. Coverage improves visibly. This is a great "show, don't tell" demo for Lattice's value.

---

### 12. Cross-Reference the Ralph Wiggum Loop

**What we learned:** OpenAI's post explicitly references a "Ralph Wiggum Loop" for autonomous iteration until reviewers are satisfied. Our global CLAUDE.md independently uses the same term for the same concept. The same pattern emerged independently — strong evidence it's real and durable.

**What to change:** Note this convergence in the Philosophy or docs. When two teams independently discover the same coordination pattern and name it the same thing, that's a signal worth recording. It validates the pattern and gives Lattice credibility when referencing it.

---

### 13. Bridge to the CI Layer (Without Becoming a CI Tool)

**What we learned:** Carson's entire Code Factory pattern is CI infrastructure — policy gates, SHA matching, review reruns, evidence verification. Lattice isn't CI and shouldn't become CI. But the two layers should talk.

**What to change:** Define clear integration points:

- **Lattice → CI:** A CI job can read `.lattice/tasks/<id>.json` to check: does this task have evidence? Is it in `done` status? This is the "policy gate" in Carson's pattern — but powered by Lattice state instead of ad-hoc checks.
- **CI → Lattice:** A CI job on PR merge can `lattice status <task> done --actor agent:ci` with evidence artifacts attached. The merge itself becomes a Lattice event.
- **Branch linking:** FutureFeatures.md already has `branch_linked` events. When a PR is opened for a Lattice task's branch, CI can verify the task exists and is in the right status.

This doesn't require building CI features into Lattice. It requires documenting and enabling the integration pattern.

---

### 14. Rethink the Notes-vs-Plans-vs-Knowledge Taxonomy

**What we learned:** OpenAI maintains separate directories for `design-docs/`, `exec-plans/`, `product-specs/`, `references/`, and generated docs. Each has a different lifecycle and audience. Lattice currently has:
- `.lattice/notes/<task_id>.md` — task-scoped documents
- `notes/` — repo-level working docs
- `docs/` — user-facing documentation
- `prompts/` — prompt templates
- `research/` — external research (just formalized today)

**What to change:** The repo-level `notes/` folder is doing too many things — code reviews, retrospectives, cube vision docs, and working documents all live there. Consider:

- **`notes/`** → working documents and internal records (reviews, retros, implementation notes)
- **`docs/`** → external-facing documentation (guides, integration docs) *(already correct)*
- **`prompts/`** → reusable prompt templates *(already correct)*
- **`research/`** → external research and competitive analysis *(just formalized)*

The key insight from OpenAI is that **different document types have different lifecycles**. Design docs need verification status. Plans need active/completed states. References need freshness checks. Don't conflate them.

For Lattice specifically: consider whether `.lattice/notes/<task_id>.md` should support a `type` or `status` field in a frontmatter header. This would let agents distinguish between a plan that's still active and one that's been superseded.

---

### 15. The Philosophy Is Validated — Now Make It Findable

**What we learned:** Every single core principle in `Philosophy_v3.md` is validated by the OpenAI experience:
- **Files as substrate** — they use plain files in a `docs/` directory
- **Events** — they wish they had immutability (editable markdown rots)
- **Attribution** — they have no equivalent and need it
- **Self-similarity** — their per-domain quality scoring is a fractal of the whole-system approach
- **Patience** — they explicitly warn against over-building

But the philosophy doc is 103 lines in a file nobody reads unless they go looking for it. The people who need to see it — the agent-first team leads — won't find it.

**What to change:**
- Excerpt the strongest philosophical points into the README (2-3 sentences max)
- Link the philosophy doc from the distribution channels
- Consider: a "Why Lattice?" page or section that specifically addresses the OpenAI/Carson pattern with "here's what they built, here's what we provide"
- The philosophy's closing line about "carbon. silicon. the emergent space between." is memorable. Use it.

---

## Prioritized Summary

| # | Change | Type | Priority | Effort |
|---|--------|------|----------|--------|
| 1 | Sharpen identity: "coordination infrastructure" not "task tracker" | Messaging | High | Low |
| 2 | Evidence-gated completion | Feature | High | Medium |
| 3 | Promote artifacts to first-class | Feature | High | Low-Medium |
| 5 | CLAUDE.md block progressive disclosure audit | Optimization | High | Low |
| 8 | Agent legibility audit of Lattice CLI | Optimization | High | Medium |
| 9 | Distribution messaging for scaffolding builders | Messaging | High | Low |
| 4 | Config as broader project contract | Feature | Medium | Medium |
| 7 | Quality metrics dashboard | Feature | Medium | Medium |
| 10 | Taste-to-code pipeline documentation | Documentation | Medium | Low |
| 11 | Doc-gardening use case positioning | Documentation | Medium | Low |
| 13 | CI integration patterns | Documentation | Medium | Low |
| 14 | Notes/plans/knowledge taxonomy | Refactor | Medium | Low |
| 15 | Make philosophy findable | Messaging | Medium | Low |
| 12 | Cross-reference Ralph Wiggum Loop | Documentation | Low | Low |
| 6 | Recurring work patterns | Feature/Pattern | Low | Low |

---

*This document is a living analysis. As Lattice evolves, revisit these items and update status.*
