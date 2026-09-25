# Lattice Hosted: User Stories

Stories and acceptance criteria for Lattice Hosted, the headline of Lattice v2. AC IDs are stable. They carry unchanged into `EVALUATION.md`, `SPEC.md`, the tickets, and validation. A criterion is retired, never renumbered.

These stories were minted at the architect stage, from the operator's rulings and the intake interview recorded in `run-state.md`. The initiation and prototype stages were not run for this infrastructure project.

## Who this is for

- **Local user.** One machine, many agents, no server. Today's Lattice user, and still the default. Must see no change.
- **Hosted client.** A person or agent on a machine that reaches the server directly (same host or private network), working a hosted board with the full CLI, a local cache, and the dashboard.
- **Thin client.** An agent in a disposable environment that reaches the server only through an authenticating reverse proxy. It has no durable disk, runs no background process, and polls.
- **Follower.** A long-lived reader: a dashboard, a monitoring panel, a watcher. It wants changes as they happen.
- **Server admin.** Installs and runs the server, creates projects, issues and revokes tokens, backs up, upgrades.
- **Adopter.** Someone outside the commissioning team who hits the limits of local Lattice (several machines, several people, heavy worktree use) and moves to a server using only the public docs.

---

## US-0: The three scenarios that define done

*As the operator, I need Lattice to stay coherent in the three situations where local Lattice breaks down today.*

- **AC-40 (Scenario W, worktrees)** One person on one machine has five git worktrees of one repository open, with agents working in each. Every status change made in any worktree is visible from every other worktree within 2 seconds, and no board file ever appears in a diff, a commit, or a merge.
- **AC-41 (Scenario B, boxes)** Three agents on the operator's laptop and three agents in a remote disposable environment work one board at the same time. The laptop's board view updates as the remote agents write, and no write forks the board, reuses a short ID, or is lost.
- **AC-42 (Scenario T, team)** Five people on their own machines, each working two tickets in their own worktrees, share one board. Each of them sees the full, current state of the project, and every change shows who made it, on which machine, from which worktree.

## US-1: One board, one writer

*As a fleet of agents on several machines working one board, I need every write serialized by one process, so the board cannot fork, duplicate an ID, or poison a log.*

- **AC-1** When clients race writes on one task, each declaring the version it read, exactly one succeeds and every loser receives HTTP 409 `CONFLICT` carrying the task's current snapshot. A write without a declared version is judged by the rules against the latest state, and any rejection also carries the snapshot. Concurrent `next --claim` callers never receive the same task. Nothing of a loser's is appended, and the task's event log replays clean afterward.
- **AC-2** Any number of concurrent task creations across any number of clients yields distinct short IDs. No short ID already present anywhere in the project's event history is ever issued again, even when the derived ID index has been regressed or deleted.
- **AC-3** No process other than the server writes a hosted project's board files. A write attempted against a client cache, or by a local CLI against a server-owned data directory, fails with a clear error and changes nothing.
- **AC-4** If the server is killed at any instant during a write and restarted, or a write fails partway, the project passes `lattice doctor`, and the write is either wholly visible or wholly absent.
- **AC-46** A write retried after a lost response (same operation ID, same arguments) is applied exactly once. Reusing an operation ID with different arguments is rejected.
- **AC-47** After a client syncs, every board file in its cache is byte-identical to the server's copy. A local edit found in the cache is moved aside and reported, never silently discarded.

## US-2: The same Lattice, wherever the board lives

*As a hosted client, I run the same commands with the same arguments and get the same output as I would locally. Where the board lives is an infrastructure detail.*

