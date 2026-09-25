# Design: lattice-remote

**Status:** Superseded by `docs/hosted/SPEC.md` (2026-09-25). The beliefs below still hold; the `StorageBackend.write_task(events, snapshot)` seam does not.
**Date:** 2026-02-21
**Author:** human:atin, agent:claude-opus

A hosted coordination server for Lattice, enabling distributed teams and multi-agent workloads to share task state without git-based sync conflicts.

---

## Beliefs

These principles shape every decision in this document. When a tradeoff arises, resolve it in favor of whichever option better honors these beliefs.

### 1. The client experience must be identical regardless of mode

A developer using Lattice locally and a developer using Lattice against a remote server should have the same mental model, the same CLI commands, the same ability to inspect files on disk. "Local" and "remote" are infrastructure details, not user-facing concepts.

This is not an aesthetic preference. It has direct engineering consequences:

- **One mental model means fewer bugs.** If the CLI behaves differently in remote mode, every command has two code paths, two sets of edge cases, two testing matrices. If the experience is the same, the surface area stays flat.
- **Developers can reason locally.** When something goes wrong, you look at files. That debugging workflow should work whether your source of truth is local or a server 200ms away.
- **Agents don't need to know.** An agent that works with Lattice locally should work identically against a remote server. No conditional logic, no mode detection. The agent calls `lattice show LAT-42` and gets a result. Where that result came from is not the agent's concern.

The mechanism that delivers this: a local read-only cache of task state, maintained by SSE streaming from the server. Reads always hit local files. Writes always go to the server. The user never thinks about sync.

### 2. Distributed work demands coordination, regardless of team size

The original assumption was that this server would matter for teams above some threshold (10 people, 50 people). That turns out to be wrong, or at least incomplete. A solo developer running ten agents has the same coordination problem as a ten-person team: multiple actors competing for the same tasks, racing on claims, producing conflicting state transitions.

The trigger for needing a server is not "how many actors you have." It's whether those actors share a local filesystem. Fifty agents on one machine are fine; file locking serializes their writes correctly. But the moment you have two machines, even with a single actor on each, you've lost the serialization guarantee that file locking provides. It's the distribution that breaks things, not the concurrency count.

A team of three people on three laptops, each running five agents, is a distributed system. It doesn't matter that there are only three humans or only fifteen agents. What matters is that those fifteen agents are writing to three separate copies of `.lattice/` with no shared lock between them.

The server exists to restore that serialization guarantee across machine boundaries. That's its core job. Everything else (auth, SSE, multi-project) is valuable but secondary. The moment actors are distributed across filesystems, you need a single point of serialization. That's what lattice-remote provides.

### 3. The server is a thin coordination layer, not a new product

lattice-remote is not "Lattice Cloud." It's not a SaaS platform, a web app, or a reimagining of the data model. It is the same file-based, event-sourced Lattice, running on a box, with an HTTP API in front of it so that multiple clients can talk to it safely.

The data on the server is still `.lattice/` directories. You can SSH in and `cat` a snapshot. You can `rsync` the whole thing for backup. If the server process dies, the data is fine; restart it and everything picks up where it left off. There is no database, no message queue, no external dependency beyond Python.

This is a deliberate constraint. The moment the server becomes its own complex system, you lose the core value proposition: that Lattice is just files, and you can always understand the state by reading them.

---

## Architecture Overview

```
+------------------+     +------------------+     +------------------+
|   Developer A    |     |   Developer B    |     |   Agent Pool     |
|   CLI + cache    |     |   CLI + cache    |     |   CLI + cache    |
+--------+---------+     +--------+---------+     +--------+---------+
         |                        |                        |
         |  HTTPS (writes)        |  HTTPS (writes)        |  HTTPS (writes)
         |  SSE (reads)           |  SSE (reads)           |  SSE (reads)
         |                        |                        |
+--------v------------------------v------------------------v---------+
|                        lattice-remote server                       |
|                                                                    |
|   +-------------------+  +------------------+  +-----------------+ |
|   | HTTP API (Starlette) | SSE broadcaster  |  | Auth middleware | |
|   +--------+----------+  +--------+---------+  +-----------------+ |
|            |                      |                                 |
|   +--------v----------------------v--------------------------------+|
|   |              lattice-tracker (core + LocalStorage)             ||
|   +----------------------------------------------------------------+|
|            |                                                        |
|   +--------v-------------------------------------------------------+|
|   |   .lattice/projects/<slug>/   (filesystem, per-project)        ||
|   +----------------------------------------------------------------+|
+--------------------------------------------------------------------+
```

