"""``issue.file``: the ``lattice issue file`` command's rules (LAT-361)."""

from __future__ import annotations

from dataclasses import dataclass

from lattice.core.events import create_issue_event
from lattice.core.ids import generate_issue_id
from lattice.core.issues import CONFIDENCE_VALUES, apply_issue_event, format_issue_short_id
from lattice.ops import issue_common
from lattice.ops.base import CommonParams, OpContext, OpError, OpResult, operation
from lattice.storage.issues import allocate_issue_seq, issue_write_context, write_issue_events


@dataclass(frozen=True, kw_only=True)
class IssueFileParams(CommonParams):
    text: str
    confidence: str | None = None
    evidence: tuple[str, ...] = ()
    source: str | None = None

    def check(self) -> None:
        if not self.text.strip():
            raise OpError("VALIDATION_ERROR", "Issue text must not be empty.")
        if self.confidence is not None and self.confidence not in CONFIDENCE_VALUES:
            raise OpError(
                "VALIDATION_ERROR",
                f"Invalid confidence: '{self.confidence}'. "
                f"Valid values: {', '.join(CONFIDENCE_VALUES)}.",
            )


@operation("issue.file")
class IssueFile:
    Params = IssueFileParams

    def run(self, ctx: OpContext, p: IssueFileParams) -> OpResult:
        issue_common.require_issue_log(ctx)
        issue_id = generate_issue_id()
        seq = allocate_issue_seq(ctx.lattice_dir, issue_id)
        data: dict = {
            "seq": seq,
            "short_id": format_issue_short_id(ctx.config.get("project_code"), seq),
            "text": p.text,
        }
        if p.confidence is not None:
            data["confidence"] = p.confidence
        if p.evidence:
            data["evidence"] = list(p.evidence)
        if p.source is not None:
            data["source"] = p.source
        event = create_issue_event("issue_filed", issue_id, ctx.actor, data, **p.provenance())
        snapshot = apply_issue_event(None, event)
        with issue_write_context(ctx.lattice_dir, issue_id):
            write_issue_events(ctx.lattice_dir, issue_id, [event], snapshot)
        return issue_common.result(ctx, snapshot, [event])
