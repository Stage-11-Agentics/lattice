"""``issue.comment``: add an issue comment or one reply."""

from __future__ import annotations

from dataclasses import dataclass

from lattice.core.comments import validate_comment_body, validate_comment_for_reply
from lattice.core.issues import issue_comment_events, issue_comments
from lattice.ops import issue_common
from lattice.ops.base import CommonParams, OpContext, OpError, OpResult, operation
from lattice.storage.issues import read_issue_events


@dataclass(frozen=True, kw_only=True)
class IssueCommentParams(CommonParams):
    issue: str
    text: str
    reply_to: str | None = None

    def check(self) -> None:
        try:
            validate_comment_body(self.text)
        except ValueError as exc:
            raise OpError("VALIDATION_ERROR", str(exc)) from exc


@operation("issue.comment")
class IssueComment:
    Params = IssueCommentParams

    def run(self, ctx: OpContext, p: IssueCommentParams) -> OpResult:
        issue_common.require_issue_log(ctx)
        issue_id = issue_common.resolve(ctx, p.issue)
        try:
            body = validate_comment_body(p.text)
        except ValueError as exc:
            raise OpError("VALIDATION_ERROR", str(exc)) from exc

        def decide(snapshot: dict) -> tuple[str, dict]:
            if p.reply_to is not None:
                existing = issue_comment_events(read_issue_events(ctx.lattice_dir, issue_id))
                try:
                    validate_comment_for_reply(existing, p.reply_to)
                except ValueError as exc:
                    raise OpError("VALIDATION_ERROR", str(exc)) from exc
            data = {"body": body}
            if p.reply_to is not None:
                data["parent_id"] = p.reply_to
            return "issue_comment_added", data

        snapshot, events = issue_common.append(ctx, issue_id, decide, p)
        view = issue_common.view(ctx, snapshot)
        materialized = issue_comments(events)
        comment_id = events[-1]["id"]
        comment = _find_comment(materialized, comment_id)
        if comment is None:
            raise AssertionError("the appended issue comment did not materialize")
        comment.pop("replies", None)
        return OpResult(
            events=events,
            value={**view, "comment": comment},
            idempotent=not events,
        )


def _find_comment(comments: list[dict], comment_id: str) -> dict | None:
    for comment in comments:
        if comment.get("id") == comment_id:
            return comment
        found = _find_comment(comment.get("replies", []), comment_id)
        if found is not None:
            return found
    return None
