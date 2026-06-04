"""Review commands: code-review, plan-review, review-status."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import click

from lattice.cli.helpers import (
    common_options,
    load_project_config,
    output_error,
    read_snapshot_or_exit,
    require_actor,
    require_root,
    resolve_task_id,
)
from lattice.cli.main import cli
from lattice.core.review import (
    claim_review_state,
    cleanup_temp_files,
    clear_review_state,
    read_review_state,
    run_single_review,
    run_triple_review,
    resolve_diff,
    write_review_state,
)
from lattice.templates import load_review_template


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _claim_or_refuse(
    lattice_dir: Path,
    task_id: str,
    *,
    mode: str,
    review_type: str,
    triggered_by: str | None,
    is_json: bool,
) -> None:
    """Claim ``review_state`` for this review subprocess, or exit with a clear error.

    Implements the LAT-211 plan-review finding 3 ordering: read existing
    state *before* calling :func:`claim_review_state`. If the existing
    record names a parent that auto-fired this review (``auto_fired=True``
    AND ``started_by_pid == os.getppid()``) and ``--triggered-by`` was
    set, write a new record directly with ``auto_fired=True`` and our PID
    — bypassing the live-other-PID refusal that would otherwise fire,
    because the "other" PID is our spawning parent.

    Otherwise call ``claim_review_state(..., auto_fired=False)``: the
    standard stale-PID reclaim handles the typical case (parent exited
    before child reached this point), and the live-other-PID refusal
    handles real contention.

    Logs the friendly "review already in flight" message and exits 1
    (or returns the structured error for ``--json``) on contention.
    """
    existing = read_review_state(lattice_dir, task_id)
    if (
        triggered_by is not None
        and isinstance(existing, dict)
        and existing.get("auto_fired") is True
        and isinstance(existing.get("started_by_pid"), int)
        and existing["started_by_pid"] == os.getppid()
    ):
        adopted: dict[str, Any] = {
            "task_id": task_id,
            "mode": mode,
            "review_type": review_type,
            "started_at": _now_iso(),
            "started_by_pid": os.getpid(),
            "auto_fired": True,
            "agents": [],
        }
        write_review_state(lattice_dir, adopted)
        return

    claimed, holder = claim_review_state(
        lattice_dir,
        task_id,
        mode=mode,
        review_type=review_type,
        started_by_pid=os.getpid(),
        auto_fired=False,
    )
    if claimed:
        return

    holder = holder or {}
    holder_pid = holder.get("started_by_pid")
    holder_started = holder.get("started_at")
    holder_auto = holder.get("auto_fired")
    holder_review_type = holder.get("review_type") or review_type
    log_hint = ""
    daemon_log = lattice_dir / ".daemon" / f"auto-{holder_review_type}-{task_id}.log"
    if daemon_log.exists():
        log_hint = f"\n  log: {daemon_log}"
    msg = (
        f"A review is already in flight for this task "
        f"(pid {holder_pid}, started {holder_started}, "
        f"auto_fired={holder_auto}).  "
        f"Use 'lattice review-status {task_id}' to monitor, "
        f"or wait for it to complete."
        f"{log_hint}"
    )
    output_error(msg, "REVIEW_IN_FLIGHT", is_json)


# ---------------------------------------------------------------------------
# lattice code-review
# ---------------------------------------------------------------------------


@cli.command("code-review")
@click.argument("task_id")
@click.option(
    "--mode",
    type=click.Choice(["inline", "single", "triple"]),
    default=None,
    help="Review mode (overrides config). One of: inline, single, triple.",
)
@click.option("--base", default=None, help="Base git ref for diff (branch or commit).")
@common_options
def code_review(
    task_id: str,
    mode: str | None,
    base: str | None,
    model: str | None,
    session: str | None,
    output_json: bool,
    quiet: bool,
    triggered_by: str | None,
    on_behalf_of: str | None,
    provenance_reason: str | None,
) -> None:
    """Run a code review for a task against its git diff."""
    is_json = output_json

    lattice_dir = require_root(is_json)
    config = load_project_config(lattice_dir)

    task_id = resolve_task_id(lattice_dir, task_id, is_json)
    snapshot = read_snapshot_or_exit(lattice_dir, task_id, is_json)

    # Resolve mode: CLI flag > config > default
    if mode is None:
        mode = config.get("review_mode", "single")

    # Inline-mode contention check: even though inline never claims, refuse
    # if a non-inline review is in flight so the operator doesn't run two
    # reviews in parallel by accident.
    if mode == "inline":
        existing = read_review_state(lattice_dir, task_id)
        if isinstance(existing, dict):
            from lattice.core.review import pid_alive

            holder_pid = existing.get("started_by_pid")
            if isinstance(holder_pid, int) and holder_pid != os.getpid() and pid_alive(holder_pid):
                output_error(
                    (
                        "A review is already in flight for this task "
                        f"(pid {holder_pid}, started {existing.get('started_at')}, "
                        f"auto_fired={existing.get('auto_fired')})."
                    ),
                    "REVIEW_IN_FLIGHT",
                    is_json,
                )
        display_id = snapshot.get("short_id") or task_id
        msg = (
            f"[code-review] Mode is 'inline' — review is happening in-session.\n"
            f"Task: {display_id}. Review the diff and provide feedback directly."
        )
        if is_json:
            click.echo(
                json.dumps({"ok": True, "data": {"mode": "inline", "task_id": task_id}}, indent=2)
            )
        else:
            click.echo(msg)
        return

    actor = require_actor(is_json)

    # Claim the in-flight slot (or adopt the parent's claim when this is
    # an auto-fired child invoked with --triggered-by).
    _claim_or_refuse(
        lattice_dir,
        task_id,
        mode=mode,
        review_type="code-review",
        triggered_by=triggered_by,
        is_json=is_json,
    )

    # Resolve diff
    success, diff_or_err = resolve_diff(lattice_dir, task_id, snapshot, base=base)
    if not success:
        output_error(diff_or_err, "DIFF_RESOLUTION_FAILED", is_json)

    diff_content = diff_or_err

    if not diff_content.strip():
        output_error(
            "Diff is empty — no changes detected. Use --base <ref> if the diff range is wrong.",
            "EMPTY_DIFF",
            is_json,
        )

    # Load and fill review template
    template = load_review_template(lattice_dir, "code-review")
    plan_content = _read_plan(lattice_dir, task_id)
    project_context = _read_project_context(lattice_dir)
    prompt = template.format(
        task_id=snapshot.get("short_id") or task_id,
        task_description=snapshot.get("description") or snapshot.get("title", ""),
        plan_content=plan_content,
        project_context=project_context,
        diff_content=diff_content,
        output_path="<write output here>",
    )

    timeout = config.get("review_timeout_seconds", 600)

    if mode == "single":
        _run_single_and_store(
            lattice_dir=lattice_dir,
            task_id=task_id,
            review_type="code-review",
            prompt=prompt,
            actor=actor,
            role="review",
            is_json=is_json,
            quiet=quiet,
            model=model,
            session=session,
            timeout=timeout,
        )

    elif mode == "triple":
        _spawn_triple_pane(
            lattice_dir=lattice_dir,
            task_id=task_id,
            snapshot=snapshot,
            review_type="code-review",
            actor=actor,
            is_json=is_json,
            quiet=quiet,
            base=base,
        )


# ---------------------------------------------------------------------------
# lattice plan-review
# ---------------------------------------------------------------------------


@cli.command("plan-review")
@click.argument("task_id")
@click.option(
    "--mode",
    type=click.Choice(["inline", "single", "triple"]),
    default=None,
    help="Review mode (overrides config). One of: inline, single, triple.",
)
@common_options
def plan_review(
    task_id: str,
    mode: str | None,
    model: str | None,
    session: str | None,
    output_json: bool,
    quiet: bool,
    triggered_by: str | None,
    on_behalf_of: str | None,
    provenance_reason: str | None,
) -> None:
    """Run a plan review for a task against its plan file."""
    is_json = output_json

    lattice_dir = require_root(is_json)
    config = load_project_config(lattice_dir)

    task_id = resolve_task_id(lattice_dir, task_id, is_json)
    snapshot = read_snapshot_or_exit(lattice_dir, task_id, is_json)

    # Resolve mode: CLI flag > config > default
    if mode is None:
        mode = config.get("plan_review_mode", "single")

    # Read plan content (required regardless of mode)
    plan_path = lattice_dir / "plans" / f"{task_id}.md"
    if not plan_path.exists():
        output_error(
            f"No plan file found for task {task_id}. Write a plan first.",
            "PLAN_NOT_FOUND",
            is_json,
        )
    plan_content = plan_path.read_text(encoding="utf-8")

    if mode == "inline":
        existing = read_review_state(lattice_dir, task_id)
        if isinstance(existing, dict):
            from lattice.core.review import pid_alive

            holder_pid = existing.get("started_by_pid")
            if isinstance(holder_pid, int) and holder_pid != os.getpid() and pid_alive(holder_pid):
                output_error(
                    (
                        "A review is already in flight for this task "
                        f"(pid {holder_pid}, started {existing.get('started_at')}, "
                        f"auto_fired={existing.get('auto_fired')})."
                    ),
                    "REVIEW_IN_FLIGHT",
                    is_json,
                )
        display_id = snapshot.get("short_id") or task_id
        msg = (
            f"[plan-review] Mode is 'inline' — review is happening in-session.\n"
            f"Task: {display_id}. Review the plan and provide feedback directly."
        )
        if is_json:
            click.echo(
                json.dumps({"ok": True, "data": {"mode": "inline", "task_id": task_id}}, indent=2)
            )
        else:
            click.echo(msg)
        return

    actor = require_actor(is_json)

    _claim_or_refuse(
        lattice_dir,
        task_id,
        mode=mode,
        review_type="plan-review",
        triggered_by=triggered_by,
        is_json=is_json,
    )

    # Load and fill plan review template
    template = load_review_template(lattice_dir, "plan-review")
    project_context = _read_project_context(lattice_dir)
    prompt = template.format(
        task_id=snapshot.get("short_id") or task_id,
        task_description=snapshot.get("description") or snapshot.get("title", ""),
        plan_content=plan_content,
        project_context=project_context,
        output_path="<write output here>",
    )

    plan_approval = config.get("plan_approval", "auto")
    timeout = config.get("review_timeout_seconds", 600)

    if mode == "single":
        art_id = _run_single_and_store(
            lattice_dir=lattice_dir,
            task_id=task_id,
            review_type="plan-review",
            prompt=prompt,
            actor=actor,
            role="plan-review",
            is_json=is_json,
            quiet=quiet,
            model=model,
            session=session,
            timeout=timeout,
        )
        if art_id and plan_approval == "human":
            _flag_needs_human(lattice_dir, task_id, actor, is_json)

    elif mode == "triple":
        # The pane drives triage and the flag itself; the CLI does not
        # flag needs_human here even when plan_approval == "human".  The
        # pane sees the trident artifact first-hand and is the right
        # place to decide.
        _spawn_triple_pane(
            lattice_dir=lattice_dir,
            task_id=task_id,
            snapshot=snapshot,
            review_type="plan-review",
            actor=actor,
            is_json=is_json,
            quiet=quiet,
            base=None,
        )


# ---------------------------------------------------------------------------
# lattice review-status
# ---------------------------------------------------------------------------


@cli.command("review-status")
@click.argument("task_id")
@click.option("--json", "output_json", is_flag=True, help="Output structured JSON.")
def review_status(task_id: str, output_json: bool) -> None:
    """Show the status of an in-flight review for a task."""
    is_json = output_json

    lattice_dir = require_root(is_json)
    task_id = resolve_task_id(lattice_dir, task_id, is_json)

    state = read_review_state(lattice_dir, task_id)
    if state is None:
        # Check if review artifacts exist (review already completed)
        has_artifacts = _check_review_artifacts(lattice_dir, task_id)
        if is_json:
            data: dict[str, Any] = {"task_id": task_id, "status": "none"}
            if has_artifacts:
                data["note"] = "Review artifacts exist — review may have already completed."
            click.echo(json.dumps({"ok": True, "data": data}, indent=2))
        else:
            if has_artifacts:
                click.echo(
                    f"No in-flight review for {task_id}. Review artifacts exist — review may have already completed."
                )
            else:
                click.echo(
                    f"No in-flight review found for {task_id}. No review artifacts found either."
                )
        return

    now = datetime.now(timezone.utc)

    if is_json:
        # Enrich with elapsed times
        for agent in state.get("agents", []):
            agent["elapsed"] = _compute_elapsed_str(
                agent.get("started_at"), agent.get("finished_at"), now
            )
        state["elapsed"] = _compute_elapsed_str(state.get("started_at"), None, now)
        click.echo(json.dumps({"ok": True, "data": state}, indent=2))
        return

    # Human-readable
    overall_elapsed = _compute_elapsed_str(state.get("started_at"), None, now)
    click.echo(f"Review status for {task_id}")
    click.echo(f"  mode:         {state.get('mode', '?')}")
    click.echo(f"  review_type:  {state.get('review_type', '?')}")
    click.echo(f"  started_at:   {state.get('started_at', '?')}")
    click.echo(f"  elapsed:      {overall_elapsed}")
    if "auto_fired" in state:
        pid_part = (
            f" (started_by_pid {state['started_by_pid']})"
            if isinstance(state.get("started_by_pid"), int)
            else ""
        )
        click.echo(f"  auto_fired:   {state['auto_fired']}{pid_part}")
    elif isinstance(state.get("started_by_pid"), int):
        click.echo(f"  started_by_pid: {state['started_by_pid']}")
    agents = state.get("agents", [])
    if agents:
        click.echo("  agents:")
        for agent in agents:
            status = agent.get("status", "?")
            name = agent.get("name", "?")
            elapsed = _compute_elapsed_str(agent.get("started_at"), agent.get("finished_at"), now)
            art_id = agent.get("artifact_id") or ""
            suffix = f"  artifact={art_id}" if art_id else ""
            click.echo(f"    {name:<10} {status} ({elapsed}){suffix}")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _run_single_and_store(
    *,
    lattice_dir: Path,
    task_id: str,
    review_type: str,
    prompt: str,
    actor: str | dict,
    role: str,
    is_json: bool,
    quiet: bool,
    model: str | None,
    session: str | None,
    timeout: int = 600,
) -> str | None:
    """Run single-agent review, store artifact, print result. Returns artifact ID or None."""
    click.echo(f"Running {review_type} (single mode)...")

    success, message, text = run_single_review(
        lattice_dir=lattice_dir,
        task_id=task_id,
        review_type=review_type,
        prompt_content=prompt,
        actor=actor,
        timeout=timeout,
    )

    if not success:
        click.echo(f"Review failed: {message}", err=True)
        cleanup_temp_files(task_id)
        return None

    assert text is not None
    art_id = _attach_review_artifact(
        lattice_dir=lattice_dir,
        task_id=task_id,
        content=text,
        title=f"{review_type} ({role})",
        role=role,
        actor=actor,
        is_json=is_json,
    )

    cleanup_temp_files(task_id)

    if art_id:
        if is_json:
            click.echo(
                json.dumps({"ok": True, "data": {"artifact_id": art_id, "role": role}}, indent=2)
            )
        elif quiet:
            click.echo(art_id)
        else:
            click.echo(f"Review stored as artifact {art_id} (role={role}).")

    return art_id


def _spawn_triple_pane(
    *,
    lattice_dir: Path,
    task_id: str,
    snapshot: dict,
    review_type: str,
    actor: str | dict,
    is_json: bool,
    quiet: bool,
    base: str | None,
) -> None:
    """Spawn a c11 pane that runs the trident review. Fire-and-forget.

    Triple mode in LAT-218 onwards no longer runs three review agents in
    the CLI — it splits one new pane in the caller's c11 workspace and
    hands off to ``/trident-{code|plan}-review``. The pane owns trident,
    artifact storage, triage, and the task-status advance.

    On failure (notably: not running inside c11) this releases the
    in-flight review claim that ``_claim_or_refuse`` made earlier, prints
    a clear error, and exits non-zero.
    """
    short_id = snapshot.get("short_id") or task_id
    success, message = run_triple_review(
        lattice_dir=lattice_dir,
        task_id=task_id,
        review_type=review_type,
        actor=actor,
        base=base,
        short_id=short_id,
        worktree=lattice_dir.parent,
    )

    if not success:
        # Release the parent claim so retries aren't blocked by a phantom record.
        clear_review_state(lattice_dir, task_id)
        if is_json:
            click.echo(
                json.dumps(
                    {"ok": False, "error": {"code": "TRIPLE_SPAWN_FAILED", "message": message}},
                    indent=2,
                ),
                err=True,
            )
        else:
            click.echo(message, err=True)
        raise click.exceptions.Exit(code=1)

    if is_json:
        click.echo(
            json.dumps(
                {"ok": True, "data": {"mode": "triple", "task_id": task_id, "message": message}},
                indent=2,
            )
        )
    elif quiet:
        click.echo(message)
    else:
        click.echo(message)


def _attach_review_artifact(
    *,
    lattice_dir: Path,
    task_id: str,
    content: str,
    title: str,
    role: str,
    actor: str | dict,
    is_json: bool,
) -> str | None:
    """Write content to a temp file and attach it as a Lattice artifact.

    Returns the artifact ID, or None on failure.
    """
    actor_flag = _actor_flag(actor)
    if actor_flag is None:
        click.echo("Cannot determine actor for artifact attachment.", err=True)
        return None

    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".md",
        prefix="lattice-review-",
        delete=False,
        encoding="utf-8",
    ) as f:
        f.write(content)
        tmp_path = f.name

    try:
        result = subprocess.run(
            [
                "lattice",
                "attach",
                task_id,
                tmp_path,
                "--title",
                title,
                "--role",
                role,
                "--actor",
                actor_flag,
                "--quiet",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return result.stdout.strip() or None
        click.echo(
            f"Failed to attach artifact: {result.stderr.strip() or result.stdout.strip()}",
            err=True,
        )
        return None
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def _actor_flag(actor: str | dict) -> str | None:
    """Extract a flat actor string for --actor flag."""
    if isinstance(actor, str):
        return actor
    if isinstance(actor, dict):
        return actor.get("name") or actor.get("base_name")
    return None


def _read_plan(lattice_dir: Path, task_id: str) -> str:
    plan_path = lattice_dir / "plans" / f"{task_id}.md"
    if plan_path.exists():
        try:
            return plan_path.read_text(encoding="utf-8")
        except OSError:
            pass
    return "(no plan found)"


def _read_project_context(lattice_dir: Path) -> str:
    """Try to read project context from CLAUDE.md or context.md."""
    for name in ("CLAUDE.md", "context.md", "README.md"):
        candidate = lattice_dir.parent / name
        if candidate.exists():
            try:
                text = candidate.read_text(encoding="utf-8")
                return text[:3000]  # cap to avoid bloating prompt
            except OSError:
                pass
    return "(no project context found)"


def _flag_needs_human(
    lattice_dir: Path,
    task_id: str,
    actor: str | dict,
    is_json: bool,
) -> None:
    """Set the needs_human flag when plan_approval == 'human'.

    The task keeps its current status (planned); the flag signals that a
    human must approve the plan before work proceeds.
    """
    actor_flag = _actor_flag(actor)
    if actor_flag is None:
        click.echo("Cannot determine actor for needs-human flag.", err=True)
        return

    result = subprocess.run(
        [
            "lattice",
            "needs-human",
            task_id,
            "Plan-review complete — awaiting human plan approval",
            "--actor",
            actor_flag,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        click.echo("needs_human flag set (plan_approval=human).")
    elif (
        "FLAG_ALREADY_SET" in result.stderr or "already has the needs_human flag" in result.stderr
    ):
        # Benign: human attention is already requested (e.g. plan-level
        # rework re-fired the review while the earlier flag still stands).
        click.echo("needs_human flag already set (plan_approval=human).")
    else:
        click.echo(
            f"Note: Could not set needs_human flag: {result.stderr.strip()}",
            err=True,
        )


def _compute_elapsed_str(
    started_at: str | None,
    finished_at: str | None,
    now: datetime,
) -> str:
    """Compute a human-readable elapsed time string."""
    if not started_at:
        return "?"
    try:
        start = datetime.fromisoformat(started_at)
    except (ValueError, TypeError):
        return "?"
    end = now
    if finished_at:
        try:
            end = datetime.fromisoformat(finished_at)
        except (ValueError, TypeError):
            pass
    delta = end - start
    total_seconds = int(delta.total_seconds())
    if total_seconds < 0:
        return "0s"
    minutes, seconds = divmod(total_seconds, 60)
    if minutes > 0:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def _check_review_artifacts(lattice_dir: Path, task_id: str) -> bool:
    """Check if any review artifacts exist for a task."""
    artifacts_dir = lattice_dir / "artifacts" / task_id
    if not artifacts_dir.exists():
        return False
    # Check for any files with review-related roles
    for f in artifacts_dir.iterdir():
        if f.suffix == ".json":
            try:
                meta = json.loads(f.read_text(encoding="utf-8"))
                role = meta.get("role", "")
                if "review" in role:
                    return True
            except (json.JSONDecodeError, OSError):
                continue
    return False
