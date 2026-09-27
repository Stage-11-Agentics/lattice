"""``task.file_link``: the ``lattice file-link`` command's rules."""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass

from lattice.ops.base import CommonParams, OpContext, OpError, OpResult, operation
from lattice.storage.operations import TaskMutationDecision


_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def check_file_paths(filepaths: tuple[str, ...]) -> list[str]:
    """Check caller-supplied project-relative paths syntactically; return them canonical.

    Normalizing a path against a checkout is the client's job (the CLI does it
    against the caller's checkout before calling). The operation never touches
    the filesystem: it refuses an empty path or a control character, then, in
    order, an absolute path and a path that climbs out with ``..``, with the
    messages the CLI has always shown for those inputs.
    """
    for path in filepaths:
        if not path or not path.strip():
            raise OpError("VALIDATION_ERROR", "File path must not be empty.")
        if "\x00" in path or any(0 <= ord(c) <= 31 for c in path if c != "\n"):
            raise OpError("VALIDATION_ERROR", f"File path contains control characters: {path!r}.")
    canonical = []
    for path in filepaths:
        if path.startswith(("/", "\\")) or _DRIVE_RE.match(path):
            raise OpError("VALIDATION_ERROR", f"Path '{path}' is outside the project root.")
        normalized = posixpath.normpath(path)
        if normalized.startswith("..") or ".." in re.split(r"[/\\]", path):
            raise OpError("VALIDATION_ERROR", f"Path '{path}' escapes the project root.")
        canonical.append(normalized)
    return canonical


@dataclass(frozen=True, kw_only=True)
class FileLinkParams(CommonParams):
    task: str
    filepaths: tuple[str, ...]

    def check(self) -> None:  # Click requires at least one; an API caller may not
        if not self.filepaths:
            raise OpError("VALIDATION_ERROR", "At least one file path is required.")


@operation("task.file_link")
class FileLink:
    Params = FileLinkParams

    def run(self, ctx: OpContext, p: FileLinkParams) -> OpResult:
        task_id = ctx.resolve_task(p.task)
        relative_paths = check_file_paths(p.filepaths)
        ctx.require_active(task_id)

        def decide(context):  # noqa: ANN001, ANN202
            existing = set(context.snapshot.get("linked_files", []))
            new_paths = [path for path in relative_paths if path not in existing]
            if not new_paths:
                raise OpError.task_state(
                    "CONFLICT",
                    "All specified files are already linked to this task.",
                    context.snapshot,
                )
            return TaskMutationDecision(
                events=[ctx.event("file_linked", task_id, {"paths": new_paths}, p)]
            )

        result = ctx.mutate(task_id, decide)
        return OpResult(task=result.snapshot, events=result.appended_events, value=result.snapshot)