The server imports `lattice-tracker` as a library. It calls the same core functions the CLI calls. The HTTP layer is a thin translation from request/response to function calls. The SSE broadcaster watches for write completions and pushes events to connected clients.

---

## Scope

### In scope (v1)

- HTTP API mirroring core CLI operations (create, list, show, update, claim, complete, archive)
- Token-based authentication (no roles, no permissions beyond "valid token or not")
- Multi-project support (admin creates projects via server CLI)
- SSE event stream for real-time change notification
- Local read-only cache on clients, kept in sync via SSE
- Project-level configuration (`.lattice/remote.json` tells CLI to use a server)
- `StorageBackend` protocol in lattice-tracker (prerequisite refactor)

### Out of scope (v1)

- Roles and permissions (admin vs. member vs. read-only)
- Web UI beyond the existing dashboard (served as static files)
- User accounts, registration, password management
- OAuth / SSO
- Horizontal scaling (one process per project is fine at this scale)
- Offline write queueing (reads work offline; writes require connectivity)
- Webhooks (SSE covers the notification need for v1)

---

## Package Structure

**Two packages, one ecosystem.**

| Package | Repo | Purpose |
|---------|------|---------|
| `lattice-tracker` | existing | Core library, CLI, local storage, `StorageBackend` protocol |
| `lattice-remote` | new repo | Server process, HTTP API, SSE, auth, client storage backend |

`lattice-remote` depends on `lattice-tracker` for core logic. It does not fork or duplicate any business logic.

```
lattice-remote/
  src/lattice_remote/
    server/
      app.py            # Starlette application, route wiring
      routes.py         # HTTP endpoint handlers
      auth.py           # Token validation middleware
      sse.py            # SSE broadcaster
      projects.py       # Multi-project management
    client/
      remote_storage.py # StorageBackend implementation (HTTP client)
      cache.py          # Local cache management
      sse_listener.py   # SSE consumer, cache updater
    cli.py              # Server management commands (start, create-project, create-token)
  tests/
  pyproject.toml
```

---

## Phase 1: StorageBackend Protocol (in lattice-tracker)

Before the server can exist, lattice-tracker needs a pluggable storage interface. This is the prerequisite refactor.

### Current state

Storage operations are spread across several modules with direct filesystem calls:

- `storage/fs.py` -- atomic writes, JSONL appends, root discovery
- `storage/operations.py` -- `write_task_event()`, `write_resource_event()`
- `storage/readers.py` -- `read_task_events()`
- `storage/locks.py` -- file-based locking
- CLI commands call these functions directly

### Target state

A `StorageBackend` protocol that abstracts read/write operations. Two implementations:

```python
# In lattice-tracker: src/lattice/storage/protocol.py

from typing import Protocol, runtime_checkable

@runtime_checkable
class StorageBackend(Protocol):
    """Abstract interface for Lattice storage operations."""

    # --- Reads ---
    def list_tasks(self, filters: dict | None = None) -> list[dict]:
        """Return snapshot dicts for all tasks matching filters."""
        ...

    def get_task(self, task_id: str) -> dict | None:
        """Return snapshot dict for a single task, or None."""
        ...

    def get_task_events(self, task_id: str) -> list[dict]:
        """Return all events for a task in chronological order."""
        ...

    # --- Writes ---
    def write_task(self, task_id: str, events: list[dict], snapshot: dict) -> None:
        """Persist event(s) and materialized snapshot atomically."""
        ...

    # --- Resources ---
    def list_resources(self, resource_name: str) -> list[dict]:
        ...

    def get_resource(self, resource_name: str, resource_id: str) -> dict | None:
        ...

    def write_resource(self, resource_name: str, resource_id: str,
                       events: list[dict], snapshot: dict) -> None:
        ...

    # --- Plans and Notes (non-authoritative) ---
    def read_plan(self, task_id: str) -> str | None:
        ...

    def write_plan(self, task_id: str, content: str) -> None:
        ...

    def read_notes(self, task_id: str) -> str | None:
        ...

    def write_notes(self, task_id: str, content: str) -> None:
        ...

    # --- Meta ---
    def root_path(self) -> Path | None:
        """Return the .lattice/ path, or None if not filesystem-backed."""
        ...
```