- **AC-5** Every board-writing command in the CLI works against a hosted project with the same arguments, the same exit codes, and the same `--json` envelope as in local mode, apart from errors that exist only in hosted mode (unreachable server, authentication, permission, version mismatch) and the maintenance commands the spec lists as local-only.
- **AC-6** After a write succeeds, the issuing client's next read reflects it, without waiting on the change stream.
- **AC-7** A write by one client is visible to another client's reads within 2 seconds when that client is following the stream, within 5 seconds when its follower has fallen back to polling, and on its next command otherwise.
- **AC-8** With the server unreachable, reads are served from the cache with a one-line staleness notice on stderr. Writes fail with a clear "cannot reach server" error, exit non-zero, and leave no local side effect. There is no offline write queue.
- **AC-9** The client cache has the same directory layout and file formats as a local `.lattice/`, so `cat`, `grep`, the dashboard, and every read command work on it unchanged.
- **AC-10** Every git worktree of one repository reads the same cache and binding. A new worktree needs no setup step and no environment variable to reach the hosted board.
- **AC-48** When client and server run different Lattice versions, an operation or option the server does not know fails with an error naming both versions, and an incompatible protocol version, or a client older than the server's stated minimum, is refused before any write.
- **AC-49** A hosted project's review workflow is configured per project, as a local board's is: automatic plan reviews only, automatic code reviews only, both, or none. Each transition to `planned` or `review` on a hosted board fires exactly the reviews its project configures, and a configuration change reaches every client by its next command.

## US-3: Identity is authenticated

*As a server admin, I know which credential wrote every event, and no client can write as someone it is not.*

- **AC-11** Every request except the health check and the dashboard login (which authenticates by the token it submits) requires a credential (a bearer token, or a dashboard session derived from one). A missing or invalid credential returns 401. A valid credential without access to the requested project returns 403. Neither appends anything.
- **AC-12** Each token is issued to one person for one machine or seat, and lists the actors it may act as (by default, only one). The actor on every event written through the server is one of those. An actor outside the list is rejected with 403. When a token permits exactly one actor, a client may omit the actor entirely.
- **AC-13** Revoking a token takes effect on the very next request made with it, including dashboard sessions derived from it.
- **AC-14** The server stores token hashes, never plaintext tokens, and never writes a token to its logs. A token is shown once, at creation.

## US-4: Many boards, one server

*As a server admin, I host many project boards on one server, each independent and each easy to reach.*

- **AC-15** One server process serves many projects, each with its own short-ID sequence, write lock, change stream, and audit history. A slow or failing project does not delay or break writes on another, and a failed project can be repaired and brought back without stopping the others. The projects still share one host's process, disk, and memory, so per-credential limits bound what any one client can consume.
- **AC-16** Each project has a stable URL path for its API and its dashboard. An index lists the projects a credential may see.
- **AC-17** Creating an empty project is one admin command. Importing an existing local board is one admin command, and it refuses any board that fails `lattice doctor`.
- **AC-18** Binding a checkout to a hosted project is one client command. The committed binding names a server alias and a project, never a hostname or a credential.

## US-5: Thin clients work a ticket end to end

*As an agent in a disposable environment behind an authenticating proxy, I claim, work, and complete a ticket with no background process and nothing that must outlive my session.*

- **AC-19** A thin client runs no follower. Its cache lives only as long as its environment, and each command catches up by polling. `next --claim`, `show`, `list`, `status`, `comment`, plan writes, artifact attachment, and `complete` all work.
- **AC-20** A client can send extra request headers per server, with values taken from environment variables, so a proxy's credentials are never written into a repository or a config file.
- **AC-21** From a clean environment holding only the Lattice package, a clone of the repository, a server URL, a token, and any proxy headers, an agent completes the full ticket loop: claim, plan, status transitions, comment, complete.

## US-6: Followers see changes as they happen

*As a dashboard, panel, or watcher, I receive every change to a project in order, and I recover cleanly from disconnects and server rebuilds.*

- **AC-22** Each project exposes a change stream (Server-Sent Events) whose entries carry a per-project, strictly increasing sequence number and the events each write appended. A reconnect that presents the last sequence it saw resumes with no gap and no duplicate.
- **AC-23** When the server can no longer serve a follower's resume point (its journal was rebuilt), the follower is told so explicitly and performs a full resync.
- **AC-24** The server serves the Lattice dashboard for each project, updating live from the change stream, with every dashboard write going through the same authenticated path and the same rules as the CLI.
- **AC-45** A client that cannot hold a stream (a proxy that buffers or drops it) falls back to polling on its own and keeps working.

