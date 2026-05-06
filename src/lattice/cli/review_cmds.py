"""Review commands: code-review, plan-review, review-status."""

from __future__ import annotations

import json
import logging
import re
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
    cleanup_temp_files,
    read_review_state,
    run_merge_agent,
    run_single_review,
    run_triple_review,
    resolve_diff,
)
from lattice.templates import load_review_template


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Verdict parsing (LAT-210)
# ---------------------------------------------------------------------------

# Reviewers must terminate their artifact with a line like:
#     VERDICT: pass | fail-rework | fail-decision
# Last match wins so a "VERDICT: fail-rework" mid-document is overridden by the
# trailing convention.  Lowercase ``verdict:`` is intentionally rejected — only
# the uppercase form is the contract.
_VERDICT_RE = re.compile(
    r"^VERDICT:\s*(pass|fail-rework|fail-decision)[\s.!]*$",
    re.MULTILINE,
)


def parse_verdict(body: str) -> str:
    """Return the parsed verdict from a review artifact body.

    Returns one of ``"pass"``, ``"fail-rework"``, ``"fail-decision"``.  When
    no valid VERDICT line is present, returns ``"fail-rework"`` and logs a
    warning — defaulting toward "needs rework" rather than "passed".
    """
    matches = _VERDICT_RE.findall(body or "")
    if matches:
        return matches[-1]
    logger.warning("Review artifact missing VERDICT line; defaulting to fail-rework")
    return "fail-rework"


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
@click.option(
    "--headless",
    is_flag=True,
    default=False,
    help="Force the headless spawn backend (subprocess.run; no panes/windows).",
)
@click.option(
    "--backend",
    type=click.Choice(["cmux", "terminal", "headless"]),
    default=None,
    help="Force a specific spawn backend. Raises if unavailable instead of falling through.",
)
@click.option(
    "--escalate-on-fail",
    is_flag=True,
    default=False,
    help="Raise a needs_human alert when verdict is fail-decision (LAT-210).",
)
@click.option(
    "--escalate-after",
    is_flag=True,
    default=False,
    help="Raise a needs_human alert after the review regardless of verdict.",
)
@click.option(
    "--escalate-short",
    "escalate_short",
    default=None,
    help="Short text for the escalation alert.",
)
@click.option(
    "--escalate-long-from-file",
    "escalate_long_from_file",
    default=None,
    type=click.Path(exists=True),
    help="Read long alert text from a file.",
)
@common_options
def code_review(
    task_id: str,
    mode: str | None,
    base: str | None,
    headless: bool,
    backend: str | None,
    escalate_on_fail: bool,
    escalate_after: bool,
    escalate_short: str | None,
    escalate_long_from_file: str | None,
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

    if mode == "inline":
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

    review_artifact_id: str | None = None
    review_text: str | None = None

    if mode == "single":
        review_artifact_id, review_text = _run_single_and_store(
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
            headless=headless,
            backend_force=backend,
            return_text=True,
        )

    elif mode == "triple":
        art_ids, review_text = _run_triple_and_store(
            lattice_dir=lattice_dir,
            task_id=task_id,
            review_type="code-review",
            prompt=prompt,
            actor=actor,
            is_json=is_json,
            quiet=quiet,
            model=model,
            session=session,
            timeout=timeout,
            headless=headless,
            backend_force=backend,
            return_text=True,
        )
        review_artifact_id = art_ids[-1] if art_ids else None

    if escalate_on_fail or escalate_after:
        verdict = parse_verdict(review_text or "")
        should_escalate = bool(escalate_after) or (
            escalate_on_fail and verdict == "fail-decision"
        )
        if should_escalate:
            display_id = snapshot.get("short_id") or task_id
            short = escalate_short or (
                f"Code review verdict: {verdict}. Human input requested."
            )
            long_text: str | None = None
            if escalate_long_from_file:
                try:
                    long_text = Path(escalate_long_from_file).read_text(encoding="utf-8")
                except OSError as exc:
                    click.echo(f"Could not read --escalate-long-from-file: {exc}", err=True)
            _raise_needs_human_alert(
                lattice_dir,
                task_id,
                actor,
                is_json,
                short=short,
                long_text=long_text,
                evidence_ref=review_artifact_id,
                prompt=f"lattice clear {display_id} needs_human --answer '...'",
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
@click.option(
    "--headless",
    is_flag=True,
    default=False,
    help="Force the headless spawn backend (subprocess.run; no panes/windows).",
)
@click.option(
    "--backend",
    type=click.Choice(["cmux", "terminal", "headless"]),
    default=None,
    help="Force a specific spawn backend. Raises if unavailable instead of falling through.",
)
@common_options
def plan_review(
    task_id: str,
    mode: str | None,
    headless: bool,
    backend: str | None,
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
        mode = config.get("plan_review_mode", "inline")

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

    plan_review_mode = mode

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
            headless=headless,
            backend_force=backend,
        )
        if art_id and plan_approval == "human":
            short = (
                f"Plan reviewed (mode: {plan_review_mode}). Approve to continue."
            )
            display_id = snapshot.get("short_id") or task_id
            _raise_needs_human_alert(
                lattice_dir,
                task_id,
                actor,
                is_json,
                short=short,
                evidence_ref=art_id,
                prompt=f"lattice clear {display_id} needs_human --answer 'approved'",
            )

    elif mode == "triple":
        art_ids = _run_triple_and_store(
            lattice_dir=lattice_dir,
            task_id=task_id,
            review_type="plan-review",
            prompt=prompt,
            actor=actor,
            is_json=is_json,
            quiet=quiet,
            model=model,
            session=session,
            timeout=timeout,
            headless=headless,
            backend_force=backend,
        )
        if art_ids and plan_approval == "human":
            short = (
                f"Plan reviewed (mode: {plan_review_mode}). Approve to continue."
            )
            display_id = snapshot.get("short_id") or task_id
            # Last artifact id is the merged artifact (per _run_triple_and_store).
            _raise_needs_human_alert(
                lattice_dir,
                task_id,
                actor,
                is_json,
                short=short,
                evidence_ref=art_ids[-1] if art_ids else None,
                prompt=f"lattice clear {display_id} needs_human --answer 'approved'",
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
    headless: bool = False,
    backend_force: str | None = None,
    return_text: bool = False,
):
    """Run single-agent review, store artifact, print result.

    Returns ``artifact_id`` by default (str | None).  When *return_text* is
    True, returns ``(artifact_id, review_text)`` so callers can parse the
    verdict for escalation decisions.
    """
    click.echo(f"Running {review_type} (single mode)...")

    success, message, text = run_single_review(
        lattice_dir=lattice_dir,
        task_id=task_id,
        review_type=review_type,
        prompt_content=prompt,
        actor=actor,
        timeout=timeout,
        headless=headless,
        backend_force=backend_force,
    )

    if not success:
        click.echo(f"Review failed: {message}", err=True)
        cleanup_temp_files(task_id)
        return (None, None) if return_text else None

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

    return (art_id, text) if return_text else art_id


def _run_triple_and_store(
    *,
    lattice_dir: Path,
    task_id: str,
    review_type: str,
    prompt: str,
    actor: str | dict,
    is_json: bool,
    quiet: bool,
    model: str | None,
    session: str | None,
    timeout: int = 600,
    headless: bool = False,
    backend_force: str | None = None,
    return_text: bool = False,
):
    """Run triple-agent review, store artifacts, print result.

    Default return: ``list[str]`` of artifact IDs (last entry is the merged
    artifact when merge succeeds).  When *return_text* is True, returns
    ``(artifact_ids, merged_text_or_first_success)`` so callers can parse
    the verdict.
    """
    click.echo(f"Running {review_type} (triple mode — spawning claude, codex, gemini)...")

    overall_success, message, results = run_triple_review(
        lattice_dir=lattice_dir,
        task_id=task_id,
        review_type=review_type,
        prompt_content=prompt,
        actor=actor,
        timeout=timeout,
        headless=headless,
        backend_force=backend_force,
    )

    artifact_ids: list[str] = []

    # Store individual reviews
    for agent, success, text in results:
        if success:
            art_id = _attach_review_artifact(
                lattice_dir=lattice_dir,
                task_id=task_id,
                content=text,
                title=f"{review_type} ({agent})",
                role="review-individual",
                actor=actor,
                is_json=False,  # suppress per-artifact JSON noise
            )
            if art_id:
                artifact_ids.append(art_id)
                click.echo(f"  Stored {agent} review as {art_id}.")
        else:
            click.echo(f"  {agent} failed: {text}", err=True)

    # Merge if at least one succeeded
    if not overall_success:
        click.echo("All agents failed. No merged review produced.", err=True)
        cleanup_temp_files(task_id)
        return (artifact_ids, None) if return_text else artifact_ids

    click.echo("Merging reviews...")
    merge_success, merged_text = run_merge_agent(
        lattice_dir=lattice_dir,
        task_id=task_id,
        reviews=results,
        review_type=review_type,
        headless=headless,
        backend_force=backend_force,
    )

    if merge_success:
        role = "review" if review_type == "code-review" else "plan-review"
        merged_id = _attach_review_artifact(
            lattice_dir=lattice_dir,
            task_id=task_id,
            content=merged_text,
            title=f"{review_type} (merged)",
            role=role,
            actor=actor,
            is_json=False,
        )
        if merged_id:
            artifact_ids.append(merged_id)
            if is_json:
                click.echo(
                    json.dumps({"ok": True, "data": {"artifact_ids": artifact_ids}}, indent=2)
                )
            elif quiet:
                click.echo(merged_id)
            else:
                click.echo(f"Merged review stored as {merged_id} (role={role}).")
    else:
        click.echo(f"Merge agent failed: {merged_text}", err=True)

    cleanup_temp_files(task_id)
    if return_text:
        text_for_verdict = merged_text if merge_success else None
        if text_for_verdict is None:
            # Fall back to the first successful individual review.
            for _agent, _success, _text in results:
                if _success:
                    text_for_verdict = _text
                    break
        return artifact_ids, text_for_verdict
    return artifact_ids


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


def _raise_needs_human_alert(
    lattice_dir: Path,
    task_id: str,
    actor: str | dict,
    is_json: bool,
    *,
    short: str,
    long_text: str | None = None,
    evidence_ref: str | None = None,
    prompt: str | None = None,
) -> None:
    """Raise a ``needs_human`` alert (LAT-210 — replaces moving to that status).

    Used by plan-review when ``plan_approval == 'human'`` and by
    code-review's ``--escalate-on-fail`` / ``--escalate-after`` paths.
    """
    actor_flag = _actor_flag(actor)
    if actor_flag is None:
        click.echo("Cannot determine actor for alert raise.", err=True)
        return

    args = [
        "lattice",
        "raise",
        task_id,
        "needs_human",
        "--short",
        short,
        "--actor",
        actor_flag,
    ]
    if long_text is not None:
        args += ["--long", long_text]
    if evidence_ref is not None:
        args += ["--evidence-ref", evidence_ref]
    if prompt is not None:
        args += ["--prompt", prompt]

    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode == 0:
        click.echo("Raised needs_human alert.")
    else:
        click.echo(
            f"Note: Could not raise needs_human alert: {result.stderr.strip()}",
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
