"""CLI commands for the Lattice worker system."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

import click

from lattice.cli.main import cli


# ---------------------------------------------------------------------------
# Worker definition loading
# ---------------------------------------------------------------------------


def _find_workers_dir(lattice_dir: Path) -> Path | None:
    """Locate the ``workers/`` directory at the project root."""
    project_root = lattice_dir.parent
    workers_dir = project_root / "workers"
    if workers_dir.is_dir():
        return workers_dir
    return None


def _load_worker_def(workers_dir: Path, name: str) -> dict | None:
    """Load a worker definition by name (case-insensitive match on JSON ``name`` field)."""
    for path in workers_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if data.get("name", "").lower() == name.lower():
            return data
    return None


def _list_worker_defs(workers_dir: Path) -> list[dict]:
    """Load all worker definitions from the directory."""
    defs = []
    for path in sorted(workers_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if "name" in data:
            data["_path"] = str(path)
            defs.append(data)
    return defs


# ---------------------------------------------------------------------------
# Worktree management
# ---------------------------------------------------------------------------


def _create_worktree(project_root: Path, task_id: str, commit_sha: str, event_id: str) -> Path:
    """Create a detached git worktree at the given commit.

    Returns the worktree path.
    """
    short_task = task_id[-8:]
    short_sha = commit_sha[:8]
    short_eid = event_id[-8:]
    tmp_base = Path(tempfile.gettempdir())
    worktree_path = tmp_base / f"lattice-worker-{short_task}-{short_sha}-{short_eid}"

    # Prune stale worktree metadata before creating new ones
    subprocess.run(
        ["git", "worktree", "prune"],
        cwd=project_root,
        capture_output=True,
    )

    if worktree_path.exists():
        # Clean up stale worktree
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree_path)],
            cwd=project_root,
            capture_output=True,
        )

    result = subprocess.run(
        ["git", "worktree", "add", "--detach", str(worktree_path), commit_sha],
        cwd=project_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise click.ClickException(f"Failed to create worktree: {result.stderr.strip()}")

    return worktree_path


def _remove_worktree(project_root: Path, worktree_path: Path) -> None:
    """Remove a git worktree. Best-effort, never raises."""
    try:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree_path)],
            cwd=project_root,
            capture_output=True,
            timeout=10,
        )
    except Exception:
        pass


def _get_head_sha(project_root: Path) -> str:
    """Get current HEAD commit SHA."""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise click.ClickException("Not a git repository or git not available.")
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Worker process spawning
# ---------------------------------------------------------------------------


def _build_clean_env(
    lattice_dir: Path,
    task_id: str,
    commit_sha: str,
    worktree_path: Path | None,
    started_event_id: str,
) -> dict[str, str]:
    """Build a clean environment for the worker process.

    Strips Claude/Codex session variables to prevent nesting failures.
    """
    env = os.environ.copy()

    # Strip session variables that prevent nesting
    strip_prefixes = ("CLAUDECODE", "CLAUDE_", "CODEX_")
    keys_to_remove = [k for k in env if any(k.startswith(p) for p in strip_prefixes)]
    for k in keys_to_remove:
        del env[k]

    # Set worker-specific variables
    env["LATTICE_ROOT"] = str(lattice_dir)
    env["LATTICE_TASK_ID"] = task_id
    env["LATTICE_COMMIT_SHA"] = commit_sha
    env["LATTICE_STARTED_EVENT_ID"] = started_event_id
    if worktree_path:
        env["LATTICE_WORKTREE"] = str(worktree_path)

    return env


def _spawn_worker(
    worker_def: dict,
    env: dict[str, str],
    project_root: Path,
    worktree_path: Path | None,
    log_path: Path,
) -> int:
    """Spawn the worker as a detached top-level process.

    Returns the PID of the spawned process.
    """
    engine = worker_def.get("engine", "claude")
    prompt_file = worker_def.get("prompt_file", "")
    # Resolve prompt from worktree if available, otherwise project root
    base = worktree_path if worktree_path else project_root
    prompt_path = base / prompt_file

    if engine == "claude":
        cmd = [
            "claude", "-p",
            f"Read {prompt_path} and follow the instructions.",
            "--dangerously-skip-permissions",
        ]
    elif engine == "codex":
        cmd = [
            "codex", "exec", "--full-auto", "--skip-git-repo-check",
            f"Read {prompt_path} and follow the instructions. "
            f"Write output to {log_path.with_suffix('.output.md')}",
        ]
    elif engine == "gemini":
        cmd = [
            "gemini", "-m", "gemini-3-pro-preview", "--yolo",
            f"Read {prompt_path} and follow the instructions.",
        ]
    elif engine == "script":
        script = worker_def.get("command", "")
        cmd = ["bash", "-c", script]
    else:
        raise click.ClickException(f"Unknown engine: {engine}")

    cwd = str(worktree_path) if worktree_path else str(project_root)

    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(log_path, "w")

    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,  # Detach from parent process group
    )

    return proc.pid


# ---------------------------------------------------------------------------
# Compensating event helper
# ---------------------------------------------------------------------------


def _emit_process_failed(
    lattice_dir: Path,
    task_id: str,
    actor: str,
    worker_name: str,
    started_event_id: str,
    commit_sha: str,
    error: str,
) -> None:
    """Emit a process_failed event to compensate for a failed setup.

    Reads the current snapshot under lock, applies the event, and writes back.
    """
    from lattice.core.events import create_event, serialize_event
    from lattice.core.tasks import apply_event_to_snapshot, serialize_snapshot
    from lattice.storage.fs import atomic_write, jsonl_append
    from lattice.storage.locks import multi_lock

    failed_event = create_event(
        type="process_failed",
        task_id=task_id,
        actor=actor,
        data={
            "process_type": worker_name,
            "started_event_id": started_event_id,
            "commit_sha": commit_sha,
            "error": error,
        },
    )

    lock_keys = sorted([f"events_{task_id}", f"tasks_{task_id}"])
    with multi_lock(lattice_dir / "locks", lock_keys):
        snapshot_path = lattice_dir / "tasks" / f"{task_id}.json"
        snapshot = json.loads(snapshot_path.read_text())
        updated = apply_event_to_snapshot(snapshot, failed_event)
        event_path = lattice_dir / "events" / f"{task_id}.jsonl"
        jsonl_append(event_path, serialize_event(failed_event))
        atomic_write(snapshot_path, serialize_snapshot(updated))


# ---------------------------------------------------------------------------
# lattice worker list
# ---------------------------------------------------------------------------


@cli.group("worker")
def worker_group() -> None:
    """Manage Lattice workers."""


@worker_group.command("list")
def worker_list() -> None:
    """List registered worker definitions."""
    from lattice.cli.helpers import require_root

    lattice_dir = require_root(False)
    workers_dir = _find_workers_dir(lattice_dir)

    if not workers_dir:
        click.echo("No workers/ directory found at project root.")
        return

    defs = _list_worker_defs(workers_dir)
    if not defs:
        click.echo("No worker definitions found in workers/.")
        return

    for d in defs:
        engine = d.get("engine", "?")
        timeout = d.get("timeout_minutes", "?")
        worktree = "worktree" if d.get("worktree") else "in-place"
        click.echo(f"  {d['name']:25s} {engine:10s} {timeout}m  {worktree}  {d.get('description', '')}")


# ---------------------------------------------------------------------------
# lattice worker run <name> <task>
# ---------------------------------------------------------------------------


@worker_group.command("run")
@click.argument("worker_name")
@click.argument("task_id")
@click.option("--actor", default=None, help="Override worker actor identity.")
@click.option("--json", "is_json", is_flag=True, help="Output as JSON.")
def worker_run(worker_name: str, task_id: str, actor: str | None, is_json: bool) -> None:
    """Run a worker against a task.

    Spawns the worker as a detached process with worktree isolation.
    Posts process_started event before spawning, and the worker is
    responsible for posting process_completed/process_failed via
    ``lattice worker complete`` / ``lattice worker fail``.
    """
    from lattice.cli.helpers import (
        output_error,
        json_envelope,
        require_root,
        resolve_task_id,
        validate_actor_or_exit,
    )
    from lattice.core.events import create_event, serialize_event
    from lattice.core.tasks import apply_event_to_snapshot, serialize_snapshot
    from lattice.storage.fs import atomic_write, jsonl_append
    from lattice.storage.locks import multi_lock

    lattice_dir = require_root(is_json)
    project_root = lattice_dir.parent

    # Resolve task
    resolved = resolve_task_id(lattice_dir, task_id, is_json)
    if resolved is None:
        return
    task_id = resolved

    # Load worker definition (before lock — read-only, no race risk)
    workers_dir = _find_workers_dir(lattice_dir)
    if not workers_dir:
        output_error("No workers/ directory found at project root.", "NOT_FOUND", is_json)
    worker_def = _load_worker_def(workers_dir, worker_name)
    if not worker_def:
        available = _list_worker_defs(workers_dir)
        names = ", ".join(d["name"] for d in available) if available else "none"
        output_error(
            f"Worker '{worker_name}' not found. Available: {names}",
            "NOT_FOUND",
            is_json,
        )

    # Validate actor
    worker_actor = actor or worker_def.get("actor", "agent:worker")
    validate_actor_or_exit(worker_actor, is_json)

    # Capture commit SHA (before lock — git state, no race risk)
    try:
        commit_sha = _get_head_sha(project_root)
    except click.ClickException:
        commit_sha = "unknown"

    # Build process_started event
    started_event = create_event(
        type="process_started",
        task_id=task_id,
        actor=worker_actor,
        data={
            "process_type": worker_def["name"],
            "commit_sha": commit_sha,
            "timeout_minutes": worker_def.get("timeout_minutes", 10),
        },
    )
    started_event_id = started_event["id"]

    # --- Critical section: read snapshot, dedup check, write event + snapshot ---
    lock_keys = sorted([f"events_{task_id}", f"tasks_{task_id}"])
    with multi_lock(lattice_dir / "locks", lock_keys):
        snapshot_path = lattice_dir / "tasks" / f"{task_id}.json"
        if not snapshot_path.exists():
            output_error(f"Task {task_id} not found.", "NOT_FOUND", is_json)
        snapshot = json.loads(snapshot_path.read_text())

        # Dedup check: skip if same worker type already running for this task
        active = snapshot.get("active_processes", [])
        for proc in active:
            if proc.get("process_type") == worker_def["name"]:
                msg = (
                    f"Worker '{worker_def['name']}' is already running for this task "
                    f"(started: {proc.get('started_event_id')})"
                )
                if is_json:
                    click.echo(json_envelope(False, error={"code": "ALREADY_RUNNING", "message": msg}))
                else:
                    click.echo(msg, err=True)
                return

        # Apply event and persist atomically
        updated_snapshot = apply_event_to_snapshot(snapshot, started_event)
        event_path = lattice_dir / "events" / f"{task_id}.jsonl"
        jsonl_append(event_path, serialize_event(started_event))
        atomic_write(snapshot_path, serialize_snapshot(updated_snapshot))

    # --- Outside lock: setup and spawn ---

    # Create worktree if configured
    worktree_path = None
    if worker_def.get("worktree") and commit_sha != "unknown":
        try:
            worktree_path = _create_worktree(project_root, task_id, commit_sha, started_event_id)
        except click.ClickException as exc:
            # Compensate: emit process_failed since process_started is already persisted
            _emit_process_failed(
                lattice_dir, task_id, worker_actor, worker_def["name"],
                started_event_id, commit_sha, f"Worktree creation failed: {exc.message}",
            )
            output_error(f"Worktree creation failed: {exc.message}", "WORKTREE_ERROR", is_json)

    # Determine log path
    log_path = lattice_dir / "logs" / "workers" / f"{started_event_id}.log"

    # Build clean env and spawn
    env = _build_clean_env(lattice_dir, task_id, commit_sha, worktree_path, started_event_id)

    try:
        pid = _spawn_worker(worker_def, env, project_root, worktree_path, log_path)
    except Exception as exc:
        # Spawn failed — compensate with process_failed event
        _emit_process_failed(
            lattice_dir, task_id, worker_actor, worker_def["name"],
            started_event_id, commit_sha, str(exc),
        )
        if worktree_path:
            _remove_worktree(project_root, worktree_path)
        output_error(f"Worker spawn failed: {exc}", "SPAWN_ERROR", is_json)

    # Success — report
    result = {
        "worker": worker_def["name"],
        "task_id": task_id,
        "started_event_id": started_event_id,
        "commit_sha": commit_sha,
        "pid": pid,
        "log_path": str(log_path),
    }
    if worktree_path:
        result["worktree_path"] = str(worktree_path)

    if is_json:
        click.echo(json_envelope(True, data=result))
    else:
        short_id = snapshot.get("short_id", task_id[-8:])
        click.echo(
            f"Worker {worker_def['name']} started for {short_id} "
            f"(PID {pid}, commit {commit_sha[:8]})"
        )
        click.echo(f"  Log: {log_path}")
        if worktree_path:
            click.echo(f"  Worktree: {worktree_path}")
        click.echo(f"  Event: {started_event_id}")


# ---------------------------------------------------------------------------
# lattice worker complete <task> <started_event_id>
# ---------------------------------------------------------------------------


@worker_group.command("complete")
@click.argument("task_id")
@click.argument("started_event_id")
@click.option("--actor", default=None, help="Override actor identity.")
@click.option("--result", "result_text", default=None, help="Result summary.")
@click.option("--json", "is_json", is_flag=True, help="Output as JSON.")
def worker_complete(
    task_id: str,
    started_event_id: str,
    actor: str | None,
    result_text: str | None,
    is_json: bool,
) -> None:
    """Mark a worker process as completed.

    Workers call this as their final step to close the process lifecycle.
    Removes the entry from active_processes in the task snapshot.
    """
    from lattice.cli.helpers import (
        output_error,
        json_envelope,
        require_root,
        resolve_task_id,
        validate_actor_or_exit,
    )
    from lattice.core.events import create_event, serialize_event
    from lattice.core.tasks import apply_event_to_snapshot, serialize_snapshot
    from lattice.storage.fs import atomic_write, jsonl_append
    from lattice.storage.locks import multi_lock

    lattice_dir = require_root(is_json)

    resolved = resolve_task_id(lattice_dir, task_id, is_json)
    if resolved is None:
        return
    task_id = resolved

    worker_actor = actor or "agent:worker"
    validate_actor_or_exit(worker_actor, is_json)

    lock_keys = sorted([f"events_{task_id}", f"tasks_{task_id}"])
    with multi_lock(lattice_dir / "locks", lock_keys):
        snapshot_path = lattice_dir / "tasks" / f"{task_id}.json"
        if not snapshot_path.exists():
            output_error(f"Task {task_id} not found.", "NOT_FOUND", is_json)
        snapshot = json.loads(snapshot_path.read_text())

        # Find the process to complete
        active = snapshot.get("active_processes", [])
        match = [p for p in active if p.get("started_event_id") == started_event_id]
        if not match:
            output_error(
                f"No active process with started_event_id {started_event_id}.",
                "NOT_FOUND",
                is_json,
            )

        data: dict = {
            "process_type": match[0]["process_type"],
            "started_event_id": started_event_id,
            "commit_sha": match[0].get("commit_sha"),
        }
        if result_text:
            data["result"] = result_text

        event = create_event(
            type="process_completed",
            task_id=task_id,
            actor=worker_actor,
            data=data,
        )

        updated = apply_event_to_snapshot(snapshot, event)
        event_path = lattice_dir / "events" / f"{task_id}.jsonl"
        jsonl_append(event_path, serialize_event(event))
        atomic_write(snapshot_path, serialize_snapshot(updated))

    if is_json:
        click.echo(json_envelope(True, data={"event_id": event["id"]}))
    else:
        click.echo(f"Worker process completed (event: {event['id']})")


# ---------------------------------------------------------------------------
# lattice worker fail <task> <started_event_id>
# ---------------------------------------------------------------------------


@worker_group.command("fail")
@click.argument("task_id")
@click.argument("started_event_id")
@click.option("--actor", default=None, help="Override actor identity.")
@click.option("--error", "error_text", default="Unknown error", help="Error description.")
@click.option("--json", "is_json", is_flag=True, help="Output as JSON.")
def worker_fail(
    task_id: str,
    started_event_id: str,
    actor: str | None,
    error_text: str,
    is_json: bool,
) -> None:
    """Mark a worker process as failed.

    Workers call this when they encounter an unrecoverable error.
    Removes the entry from active_processes in the task snapshot.
    """
    from lattice.cli.helpers import (
        output_error,
        json_envelope,
        require_root,
        resolve_task_id,
        validate_actor_or_exit,
    )
    from lattice.core.events import create_event, serialize_event
    from lattice.core.tasks import apply_event_to_snapshot, serialize_snapshot
    from lattice.storage.fs import atomic_write, jsonl_append
    from lattice.storage.locks import multi_lock

    lattice_dir = require_root(is_json)

    resolved = resolve_task_id(lattice_dir, task_id, is_json)
    if resolved is None:
        return
    task_id = resolved

    worker_actor = actor or "agent:worker"
    validate_actor_or_exit(worker_actor, is_json)

    lock_keys = sorted([f"events_{task_id}", f"tasks_{task_id}"])
    with multi_lock(lattice_dir / "locks", lock_keys):
        snapshot_path = lattice_dir / "tasks" / f"{task_id}.json"
        if not snapshot_path.exists():
            output_error(f"Task {task_id} not found.", "NOT_FOUND", is_json)
        snapshot = json.loads(snapshot_path.read_text())

        # Find the process to fail (best-effort — allow even if not found)
        active = snapshot.get("active_processes", [])
        match = [p for p in active if p.get("started_event_id") == started_event_id]
        process_type = match[0]["process_type"] if match else "unknown"
        commit_sha = match[0].get("commit_sha") if match else None

        event = create_event(
            type="process_failed",
            task_id=task_id,
            actor=worker_actor,
            data={
                "process_type": process_type,
                "started_event_id": started_event_id,
                "commit_sha": commit_sha,
                "error": error_text,
            },
        )

        updated = apply_event_to_snapshot(snapshot, event)
        event_path = lattice_dir / "events" / f"{task_id}.jsonl"
        jsonl_append(event_path, serialize_event(event))
        atomic_write(snapshot_path, serialize_snapshot(updated))

    if is_json:
        click.echo(json_envelope(True, data={"event_id": event["id"]}))
    else:
        click.echo(f"Worker process failed (event: {event['id']})")


# ---------------------------------------------------------------------------
# lattice worker ps
# ---------------------------------------------------------------------------


@worker_group.command("ps")
@click.option("--json", "is_json", is_flag=True, help="Output as JSON.")
def worker_ps(is_json: bool) -> None:
    """Show tasks with active worker processes."""
    from lattice.cli.helpers import json_envelope, require_root

    lattice_dir = require_root(is_json)
    tasks_dir = lattice_dir / "tasks"

    active_tasks = []
    for snap_path in sorted(tasks_dir.glob("task_*.json")):
        try:
            snap = json.loads(snap_path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        procs = snap.get("active_processes", [])
        if procs:
            for p in procs:
                active_tasks.append({
                    "task_id": snap.get("id"),
                    "short_id": snap.get("short_id"),
                    "title": snap.get("title"),
                    "process_type": p.get("process_type"),
                    "started_at": p.get("started_at"),
                    "commit_sha": p.get("commit_sha"),
                    "started_event_id": p.get("started_event_id"),
                })

    if is_json:
        click.echo(json_envelope(True, data=active_tasks))
        return

    if not active_tasks:
        click.echo("No active worker processes.")
        return

    for t in active_tasks:
        sid = t.get("short_id") or t["task_id"][-8:]
        sha = (t.get("commit_sha") or "")[:8]
        click.echo(
            f"  {sid:12s} {t['process_type']:25s} {sha}  "
            f"started {t.get('started_at', '?')}"
        )
