"""``task.complete``: the ``lattice complete`` command's rules.

A review comment, a move to ``review`` (unless already there), a review
artifact, and a move to ``done``, in one mutation. Everything is validated
before any file is written (SPEC §3.8): the review payload and its metadata
are written under the task lock only once every rule has passed, just before
the events that reference them are appended, so a refused completion leaves
nothing behind.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from lattice.core.artifacts import create_artifact_metadata, serialize_artifact
from lattice.core.comments import validate_comment_body
from lattice.core.config import (
    get_configured_roles,
    get_valid_transitions,
    validate_completion_policy,
    validate_transition,
)
from lattice.core.events import create_event, utc_now
from lattice.core.ids import generate_artifact_id
from lattice.core.tasks import apply_event_to_snapshot
from lattice.ops.attestation_check import attested_review_commits
from lattice.ops.base import CommonParams, OpContext, OpError, OpResult, operation
from lattice.storage.fs import atomic_write, ensure_artifact_dirs
from lattice.storage.operations import TaskMutationDecision, read_task_authority

_COMMIT_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


@dataclass(frozen=True, kw_only=True)
class CompleteParams(CommonParams):
    task: str
    review: str | None = None
    review_file: str | None = None  # the text of --review-file PATH

    def check(self) -> None:
        if self.review is not None and self.review_file is not None:
            raise OpError(
                "VALIDATION_ERROR", "Provide either --review or --review-file, not both."
            )
        if self.review is None and self.review_file is None:
            raise OpError(
                "VALIDATION_ERROR", "Provide review findings as --review or via --review-file."
            )


def prior_status(result: OpResult) -> str:
    """The status the task had before ``complete`` moved it (for the summary line)."""
    first_move = next(e for e in result.events if e["type"] == "status_changed")
    return first_move["data"]["from"]


@operation("task.complete")
class Complete:
    """Attestations (``Caller.attestations``, SPEC §3.4): ``review_head``, the
    caller's ``HEAD``, written as the review payload's marker when the done
    policy requires a reachable review commit; ``reachable_review_commits``,
    checked against the task and that payload."""

    Params = CompleteParams

    def run(self, ctx: OpContext, p: CompleteParams) -> OpResult:
        config = ctx.config
        text = p.review if p.review is not None else p.review_file
        assert text is not None
        task_id = ctx.resolve_task(p.task)
        if not validate_transition(config, "review", "done"):
            raise OpError(
                "INVALID_TRANSITION",
                "Cannot complete: no transition from review to done in workflow.",
            )
        configured_roles = get_configured_roles(config)
        if configured_roles and "review" not in configured_roles:
            raise OpError(
                "INVALID_ROLE",
                f"Unknown role: 'review'. Valid roles: {', '.join(sorted(configured_roles))}.",
            )
        try:
            review_text = validate_comment_body(text)
        except ValueError as exc:
            raise OpError("VALIDATION_ERROR", str(exc)) from exc

        review_payload = review_text
        policy = config.get("workflow", {}).get("completion_policies", {}).get("done", {})
        if policy.get("require_reachable_review_commit"):
            head = ctx.caller.attestations.get("review_head")
            if head is None:
                raise OpError("COMPLETION_BLOCKED", "Not inside a git worktree.")
            if not isinstance(head, str) or not _COMMIT_RE.fullmatch(head):
                raise OpError("VALIDATION_ERROR", f"Malformed review_head attestation: {head!r}.")
            review_payload = f"Lattice-Reviewed-Commit: {head}\n\n{review_text}"

        shared_ts = utc_now()
        art_id = generate_artifact_id()
        payload_file = f"{art_id}.md"
        actor = ctx.actor
        actor_str = actor if isinstance(actor, str) else actor.get("name", "unknown")
        metadata = create_artifact_metadata(
            art_id,
            "note",
            "Review findings",
            created_by=actor_str,
            created_at=shared_ts,
            summary=review_text[:200] if len(review_text) > 200 else review_text,
            model=p.model,
            payload_file=payload_file,
            content_type="text/markdown",
            size_bytes=len(review_payload.encode("utf-8")),
        )

        def event(type_: str, data: dict) -> dict:
            return create_event(type_, task_id, actor, data, ts=shared_ts, **p.provenance())

        def decide(context):  # noqa: ANN001, ANN202
            snapshot = context.snapshot
            current_status = snapshot["status"]
            already_in_review = current_status == "review"
            if not already_in_review and not validate_transition(config, current_status, "review"):
                valid_targets = get_valid_transitions(config, current_status)
                valid_list = ", ".join(valid_targets) if valid_targets else "(none)"
                raise OpError.task_state(
                    "INVALID_TRANSITION",
                    f"Cannot complete: task is in '{current_status}' which cannot "
                    f"transition to review. Valid transitions: {valid_list}.",
                    snapshot,
                )
            events = [event("comment_added", {"body": review_text, "role": "review"})]
            if not already_in_review:
                events.append(event("status_changed", {"from": current_status, "to": "review"}))
            events.append(event("artifact_attached", {"artifact_id": art_id, "role": "review"}))
            working = snapshot
            for proposed in events:
                working = apply_event_to_snapshot(working, proposed)
            # The policy judges the prospective post-transition snapshot; the
            # attestation must cover the payload this completion attaches.
            attested = attested_review_commits(ctx, snapshot, policy, prospective=[review_payload])
            policy_ok, policy_failures = validate_completion_policy(
                config, working, "done", reachable_review_commits=attested
            )
            if not policy_ok:
                raise OpError.task_state(
                    "COMPLETION_BLOCKED",
                    f"Completion policy not satisfied: {'; '.join(policy_failures)}.",
                    snapshot,
                )
            done_data: dict = {"from": "review", "to": "done"}
            if attested is not None:
                done_data["attestations"] = {"reachable_review_commits": attested}
            events.append(event("status_changed", done_data))

            # Every rule has passed: write the payload and its metadata now,
            # before the events that reference them are appended.
            lattice_dir = ctx.lattice_dir
            ensure_artifact_dirs(lattice_dir)
            atomic_write(lattice_dir / "artifacts" / "payload" / payload_file, review_payload)
            atomic_write(
                lattice_dir / "artifacts" / "meta" / f"{art_id}.json",
                serialize_artifact(metadata),
            )
            return TaskMutationDecision(events=events, value=current_status)

        # A task with no log at all is absent, not corrupt (an archived one is
        # reported by the mutation with its placement message).
        if read_task_authority(ctx.lattice_dir, task_id, allow_missing=True) is None:
            raise OpError("NOT_FOUND", f"Task {task_id} not found.")
        result = ctx.mutate(task_id, decide)
        return OpResult(task=result.snapshot, events=result.appended_events, value=result.snapshot)
