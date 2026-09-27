"""Shared rules of the prose writes (SPEC §3.9): plans, notes, context, board files.

Each write takes its content from exactly one of ``file`` (the text of
``--file PATH``) or ``stdin`` (the text read from ``--stdin``), and an
optional ``expect_sha256``: the SHA-256 the caller last saw, refused with
``CONFLICT`` when the file has changed since.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from lattice.core.errors import OpError

_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}")


def check_content_sources(has_file: bool, has_stdin: bool, what: str) -> None:
    """Exactly one of ``--file`` / ``--stdin`` (``VALIDATION_ERROR`` otherwise)."""
    if has_file and has_stdin:
        raise OpError("VALIDATION_ERROR", "Provide either --file or --stdin, not both.")
    if not has_file and not has_stdin:
        raise OpError("VALIDATION_ERROR", f"Provide {what} as --file PATH or --stdin.")


def check_expect_sha256(value: str | None) -> None:
    if value is not None and not _SHA256_RE.fullmatch(value):
        raise OpError(
            "VALIDATION_ERROR",
            f"Invalid --expect-sha256 {value!r}: expected 64 hexadecimal characters.",
        )


@dataclass(frozen=True, kw_only=True)
class ContentParams:
    """The content options every prose write shares."""

    file: str | None = None  # the text of --file PATH
    stdin: str | None = None  # the text read from --stdin

    what = "the content"

    def check(self) -> None:
        check_content_sources(self.file is not None, self.stdin is not None, self.what)
        check_expect_sha256(getattr(self, "expect_sha256", None))

    @property
    def content(self) -> str:
        text = self.file if self.file is not None else self.stdin
        assert text is not None
        return text


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def current_sha256(path: Path) -> str | None:
    """The SHA-256 of the file at *path*, or ``None`` when there is none."""
    try:
        return sha256_hex(path.read_bytes())
    except FileNotFoundError:
        return None


def check_expectation(path: Path, expect: str | None, label: str) -> str | None:
    """Refuse with ``CONFLICT`` unless *path* still hashes to *expect*.

    Returns the file's current SHA-256 (``None`` when absent).
    """
    found = current_sha256(path)
    if expect is not None and found != expect.lower():
        raise OpError(
            "CONFLICT",
            f"{label} changed: expected sha256 {expect.lower()}, found "
            f"{found if found is not None else 'no file'}.",
            {"reason": "EXPECTATION_FAILED", "sha256": found},
        )
    return found


def written_value(path: str, data: bytes) -> dict:
    """The ``--json`` data of a prose write: where, and what was written."""
    return {"path": path, "sha256": sha256_hex(data), "bytes": len(data)}


def write_task_prose(ctx, p, kind: str):  # noqa: ANN001, ANN201
    """``task.plan_write`` / ``task.notes_write``: replace the task's plan or notes.

    Under the task's locks: the file at its current placement (active or
    archived) is checked against ``expect_sha256``, written with
    ``atomic_write``, and a ``plan_written`` / ``notes_written`` event
    ``{sha256, bytes}`` is appended. Content equal to the file's is idempotent:
    nothing is written or appended.
    """
    from lattice.ops.base import OpResult
    from lattice.storage.fs import atomic_write, ensure_dir
    from lattice.storage.operations import TaskMutationDecision, read_task_authority

    task_id = ctx.resolve_task(p.task)
    if read_task_authority(ctx.lattice_dir, task_id, allow_missing=True) is None:
        raise OpError("NOT_FOUND", f"Task {task_id} not found.")
    data = p.content.encode("utf-8")
    folder = "plans" if kind == "plan" else "notes"
    label = "Plan" if kind == "plan" else "Notes"

    def decide(context):  # noqa: ANN001, ANN202
        archived = context.location == "archived"
        relative = f"{'archive/' if archived else ''}{folder}/{task_id}.md"
        other_relative = f"{'' if archived else 'archive/'}{folder}/{task_id}.md"
        path = ctx.lattice_dir / relative
        if (ctx.lattice_dir / other_relative).exists():
            raise OpError(
                "INTEGRITY_ERROR",
                f"Both active and archived {kind} files exist for {task_id}; "
                "manual recovery is required.",
            )
        display_id = context.snapshot.get("short_id") or task_id
        try:
            found = check_expectation(path, p.expect_sha256, f"{label} for {display_id}")
        except OpError as exc:
            raise OpError.task_state(exc.code, exc.message, context.snapshot) from exc
        value = {"task_id": task_id, **written_value(relative, data)}
        if found == value["sha256"]:
            return TaskMutationDecision(value=value, idempotent=True)
        ensure_dir(path.parent)
        atomic_write(path, data)
        event = ctx.event(
            f"{kind}_written", task_id, {"sha256": value["sha256"], "bytes": len(data)}, p
        )
        return TaskMutationDecision(events=[event], value=value)

    result = ctx.mutate(task_id, decide, source="either")
    return OpResult(
        task=result.snapshot,
        events=result.appended_events,
        value=result.callback_value,
        idempotent=result.idempotent,
    )