**`LocalStorage`** wraps the existing `fs.py`, `operations.py`, `readers.py`, and `locks.py` code behind this protocol. No behavior changes. The refactor is purely structural.

**`RemoteStorage`** (in `lattice-remote`) implements the same protocol via HTTP calls. The CLI doesn't know the difference.

### How the CLI resolves which backend to use

```
1. Check for .lattice/remote.json in the project
   -> If present, use RemoteStorage (with URL and token from the file)
2. Otherwise, use LocalStorage (current behavior)
```

The CLI wiring changes from:

```python
# Before: direct filesystem calls
from lattice.storage.operations import write_task_event
write_task_event(lattice_dir, task_id, events, snapshot)
```

To:

```python
# After: backend-agnostic
backend = resolve_backend()  # returns LocalStorage or RemoteStorage
backend.write_task(task_id, events, snapshot)
```

### Migration path

The refactor can be done incrementally:

1. Define the protocol
2. Implement `LocalStorage` (wrapping existing code)
3. Update CLI commands one at a time to use the backend
4. Once all commands use the backend, `RemoteStorage` can be implemented

At no point does existing behavior change. Local mode works exactly as before. This is a pure refactor.

---

## Phase 2: The Server

### HTTP API

The API mirrors CLI operations. Request/response format matches the existing `--json` output.

```
Authentication:
  All endpoints require: Authorization: Bearer <token>

Tasks:
  POST   /projects/:slug/tasks                  # create
  GET    /projects/:slug/tasks                  # list (query params for filtering)
  GET    /projects/:slug/tasks/:id              # show (snapshot)
  PATCH  /projects/:slug/tasks/:id              # update (status, fields, etc.)
  GET    /projects/:slug/tasks/:id/events       # event history

Resources:
  GET    /projects/:slug/resources/:name        # list resources of a type
  GET    /projects/:slug/resources/:name/:id    # show resource
  POST   /projects/:slug/resources/:name        # create resource
  PATCH  /projects/:slug/resources/:name/:id    # update resource

Plans and Notes:
  GET    /projects/:slug/plans/:task_id         # read plan
  PUT    /projects/:slug/plans/:task_id         # write plan
  GET    /projects/:slug/notes/:task_id         # read notes
  PUT    /projects/:slug/notes/:task_id         # write notes

Streaming:
  GET    /projects/:slug/events/stream          # SSE endpoint

Dashboard:
  GET    /projects/:slug/dashboard/data         # dashboard JSON
  GET    /dashboard/                            # static dashboard files

Admin (server CLI only, not HTTP):
  lattice-remote project create <slug>
  lattice-remote project list
  lattice-remote token create --project <slug> --actor <actor_id>
  lattice-remote token revoke <token>
```

### Write serialization

The server is the single writer. All mutations flow through one async process with a per-project asyncio Lock:

```python
class ProjectState:
    def __init__(self, slug: str, lattice_dir: Path):
        self.slug = slug
        self.lattice_dir = lattice_dir
        self.storage = LocalStorage(lattice_dir)
        self.write_lock = asyncio.Lock()
        self.sse_broadcaster = SSEBroadcaster()

    async def apply_write(self, task_id: str, events: list[dict], snapshot: dict):
        async with self.write_lock:
            # Serialized: only one write at a time per project
            self.storage.write_task(task_id, events, snapshot)
            # Notify all SSE subscribers
            for event in events:
                await self.sse_broadcaster.send(event)
```

This is the entire concurrency model. One lock per project. All writes go through it. Reads don't need the lock (they read snapshots, which are atomically written). Simple, correct, and fast enough for hundreds of concurrent actors.

### SSE Broadcasting

When a write completes, the server pushes the event to all connected SSE clients for that project:

```
event: task_event
data: {"task_id": "01JK...", "type": "status_changed", "new_status": "in_progress", ...}

event: task_snapshot
data: {"task_id": "01JK...", "snapshot": { ... }}
```

Clients receive both the event (for the log) and the updated snapshot (for the cache). This means clients don't need to re-derive the snapshot from events; they just overwrite the cached snapshot file.

### Authentication

A token file on the server maps tokens to actor identities:

```json
// .lattice-remote/tokens.json
{
  "tok_a1b2c3d4": {
    "actor": "human:atin",
    "projects": ["lattice", "webapp"],
    "created": "2026-02-21T00:00:00Z"
  },
  "tok_e5f6g7h8": {
    "actor": "agent:claude-opus-session-42",
    "projects": ["lattice"],
    "created": "2026-02-21T00:00:00Z"
  }
}
```

Middleware checks the `Authorization` header on every request, resolves the actor ID, and attaches it to the request context. Invalid or missing tokens get a 401. Token is scoped to specific projects; requests for other projects get a 403.

No passwords. No sessions. No user accounts. Tokens are generated by the admin via the server CLI and distributed out of band (shared in a team chat, added to agent configs, etc.).

### Multi-Project Layout

```
/var/lattice-remote/
  config.json             # server config (port, host, etc.)
  tokens.json             # token registry
  projects/
    lattice/
      .lattice/           # standard Lattice directory structure
        tasks/
        events/
        plans/
        ...
    webapp/
      .lattice/
        tasks/
        events/
        ...
```

Each project is a fully independent Lattice instance. The server routes requests by the `:slug` path parameter. No cross-project queries, no shared state. A project on the server is identical in structure to a local `.lattice/` directory.

---

## Phase 3: Client-Side Cache

### How the cache works

When the CLI is configured for remote mode, it maintains a local `.lattice/` directory that mirrors the server's state for the configured project.

```
my-project/
  .lattice/
    remote.json           # remote config (URL, token, project slug)
    tasks/                # cached snapshots (read-only mirror)
    events/               # cached event logs (read-only mirror)
    plans/                # cached plans
    notes/                # cached notes
    cache_state.json      # last-seen event ID, sync metadata
```

**Initial sync:** On first connection (or after a long disconnect), the CLI pulls a full snapshot of all tasks via the REST API and populates the local cache.

**Ongoing sync:** An SSE listener runs in the background (or is started on-demand when the CLI runs). As events arrive, it updates local snapshot and event files. This is append-only for events and atomic-replace for snapshots, exactly matching the local write pattern.

**Reads:** Always hit local files. `lattice list`, `lattice show`, the dashboard -- all read from the cache. This means reads are instant, work offline, and behave identically to local mode.

**Writes:** Always go to the server via HTTP. The server processes the write, then pushes the event via SSE, which updates the local cache. The CLI does not write to the local cache directly (except the initial sync).

**Staleness window:** Between issuing a write and receiving the SSE confirmation, the local cache is slightly behind. In practice this is milliseconds. For the CLI, this is invisible: the write endpoint returns the updated snapshot in its response, so the CLI can display the result immediately without waiting for SSE.

### Offline behavior

- **Reads:** Work. The cache is on disk.
- **Writes:** Fail with a clear error ("cannot reach server"). No offline write queue in v1. This is an intentional simplification. Offline writes require conflict resolution, which is a significant complexity increase. For v1, the answer is: reconnect and retry.

### Cache invalidation