## US-7: The history survives

*As a server admin, the board's full history is plain files I can read, back up, and audit.*

- **AC-25** A hosted project's data directory is a standard `.lattice/` directory. Its files can be read with `cat` and backed up with `rsync` or `tar` while the server is stopped (or with a filesystem snapshot), with no export step.
- **AC-26** The server records each project's data directory in a local git history on a short debounce, and can push that history to a configured remote. A failing push never blocks a write.
- **AC-27** No hosted operation deletes board data. Removing a task from view is a tombstone event. The only removals are relocations (archive, session end) and the rollback of an operation that never committed, and `lattice doctor` reports any task file missing without a tombstone.
- **AC-28** `lattice doctor` reports every short ID that does not resolve and every short ID issued twice, not only the first it finds, and checks the ID counter against the event history.

## US-8: Local mode is untouched

*As a local user who never opts in, nothing changes.*

- **AC-29** With no hosted binding, the CLI's behavior and output do not change: every existing test passes, and a recorded corpus of every board-writing command produces the same boards and the same output before and after v2, apart from the new optional origin fields (AC-36).
- **AC-30** The base install adds no runtime dependency. Everything only the server needs is an optional extra.

## US-9: It runs as a service

*As a server admin, I run the server as an ordinary supervised service.*

- **AC-31** The server runs in the foreground under a supervisor, logs structured JSON lines, exposes an unauthenticated health endpoint, and on SIGTERM finishes in-flight writes before exiting.
- **AC-32** Public docs cover install, project and token administration, a launchd template, a systemd template, and reverse-proxy settings (TLS termination, stream buffering, timeouts), using placeholder hostnames only.
- **AC-33** The repository contains no deployment-specific hostnames, tokens, or proxy secrets, and a check in the default test suite enforces it.

## US-10: A board can move to the server

*As an operator, I move an existing local board onto a server without losing anything.*

- **AC-34** Moving a board is one doctor-gated admin import on the server plus a short procedure in the guide that a person or an agent can follow: stop writers, import, move the old board aside (never deleted), and attach the checkout. The checkout's ignore rules keep the cache out of every commit.
- **AC-35** After a move, every task, event, plan, note, artifact, template, and configuration setting from the old board is present on the server, byte for byte apart from the derived files the import rebuilds, and the moved board passes `lattice doctor`. The import names every path it does not move and refuses a board it cannot copy safely.

## US-11: Every change says where it came from

*As anyone reading the board, I can see who made each change, on which machine, from which worktree, and as which user.*

- **AC-36** Every event written by Lattice v2, local or hosted, carries an origin: the operation name and ID, and the reported host, operating-system user, git worktree, branch, and Lattice version, where the worktree and branch are those of the operation itself. A change made from a dashboard says it came from a browser in place of a worktree. Events without an origin (written before v2) still read and replay unchanged.
- **AC-37** Every event written through a server also carries the authenticated token ID, user, and machine, stamped by the server. A client cannot supply or override them.
- **AC-38** `lattice show --events` and the dashboard's event views display the actor, user, machine, and worktree of each event.
- **AC-39** *(follow-up ticket)* `lattice list` and the dashboard can filter by machine, user, and worktree.

## US-12: Hosted is opt-in and easy to adopt

*As an adopter, I find local Lattice unchanged, and when I outgrow it, moving to a server is easy.*

- **AC-43** The README and user guide present local Lattice as the default and hosted as an opt-in for several machines, several people, or heavy worktree use. No local-mode command mentions hosting unless the checkout is bound to a server.
- **AC-44** A fresh agent given only the public docs sets up a server, creates a project, issues a token, binds a checkout, and completes a write, end to end.
