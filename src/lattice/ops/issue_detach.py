"""``issue.detach``: the ``lattice issue detach`` command's rules (LAT-366).

Removes one media file for good, with a reason, closed issues included. Under
the issue's lock, ``issue_media_removed`` is appended before the bytes are
deleted: hosted server transactions can roll back the event, but cannot restore
an unlink. If cleanup fails, retrying the command deletes the remaining bytes.
Media already removed is idempotent, and any bytes found at its paths (restored
by a merge) are deleted again.
"""

from __future__ import annotations

from dataclasses import dataclass

from lattice.core.events import create_issue_event
from lattice.core.ids import validate_id
from lattice.core.issues import apply_issue_event
from lattice.ops import issue_common
from lattice.ops.base import OpContext, OpError, OpResult, operation
from lattice.ops.issue_common import IssueParams
from lattice.storage.issue_media import delete_media_files
from lattice.storage.issues import current_issue, issue_write_context, write_issue_events


@dataclass(frozen=True, kw_only=True)
class IssueDetachParams(IssueParams):
    #: The media's ordinal on the issue (``2``) or its ``med_`` ID.
    media: str

    def check(self) -> None:
        if self.reason is None or not self.reason.strip():
            raise OpError("VALIDATION_ERROR", "--reason is required.")
        if _media_ref(self.media) is None:
            raise OpError(
                "VALIDATION_ERROR",
                f"Invalid media '{self.media}': give its number on the issue (e.g. 2) "
                "or its med_ ID.",
            )


def _media_ref(raw: str) -> int | str | None:
    """The ordinal or ``med_`` ID *raw* names, or ``None``."""
    text = raw.strip()
    if text.isascii() and text.isdigit():
        # Media ordinals are small, and int() rejects very long decimals on
        # recent Python versions. Keep overlong numbers as unmatched strings.
        return int(text) if len(text) <= 6 else text
    if text.lower().startswith("med_"):
        candidate = "med_" + text[4:].upper()
        if validate_id(candidate, "med"):
            return candidate
    return None


def _matches(entries: list[dict], raw: str) -> list[dict]:
    ref = _media_ref(raw)
    if isinstance(ref, int):
        return [e for e in entries if e.get("n") == ref]
    return [e for e in entries if e.get("id") == ref]


@operation("issue.detach")
class IssueDetach:
    Params = IssueDetachParams

    def run(self, ctx: OpContext, p: IssueDetachParams) -> OpResult:
        issue_common.require_issue_log(ctx)
        issue_id = issue_common.resolve(ctx, p.issue)
        with issue_write_context(ctx.lattice_dir, issue_id):
            snapshot = current_issue(ctx.lattice_dir, issue_id)
            if snapshot is None:
                raise OpError("NOT_FOUND", f"Issue {p.issue} not found.")
            name = issue_common.display(snapshot)
            matches = _matches(snapshot.get("media", []), p.media)
            if not matches:
                raise OpError("NOT_FOUND", f"Media {p.media} of {name} not found.")
            present = [e for e in matches if not e.get("removed")]
            if len(present) > 1:
                ids = ", ".join(e["id"] for e in present)
                raise OpError(
                    "CONFLICT",
                    f"{name} has more than one media {p.media} (two copies of the board "
                    f"were merged): {ids}. Detach one by its med_ ID.",
                    {"media": [e["id"] for e in present]},
                )
            if not present:
                for entry in matches:
                    delete_media_files(ctx.lattice_dir, issue_id, entry)
                return issue_common.result(ctx, snapshot, [])
            entry = present[0]
            event = create_issue_event(
                "issue_media_removed",
                issue_id,
                ctx.actor,
                {"media_id": entry["id"], "n": entry.get("n"), "reason": p.reason},
                **p.provenance(),
            )
            snapshot = apply_issue_event(snapshot, event)
            write_issue_events(ctx.lattice_dir, issue_id, [event], snapshot)
            delete_media_files(ctx.lattice_dir, issue_id, entry)
        return issue_common.result(ctx, snapshot, [event])
