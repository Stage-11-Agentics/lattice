# Run State: Lattice Hosted

The resume anchor for the Lattice Hosted Tone arc. Every stage reads this first.

## Current position

- **Stage:** tone-architect
- **Phase:** 4 to 5, operator read of the contract; final review round in parallel
- **Tracking task:** LAT-283 (architect stage)
- **Working branch:** `docs/lattice-hosted-contract`
- **Next stage:** lattice-orchestrator-v2, consuming the contract in `docs/hosted/`

## Commission

Commissioned 2026-09-25 by the operator (Atin). Lattice gets a hosted mode: one server per host, one writer per project board, many project boards per server. Clients write over HTTP and read a local cache fed by a change stream; thin clients poll. It is a Lattice improvement for every Lattice user, not a feature of any one deployment.

## Rulings carried in (operator, 2026-09-25)

1. Run as a Tone process: tone-architect, then lattice-orchestrator-v2.
2. The server is independent of c11. It never runs inside a c11 surface and never calls the c11 socket.
3. Many project boards per server from day one, each easy to reach.
4. Ships inside the Lattice repo, public, as part of Lattice. Nothing deployment-specific (hostnames, tokens, proxy secrets) lands in the repo. Per-machine config only.
5. Migration order of existing boards is deferred. The contract does not plan which board moves first.
6. Single-writer server adopted. Rejected: git plus backlog fixes, event-file sync (Syncthing), forge as bus.

## Proportion (recorded shrinks)

- **tone-initiation and tone-prototype were not run.** This is infrastructure with no visual design to discover. Their inputs are replaced by: the operator's rulings above, `docs/design-lattice-remote.md` (the Feb 2026 base design), and the operator's multi-box architecture note (private). The architect stage mints the stories and AC IDs itself, in `docs/hosted/sequence/USER_STORIES.md`, so AC lineage still starts at a story.
- **No ECONOMICS.md.** Lattice is open source; the server has no P&L.
- **No DESIGN.md.** The only user-facing surfaces are the existing CLI and dashboard, reused unchanged.

## Placement

The contract lives in `docs/hosted/` rather than at the repo root, because the root already carries Lattice's own specification (`ProjectRequirements_v1.md`) and a root `SPEC.md` would read as the spec for all of Lattice. `CLAUDE.md` points at `docs/hosted/`. Deployment specifics for the first host live in the operator's private platform notes, never here.

## Decisions log

