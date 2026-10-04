"""``issue.file``: the ``lattice issue file`` command's rules (LAT-361, LAT-366, LAT-371).

Photos and videos travel as ``media`` items (see ``issue_common``). All of
them are decoded and checked against the limits before the issue sequence is
reserved; then blobs are staged before ``issue_filed`` and one
``issue_media_added`` per file are committed in one write.
"""

from __future__ import annotations

import unicodedata
from contextlib import nullcontext
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
from lattice.storage.issues import (
    issue_seq_reservation,
    issue_write_context,
    list_issue_snapshots,
    source_ref_lock,
    write_issue_events,
)


def normalize_source_ref(source: str | None, source_ref: str | None) -> tuple[str, str] | None:
    """Validate and normalize the source identity when a caller supplies a reference."""
    if source_ref is None:
        return None
    if source is None:
        raise OpError("VALIDATION_ERROR", "source_ref requires a nonempty source.")
    normalized_source = source.strip()
    normalized_ref = source_ref.strip()
    if not normalized_source:
        raise OpError("VALIDATION_ERROR", "source must not be blank when source_ref is set.")
    if not normalized_ref:
        raise OpError("VALIDATION_ERROR", "source_ref must not be blank.")
    if any(unicodedata.category(char) == "Cc" for char in normalized_source + normalized_ref):
        raise OpError(
            "VALIDATION_ERROR", "source and source_ref may not contain control characters."
        )
    if len(normalized_source) > 128:
        raise OpError("VALIDATION_ERROR", "source must be at most 128 characters.")
    if len(normalized_ref) > 256:
        raise OpError("VALIDATION_ERROR", "source_ref must be at most 256 characters.")
    return normalized_source, normalized_ref


def normalize_reporter(value: str | None) -> str | None:
    if value is None:
        return None
    reporter = value.strip()
    if not reporter:
        raise OpError("VALIDATION_ERROR", "on_behalf_of must not be blank.")
    if len(reporter) > 256:
        raise OpError("VALIDATION_ERROR", "on_behalf_of must be at most 256 characters.")
    if not reporter.isprintable():
        raise OpError("VALIDATION_ERROR", "on_behalf_of may not contain control characters.")
    return reporter


@dataclass(frozen=True, kw_only=True)
class IssueFileParams(CommonParams):
    title: str | None = None
    # ``text`` remains accepted for callers of the LAT-366 operation contract.
    text: str | None = None
    description: str | None = None
    confidence: str | None = None
    evidence: tuple[str, ...] = ()
    source: str | None = None
    source_ref: str | None = None
    media: tuple[dict, ...] = ()
    keep_photo_metadata: bool = False

    def check(self) -> None:
        if not isinstance(self.keep_photo_metadata, bool):
            raise OpError("VALIDATION_ERROR", "keep_photo_metadata must be a boolean.")
        if self.title is not None and self.text is not None:
            raise OpError("VALIDATION_ERROR", "Provide title or legacy text, not both.")
        raw_title = self.title if self.title is not None else self.text
        if raw_title is None or not raw_title.strip():
            raise OpError("VALIDATION_ERROR", "Issue title must not be empty.")
        if self.confidence is not None and self.confidence not in CONFIDENCE_VALUES:
            raise OpError(
                "VALIDATION_ERROR",
                f"Invalid confidence: '{self.confidence}'. "
                f"Valid values: {', '.join(CONFIDENCE_VALUES)}.",
            )
        normalize_source_ref(self.source, self.source_ref)
        normalize_reporter(self.on_behalf_of)
        issue_common.check_media_items(self.media)


@operation("issue.file")
class IssueFile:
    Params = IssueFileParams

    def run(self, ctx: OpContext, p: IssueFileParams) -> OpResult:
        issue_common.require_issue_log(ctx)
        pair = normalize_source_ref(p.source, p.source_ref)
        reporter = normalize_reporter(p.on_behalf_of)
        lock = (
            source_ref_lock(ctx.lattice_dir, *pair)
            if pair is not None and not ctx.caller.source_ref_lock_held
            else nullcontext()
        )
        with lock:
            if pair is not None:
                existing = next(
                    (
                        snapshot
                        for snapshot in list_issue_snapshots(ctx.lattice_dir)
                        if snapshot.get("source") == pair[0]
                        and snapshot.get("source_ref") == pair[1]
                    ),
                    None,
                )
                if existing is not None:
                    result = issue_common.result(ctx, existing, [])
                    value = dict(result.value)
                    value["deduplicated"] = True
                    return OpResult(events=[], value=value, idempotent=True)
            return self._file_new(ctx, p, pair, reporter)

    def _file_new(
        self,
        ctx: OpContext,
        p: IssueFileParams,
        pair: tuple[str, str] | None,
        reporter: str | None,
    ) -> OpResult:
        decoded = issue_common.decode_media(
            p.media,
            ctx.config,
            nothing="Nothing was filed.",
            stage_manager=ctx.issue_media,
            token_id=(ctx.caller.origin.get("authenticated") or {}).get("token_id"),
            require_stage_owner=ctx.caller.filing_only,
            keep_photo_metadata=p.keep_photo_metadata,
        )
        issue_common.check_issue_total(ctx.config, "The issue", 0, decoded)
        raw_title = p.title if p.title is not None else (p.text or "")
        title, overflow, _shortened = split_title(raw_title)
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
                    source = pair[0] if pair is not None else p.source
                    if source is not None:
                        data["source"] = source
                    if pair is not None:
                        data["source_ref"] = pair[1]
                    if ctx.caller.filing_only:
                        data["external"] = True
                    provenance = p.provenance()
                    provenance["on_behalf_of"] = reporter
                    events = [
                        create_issue_event("issue_filed", issue_id, ctx.actor, data, **provenance),
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
