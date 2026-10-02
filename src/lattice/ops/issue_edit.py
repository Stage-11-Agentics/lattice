"""``issue.edit``: deliberate title and description corrections."""

from __future__ import annotations

from dataclasses import dataclass

from lattice.core.issues import (
    check_edit_title,
    issue_title_description,
    normalize_issue_description,
)
from lattice.ops import issue_common
from lattice.ops.base import CommonParams, OpContext, OpError, OpResult, operation


@dataclass(frozen=True, kw_only=True)
class IssueEditParams(CommonParams):
    issue: str
    title: str | None = None
    description: str | None = None

    def check(self) -> None:
        if self.title is None and self.description is None:
            raise OpError(
                "VALIDATION_ERROR",
                "Nothing to edit: pass --title, --description or --description-file.",
            )
        if self.title is not None:
            try:
                check_edit_title(self.title)
            except ValueError as exc:
                raise OpError("VALIDATION_ERROR", str(exc)) from exc


@operation("issue.edit")
class IssueEdit:
    Params = IssueEditParams

    def run(self, ctx: OpContext, p: IssueEditParams) -> OpResult:
        issue_common.require_issue_log(ctx)
        issue_id = issue_common.resolve(ctx, p.issue)
        title = check_edit_title(p.title) if p.title is not None else None
        description = (
            normalize_issue_description(p.description) if p.description is not None else None
        )

        def decide(snapshot: dict) -> tuple[str, dict] | None:
            current_title, current_description = issue_title_description(snapshot)
            wanted = {
                "title": title if title is not None else current_title,
                "description": (description if description is not None else current_description),
            }
            current = {"title": current_title, "description": current_description}
            changed = [key for key in wanted if wanted[key] != current[key]]
            if not changed:
                return None

            # Materialize a legacy text-only issue in full on its first edit.
            fields = ("title", "description") if "title" not in snapshot else tuple(changed)
            data: dict = {}
            for key in fields:
                data[f"from_{key}"] = current[key]
                data[key] = wanted[key]
            return "issue_edited", data

        snapshot, events = issue_common.append(ctx, issue_id, decide, p)
        return OpResult(
            events=events,
            value=issue_common.view(ctx, snapshot),
            idempotent=not events,
        )
