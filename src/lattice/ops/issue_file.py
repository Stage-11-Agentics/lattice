"""``issue.file``: the ``lattice issue file`` command's rules (LAT-361, LAT-366, LAT-371).

Photos and videos travel as ``media`` items (see ``issue_common``). All of
them are decoded and checked against the limits before the issue sequence is
reserved; then blobs are staged before ``issue_filed`` and one
``issue_media_added`` per file are committed in one write.
"""

from __future__ import annotations

from dataclasses import dataclass

from lattice.core.events import create_issue_event
from lattice.core.ids import generate_issue_id
from lattice.core.issues import (
    CONFIDENCE_VALUES,
    apply_issue_event,
    format_issue_short_id,
    normalize_issue_description,
    split_title,
)
from lattice.ops import issue_common
from lattice.ops.base import CommonParams, OpContext, OpError, OpResult, operation
from lattice.storage.issues import issue_seq_reservation, issue_write_context, write_issue_events


@dataclass(frozen=True, kw_only=True)
class IssueFileParams(CommonParams):
    title: str
    description: str | None = None
    confidence: str | None = None
    evidence: tuple[str, ...] = ()
    source: str | None = None
    media: tuple[dict, ...] = ()

    def check(self) -> None:
        if not self.title.strip():
            raise OpError("VALIDATION_ERROR", "Issue title must not be empty.")
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
        title, overflow, _shortened = split_title(p.title)
        description = normalize_issue_description(p.description)
        if overflow:
            description = normalize_issue_description(
                overflow + (f"\n\n{description}" if description else "")
            )
        issue_id = generate_issue_id()
        with issue_write_context(ctx.lattice_dir, issue_id):
            media_events = issue_common.stage_media(ctx, issue_id, decoded, 1, p)
            try:
                with issue_seq_reservation(ctx.lattice_dir, issue_id) as (seq, commit_seq):
                    data: dict = {
                        "seq": seq,
                        "short_id": format_issue_short_id(ctx.config.get("project_code"), seq),
                        "title": title,
                    }
                    if description:
                        data["description"] = description
                    if p.confidence is not None:
                        data["confidence"] = p.confidence
                    if p.evidence:
                        data["evidence"] = list(p.evidence)
                    if p.source is not None:
                        data["source"] = p.source
                    events = [
                        create_issue_event(
                            "issue_filed", issue_id, ctx.actor, data, **p.provenance()
                        ),
                        *media_events,
                    ]
                    snapshot = None
                    for event in events:
                        snapshot = apply_issue_event(snapshot, event)
                    assert snapshot is not None
                    try:
                        write_issue_events(ctx.lattice_dir, issue_id, events, snapshot)
                    except BaseException:
                        if issue_common.issue_filing_event_committed(ctx.lattice_dir, issue_id):
                            commit_seq()
                        raise
                    commit_seq()
            except BaseException as failure:
                issue_common.cleanup_uncommitted_media(
                    ctx.lattice_dir, issue_id, media_events, failure
                )
                raise
        return issue_common.result(ctx, snapshot, events)
