"""``issue.file``: the ``lattice issue file`` command's rules (LAT-361, LAT-366).

Photos and videos travel as ``media`` items (see ``issue_common``). All of
them are decoded and checked against the limits before the issue number is
allocated; then the files are written and ``issue_filed`` plus one
``issue_media_added`` per file are appended in one write.
"""

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
    media: tuple[dict, ...] = ()

    def check(self) -> None:
        if not self.text.strip():
            raise OpError("VALIDATION_ERROR", "Issue text must not be empty.")
        if self.confidence is not None and self.confidence not in CONFIDENCE_VALUES:
            raise OpError(
                "VALIDATION_ERROR",
                f"Invalid confidence: '{self.confidence}'. "
                f"Valid values: {', '.join(CONFIDENCE_VALUES)}.",
            )
        issue_common.check_media_items(self.media)


@operation("issue.file")
class IssueFile:
    Params = IssueFileParams

    def run(self, ctx: OpContext, p: IssueFileParams) -> OpResult:
        issue_common.require_issue_log(ctx)
        decoded = issue_common.decode_media(p.media, ctx.config, nothing="Nothing was filed.")
        issue_common.check_issue_total(ctx.config, "The issue", 0, decoded)
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
        events = [create_issue_event("issue_filed", issue_id, ctx.actor, data, **p.provenance())]
        with issue_write_context(ctx.lattice_dir, issue_id):
            events += issue_common.stage_media(ctx, issue_id, decoded, 1, p)
            snapshot = None
            for event in events:
                snapshot = apply_issue_event(snapshot, event)
            assert snapshot is not None
            write_issue_events(ctx.lattice_dir, issue_id, events, snapshot)
        return issue_common.result(ctx, snapshot, events)