The SSE stream includes a monotonic sequence number per project. The client tracks the last-seen sequence number in `cache_state.json`. On reconnect, the client requests events since its last-seen number. If the gap is too large (or the server doesn't have the history), it does a full re-sync.

---

## Project-Level Configuration

A project opts into remote mode by adding `.lattice/remote.json`:

```json
{
  "url": "https://lattice.myteam.dev",
  "project": "webapp"
}
```

The token is NOT stored in this file (it would be committed to the repo). Instead, the token is stored per-user:

- `~/.config/lattice/tokens.json` (or `$XDG_CONFIG_HOME/lattice/tokens.json`)
- Alternatively, the `LATTICE_TOKEN` environment variable

This separation means the project config can be committed to the repo (everyone on the team gets remote mode automatically on clone), while credentials stay per-user.

---

## Server Framework Choice

**Starlette** (directly, not via FastAPI).

Rationale:
- Async-native, required for SSE long-lived connections
- Minimal dependency footprint (starlette + uvicorn + httptools)
- No Pydantic requirement (request/response validation can use the existing JSON schemas from lattice-tracker)
- Well-suited for the thin wrapper pattern: the server does very little processing, mostly proxying calls to lattice-tracker's core

FastAPI would add Pydantic and automatic OpenAPI generation, which is nice but not necessary for v1. The API is simple enough that hand-written route handlers are clearer and have fewer dependencies. If OpenAPI becomes valuable later, it can be added without changing the framework.

---

## Deployment

### Minimal deployment

```bash
pip install lattice-remote
lattice-remote init /var/lattice-remote
lattice-remote project create lattice
lattice-remote token create --project lattice --actor "human:atin"
# -> tok_a1b2c3d4 (share this with the user)
lattice-remote start --host 0.0.0.0 --port 8443
```

Put it behind Caddy or nginx for TLS. The process is a single async Python server. Resource requirements are minimal: it's doing file I/O and JSON serialization. A $5/month VPS handles dozens of projects and hundreds of actors comfortably.

### What you get

- A URL that the team configures in their project's `.lattice/remote.json`
- A shared dashboard at `https://lattice.myteam.dev/projects/lattice/dashboard`
- SSE-powered real-time updates for all connected clients
- Centralized write serialization (no more merge conflicts)
- Token-based access control

### Backup

It's a directory of files. `rsync`, `tar`, `cp -r`, whatever you already use. No database dumps, no export commands. The server's data directory is the backup.

---

## Implementation Phases

### Phase 1: StorageBackend protocol (lattice-tracker)

- Define `StorageBackend` protocol in `src/lattice/storage/protocol.py`
- Implement `LocalStorage` wrapping existing code
- Update CLI commands to use `resolve_backend()` instead of direct storage calls
- No behavior changes; all existing tests should pass without modification
- Adds `remote.json` detection to `resolve_backend()` (returns `LocalStorage` when no remote config exists)

### Phase 2: Server core (lattice-remote)

- Starlette app with task CRUD endpoints
- Per-project write locks
- Token auth middleware
- Server CLI (`init`, `project create`, `token create`, `start`)
- Integration tests against a running server

### Phase 3: SSE and client cache (lattice-remote)

- SSE broadcaster on the server
- SSE listener on the client
- `RemoteStorage` implementation (HTTP writes, local cache reads)
- Cache sync logic (initial sync, incremental updates, reconnection)
- End-to-end tests: CLI -> server -> SSE -> cache -> CLI reads

### Phase 4: Dashboard integration

- Server serves dashboard static files
- Dashboard reads from the server's API (or the local cache, depending on mode)
- Shared dashboard URL for the team

---

## Open Questions

These don't need to be resolved before starting implementation, but should be addressed before v1 ships.

1. **Plans and notes in remote mode.** Plans and notes are markdown files tied to tasks but not event-sourced. Should they live on the server (synced like snapshots), or stay in the repo (since they're often tied to code)? The current design syncs them, but there's an argument for keeping them local.

2. **Agent token provisioning.** If a developer spins up 10 agents, do they each get their own token, or share the developer's token? Shared tokens are simpler but lose per-agent attribution in auth logs. Separate tokens are more correct but add provisioning friction.

3. **Cache lifecycle.** When does the SSE listener start? Always-on daemon? Spawned by the CLI on demand? Lazy-started on first read? Each has tradeoffs for freshness vs. resource usage.

4. **Server-side hooks.** lattice-tracker supports post-event hooks. Should these run on the server? If so, who configures them? This could enable server-side automation (e.g., Slack notification on task completion) but adds complexity.

5. **Event log compaction.** Long-lived tasks accumulate large event logs. The server is a natural place to run compaction (snapshot + truncate old events), but this needs design to avoid breaking clients that are mid-sync.

6. **Migration tooling.** A team that starts with local Lattice and wants to move to hosted needs a way to push their existing `.lattice/` to the server. `lattice-remote import --from /path/to/.lattice --project slug` would handle this.
