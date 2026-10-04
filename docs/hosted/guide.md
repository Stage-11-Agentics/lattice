# Lattice Hosted: the guide

Local Lattice is the default and stays the default. A board is a `.lattice/` directory next to your code, written by the `lattice` CLI on one machine, with no server. Most projects never need anything else.

Lattice v2 adds an optional server. One server process per host owns every board it hosts and is the only process that writes them. Your checkouts send every write to it and keep a read-only copy of the board (the cache) in `.lattice/`, so `cat .lattice/...`, `lattice list`, and the dashboard work as before.

**This guide is written for agents first.** A person using Lattice usually asks their agent: "set up a Lattice server", "bind this repository to our server", "move this board to the server". The agent follows this guide. If you are that agent:

- Run the command blocks in order, as written, unless the text says a block is for a different machine or shows a file to create. The blocks assume one shell session: a variable exported in one block (`TRIAL`, `LATTICE_SERVER_ROOT`, `SEAT_TOKEN_ID`) and the working directory carry into later ones. **Agent runners usually start a fresh shell for each command**, so none of that carries over: begin each command with the `cd` and `export` lines it depends on, and start the server detached, as section 4 does, so it outlives the command that started it.
- Check each result against what the text says you should see before going on. Stop and report when something differs; do not improvise around a failure.
- Ask your human only for what only they have: the server URL, a token (or shell access to the server host to mint one), which project, and approval before anything that touches a teammate's machine or a shared branch.
- Never delete a board. Every step that retires one moves it aside.

A person can run every block by hand too.

**Contents**

