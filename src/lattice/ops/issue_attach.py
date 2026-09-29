"""``issue.attach``: the ``lattice issue attach`` command's rules (LAT-366).

Adds photos and videos to an issue after it was filed, closed issues included.
All or nothing: every item must be accepted media within the limits. Content
the issue already holds is skipped; when every item is such a duplicate the
call is idempotent and writes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass

from lattice.core.issue_media import next_media_n, present_media, present_media_bytes
from lattice.core.issues import apply_issue_event
from lattice.ops import issue_common
from lattice.ops.base import OpContext, OpError, OpResult, operation
from lattice.ops.issue_common import IssueParams
from lattice.storage.issues import current_issue, issue_write_context, write_issue_events


@dataclass(frozen=True, kw_only=True)
class IssueAttachParams(IssueParams):
    media: tuple[dict, ...] = ()

    def check(self) -> None:
        if not self.media:
            raise OpError("VALIDATION_ERROR", "Give at least one photo or video to attach.")
        issue_common.check_media_items(self.media)


@operation("issue.attach")
class IssueAttach:
    Params = IssueAttachParams

    def run(self, ctx: OpContext, p: IssueAttachParams) -> OpResult:
        issue_common.require_issue_log(ctx)
        issue_id = issue_common.resolve(ctx, p.issue)
        decoded = issue_common.decode_media(p.media, ctx.config, nothing="Nothing was attached.")
        with issue_write_context(ctx.lattice_dir, issue_id):
            snapshot = current_issue(ctx.lattice_dir, issue_id)
            if snapshot is None:
                raise OpError("NOT_FOUND", f"Issue {p.issue} not found.")
            held = issue_common.held_hashes(present_media(snapshot))
            new = [d for d in decoded if not d.hashes & held]
            if not new:
                return issue_common.result(ctx, snapshot, [])
            issue_common.check_issue_total(
                ctx.config, issue_common.display(snapshot), present_media_bytes(snapshot), new
            )
            events = issue_common.stage_media(ctx, issue_id, new, next_media_n(snapshot), p)
            for event in events:
                snapshot = apply_issue_event(snapshot, event)
            write_issue_events(ctx.lattice_dir, issue_id, events, snapshot)
        return issue_common.result(ctx, snapshot, events)