- 2026-09-25 (architect): working branch cut from `origin/main` at 5a9917a, because local `main` is checked out in a sibling worktree.
- 2026-09-25 (architect): contract placed in `docs/hosted/` (see Placement).
- 2026-09-25 (architect): one client mode, not two. Thin clients use the same cache-plus-catch-up client as everyone else, with a cache that dies with their environment and no follower; the documented HTTP API also serves curl-only agents.
- 2026-09-25 (architect): the change stream is a doorbell plus the appended events; cache contents always travel through `sync`, so followers and pollers share one data path.
- 2026-09-25 (architect): hosted events carry the server's timestamps and IDs; clients supply only an operation ID.
- 2026-09-25 (architect): multi-event operations append in one write, and the server's startup recovery removes an unacknowledged operation's partial tail, so a crash leaves a write wholly present or wholly absent.
- 2026-09-25 (architect): client-local effects (hooks, c11 bridge, auto-review) run on the issuing client after a successful write; the server runs none of them.
- 2026-09-25 (architect, from review round 1): every operation has one commit point, with staging for anything written before it; recovery is journal-baselined so imported history is never touched; replays are `replayed: true` (distinct from `idempotent`); one `op_id` per operation call; hosted board hooks run on clients only by explicit opt-in (`run_board_hooks`); cache directories are 0555 plus a tamper fingerprint; `resource acquire --wait` loops on the client.
- 2026-09-25 (architect): audit defects outside this build filed as LAT-284 to LAT-290.
- 2026-09-25 (architect, from review round 4): any in-process failure runs transaction recovery (the startup recovery logic, scoped to that operation) before the project admits another request; epoch rotation is recoverable through `hosted/rotation.json`; all syncs serialize on one lock; `lattice plan <task>` keeps working beside the new `plan write`.
- 2026-09-25 (architect, from review round 2): per-family commit points and staging replaced by one mechanism: every server operation is a transaction (undo log with pre-images, a receipt holding the full result, the journal line as the single commit point, rollback on any failure or crash). Local mode keeps today's crash behavior exactly; AC-4 is a server guarantee. Live epoch rotation goes through a control-request file the owning server executes.
- 2026-09-25 (architect): the build splits into 24 tickets (H-0 to H-23) on the `v2` branch; the deployment track (H-18, H-23) lives in the private platform addendum `platform/lattice-hosted.md`.
- 2026-09-25 (architect, from the triple plan review): fresh-context Codex, Opus 5.5, and Fable 5.1 reviews, collated by Opus 5.5, produced 63 items (13 questions for the operator, 36 fixes, 5 risks, 9 declined); the 36 fixes (section B) are applied, including the move per the operator's ruling (a doctor-gated import plus guide steps; `attach --migrate` and `import --replace` removed), new criterion AC-49 (per-project review workflows), and new tickets H-22a (transactions proven before sync and client), H-10a to H-10c (replacing H-10), and H-13a and H-13b (replacing H-13), for 28 tickets; the 13 questions (section A) await the operator.
- 2026-09-26 (architect, from the Orchestrator during H-2): G-6's declared changes gain uniform actor validation (`--on-behalf-of` included) and §3.1's storage-error mapping applied in every command; H-17's "Upgrading to v2" note lists both. The end-to-end `task.event` data-cap case (AC-15) and hostile attach-payload case (G-1) move from H-9 to H-12, which depends on the operations they exercise; H-9 unit-tests the checks and writes no tests that skip until another ticket lands. Multi-ID archive touching a named session once per task needs no amendment.
- 2026-09-26 (architect, from the Orchestrator during H-10b): envelope-scale torture tests (supported size, sync envelope, load) carry a new `envelope` marker and leave the per-PR torture lane, which H-10b's roughly five minutes would otherwise add to nearly every landing. They run at their owning ticket's gate, in the background after each landing that touches the server-side paths (never holding the landing slot), and in the complete torture suite before CP3, CP4, and the release gate. Nothing is dropped.
- 2026-09-26 (architect, from the Orchestrator during H-10a): the stream is written directly on Starlette and `sse-starlette` leaves the `server` extra; the operator's stack ruling was Starlette and uvicorn, and §8.9 already pinned the stream at the wire level. §8.9 now also requires what the library gave for free: SSE framing and headers (including `X-Accel-Buffering: no`), a disconnect releasing its slot within one heartbeat, and streams ended on SIGTERM so followers never hold up shutdown (AC-15 and AC-31 test them).
- 2026-09-26 (architect, from the Orchestrator during H-14): `project import` loses `--code`, which no section defined. Import never changes configuration (AC-35's byte-for-byte promise), and `set-project-code` is already an ordinary operation any token may run on a bound checkout (§3.9), attributed like any other change. The symlink and special-file refusal uses `VALIDATION_ERROR` with `details.path`, not a new error code.
- 2026-09-27 (operator, through the Merge Captain; amended by the architect): the default-suite budget is 40 s, kept by pruning excessive tests rather than growing it, with no gating on Atlas; envelope tests never block a landing or an admission and run only in the background, where a failure files a ticket against the landing that introduced it (this replaces the 2026-09-26 rule that a failure paused admissions); green CI at the exact head stands in for the captain's local ruff and default-suite run; an operation module that receives moved CLI logic joins BUILDPLAN §4's shared-file list.
- 2026-09-27 (architect, from Sol's post-merge review of H-11 via the Orchestrator): op-status gains `in_flight`. Check-before-retry answered `not_found` while an operation was still pending, so a rerun under a new `op_id` could apply the write twice. The pending set covers a request from when it passes the per-token limits, not only once admitted, because a request queued for the locks can still commit after its client gives up. A pair leaves the set only after its commit is recorded, so no lookup falls between the two. Implemented as an H-11 follow-up on LAT-310.
- 2026-09-27 (architect, from Sol's review of H-13a via the Orchestrator): the dashboard status POST keeps its existing `force` + `reason` API parameters, passed to `task.status` exactly as the CLI's `--force --reason`. "No force control" in §10 means no UI control. Removing them would break local behavior for API callers (today's refusal message tells them to send `force`). Keeping them grants nothing beyond what the same caller can do from the CLI, since the reason is required and the event records `force` and the actor, and v2's content-type and `Origin` checks close the cross-site route.

## Interview record

**Round 1 answers (operator, 2026-09-25):**

- Success is three scenarios, all at once: (W) one person, several worktrees on one machine, every worktree sees every status change and the board never shows up in a merge; (B) three agents local and three on a remote box on one board, with the local view updating live; (T) five people, two tickets each, each in their own worktrees, everyone sees the full current state.
- Validation: the operator will stage worktree and remote-box scenarios directly.
- Amendment 7 (LAT-275/276/277): defer to the architect's recommendation (out of this build).
- One server hosts many boards, because the operator works many projects at once.
- The existing local Lattice flow must not change. Users outside the commissioning team who hit its limits should find moving to a server easy.
- Round 2 (architecture calls) deferred until round 1 is settled.

**Round 1 follow-ups (operator, 2026-09-25):**

- Network topology is the adopter's choice. Ship a setup guide that tells humans and agents exactly what to do for their own setup, rather than baking one topology in.
- Hosted is a secondary, opt-in option. The existing local flow works well and must not be disrupted or displaced for existing users.
- Local mode behaves identically, including boards tracked in git, even though its internals are refactored.
- Do not push this out to users until it has been tested heavily: it is a major change to publicly used infrastructure.

**Release shape (operator, 2026-09-25):**

- This is a v2 of Lattice, built on its own release branch (`v2`). Ticket PRs merge into `v2`, not `main`. `main` stays releasable.
- Release gate before `v2` merges to `main`: full suite, write-path parity test, concurrency and crash tests, scenarios W, B and T driven by the operator, then a trial of about a week across two or three projects. The operator makes the call when it is working well.
- `main`, then `prod` and PyPI, each only with the operator's named approval.
- Round 1 settled.

**Round 2 answers (operator, 2026-09-25):**

1. Writes are validated by named operations, run by whichever process owns the board. Accepted.
2. Server stack: Starlette + uvicorn as an optional `lattice-tracker[server]` extra. The CLI client stays standard library. Accepted.
3. Identity: a token names an identity and the actors it may act as, default strict; the server stamps the token identity on every event. Accepted, and extended: the machine, the worktree, and the user running the work are first-class on every event.
4. Plans and notes are written through a command in both modes; a hosted cache is read-only. Accepted.
- Identity model (operator, 2026-09-25): a token is issued to one person for one machine or seat, so the user and machine are authenticated; the client reports worktree path, branch, OS account and Lattice version on every write, stamped as reported. Local mode records the same fields as optional additive fields (the parity test compares everything except them). v2 shows them everywhere; filtering by them is a follow-up ticket built in near parallel.
- Assumptions accepted: dashboard login by token paste then cookie, per-project dashboard URL; admin by shell on the server host only; archive relocation allowed, `doctor --fix` never trims a hosted log; the Lattice repo's own default test suite gets a parallelization ticket and server tests stay fast.

**Contract read (operator, 2026-09-25):**

- The four review-time calls are confirmed: hosted board hooks run on a client only by opt-in; every token may act as `agent:lattice-auto-review`; `claim` keeps today's non-exclusive meaning; the move of a local board is refused when it changed after import (superseded by the next point).
- Review workflows must stay configurable per project in hosted mode: automatic plan reviews, automatic code reviews, either, or no reviews at all.
- Moving an existing v1 board onto a server is not worth heavy tooling: an agent can do it by following the guide. Keep a doctor-gated import; drop the automated migrate-and-verify machinery.
- For the fresh-context plan reviews: apply fixes the architect is confident in; bring only genuine questions to the operator.

**Collated review, section A (operator, 2026-09-25):** A3 full origin everywhere (the identity ruling stands); A4 no v1 guard release, trial clients pinned to v2; A5 accept the rule convergence for the local dashboard and MCP (the dashboard error names the CLI escape); A6 board config admin-only, `lattice context write` for any token; A9 every dashboard setting stays shared; A10 keep the hosted dashboard (H-13b); A11 keep attestations, labeled as client claims; A12 the guide leads with an agent-followable recipe to untrack the board for Scenario W without a server, proven by the docs agent test; A13 version 2.0.0. Then (operator, 2026-09-26, accepting the architect's recommendations): A1 orchestrator state is synced board data (`orchestration/` becomes the workspace class; loose files under `plans/` and `notes/` are written through `lattice board write`); A2 auto-reviews run by default from the board config on the machine that made the transition, a machine may decline with `run_auto_reviews: false`, and server-run reviews are a possible later expansion; A7 a person token's default actors are the user plus `agent:*` (this replaces the earlier strict default; seats are minted with one explicit actor); A8 `lattice unerase` makes every erase reversible.

**Final executability check (Codex, 2026-09-26):** REVISE 7/10 on the fully revised contract, 2 blocking (reloading an unloaded project; config authorization versus ordinary config-writing operations) and 6 important; all applied by the architect.

**Philosophy (operator, 2026-09-26):** `Philosophy.md` gets a short amendment in its own voice explaining why v2 adds an opt-in server despite the "patience" section; owned by H-17.

## Touchpoints

| Touchpoint | State |
|---|---|
| Intake interview with the operator | complete 2026-09-25 (rounds 1 and 2 settled) |
| Spec and plan ready for read | opened for the operator 2026-09-25, with a one-page read guide |

## Stats

| Phase | Agents spawned | Human touchpoints | Wall-clock |
|---|---|---|---|
| 0 intake | 2 (brownfield audit, hosting context) | 2 interview rounds plus follow-ups | about 1.5 h |
| 1-3 contract | 0 | 0 | about 1 h |
| 4 review, round 1 | 2 (Codex executability judge: REVISE 5/10, 6 blocking; Opus adversarial codebase review: 6 blocking, 14 important) | 0 | about 30 min; all findings applied |
| 4 review, round 2 | 1 (Codex re-judge: REVISE 6/10; most round-1 findings resolved, 4 new blocking, all in crash atomicity and receipts) | 0 | about 30 min; applied |
| 4 review, round 3 | 1 (Codex re-judge after the transaction redesign: REVISE 7/10; 2 blocking, both inside the transaction protocol) | 0 | applied |
| 4 review, round 4 | 1 (Codex re-judge: REVISE 7/10; 2 blocking, both about in-process failure handling inside the transaction protocol) | 0 | applied: one recovery path for every in-process failure |
| 4 review, round 5 | 1 (Codex re-judge: REVISE 8/10; 1 blocking, offline rotation versus undo classification) | 0 | applied |
| 4 review, round 6 | 1 (Codex confirmation: PASS 8/10, no blocking; 1 important and 2 minor applied) | 0 | done |
| 4 review, triple plan review | 4 (fresh-context Codex, Opus 5.5, Fable 5.1 reviewers; an Opus 5.5 collator), requested by the operator | pending (section A) | done: 63 items (A 13, B 36, C 5, D 9) |
| 4 review, triple review applied | 1 (Opus 5.5 architect applying section B) | 0 | done: all 36 section B fixes applied; section A open |