1. [Before any server: several worktrees on one machine](#1-before-any-server-several-worktrees-on-one-machine)
2. [When to use hosted, and when not](#2-when-to-use-hosted-and-when-not)
3. [The pieces](#3-the-pieces)
4. [Quick start on one machine](#4-quick-start-on-one-machine)
5. [Install](#5-install)
6. [The server root and server.json](#6-the-server-root-and-serverjson)
7. [Projects and their configuration](#7-projects-and-their-configuration)
8. [Tokens](#8-tokens)
9. [Running the server as a service](#9-running-the-server-as-a-service)
10. [Reverse proxies](#10-reverse-proxies)
11. [Clients: remotes, binding, thin clients, the follower](#11-clients-remotes-binding-thin-clients-the-follower)
12. [Working on a hosted checkout](#12-working-on-a-hosted-checkout)
13. [Auto-review on hosted boards](#13-auto-review-on-hosted-boards)
14. [Moving a board to the server](#14-moving-a-board-to-the-server)
15. [Moving a board back to local](#15-moving-a-board-back-to-local)
16. [Backup, restore, and the audit history](#16-backup-restore-and-the-audit-history)
17. [Daily checks](#17-daily-checks)
18. [Unknown write outcomes](#18-unknown-write-outcomes)
19. [Upgrading](#19-upgrading)
20. [Trust](#20-trust)
21. [Troubleshooting](#21-troubleshooting)
22. [Cleaning up the trial](#22-cleaning-up-the-trial)

---

## 1. Before any server: several worktrees on one machine

If your only problem is several git worktrees of one repository on one machine, you do not need a server.

Linked worktrees already share one board. `lattice` run in any linked worktree finds the primary checkout and uses its `.lattice/`. What breaks is git: when `.lattice/` is tracked, every worktree and every branch carries its own copy of the board, so a status change made in one worktree shows up as a diff in another, and board files end up in merges.

The fix is to stop tracking the board in git. Run this in the **primary** checkout (the one you cloned; `git worktree list` prints it first):

```bash
git rm -r --cached -q .lattice
echo '/.lattice/' >> .gitignore
echo '/.lattice/' >> "$(git rev-parse --git-common-dir)/info/exclude"
git add .gitignore
git commit -m "Stop tracking the Lattice board in git"
```

`git rm --cached` removes the files from git, not from disk: the primary checkout keeps its board. The line in `info/exclude` covers every branch and worktree of this clone, including branches whose `.gitignore` does not have the line yet.

Each linked worktree still has its own copy of `.lattice/`, tracked on its own branch, and `lattice` ignores it (it uses the primary's). Move those copies aside into a dated backup directory inside the clone's git directory, where git never shows them, **never the primary's board**, and commit their removal on each linked worktree's branch. The commit touches only `.lattice`, so other staged work in that worktree is left alone. From the primary checkout:

```bash
primary="$(git rev-parse --show-toplevel)"
backup="$(cd "$(git rev-parse --git-common-dir)" && pwd)/lattice-board-copies/$(date -u +%Y%m%d-%H%M%S)"
n=0
git worktree list --porcelain | sed -n 's/^worktree //p' | while read -r wt; do
  [ "$wt" = "$primary" ] && continue
  n=$((n + 1))
  if [ -e "$wt/.lattice" ]; then
    mkdir -p "$backup/$n"
    echo "$wt" > "$backup/$n/worktree"
    mv "$wt/.lattice" "$backup/$n/.lattice"
  fi
  if git -C "$wt" ls-files --error-unmatch .lattice > /dev/null 2>&1; then
    git -C "$wt" commit -q -m "Stop tracking the Lattice board in git" -- .lattice
  fi
done
echo "worktree copies kept in $backup"
```

The copies are stale duplicates of the board; keep the backup directory until you are sure, then delete it yourself.

If you would rather not commit on those branches now, skip the `git commit` line: `git status` in each linked worktree then shows the board files as deleted until the branch is merged with the commit that untracked the board.

Check it: a write in one worktree is visible in every other, and `git status` shows no board file anywhere. The block writes a check task from a linked worktree, reads it from the primary, then erases it (hidden from every view; `lattice unerase` restores it). Pass your own actor:

```bash
primary="$(git rev-parse --show-toplevel)"
other="$(git worktree list --porcelain | sed -n 's/^worktree //p' | grep -vxF "$primary" | head -n 1)"
check="$(cd "${other:-$primary}" && lattice create "Worktree check: delete me" --actor agent:worktree-check --quiet)"
lattice list | grep "Worktree check"
git worktree list --porcelain | sed -n 's/^worktree //p' | grep -vxF "$primary" | while read -r wt; do
  (cd "$wt" && lattice list | grep "Worktree check")
done
lattice erase "$check" --reason "worktree check" --actor agent:worktree-check
git status --short
git worktree list --porcelain | sed -n 's/^worktree //p' | while read -r wt; do
  git -C "$wt" status --short
done
```

`lattice list` in the primary and in every linked worktree shows the task written in the first linked worktree, and every `git status` prints nothing about `.lattice`.

**What it costs.** The board no longer travels with the repository. A clone on another machine, a teammate, or a CI job no longer sees it. If you need that, that is what a server is for: read on.

## 2. When to use hosted, and when not

Stay local (the default) when one person works on one machine, whatever the number of agents and worktrees. Section 1 covers the worktree case.

Use a server when a board must be shared **across machines**:

- several people, each with their own machine, working one board;
- agents on remote boxes or seats writing the same board as agents on your laptop;
- a board you want to watch live from anywhere, through a browser.

A board tracked in git cannot do this safely: two machines append to the same logs, and git merges event logs line by line. A server keeps Lattice's one guarantee, one writer per board, across machines.

What hosted does **not** give you: offline writes (a write while the server is unreachable fails, and nothing is queued), user accounts or roles beyond "this token may act as these actors on these projects", replication, or failover. Admin is shell access to the server host.

## 3. The pieces

| Piece | What it is | Where it lives |
|---|---|---|
| Server | `lattice server serve`, one process per host | The server host |
| Server root | The directory holding `server.json`, `tokens.json`, and every project | `$LATTICE_SERVER_ROOT` on the server host |
| Project | One hosted board, named by a slug (`demo`, `my-app`) | `<server_root>/projects/<slug>/.lattice/` |
| Token | A secret issued to one person for one machine or seat, listing the actors it may act as and the projects it may reach | Given once by `lattice server token create` |
| Remote | Your machine's name for a server: an alias, a URL, and where to find the token | `~/.config/lattice/remotes.json` (or environment variables) |
| Binding | A committed file naming the alias and project, never a host or a secret | `<repo>/.lattice-remote.json` |
| Cache | The read-only copy of the board in your checkout, private to you | `<repo>/.lattice/` |

Every write goes to the server as a named operation over HTTP. The server runs the same rules local Lattice runs, stamps who made the change (the token's user and machine), and records it. Before every read, your client catches its cache up; a follower (`lattice sync --follow`) keeps it live instead.

## 4. Quick start on one machine

This runs a server, a project, a token, and a bound checkout on one machine, over loopback. It is the fastest way to see hosted Lattice work, and it is safe to run in a scratch directory. Everything in it applies to a real deployment; later sections add what changes when the server is on another host.

You need Lattice with the `server` extra (section 5) and `git`. Work in a scratch directory:

```bash
mkdir -p "$HOME/lattice-trial" && cd "$HOME/lattice-trial"
export TRIAL="$PWD"
```

**Create the server root, a project, and a token.** The token is printed once, on the first line; save it to a file only you can read.

```bash
export LATTICE_SERVER_ROOT="$TRIAL/server-root"
lattice server init
lattice server project create demo --code DEMO
umask 077
lattice server token create --user human:alice --machine laptop --project demo | head -n 1 > "$TRIAL/token"
lattice server token list
```

`human:alice` is the person the token is issued to and `laptop` names the machine it is for. With no `--actor`, the token may act as `human:alice` and as any `agent:*`. A token reads `lat_<token id>_<secret>`; the token ID (`tok_...`) is what admin commands take, and `token list` shows it.

**Start the server.** In a real deployment it runs as a service (section 9). Here it runs detached: in its own session, with no terminal, stdin from `/dev/null`, and its log in a file, so it keeps running after the command that started it ends (a plain `nohup ... &` dies with an agent runner's shell). This works on Linux and macOS:

```bash
python3 - "$LATTICE_SERVER_ROOT" "$TRIAL" <<'PY'
import subprocess, sys
root, trial = sys.argv[1], sys.argv[2]
with open(f"{trial}/server.log", "ab") as log:
    server = subprocess.Popen(
        ["lattice", "server", "serve", "--root", root],
        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
        start_new_session=True,
    )
with open(f"{trial}/server.pid", "w") as pid:
    pid.write(f"{server.pid}\n")
PY
for i in $(seq 1 50); do
  curl -sf http://127.0.0.1:8740/healthz > /dev/null && break
  sleep 0.2
done
curl -s http://127.0.0.1:8740/healthz
```

The loop waits up to 10 seconds for the server to answer. `/healthz` answers `{"ok": true, "version": ..., ...}`. Until the 2.0 release sets the version (SPEC §15), the package reports its pre-release number (`0.2.x`) wherever a version appears; that is the same v2 code. The server listens on `127.0.0.1:8740` by default. If nothing answers, read `$TRIAL/server.log`.

**Add the remote.** `team` is your local name for this server. `--token-stdin` stores the token in `~/.config/lattice/remotes.json`, which Lattice keeps at mode 0600:

```bash
lattice remote add team http://127.0.0.1:8740 --token-stdin < "$TRIAL/token"
lattice remote list
```

**Bind a checkout.** Any git repository works. Here, a new one:

```bash
git init -q "$TRIAL/app" && cd "$TRIAL/app"
git commit -q --allow-empty -m "initial commit"
lattice remote attach team demo
```

`attach` writes `.lattice-remote.json`, adds `/.lattice/` to `.gitignore`, runs the first sync, and prints what to commit. Commit the binding:

```bash
git add .lattice-remote.json .gitignore
git commit -q -m "Bind the Lattice board to team/demo"
```

**Write.** Every command works as it does locally. `--actor` is optional on a hosted checkout when the server can default it; here it is given explicitly:

```bash
lattice create "Try hosted Lattice" --actor human:alice
lattice list
lattice status DEMO-1 in_planning --actor human:alice
printf '# Plan\n\nTry every command once.\n' > "$TRIAL/plan.md"
lattice plan write DEMO-1 --file "$TRIAL/plan.md" --actor human:alice
lattice show DEMO-1
lattice remote status
```

`show` prints, for each event, who wrote it and from where: `actor · user@machine · worktree (branch)`. On a hosted board, user and machine come from the token; when the token's user is not the actor, the line reads `actor · via token user@machine · worktree (branch)`.

The write went to the server; your `.lattice/` holds a read-only copy:

```bash
ls -l .lattice/plans/
```

That is a working hosted board. Leave the server running: the examples in later sections use it, and section 15 moves this board back to a local one. Section 22 cleans the trial up.

## 5. Install

The server needs the `server` extra, which adds Starlette and uvicorn. Hosted dashboard video filing also needs ffmpeg and ffprobe on the server: set `LATTICE_FFMPEG` to the ffmpeg binary (ffprobe is found beside it), or leave it unset to discover both on `PATH`; supported photos do not require ffmpeg. Clients need only the base install; the extra is harmless on a client.

<!-- guide: skip: installs from the network; the runner uses the repository's virtualenv -->
```bash
uv tool install 'lattice-tracker[server]'
lattice --version
```

During the v2 trial, install from the `v2` branch instead of PyPI, on the server and on every client:

<!-- guide: skip: installs from the network; the runner uses the repository's virtualenv -->
```bash
uv tool install --force 'lattice-tracker[server] @ git+https://github.com/Stage-11-Agentics/lattice@v2'
```

`pip install 'lattice-tracker[server]'` works too. Hosted mode needs macOS or Linux on the server and on every client; on another platform, hosted commands fail with `HOSTED_UNSUPPORTED_PLATFORM` (local Lattice still works there).

Without the extra, `lattice server serve` exits 1 with an install hint. Every other `lattice server` command works without it.

## 6. The server root and server.json

The server root is `--root`, else `$LATTICE_SERVER_ROOT`, else `$XDG_DATA_HOME/lattice-server` (by default `~/.local/share/lattice-server`). Every `lattice server` command takes `--root` and `--json`.

```
<server_root>/
  server.json          config (below)
  tokens.json          token registry, mode 0600, secrets stored hashed
  web_sessions.json    dashboard sessions, hashed, mode 0600
  projects/<slug>/     one per project; a git repository when the audit history is on
    .lattice/          a standard board, plus hosted/ (journal, receipts, owner lease)
```

`lattice server init` creates the root. Every key of `server.json` is optional; the defaults:

```json
{
  "bind": "127.0.0.1",
  "port": 8740,
  "trusted_proxies": [],
  "public_origins": [],
  "log_level": "info",
  "audit": {"enabled": true, "debounce_seconds": 5, "max_interval_seconds": 60, "push": null},
  "limits": {
    "max_body_bytes": 16777216,
    "inline_file_bytes": 1048576,
    "lock_timeout_seconds": 30,
    "max_inflight_per_token": 8,
    "token_ops_per_minute": 600,
    "token_body_bytes_per_minute": 268435456,
    "max_event_data_bytes": 65536,
    "max_stream_subscribers_per_project": 64,
    "stream_queue_entries": 1000,
    "replay_reset_entries": 1000,
    "min_free_disk_bytes": 1073741824,
    "max_issue_media_file_bytes": 104857600,
    "max_issue_media_issue_bytes": 262144000,
    "max_issue_media_project_bytes": 10737418240
  },
  "stream": {"heartbeat_seconds": 2}
}
```

- `bind` and `port`: where the server listens. Keep `127.0.0.1` and put a reverse proxy in front (section 10), or bind a private-network address.
- `trusted_proxies`: the addresses or CIDR ranges of your reverse proxies, for example `["127.0.0.1"]` for a proxy on the same host. The server honors `X-Forwarded-Proto` and `X-Forwarded-For` only on connections from those addresses and ignores them from every other peer. Leave it empty (the default) with no proxy. Section 10 says what to list.
- `public_origins`: the browser origins of the dashboard when a proxy rewrites `Host`, for example `["https://lattice.example.internal"]`.
- `limits`: what one token can take from the others. All projects share one process, one disk, and one memory. `lock_timeout_seconds` may not exceed 60. Below `min_free_disk_bytes` of free disk, writes fail with `STORAGE_LOW` and reads keep working. The three `max_issue_media_*` keys cap issue media: 100 MiB per stored file, 250 MiB per issue (frames included) and 10 GiB per project; an upload over a cap is refused with `PAYLOAD_TOO_LARGE` or `MEDIA_QUOTA_EXCEEDED` (HTTP 413).

Edit `server.json` with the server stopped; the server reads it at start.

The server logs one JSON object per line to stdout: requests, project loads, recovery, audit commits, lease changes. It never logs a token secret, a payload, or plan text. A few lines worth knowing:

- `startup` carries `fd_limit`. The server raises its own open-file limit at start, toward the hard limit and at most 65536 (10240 where macOS refuses more), and never lowers it. `before` is what the supervisor gave it. The service templates (section 9) raise the hard limit so the server can take what it asks for.
- `audit_commit` carries the audit cycle's timings (`lock_wait_ms`, `prehash_ms`, `stage_ms`, `commit_ms`). The audit history is staged by a helper process per project (a child of the server running the server's Python, `-c` in `ps`), replaced if it dies and stopped with the server.
- `work_lock_slow` names any hold of a project's work lock of 1 second or more, and the thread that held it. Every other request of that project waited on it.

## 7. Projects and their configuration

A project is one board. Create one with the options `lattice init` takes:

```bash
lattice server project create web --code WEB --review-mode single --plan-review-mode single
lattice server project list
```

The slug is lowercase letters, digits, and dashes, starting with a letter or digit, at most 63 characters. `--code` sets the short-ID prefix (`WEB-1`); a project without one can get one later from any bound checkout with `lattice set-project-code`.

**Review workflow.** Each project keeps its own: plan reviews only, code reviews only, both, or none. Set it at creation (`--review-mode`, `--plan-review-mode`, `--plan-approval`, `--auto-code-review/--no-auto-code-review`, `--auto-plan-review/--no-auto-plan-review`) or change it later:

```bash
lattice server project config web --set auto_plan_review_on_transition=false
lattice server project config web --set auto_code_review_on_transition=true --set review_mode=single
lattice server project config web --set review_base_branch=v2
lattice server project config web --set review_integration_branches=v2,release/next
```

`project config` accepts `review_mode` and `plan_review_mode` (`inline`, `single`, `triple`), `plan_approval` (`auto`, `human`), `auto_code_review_on_transition` and `auto_plan_review_on_transition` (`true`, `false`), `review_base_branch` (a non-empty branch/ref), `review_integration_branches` (a comma-separated ordered list of unique Git-valid branch names), `review_timeout_seconds` (a positive integer), `review_max_diff_lines` and `review_max_diff_chars` (non-negative integers; zero disables that cap), `task_types` (a complete replacement list), and `issues.enabled` (`true`, `false`):

- Integration branch names must be valid Git branch refs and resolve to remote-tracking refs. Inference considers the ordered `review_integration_branches` list plus one safe default: the branch named by `origin/HEAD`, or `origin/main` then `origin/master` if it does not resolve. When the configured list is empty and no remote default resolves, local `main` then `master` is the final fallback. A non-empty list with no resolvable entry fails closed, unresolved entries are named in a warning, and arbitrary remote branches are never candidates. Review resolution never fetches.
- `task_types` must be a JSON array of unique, non-empty, trimmed strings containing `task`:

```bash
lattice server project config web --set 'task_types=["task","bug","chore","research"]'
```

The task type list is replaced as a whole, so include every type the project should keep. New boards allow `task`, `bug`, and `chore` by default; existing project configs and tasks are preserved. With the server running, changes go through the server and every cache receives them at its next sync. Board configuration is admin-only: from a checkout, only `set-project-code`, `set-subproject-code`, and dashboard settings can change `config.json`.

The issue log is off by default. After the server and every client have been upgraded to at least `0.2.2` (section 19), enable it on the server host:

```bash
lattice server project config web --set issues.enabled=true
```

Setting `issues.enabled` preserves the project's other `issues.*` limits and settings. Hosted issue commands read synced metadata and send writes through named server operations; an ordinary operation cannot change the project configuration.

The dashboard's Issues view (the Inbox) reads the authoritative issue list and detail on the server's hosted dashboard, and can file issues, add comments, and show staged media. A bound checkout's dashboard reads issue metadata from its synced mirror; it does not serve checkout-local media or accept issue writes. Hosted browser writes use the token's browser actor, even if a request body names another actor. The local single-user dashboard still honors an explicit actor and otherwise uses its configured human actor or `dashboard:web`.

To bring an existing local board onto the server, import it instead of creating a project: section 14.

Other project commands, all run on the server host:

| Command | Use |
|---|---|
| `lattice server project list` | Slug, code, head seq, task count, state (`loaded`, `loading`, `unloaded`, `unavailable`), owner |
| `lattice server project doctor <slug> [--verify-media]` | `lattice doctor`'s read-only checks on the server's copy, plus a check of every issue media file, without racing a write |
| `lattice server project unload <slug>` | Release one project so offline maintenance can run on it; the others keep serving |
| `lattice server project load <slug>` / `reload <slug>` | Take it back and run its startup recovery |
| `lattice server project rotate-epoch <slug>` | Make every cache resync from scratch (after a restore) |
| `lattice server project unlock <slug>` | Remove a stale owner marker left by a crashed server |
| `lattice server project recover <slug> --rollback \| --keep` | Settle interrupted writes when the journal is missing (the server log says when) |
| `lattice server project audit <slug> --push-remote NAME --branch B` | Push the project's audit history to a git remote (section 16) |

**Maintenance on a hosted board.** `init`, `rebuild`, `doctor --fix`, `backfill-ids`, and `migrate` refuse on a hosted checkout with `LOCAL_ONLY`. Run them on the server host instead, against the project's directory, with the project unloaded:

```bash
lattice server project unload web
(cd "$LATTICE_SERVER_ROOT/projects/web" && lattice doctor --fix --offline-maintenance)
lattice server project load web
```

`--offline-maintenance` takes the project's owner lock for the command's duration. The next load starts a new epoch, so every cache resyncs.

## 8. Tokens

A token is issued to **one person for one machine or seat**. The server stamps the token's user and machine on every event it writes, so attribution is not something a client can forge.

```bash
lattice server token create --user human:alice --machine alice-laptop --project web
```

The token is printed once, on the first line. Give it to its owner over a channel you trust. It is stored hashed; it cannot be shown again. A lost token is revoked and replaced.

**Actors.** With no `--actor`, a person token may act as its user and as any `agent:*`, so the person and their agents share it. Any `--actor` replaces that default entirely. A seat (one agent on one box) gets a token with exactly one actor, which the server then uses by default:

```bash
lattice server token create --user human:alice --machine seat-7 --actor agent:seat-7 --project web --json > "$TRIAL/seat-7.json"
export SEAT_TOKEN_ID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["data"]["record"]["id"])' "$TRIAL/seat-7.json")"
echo "$SEAT_TOKEN_ID"
```

`--json` prints the token under `data.token` and its record under `data.record`. A seat token's user is still the person responsible for it; `create` warns that dashboard writes with it will not act as that person, which is expected for a seat.

Patterns are shell-style (`agent:*`, `agent:review-*`). Every token may also act as `agent:lattice-auto-review`, so automatic reviews work under strict tokens. A write as an actor the token does not list fails with `ACTOR_NOT_PERMITTED`, and the message names the command that widens it.

**Scope.** `--project SLUG` (repeatable) or `--all-projects`. To add a project or an actor to an existing token, grant it; no new secret is needed and the change applies on the next request:

```bash
lattice server token grant "$SEAT_TOKEN_ID" --project demo
lattice server token ungrant "$SEAT_TOKEN_ID" --project demo
lattice server token list
```

That grant workflow applies to unrestricted tokens. A filing-only token is restricted to exactly one project and `issue.file`; a grant cannot widen its project or operation scope. Revoke it and mint a replacement to change the scope.

### Filing-only issue token

For an external intake service, mint a token for one project and bind it to one source namespace. `--source` is required with `--only issue.file`, and every `issue.file` request from that token must send the same `params.source`:

```bash
lattice server token create \
  --user human:intake \
  --machine intake-worker \
  --actor agent:intake-worker \
  --project demo \
  --only issue.file \
  --source reporter-links \
  --json
```

The token is printed once as `data.token`. Store it in the intake service's secret store. Optional integer overrides are `--ops-per-minute N`, `--bytes-per-minute N`, and `--max-staged-bytes N`; the latter two are byte counts. An unrestricted token with omitted operation or body-byte overrides keeps the server's configured per-token limits; without a staged-byte override it keeps the existing project-quota behavior. A filing-only token defaults to 30 operations per minute, `max(64 MiB, the live per-file media cap)` of request bodies per minute, and 512 MiB of unreferenced staged bytes owned by that token.

This token can use only the exact `POST /v1/projects/demo/ops/issue.file` and `PUT /v1/projects/demo/issues/media/staging/{sha256}` routes. Other methods and routes return 403 `TOKEN_RESTRICTED`, including `GET /v1/projects/demo/ops/{op_id}` (op-status), every read route, and dashboard routes. It cannot create a dashboard session at `/login`; sessions backed by it are also denied. An `issue.file` request must use the token's permitted actor, cannot use `actor_name`, and cannot change the bound source. See the full [HTTP API contract](api.md#filing-only-tokens) for the exact response and route boundary.

Optional `source_ref` makes retries idempotent across distinct operation IDs. It requires a nonempty source. With a reference, Lattice trims surrounding whitespace from `source` and `source_ref`, preserves case, rejects blanks and control characters, and limits `source` to 128 characters and `source_ref` to 256. A longer `source_ref` returns 400 `VALIDATION_ERROR`; it is never truncated. For email, hash unusually long RFC 5322 Message-IDs before using them as `source_ref`. Optional `on_behalf_of` is free-form reporter text, separate from the service actor. A new issue filed by this token is marked `external: true`; a dedupe receipt reports the original issue's marker.

The issue media file cap is the live `limits.max_issue_media_file_bytes` (100 MiB by default). An unset filing-token byte rate is `max(64 MiB, the current per-file cap)`, so one legal upload fits. An explicit `--bytes-per-minute` override for any token must be at least the current per-file cap at mint. If a later server config change makes one body larger than a token's effective byte capacity, it gets a non-retryable 413 `PAYLOAD_TOO_LARGE` with `details.scope: "token"`.

The following example stages one image as raw bytes, files it, then repeats the same `(source, source_ref)` with a fresh operation ID. It uses `jq` to JSON-escape the request and Python's standard library to mint operation IDs:

```bash
export LATTICE_URL="${LATTICE_URL:-http://127.0.0.1:8740}"
: "${LATTICE_TOKEN:?Export LATTICE_TOKEN from the intake service secret store first}"
IMAGE="${IMAGE:-shot.jpg}"
SHA256="$(shasum -a 256 "$IMAGE" | awk '{print $1}')"
SIZE="$(wc -c < "$IMAGE" | tr -d '[:space:]')"

curl -sS -X PUT \
  -H "Authorization: Bearer $LATTICE_TOKEN" \
  -H 'Content-Type: application/octet-stream' \
  -H "Content-Length: $SIZE" \
  --data-binary @"$IMAGE" \
  "$LATTICE_URL/v1/projects/demo/issues/media/staging/$SHA256"

new_op_id() {
  python3 - <<'PY'
import os, time
alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
value = (int(time.time() * 1000) << 80) | int.from_bytes(os.urandom(10), "big")
print("op_" + "".join(alphabet[(value >> (5 * i)) & 31] for i in range(25, -1, -1)))
PY
}

OP_ID="$(new_op_id)"
jq -n \
  --arg op_id "$OP_ID" \
  --arg filename "$IMAGE" \
  --arg sha256 "$SHA256" \
  --argjson size "$SIZE" \
  '{op_id:$op_id, actor:"agent:intake-worker", params:{title:"Export fails after reconnect", description:"The original report text is untrusted input.", source:"reporter-links", source_ref:"ISS-7K2MQ", on_behalf_of:"Alex Example <alex@example.test>", media:[{payload:{filename:$filename, sha256:$sha256, size:$size, staged:true}}]}}' \
  | curl -sS -H "Authorization: Bearer $LATTICE_TOKEN" \
      -H 'Content-Type: application/json' --data-binary @- \
      "$LATTICE_URL/v1/projects/demo/ops/issue.file"

OP_ID="$(new_op_id)"
jq -n --arg op_id "$OP_ID" \
  '{op_id:$op_id, actor:"agent:intake-worker", params:{title:"Export fails after reconnect", source:"reporter-links", source_ref:"ISS-7K2MQ"}}' \
  | curl -sS -H "Authorization: Bearer $LATTICE_TOKEN" \
      -H 'Content-Type: application/json' --data-binary @- \
      "$LATTICE_URL/v1/projects/demo/ops/issue.file"
```

The first response contains a filing receipt with `deduplicated: false` and `events: []`. The second has the same issue `id`, `short_id`, `filed_at`, `source`, and `source_ref`, with `deduplicated: true`, `idempotent: true`, and `events: []`. It does not need to upload or send media again. A source/ref match can return an issue originally filed by another token. Any retry media the filing token staged stays owned by it and counts against its staged-byte quota until expiry; the dedupe hit neither attaches nor consumes it. Filing-only responses never include issue text, evidence, media details, task links, or closure state; `task`, `resource_id`, and `resource_name` are always null. Full issue views are returned only to unrestricted tokens. A distinct operation still has its ordinary journal entry and receipt, but the source-ref hit creates no second `issue_filed` event.

Because op-status is denied to this token, retry a possibly lost filing with the same bound `source` and `source_ref`. Reuse the same `op_id` for the normal operation-receipt replay while retained, or use a fresh `op_id` and receive the source-ref dedupe receipt. The source-ref pair is the safe recovery key, including after the seven-day operation-receipt window.

**Revoke** a token when a machine is retired or a secret leaks. It stops working on the server's next request, including open streams and dashboard sessions made with it:

```bash
lattice server token revoke "$SEAT_TOKEN_ID"
```

`token list` never prints secrets. See section 20 for what a token does and does not prove.

## 9. Running the server as a service

`lattice server serve` runs in the foreground, one process, until SIGTERM. On SIGTERM it stops accepting requests, lets in-flight writes finish (up to 30 seconds), ends open streams, makes final audit commits, and exits 0. Run it under your platform's supervisor so it restarts on failure and at boot.

Templates live in [`deploy/`](deploy/). Every path, user, and host in them is a placeholder: replace them before use.

**Linux (systemd).** [`deploy/lattice-server.service.example`](deploy/lattice-server.service.example) runs the server as a dedicated user, appends its log to `/var/log/lattice-server/server.log`, and sets `LimitNOFILE` so the server can hold one open file per stream, lock, and helper it needs.

<!-- guide: skip: needs root and systemd on a real host -->
```bash
sudo useradd --system --home /var/lib/lattice-server --create-home lattice
sudo install -d -o lattice -g lattice -m 0750 /var/log/lattice-server
sudo -u lattice lattice server init --root /var/lib/lattice-server
sudo cp lattice-server.service.example /etc/systemd/system/lattice-server.service
sudo systemctl daemon-reload
sudo systemctl enable --now lattice-server
systemctl status lattice-server
```

Log rotation: [`deploy/logrotate.example`](deploy/logrotate.example) rotates the log daily with `copytruncate`, because the server holds its stdout open. Install it as `/etc/logrotate.d/lattice-server`. If you prefer the journal, delete the two `Standard*` lines from the unit and read the log with `journalctl -u lattice-server`; journald rotates it for you.

Admin commands run as the service user, against the service's root: `sudo -u lattice lattice server token create --root /var/lib/lattice-server ...`, or set `LATTICE_SERVER_ROOT` in that user's shell.

**macOS (launchd).** [`deploy/lattice-server.plist.example`](deploy/lattice-server.plist.example) is a LaunchDaemon (or, for a server that only runs while you are logged in, a LaunchAgent in `~/Library/LaunchAgents`). It records the server's PID in a file so log rotation can find it. A launchd job starts with a limit of 256 open files, which a busy server outgrows; the template raises it with `SoftResourceLimits` and `HardResourceLimits` (`NumberOfFiles`).

<!-- guide: skip: needs root and launchd on macOS -->
```bash
sudo cp lattice-server.plist.example /Library/LaunchDaemons/com.example.lattice-server.plist
sudo launchctl bootstrap system /Library/LaunchDaemons/com.example.lattice-server.plist
sudo launchctl print system/com.example.lattice-server
```

Log rotation: [`deploy/newsyslog.conf.example`](deploy/newsyslog.conf.example), installed as `/etc/newsyslog.d/lattice-server.conf`. newsyslog cannot truncate a file in place, so after it rotates the log it sends SIGTERM to the server and launchd starts it again on a fresh file. The restart is graceful and takes seconds; clients retry through it (section 18).

**Health.** `GET /healthz` needs no token and touches no board. It answers 503 with `"ok": false` when free disk is below `limits.min_free_disk_bytes`. Point your uptime check at it.

## 10. Reverse proxies

The server speaks plain HTTP. For anything beyond loopback or a private encrypted network, put a reverse proxy in front of it to terminate TLS. The proxy must:

- **terminate TLS** and forward to the server's `bind:port`;
- **not buffer responses**, so the change stream (`/v1/projects/<slug>/stream`, Server-Sent Events) flows as it is written. The server sends `X-Accel-Buffering: no`, which nginx honors; other proxies need their own setting. A proxy that strips that header but does not buffer is fine. One that does buffer does not break anything: followers and hosted dashboards fall back to polling, and you lose only liveness;
- **allow long reads**: an idle or read timeout well above `stream.heartbeat_seconds` (2 seconds by default). Minutes are fine. A fronting proxy such as Cloudflare closes idle connections after about 100 seconds, far above the default heartbeat; if you raise the heartbeat, keep it well below the proxy's idle timeout. The client reconnects when a stream ends;
- **admit Lattice's User-Agent**: every request sends `User-Agent: lattice/<version>` (for example `lattice/2.0.0`; before the release, `lattice/0.2.x`), unless the remote's `headers` set a `User-Agent`, which then wins. Bot protection in front of the server (Cloudflare's Browser Integrity Check, for example) must let it through; otherwise requests fail with `PROXY_REJECTED` even with valid service credentials;
- **allow bodies** up to `limits.max_body_bytes` (16 MiB by default; attachments travel in the body);
- **never redirect an API path** (`/v1/...`, `/healthz`) to a login page. The client refuses any redirect with `PROXY_REJECTED` and never sends its token to another location. If your proxy puts a login in front of the site, exempt `/v1/` from it, or give clients service credentials as headers (below).

**Tell the server about the proxy.** In `server.json`:

- `bind`: an address the proxy can reach, and no wider. A proxy on the same host reaches `127.0.0.1`, the default. A proxy on another host needs the server's address on the network between them (a private or overlay network), never `0.0.0.0` on a public interface.
- `trusted_proxies`: only the proxy's own address, as the server sees it. Never a whole network: any peer in the list can claim any client address and HTTPS. A token still gates every request either way; the list decides the logged client address and whether the dashboard's cookie is marked `Secure`.
- `public_origins`: the dashboard's public origin.

A proxy on the same host:

```json
{
  "bind": "127.0.0.1",
  "trusted_proxies": ["127.0.0.1"],
  "public_origins": ["https://lattice.example.internal"]
}
```

A proxy on another host, where `192.0.2.20` is the server's private address and `192.0.2.10` is the proxy's:

```json
{
  "bind": "192.0.2.20",
  "trusted_proxies": ["192.0.2.10"],
  "public_origins": ["https://lattice.example.internal"]
}
```

The proxy then forwards to `http://192.0.2.20:8740`. Merge these keys into your `server.json` and restart the server. The server refuses to start with the old `trusted_proxy` key from early v2 trials, and names `trusted_proxies`.

nginx, as a sketch:

```nginx
server {
    listen 443 ssl;
    server_name lattice.example.internal;
    ssl_certificate     /etc/ssl/example/fullchain.pem;
    ssl_certificate_key /etc/ssl/example/privkey.pem;
    client_max_body_size 16m;

    location / {
        proxy_pass http://127.0.0.1:8740;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_buffering off;
        proxy_read_timeout 1h;
    }
}
```

Caddy:

```caddy
lattice.example.internal {
    reverse_proxy 127.0.0.1:8740 {
        flush_interval -1
    }
}
```

**Proxies that need credentials.** Some access proxies admit a client that presents service headers. Name each header and the environment variable that holds its value; values never go in the file:

```bash
lattice remote add edge https://lattice.example.internal --token-env LATTICE_TOKEN_EDGE \
  --header CF-Access-Client-Id=PROXY_ID --header CF-Access-Client-Secret=PROXY_SECRET
lattice remote list
```

`remote add` only writes the entry; it contacts nothing. The variables must be set when a command uses the remote.

**Without TLS.** An `http://` URL to a host that is not loopback fails with `INSECURE_URL`. If the network itself is encrypted (a private overlay network), allow it per remote with `lattice remote add ... --allow-plaintext`.

## 11. Clients: remotes, binding, thin clients, the follower

### Remotes

A remote is this machine's name for a server. `~/.config/lattice/remotes.json` (or `$XDG_CONFIG_HOME/lattice/remotes.json`) holds them, mode 0600; Lattice refuses to read a token from it if other users can read it.

```json
{
  "remotes": {
    "team": {
      "url": "https://lattice.example.internal",
      "token": {"env": "LATTICE_TOKEN_TEAM"},
      "headers": {"CF-Access-Client-Id": {"env": "PROXY_ID"}},
      "run_board_hooks": false,
      "run_auto_reviews": true,
      "allow_plaintext": false,
      "retry_seconds": 15
    }
  }
}
```

- `token`: a literal string (what `--token-stdin` stores) or `{"env": "VAR"}` (what `--token-env VAR` stores). With `--token-env`, the variable must be set in every shell that runs `lattice`, agents included.
- `run_board_hooks` (default `false`): run the hosted board's hooks on this machine after its writes. A hosted board's hook commands are chosen by whoever administers the server, so this is opt-in.
- `run_auto_reviews` (default `true`): set `false` to decline the board's automatic reviews on this machine (section 13).
- `retry_seconds` (default 15): how long one write keeps retrying while the server is not available (section 18).

`lattice remote add ALIAS URL [--token-env VAR | --token-stdin] [--header NAME=ENVVAR]... [--allow-plaintext]` writes an entry. `lattice remote list` shows aliases and URLs, never tokens.

### Binding a checkout

From the primary checkout or any linked worktree of it:

<!-- guide: skip: section 4 already attached this checkout -->
```bash
lattice remote attach team demo
```

It checks that the alias works and the project is visible to your token, writes `.lattice-remote.json` (`{"remote": "team", "project": "demo"}`, no host, no secret), adds `/.lattice/` to `.gitignore` and to the clone's `info/exclude`, runs the first sync, and prints what to commit. Every worktree of the clone shares the binding and the cache.

Commit and push `.lattice-remote.json` and `.gitignore`. A teammate who clones or pulls it needs only their own remote named the same way (`lattice remote add team <url> ...` with their own token); their first `lattice` command fills their cache. If their alias is missing, the command fails with `REMOTE_NOT_CONFIGURED` and prints the line to run.

**Refresh agent instructions.** CLAUDE.md blocks and skills installed before v2 tell agents to edit plan files directly, which a hosted cache refuses. After attaching, refresh them (`attach` prints the same reminder):

```bash
lattice setup-claude --force
lattice setup-claude-skill --force
```

`lattice remote status` shows the binding, your identity on the server, the cache's position and freshness, the follower, and any branch that still tracks board files (section 14).

### Thin clients (boxes, CI, seats)

A machine with no config file configures the remote entirely from the environment. `<ALIAS>` is the alias uppercased, with anything not a letter or digit replaced by `_`:

<!-- guide: skip: placeholder values; they would override the remote the trial uses -->
```bash
export LATTICE_REMOTE_TEAM_URL=https://lattice.example.internal
export LATTICE_REMOTE_TEAM_TOKEN="<the seat's token>"
export LATTICE_REMOTE_TEAM_HEADERS='{"CF-Access-Client-Id": "PROXY_ID"}'
```

`LATTICE_REMOTE_<ALIAS>_HEADERS` maps each header name to the name of the variable holding its value. `LATTICE_REMOTE_<ALIAS>_ALLOW_PLAINTEXT=1` allows plaintext. The environment wins over the file. A thin client clones the repository (which carries the binding) and runs `lattice` as usual; its cache dies with the box. Give each seat its own single-actor token (section 8).

### Keeping the cache fresh

Without a follower, every read command first asks the server for what changed (a small request when nothing did) and then reads the cache. For a live view, run a follower:

```bash
lattice sync
```

<!-- guide: skip: runs in the foreground until stopped -->
```bash
lattice sync --follow
```

`lattice sync` catches up once. `lattice sync --follow` holds the server's change stream and syncs on every change, falling back to polling when a proxy blocks the stream. While it runs, reads on this machine skip their own catch-up. Stop it with Ctrl-C or SIGTERM. `lattice dashboard` on a hosted checkout runs a follower of its own. `lattice watch` and `lattice wait` work on hosted checkouts too.

### Dashboards

**On a bound checkout.** `lattice dashboard` works as locally, with a follower of its own. The page checks the cache's position every second and refetches as soon as a write lands, from any machine. Its writes go to the server as your token's user when the token permits it (the "browser actor"), else as the token's default actor. On a local board the page refreshes every 5 seconds, as before.

**On the server.** The server serves each project's dashboard in the browser, with no checkout and no install:

1. Open `<server url>/login` and paste a token. The token is checked, and the browser gets a session cookie; the token itself is not stored in the browser.
2. `<server url>/` lists the projects the token may reach. Each project's dashboard is at `/p/<slug>/`. Opening one without a session sends you to the login first.
3. The page follows the project's change stream and refetches on every write, so it is live. If the stream drops (a proxy that buffers it, say), it falls back to refreshing every 5 seconds.
4. **Log out** ends the session. A session lasts 30 days and dies with its token: revoking the token (section 8) logs out every browser that used it.

Writes from the hosted page are ordinary operations with the session's token, as the browser actor. The page gives each write one operation ID and reuses it on retry, so a double click or a retry after a lost answer applies once. Two things the hosted page cannot do: open a plan or notes file in your editor (use `lattice plan write` / `lattice notes write` from a checkout), and show git branches (the server has no worktree).

Behind a proxy that rewrites `Host`, list the public origin in `public_origins` (section 6); without it, logins and writes are refused by the `Origin` check. Under the hosted page's content policy, a dashboard background image from another site does not load. Moving a task in either dashboard starts no automatic review (section 13).

**Filtering by machine, user, and worktree.** Every dashboard, local or hosted, filters the board by where the work was done, as `lattice list --machine/--user/--worktree` does: a task matches when one of its events came from that machine, user, or worktree. On a hosted board, machine and user are the token's (`laptop`, `human:alice`); on a local board, the host name and OS user. Open the filter drawer's Origin section, or put the filters in the URL, which you can bookmark and share: `/p/demo/?machine=laptop&user=human:alice` hosted, `/?user=alice` locally. The origin filters combine with the drawer's other filters (all must match). A worktree filter is an absolute path, matched as recorded (`/home/alice/src/app`, trailing slashes and `.` or `..` segments folded). In the dashboard a relative path is refused and a path through a symlink matches nothing, because the server never looks at your filesystem. (`lattice list --worktree` in a terminal does resolve a relative path from the current directory.)

The API behind the page takes the same filters, with the token as a bearer, from any machine:

```bash
curl -s -H "Authorization: Bearer $(cat "$TRIAL/token")" \
  "http://127.0.0.1:8740/p/demo/api/tasks?machine=laptop" | python3 -m json.tool | head -n 12
curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' http://127.0.0.1:8740/p/demo/
```

The first prints the tasks written from the machine named `laptop`. The second shows that the page without a session redirects (303) to the login.

**Nothing from the network.** The dashboard's graph libraries ship inside Lattice. Neither dashboard loads anything from another site, so it works offline and behind a strict firewall.

### Offline

When the server is not available, **reads** show the cache and print one line to stderr:

```
lattice: cannot reach team; showing cache as of 2026-09-27T10:15:02Z
```

For 15 seconds after a failed attempt, reads skip the network entirely. `--json` output is unchanged.

**Writes** are not queued: a write needs the server. While it retries, it says so on stderr, in plain and `--json` modes alike (stdout stays clean):

```
lattice: server team (https://lattice.example.internal) is not available; retrying for up to 15 s
lattice: team still not available (5 s of 15 s)
lattice: team still not available (10 s of 15 s)
Error: server team (https://lattice.example.internal) is not available. Nothing was written; run the command again when it is back.
```

It exits 1 with `SERVER_UNREACHABLE`, and nothing is written anywhere. `--json` adds `details` with `remote`, `url`, `waited_seconds`, and the raw `os_error`. When the server answered but was overloaded (429, 502, 503, 504), the progress lines say `busy` instead. A 502, 503, or 504 from a gateway in front of the server (an HTML error page, no `Lattice-Protocol`) counts as the server not being available: writes retry it, and reads retry it once and then show the cache.

You wait once per outage, not once per write: once a command has found the server down, a write in the next 15 seconds tries one connection and fails at once. Retry when the server is back (`curl <url>/healthz`, or any read that no longer prints the notice). A write whose request did reach the server always retries for the full time, because it may have been applied (section 18).

## 12. Working on a hosted checkout

Everything you and your agents do locally works the same: `create`, `status`, `comment`, `next --claim`, `complete`, `attach`, `list`, `show`, the dashboard. Error codes and exit codes are the local ones. The differences:

- **`.lattice/` is read-only.** Board files are mode 0400 and their directories 0500, so an editor cannot save into them. Write plans and notes with commands, which work in every mode:

  ```bash
  lattice plan write DEMO-1 --file "$TRIAL/plan.md" --actor human:alice
  printf 'Tried it.\n' | lattice notes write DEMO-1 --stdin --actor human:alice
  printf 'Shared context for every agent.\n' | lattice context write --stdin
  printf 'run state\n' | lattice board write orchestration/run-state.md --stdin
  ```

  `context write` and `board write` take no `--actor`; on a server they are attributed to your token's default actor (the person for a person token, the seat's actor for a seat token), with the token's user and machine in `origin.authenticated`. `lattice board write` writes files under `orchestration/` (an orchestrator's run-state and working files) and loose files directly under `plans/` or `notes/`. There is no remove; overwrite instead. If an edit gets into the cache anyway (after a `chmod`, or as root), the next command moves it to `.lattice/cache/rescued/<time>/` and says so. It is never silently discarded; write it back with `lattice plan write`.
- **Who acted, and whose token.** `lattice remote status` shows the token's person and machine (for example `human:alice on laptop`). Agents still pass `--actor agent:<id>`. Every event records both: the actor, and the token's user and machine in `origin.authenticated`. `lattice show <task>` prints `actor · user@machine · worktree (branch)` per event (`actor · via token user@machine · …` when the token's user is not the actor); `lattice show <task> --full` and `--json` include the whole origin.
- **Linked worktrees** of a bound clone hold only `.lattice-remote.json`; the read-only mirror is the primary checkout's `.lattice/`, and every worktree uses it.
- **Finishing without a PR.** For work not merged through a pull request, go `review -> done` with `lattice complete`, not through `pr_open`, and say in the completion review how the work was integrated (commit SHA and branch, or where the change lives).
- **`--actor` is optional.** Without it, the server acts as your token's one actor pattern without a wildcard: the person for a person token (`human:alice`, beside `agent:*`), the seat's actor for a seat token. Agents should still pass their own `--actor agent:<id>`, so the board shows which agent did what.
- **Maintenance commands** (`init`, `rebuild`, `doctor --fix`, `backfill-ids`, `migrate`) refuse with `LOCAL_ONLY`; they run on the server host (section 7). `lattice doctor` without `--fix` runs on the cache and also compares every file with the server's copy.
- **Hooks** configured on the board run on your machine only if your remote sets `run_board_hooks: true`. The server runs no hooks and spawns no agents.
- **Plugins.** An operation from a plugin package runs on a hosted board only if the plugin is installed on the server (section 19).
- **Grouping work.** New boards allow `task`, `bug`, and `chore`, and custom types can be configured per board. An agent must never create an umbrella task to stand in for an epic, even if the board has a custom `epic` type. Group related tasks with a shared tag (`--tags`) and order them with `depends_on` links. On a hosted board, an admin changes types with `lattice server project config <slug> --set 'task_types=[...]'`, including `task` and every type the project should keep:

  ```bash
  lattice create "Hosted trial: write the runbook" --tags hosted-trial --actor human:alice
  lattice create "Hosted trial: invite the team" --tags hosted-trial --actor human:alice
  lattice link DEMO-3 depends_on DEMO-2 --actor human:alice
  lattice list --tag hosted-trial
  ```
- **Other people's text** reaches your terminal. Plain output replaces control characters from the board with `�`; `--json` is unchanged.

## 13. Auto-review on hosted boards

Automatic reviews work as locally, from the project's review workflow (section 7), with three differences:

- **The review runs on the machine that made the transition.** When you move a task to `review` or `planned`, your client starts the review agent in your worktree and records it on the board. A thin client must stay alive until its review lands; a box torn down mid-review leaves no review.
- **Whether a transition fires a review depends on the project's config** (section 7). The `lattice status` output says whether it fired, and why not.
- **Dashboard moves start no review**, locally or hosted. Move a task from the CLI when you want its review to fire, or run `lattice code-review <task>` by hand.
- **`lattice review-status <task>` sees reviews running on other machines.** It reports `running on <host> since <time>` until the review's artifact arrives, or `no artifact after <timeout> s; treat as failed` when the review timeout passes. `lattice code-review` and `plan-review` refuse with `REVIEW_IN_FLIGHT` while another machine's review of the same gate is in flight; `--force` overrides.

A machine that should never run reviews (a small box, a CI job) sets `"run_auto_reviews": false` on its remote; its transitions then say the review was skipped for that reason. `lattice code-review <task>` still runs one by hand.

## 14. Moving a board to the server

Moving a board needs one tool, a doctor-gated import, and five steps in the checkout. Nothing is deleted: the old board is kept beside the checkout. For a board with issues, first follow section 19: upgrade every client to at least `0.2.2`, then the server, before importing or enabling it.

The example below makes a local board to move, so it can be run on a scratch machine. For a real board, start at step 1 in your own checkout, with your own slug and alias. The quick start's server is still running:

```bash
git init -q "$TRIAL/legacy" && cd "$TRIAL/legacy"
lattice init --project-code LEG --actor human:alice
lattice create "A task from before the move" --actor human:alice
git add -A && git commit -q -m "A repository whose board is tracked in git"
```

**1. Stop every writer of the local board:** agents, dashboards, MCP servers. Anything still writing after the copy is lost to the move.

**2. Import a copy on the server host.** Copy the checkout's `.lattice/` to the server host (the import reads a directory holding `.lattice/`), then import it:

```bash
mkdir -p "$TRIAL/legacy-copy"
cp -R "$TRIAL/legacy/.lattice" "$TRIAL/legacy-copy/"
lattice server project import legacy --from "$TRIAL/legacy-copy"
```

By default, import copies issue media into private server storage along with the durable issue logs, snapshots, and ID index. Add `--omit-media` to import issue metadata without copying any media bytes:

```bash
lattice server project import legacy --from "$TRIAL/legacy-copy" --omit-media
```

`--omit-media` is an explicit metadata-only choice. Issue metadata syncs to bound clients; media bytes do not enter ordinary sync, reset manifests, deltas, stream events, or audit history. When a user requests media, a bound checkout fetches it on demand into a separate private `.lattice/cache/issue-media/` cache. `lattice cache clear` removes that cache. Default import and later uploads retain the media in private server storage. Phone photos may contain location in EXIF data; Lattice does not strip photo metadata. For local CLI filing, ffmpeg strips video location metadata on the filing machine before upload; if it is missing, disabled (`LATTICE_FFMPEG=off`), or fails, `issue file --evidence` and `issue attach` refuse the video unless the filer passes `--keep-video-metadata` to keep the original. The hosted dashboard prepares raw video on the server instead and requires its ffmpeg/ffprobe toolchain (§8.12); if unavailable or disabled, it refuses with “This server cannot remove location data from videos yet; ask the board admin to install ffmpeg”. This video-only guard does not block supported photos.

Before creating the project's staging directory or writing any imported file, the importer replays and validates source issue logs and metadata, then preflights every referenced original and frame. It refuses symlinks and special files, checks media IDs, extensions and lowercase SHA-256 fields, verifies file contents against their hashes, and calculates all three media quotas. Only after preflight succeeds does it stage the import, rebuild issue snapshots, and publish the project. Limits are 100 MiB per stored file, 250 MiB per issue including frame sidecars, and 10 GiB per project by default; the project cap is configurable as `max_issue_media_project_bytes` in `server.json`. Missing or corrupt media and any quota failure stop the default import before imported files are written. The error names an exceeded quota and says when `--omit-media` is an acceptable retry.

The import report gives the exact copied media-object count and bytes when media is copied. In `--omit-media` mode, logs and snapshots still validate, including the syntax of stored hash fields, but media contents are never opened or hash-compared and no media quota is applied. The report inventories metadata-referenced originals and discoverable frame sidecars with no-follow directory enumeration and `lstat` only; it counts a missing original or an entry with failed/non-regular `lstat` as an object of unknown size. JSON always includes `media_count`, `media_bytes`, `media_known_bytes`, `media_unknown_size_count`, and `media_inventory_complete`. `media_bytes` is null when any object's size is unknown or frame enumeration is incomplete; `media_inventory_complete` is false when a frame directory could not be safely enumerated. Human output says when the byte total is unknown or the media count is incomplete.

Every token that should reach the new project needs it granted (section 8). Here, the quick start's token; its ID is the middle of the token string:

```bash
lattice server token grant "$(cut -d_ -f2,3 "$TRIAL/token")" --project legacy
```

From another machine, copy it with `rsync -a` or `scp -r`, preserving the tree. The import refuses a symbolic link or special file under the board, a slug that already exists, and a board that fails `lattice doctor`.

**If the import refuses the board.** It prints doctor's findings and the next step. Boards written by several v1 agents at once often carry history damage: a status or assignment event whose recorded `from` disagrees with the task's state, or two tasks holding one short ID. Repair it on the **local** board, in the checkout (its writers are already stopped), with Lattice 2:

```bash
cd "$TRIAL/legacy"
lattice doctor --fix --actor human:alice
lattice doctor
```

`doctor --fix --actor` only appends events: it never rewrites or removes one. Each task keeps the status, assignment, and fields the board showed before the repair; a task holding a duplicate or out-of-prefix short ID gets the next free one, and the old ID stays in its history. It prints each appended event, each reassignment as `old -> new` with the task's title, and each restored field. Without `--actor` it lists what it would append and changes nothing. On a board with no damage it appends nothing, so the example board runs it harmlessly. Damage of any other kind it names and leaves alone. When `lattice doctor` is clean, commit nothing yet: copy the board to the server host again and repeat this step.
 It never modifies its source and never changes the board's configuration, so the project keeps its review workflow and prompt overrides.

Read the two lists it prints:

- **paths not copied**: anything that is not board data (for example `reviews/`, `logs/`, and `exports/`). Issue logs, snapshots, and the ID index are durable metadata and are copied; `issues/media/` follows the explicit default-copy or `--omit-media` policy above.
- **non-canonical plan and notes files**: loose files under `plans/` or `notes/`. They are copied, and on a hosted checkout they are read-only; write them with `lattice board write`, and put new working files under `orchestration/`.

The import also repairs short-ID bookkeeping from the logs, starts the project's journal and its audit history (section 16), and prints these same steps with your slug filled in.

**3. Move the old board aside in the checkout. Never delete it.** If board files are tracked in git, stage their removal:

```bash
mv .lattice ".lattice.pre-hosted-$(date -u +%Y%m%d-%H%M%S)"
echo '/.lattice.pre-hosted-*/' >> .gitignore
git rm -r --cached -q --ignore-unmatch .lattice
```

**4. Attach the checkout:**

```bash
lattice remote attach team legacy
lattice list
lattice comment LEG-1 "Moved to the server." --actor human:alice
```

**5. Commit and push** the binding, `.gitignore`, and the staged removal, so teammates and other branches pick up the move. Then check for branches that still track board files:

```bash
git add .lattice-remote.json .gitignore
git commit -q -m "Move the Lattice board to the server"
lattice remote status
```

Then `git push`, when the repository has a remote.

**Branches that still track board files.** `lattice remote status` lists every local and remote-tracking branch that still has files under `.lattice/`. Checking such a branch out in the primary checkout makes git try to write board files into the read-only cache and stop partway; merging it brings board files back. Fix each branch: merge the commit that untracked the board, or on that branch run

<!-- guide: skip: needs a second branch that still tracks the board -->
```bash
git rm -r --cached -q .lattice
git commit -m "Stop tracking the Lattice board"
```

If a checkout did get into that state, run `lattice cache clear` and then any `lattice` command: the cache resyncs from the server.

**Teammates** pull the move, add their own remote under the same alias, and run any `lattice` command. Git deletes the tracked board files in their clone; the binding takes over, and their first command fills the cache.

## 15. Moving a board back to local

The reverse needs no new tooling either. The board keeps the events v2 wrote, so it needs Lattice v2 to read correctly. Continuing the example, this moves project `demo` from section 4 back into its checkout:

**1. Stop every writer** of the hosted board on every machine, and unload the project on the server host (or stop the server):

```bash
lattice server project unload demo
```

**2. In the checkout**, forget the cache and remove the binding:

```bash
cd "$TRIAL/app"
lattice cache clear --forget
git rm -q .lattice-remote.json
```

**3. Copy the board** from the server host into the checkout, without its `hosted/` directory. On the same host:

```bash
mkdir -p .lattice
cp -R "$LATTICE_SERVER_ROOT/projects/demo/.lattice/." .lattice/
backup="$(cd "$(git rev-parse --git-common-dir)" && pwd)/lattice-hosted-copy-$(date -u +%Y%m%d-%H%M%S)"
mv .lattice/hosted "$backup"
echo "server control files kept in $backup"
```

`hosted/` holds the server's journal and receipts, which a local board does not use. It is moved into the clone's git directory, where git never shows it; delete it yourself once the board works.

From another host: `rsync -a --exclude hosted/ server-host:/var/lib/lattice-server/projects/demo/.lattice/ .lattice/`. `cache clear --forget` keeps `.lattice/cache/rescued/` if it held any rescued edits; review and delete it afterwards.

**4. Decide whether git tracks the board again.** If it should not (the section 1 setup, and the default here), leave `TRACK_BOARD=no`. If it should, set `TRACK_BOARD=yes`: the block removes the `/.lattice/` line from `.gitignore` and from the clone's `info/exclude`, and stages the board, which is untracked after the copy (a `git commit -a` alone would leave it out).

```bash
TRACK_BOARD=no
if [ "$TRACK_BOARD" = yes ]; then
  exclude="$(git rev-parse --git-common-dir)/info/exclude"
  for f in .gitignore "$exclude"; do
    [ -f "$f" ] || continue
    grep -vxF '/.lattice/' "$f" > "$f.tmp" || true
    mv "$f.tmp" "$f"
  done
  git add .gitignore .lattice
fi
```

**5. Check and commit:**

```bash
lattice doctor
lattice list
git commit -q -am "Move the Lattice board back to local"
```

`lattice doctor` must report no errors. With `TRACK_BOARD=yes`, `git ls-files .lattice` now lists the board's files; with `no`, it prints nothing. The server keeps the project, unloaded until its next start. To retire it, stop the server and move `projects/demo/` out of `$LATTICE_SERVER_ROOT/projects/` (keep it until you are sure).

## 16. Backup, restore, and the audit history

**Back up** the whole server root, consistently:

- stop the server (or unload each project) and then copy the root; or
- take an atomic filesystem snapshot (ZFS, Btrfs, LVM, APFS) of the whole root.

A live `tar` or `rsync` of a running server is **not** a consistent backup: it can copy a board mid-write.

```bash
lattice server project unload legacy
tar -C "$TRIAL" -czf "$TRIAL/server-root-backup.tgz" server-root
lattice server project load legacy
```

**Issue media lives in the server root, and only there.** The photos, videos and video frames filed on issues are under `projects/<slug>/.lattice/issues/media/`. They are not in the audit history (the audit repository ignores that directory, so `audit --push-remote` never carries them) and nothing else holds a copy: a detach deletes the bytes for good, and a project whose root is lost without a root backup loses its media. The root backup above includes them. To back up one project, unload it and copy its whole `projects/<slug>/` directory, which holds the board, the journal and the media together. Include the project's `.lattice/issues/media/` in every backup of the board; a backup taken from the audit history alone has the issue metadata and none of the media.

**Restore** with the server stopped: put the copy back in place, then start a new epoch for every project **before any client connects**, so every cache resyncs instead of trusting history the server no longer has:

<!-- guide: skip: run only after restoring a backup, with the server stopped -->
```bash
for slug in $(ls "$LATTICE_SERVER_ROOT/projects"); do
  lattice server project rotate-epoch "$slug"
done
```

To restore one project while the server runs, unload it, replace its `projects/<slug>/` directory with the copy from the backup (board, journal and media from the same backup, never a newer media tree under an older board: a load deletes media files that no issue snapshot lists), then load it and start the new epoch:

<!-- guide: skip: run only after restoring a project from a backup -->
```bash
lattice server project unload legacy
# replace $LATTICE_SERVER_ROOT/projects/legacy from the backup here
lattice server project load legacy
lattice server project rotate-epoch legacy
lattice server project doctor legacy --verify-media
```

A write acknowledged after the backup was taken is lost by the restore. `lattice remote verify` (section 17) on each client reports every such write. The doctor's media pass reports every media file a restored issue lists that is missing or damaged.

**The audit history.** With `audit.enabled` (the default) and `git` on the server's `PATH`, each `projects/<slug>/` is a git repository that commits the board's data a few seconds after each burst of writes (`audit: seq 41-47 (7 ops)`). It holds only board data: no journal, receipts, or runtime files. To push it somewhere safe, add a git remote to the project's repository and point the project at it:

```bash
git init -q --bare "$TRIAL/audit-backup.git"
git -C "$LATTICE_SERVER_ROOT/projects/legacy" remote add backup "$TRIAL/audit-backup.git"
lattice server project audit legacy --push-remote backup --branch main
```

Here the remote is a local bare repository; in practice it is any git URL the server's user can push to.

`server.json`'s `audit.push` sets a default for every project. A failed push is logged and retried at the next commit; it never blocks writes. Without `git`, the audit history is off and the server logs one warning.

Restoring from the audit history is an import, not a restore: check the commit you want out into a scratch directory and run `lattice server project import <new-slug> --from <that directory>`. It starts a new epoch; the old journal cannot be resumed.

The audit history holds no media, and the default import copies every media file the issues list from the source directory, refusing when one is missing. So put the media back into the checkout before importing, from a server-root backup (the same project's `.lattice/issues/media/`):

<!-- guide: skip: run only when restoring a project from its audit history -->
```bash
git clone "$TRIAL/audit-backup.git" "$TRIAL/restore"
git -C "$TRIAL/restore" checkout "<commit>"
mkdir -p "$TRIAL/restore/.lattice/issues"
cp -R "<backup>/projects/legacy/.lattice/issues/media" "$TRIAL/restore/.lattice/issues/"
lattice server project import legacy-restored --from "$TRIAL/restore"
lattice server project doctor legacy-restored --verify-media
```

Media in the backup that the commit's issues do not list is left behind (the import prints it as not copied). An issue the commit lists whose media is not in the backup stops the import, naming the file. `--omit-media` imports the issue metadata without those bytes: use it only when the media is truly gone, because the issues then keep entries whose files the server cannot serve.

## 17. Daily checks

On the server host, for every hosted project:

```bash
lattice server project list
lattice server project doctor legacy
```

`project doctor` also checks issue media: every file an issue lists must exist, be a regular file and match its recorded size and type; files no issue lists, videos with no frames, and staged uploads left over a day are warnings. After a crash or a restore, add `--verify-media`, which also hashes every original against its recorded sha256 (slow on a large project; the hashing runs after the project is released, so reads and writes keep working while it runs): `lattice server project doctor legacy --verify-media`. Its `--json` summary carries `media_checked`, `media_missing`, `media_corrupt`, `media_orphans` and `staged_objects`; a damaged or missing file is an error and exits 1.

After a crash in the middle of a filing, the server finishes publishing the media when the project loads. If the staged copy of a committed upload was lost or cannot be read first (a damaged disk, a cleaned `projects/<slug>/.runtime/issue-media/staging/`, wrong file permissions), the project still loads: the server logs `issue_media_reconcile_failed` with the operation ID and the reason, keeps that operation's manifest in `projects/<slug>/.runtime/issue-media/manifests/`, and doctor and the media route report the file as missing. To recover, detach the missing entry and attach the file again: `lattice issue detach <issue> <n> --reason "bytes lost"`, then `lattice issue attach <issue> <file>` (the file gets a new number; attaching without detaching first does nothing, because the issue already lists the file). Then run `lattice server project reload <slug>` (or wait for the next load): it drops the stale manifest and doctor comes back clean. If the whole `.runtime/` directory was removed there is no manifest and nothing is logged; doctor still reports the missing file, and the same two commands recover it.

On every client that wrote, from its bound checkout:

```bash
cd "$TRIAL/legacy"
lattice remote verify
```

`lattice remote verify` checks every write this checkout was told succeeded (recorded in `.lattice/cache/acked.jsonl`) against the server, and prints how many it checked. It prints each one the server does not hold and exits 1 if there is any: that is a lost write. Confirmed writes stay on the list for 90 days, so a later restore that loses one is still caught. `lattice doctor` on each local board stays as it was.

## 18. Unknown write outcomes

A write retries on its own for up to `retry_seconds` (15 by default) on connection errors, timeouts, and 429, 502, 503, and 504 responses, reusing the same operation ID, so the server applies it at most once. It prints its progress to stderr while it retries (section 11, Offline). When a request reached the server and no answer has come back yet, the line says so instead: `lattice: still waiting for team to answer (20 s); if the request reached it, the write may have applied`. When the retries run out:

- `SERVER_UNREACHABLE` ("server ... is not available. Nothing was written"): no attempt reached the server. Retry when it is back.
- `OUTCOME_UNKNOWN`: a request reached the server but no answer came back. The server may have applied the write. The message names the operation ID. **Check before retrying**:

  <!-- guide: skip: needs an operation ID from a real OUTCOME_UNKNOWN -->
  ```bash
  lattice remote op-status op_01J9ZEXAMPLE0000000000000
  ```

  - `committed` (with its epoch and seq): it was applied. Do not run the command again.
  - `in flight`: the server is still applying it. Check again in a moment; do not rerun it yet.
  - `not found`: it did not apply. Running the command again applies it once, as a new operation.

  `op-status` exits 0 in all three cases; `--json` carries `state` (`committed`, `in_flight`, `not_found`). If the lookup itself cannot reach the server, it exits 1 with `SERVER_UNREACHABLE`: the outcome is still unknown, so run `op-status` again later, not the write. An agent that reruns an `OUTCOME_UNKNOWN` command without checking may apply it twice.

## 19. Upgrading

**Order: clients first, then the server, then enable the issue log.** The `0.2.2` server refuses every operation from a client below `0.2.2`, on every project, so a server-first upgrade cuts off hosted writes from each machine and seat you have not upgraded yet. A `0.2.2` client works against a `0.2.1` server (issue commands report that the log is off or that the server does not support it), so there is no outage in this order.

1. **Release.** The install command (section 5) installs from the `v2` branch: make sure the `0.2.2` build is merged there first.
2. **Every client.** On each laptop, seat, box and CI image, upgrade with the same command (`--force` for `uv tool install`). Check `lattice --version` prints `0.2.2`. Then restart anything long-lived that loaded the old code (MCP servers, `lattice dashboard`, `lattice sync --follow`) and refresh agent instructions: `lattice setup-claude --force` and `lattice setup-claude-skill --force`.
3. **The server.** Upgrade it the same way and restart it. The server reads `server.json` only at start, so restart it after editing `limits` too. Check `curl http://<server>/healthz` reports `"version":"0.2.2"`. (A client's `lattice remote status` proves nothing here: a `0.2.2` client against a `0.2.1` server prints no upgrade line either.)
4. **Enable the issue log, per project,** on the server host: `lattice server project config <slug> --set issues.enabled=true`. With the server running this is a journaled write at the same epoch and caches pick it up at their next sync; only with the server stopped does the next load start a new epoch. Check from a bound checkout: `lattice issue list` prints `0 issues`.
5. **Import a board that has issues** (section 14): the import copies issue media by default. Back up media with the server root (section 16).

**Rolling back to `0.2.1`** destroys nothing. The server keeps the issue data and media on disk but serves none of it. A `0.2.2` client then sees no issues, `lattice sync` moves its cached `issues/` files to `.lattice/cache/rescued/` (the message says "locally edited board file(s)"; they are not edits), and issue writes, including `issue file --evidence` and `issue attach`, answer that the server does not support the issue log or issue media and to upgrade it. **A `0.2.1` server refuses to start while `server.json` holds the three `max_issue_media_*` keys** under `limits` (it reports an unknown key), and `0.2.2` writes them at `server init`: remove them from `limits` before starting `0.2.1`. Upgrading the server again brings everything back.

- **`server.json` from an early v2 trial** may hold `"trusted_proxy": false`. The server refuses to start with that key; replace it with `"trusted_proxies": []`, or with your proxy's address (section 10).
- **Every machine that works in a bound checkout needs Lattice 2.** A recent Lattice 1 refuses a bound checkout with `BOUND_CHECKOUT`; an older one reads the read-only cache as if it were a board and fails on its first write. Upgrade it (section 5).
- The server and clients speak protocol 1. A client and server on different protocols refuse each other before any write.
- A client older than the server prints one line per command asking you to upgrade. When the server raises its minimum client version, older clients' writes fail with `CLIENT_TOO_OLD`, naming both versions.
- The issue path has two version gates. Operations from any client below `0.2.2` are refused before execution, even on projects without issues. Sync and stream return `CLIENT_TOO_OLD` before any data only when the project holds any synced issue file (even an ID map with no entries) and the client is below `0.2.2`; projects with no synced issue file keep their existing read behavior. Upgrade every client before importing or enabling an issue-bearing project so the new durable path is never exposed to an older reader.
- A newer client works against an older server until it uses something the server lacks: `UNKNOWN_OP` or `UNSUPPORTED_PARAM`, naming both versions. Upgrade the server.
- **Plugin operations** (from packages registering the `lattice.operations` entry point) run on a hosted board only when the plugin is installed on the server too. Install it into the server's environment (for example `uv tool install --with <plugin> 'lattice-tracker[server]'`) and restart.
- After upgrading clients, refresh installed agent instructions with `lattice setup-claude --force` and `lattice setup-claude-skill --force`.

## 20. Trust

- A token's `machine` label names the machine or seat it was **issued to**, not where a copy of it runs. Anyone holding the token is that person on that machine to the server. Store tokens like SSH keys; revoke and reissue when a machine is retired or a token leaks.
- **Revoking a token cannot retract copies of the board** its caches already hold. A cache is a full copy of the board as of its last sync.
- **Completion attestations are claims.** When a completion policy requires a reviewed commit to be reachable from the task's branch, the client checks that in its own git worktree and sends the result. The server records it with the attesting token; it cannot check git itself.
- The server runs no hooks, spawns no agents, and executes no board-configured command. Board hooks run only on clients that opt in with `run_board_hooks`.
- Any token may run `lattice context write` and ordinary task writes on the projects it can reach. Board configuration (workflow, reviews, hooks, policies) changes only through `lattice server project config` on the server host.

## 21. Troubleshooting

**`rm -rf` of a checkout fails.** A hosted cache's directories are read-only (0500), so `rm -rf`, `shutil.rmtree`, and `git clean -xdf` fail on them. Run `lattice cache clear` in the checkout first (add `--forget` to drop the routing too), then delete it. `lattice cache clear` refuses with `NOT_HOSTED` on a checkout that is not bound, so it can never delete a local board. It keeps `.lattice/cache/rescued/` and names it.

**The cache looks wrong or stale.** `lattice sync`. If it still looks wrong, `lattice cache clear` and run any command: the cache resyncs from the server.

**Error codes that only a client prints:**

| Code | Meaning | Remedy |
|---|---|---|
| `REMOTE_NOT_CONFIGURED` | The binding names an alias this machine has no remote for | Run the printed `lattice remote add <alias> <url> --token-env <VAR>`; ask your server admin for the URL and a token |
| `TOKEN_ENV_UNSET` | The token or a header comes from an environment variable that is unset or empty | Export the named variable in the shell (and in your agents' environment) |
| `SERVER_UNREACHABLE` | The server is not available: no request reached it, and nothing was written. `--json` details name the remote, URL, and OS error | Check the server (`curl <url>/healthz`) and the network; retry |
| `OUTCOME_UNKNOWN` | A write reached the server but no answer came back | `lattice remote op-status <op_id>` before retrying (section 18) |
| `PROXY_REJECTED` | Something other than a Lattice server answered: a redirect, a login page, an error page (a gateway's own 502, 503, or 504 counts as unreachable instead) | Fix the proxy: no redirects or login on `/v1/`; add service headers; let bot protection admit the `lattice/<version>` User-Agent (section 10) |
| `INSECURE_URL` | `http://` to a host that is not loopback | Use `https://`, or `--allow-plaintext` on an encrypted private network |
| `BINDING_CONFLICT` | The checkout's binding meets a local board (a `.lattice/` with board files and no cache), or a cache of a different remote or project. With `details.reason` `UNSAFE_CACHE_PATH`: `.lattice`, its `cache/`, or a runtime directory is a symbolic link or a file, which Lattice never writes through | A local board: follow section 14 to move it. Another project's cache: `lattice cache clear --forget`, then run the command again. An unsafe path: remove it (the message names it), then run any command |
| `BOARD_IS_CACHE` | Something tried to write the read-only cache directly. With `details.reason` `CACHE_ACCESS`: Lattice cannot read or write a path in the cache (its permissions were changed, or another user owns it, after a `sudo lattice` say) | Write through the command (`plan write`, `notes write`, `board write`); the cache is written only by sync. For `CACHE_ACCESS`: Lattice already gives its own directories back their 0700 when you own them; for anything else, restore the path's permissions, or `lattice cache clear` and then any command to rebuild the cache |
| `BOARD_IS_HOSTED` | Something tried to write a board the server owns, on the server host | Go through a bound checkout, or unload the project and use `--offline-maintenance` (section 7) |
| `CACHE_INCOMPLETE` | A sync was interrupted mid-update and the server is unreachable | `lattice sync` when the server is back |
| `BOUND_CHECKOUT` | Printed by Lattice 1, not 2: the checkout is bound to a server, which Lattice 1 cannot use | Install Lattice 2 on this machine (section 5); check with `lattice --version` |
| `NOT_HOSTED` | A hosted-only command (`cache clear`, `remote status`, `sync`) ran in a checkout that is not bound | Run it in a bound checkout |
| `HOSTED_UNSUPPORTED_PLATFORM` | Hosted mode on a platform other than macOS or Linux | Use macOS or Linux; local Lattice still works here |
| `LOCAL_ONLY` | A maintenance command on a hosted checkout | Run it on the server host with `--offline-maintenance` (section 7) |

**Error codes the server returns** (and the CLI prints unchanged):

| Code | Remedy |
|---|---|
| `UNAUTHENTICATED` | The token is missing, wrong, or revoked. Check the remote's token; ask for a new one |
| `FORBIDDEN` | The token cannot reach this project, or the change is admin-only configuration. `lattice server token grant <id> --project <slug>` |
| `ACTOR_NOT_PERMITTED` | The actor is outside the token's patterns. Use a permitted actor, or `lattice server token grant <id> --actor '<pattern>'` |
| `MISSING_ACTOR` | No `--actor` and the token has no single default actor. Pass `--actor` |
| `BOARD_BUSY` | The project stayed locked past `lock_timeout_seconds`. The client retries; if it persists, check the server log |
| `BOARD_UNAVAILABLE` | The project failed its integrity check or recovery, or is unloaded. The server log names the reason; repair offline and `lattice server project load <slug>` (section 7) |
| `RATE_LIMITED` | A per-token limit. The client waits and retries; raise `limits` in `server.json` if it is routine |
| `STORAGE_LOW` | The server's disk is below `min_free_disk_bytes`. Free space; reads keep working meanwhile |
| `PAYLOAD_TOO_LARGE` | The request or an attachment is over `max_body_bytes` (or custom event data over `max_event_data_bytes`) |
| `CLIENT_TOO_OLD`, `UNKNOWN_OP`, `UNSUPPORTED_PARAM`, `PROTOCOL_MISMATCH` | Version skew. Upgrade the older side (section 19) |
| `TASK_ERASED` | The task is erased. `lattice unerase <task> --reason "..."` restores it |
| `CONFLICT` with `OP_ID_REUSED` | An operation ID was reused with different arguments. The client never does this; a script calling the API directly must mint a fresh ID per request |

The HTTP API behind all of this is in [`api.md`](api.md), for scripts and agents without a Lattice install.

## 22. Cleaning up the trial

To end the quick-start trial: stop the server, clear the one cache still bound, and delete the directory.

```bash
kill "$(cat "$TRIAL/server.pid")"
cd "$TRIAL/legacy" && lattice cache clear --forget
cd "$HOME" && rm -rf "$TRIAL"
```

The server shuts down gracefully on SIGTERM. `lattice cache clear` is needed first because the cache's directories are read-only (section 21).
