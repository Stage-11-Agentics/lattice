"""``task.complete``: the ``lattice complete`` command's rules.

A review comment, a move to ``review`` (unless already there, or the task
sits past review with a direct edge to ``done``), a review artifact, and a
move to ``done``, in one mutation. Everything is validated
before any file is written (SPEC §3.8): the review payload and its metadata
are written under the task lock only once every rule has passed, just before
the events that reference them are appended, so a refused completion leaves
nothing behind.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from lattice.core.artifacts import create_artifact_metadata, serialize_artifact
from lattice.core.comments import validate_comment_body
from lattice.core.config import (
    contains_control_characters,
    get_configured_roles,
    get_valid_transitions,
    is_terminal_status,
    validate_completion_policy,
    validate_status,
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
_HTTP_SCHEME_RE = re.compile(r"^https?://", re.IGNORECASE)
_PR_NUMBER_RE = re.compile(r"#[1-9][0-9]*")
_MAX_VIA_LENGTH = 256
_VIA_FORMS = "<task ID>, #<N>, or http(s)://<host>/…"


def _via_error(value: object, detail: str = "is not a valid bundle reference") -> OpError:
    return OpError(
        "VALIDATION_ERROR",
        f"Invalid --via value {value!r}: {detail}. Accepted forms are {_VIA_FORMS}.",
    )


def normalize_via(value: object) -> tuple[str, str]:
    """Classify and normalize a bundle reference without reading a board."""
    if not isinstance(value, str):
        raise _via_error(value, "must be text")
    if not value or len(value) > _MAX_VIA_LENGTH:
        raise _via_error(value, f"must contain 1 to {_MAX_VIA_LENGTH} characters")
    if contains_control_characters(value):
        raise _via_error(value, "must not contain control characters")
    if value.startswith("#"):
        if not _PR_NUMBER_RE.fullmatch(value):
            raise _via_error(value, "pull request numbers must use ASCII #N syntax")
        return "pull_request", value
    if _HTTP_SCHEME_RE.match(value):
        if any(not 0x21 <= ord(char) <= 0x7E for char in value):
            raise _via_error(value, "HTTP(S) references must contain printable ASCII only")
        if "?" in value or "#" in value:
            raise _via_error(value, "HTTP(S) references cannot contain a query or fragment")
        try:
            parts = urlsplit(value)
            host = parts.hostname
            port = parts.port
        except ValueError as exc:
            raise _via_error(value, "HTTP(S) reference must have a valid host and port") from exc
        if (
            parts.scheme.lower() not in {"http", "https"}
            or not parts.netloc
            or "@" in parts.netloc
            or not host
        ):
            raise _via_error(value, "HTTP(S) reference must have a host and no userinfo")
        del port  # access above validates its syntax and range; retain the input spelling.
        if parts.netloc.startswith("["):
            close = parts.netloc.find("]")
            if close < 0:
                raise _via_error(value, "HTTP(S) reference must have a valid host")
            authority = f"[{host.lower()}]{parts.netloc[close + 1 :]}"
            suffix = parts.netloc[close + 1 :]
            if suffix and not re.fullmatch(r":[0-9]+", suffix):
                raise _via_error(value, "HTTP(S) reference must have a valid port")
        else:
            raw_host, separator, raw_port = parts.netloc.partition(":")
            if parts.netloc.count(":") > 1 or (
                separator and not re.fullmatch(r"[0-9]+", raw_port)
            ):
                raise _via_error(value, "HTTP(S) reference must have a valid host and port")
            authority = raw_host.lower() + (f":{raw_port}" if separator else "")
        return "pull_request", urlunsplit((parts.scheme.lower(), authority, parts.path, "", ""))
    return "task", value


@dataclass(frozen=True, kw_only=True)
class CompleteParams(CommonParams):
    task: str
    review: str | None = None
    review_file: str | None = None  # the text of --review-file PATH
    via: str | None = None

    def check(self) -> None:
        if self.via is not None:
            normalize_via(self.via)
        if self.review is not None and self.review_file is not None:
            raise OpError(
                "VALIDATION_ERROR", "Provide either --review or --review-file, not both."
            )
        if self.review is None and self.review_file is None:
            raise OpError(
                "VALIDATION_ERROR", "Provide review findings as --review or via --review-file."
            )


def completion_path(result: OpResult) -> str:
    """The statuses ``complete`` moved the task through, as ``a -> b -> done``."""
    moves = [e["data"] for e in result.events if e["type"] == "status_changed"]
    return " -> ".join([moves[0]["from"], *(m["to"] for m in moves)])


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

        bundle_event: dict | None = None
        bundle_label: str | None = None
        if p.via is not None:
            via_kind, via_reference = normalize_via(p.via)
            if via_kind == "pull_request":
                bundle_event = {"kind": "pull_request", "reference": via_reference}
                bundle_label = via_reference
            else:
                try:
                    bundle_task_id = ctx.resolve_task(p.via)
                except OpError as exc:
                    raise _via_error(p.via, "task target could not be resolved") from exc
                if bundle_task_id == task_id:
                    raise _via_error(p.via, "cannot refer to the task being completed")
                target = read_task_authority(ctx.lattice_dir, bundle_task_id, allow_missing=True)
                if (
                    target is None
                    or target.location not in {"active", "archived"}
                    or target.snapshot.get("tombstoned")
                ):
                    raise _via_error(p.via, "task target was not found or is erased")
                short_id = target.snapshot.get("short_id")
                bundle_event = {
                    "kind": "task",
                    "id": bundle_task_id,
                    "short_id": short_id,
                }
                bundle_label = short_id or bundle_task_id

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
            if p.via is not None and (
                current_status == "done"
                or not validate_status(config, current_status)
                or is_terminal_status(config, current_status)
            ):
                raise OpError.task_state(
                    "INVALID_TRANSITION",
                    f"Cannot complete via {bundle_label}: task is in terminal or unknown "
                    f"status '{current_status}'.",
                    snapshot,
                )
            # A status past review (in_validation after a merge-first flow)
            # closes directly when the workflow allows it; otherwise the
            # completion goes through review and needs the review -> done edge.
            direct_to_done = (
                not already_in_review
                and not validate_transition(config, current_status, "review")
                and validate_transition(config, current_status, "done")
            )
            needs_force = (
                not already_in_review
                and not direct_to_done
                and not validate_transition(config, current_status, "review")
            )
            if needs_force and p.via is None:
                valid_targets = get_valid_transitions(config, current_status)
                valid_list = ", ".join(valid_targets) if valid_targets else "(none)"
                raise OpError.task_state(
                    "INVALID_TRANSITION",
                    f"Cannot complete: task is in '{current_status}' which cannot "
                    f"transition to review. Valid transitions: {valid_list}.",
                    snapshot,
                )
            if not direct_to_done and not validate_transition(config, "review", "done"):
                raise OpError.task_state(
                    "INVALID_TRANSITION",
                    "Cannot complete: no transition from review to done in workflow.",
                    snapshot,
                )
            events = [event("comment_added", {"body": review_text, "role": "review"})]
            if not already_in_review and not direct_to_done:
                move_data: dict = {"from": current_status, "to": "review"}
                if needs_force:
                    assert bundle_label is not None
                    move_data["force"] = True
                    move_data["reason"] = f"Complete through bundle {bundle_label}."
                events.append(event("status_changed", move_data))
            events.append(event("artifact_attached", {"artifact_id": art_id, "role": "review"}))
            working = snapshot
            for proposed in events:
                working = apply_event_to_snapshot(working, proposed)
            # The policy judges the prospective post-transition snapshot; the
            # attestation must cover the payload this completion attaches.
            attested = attested_review_commits(ctx, snapshot, policy, prospective=[review_payload])
            policy_ok, policy_failures = validate_completion_policy(
                config,
                working,
                "done",
                events=(*context.events, *events),
                reachable_review_commits=attested,
            )
            if not policy_ok:
                raise OpError.task_state(
                    "COMPLETION_BLOCKED",
                    f"Completion policy not satisfied: {'; '.join(policy_failures)}.",
                    snapshot,
                )
            done_data: dict = {
                "from": current_status if direct_to_done else "review",
                "to": "done",
            }
            if bundle_event is not None:
                done_data["via"] = bundle_event
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
