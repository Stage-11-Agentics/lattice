"""``task.file_link``: the ``lattice file-link`` command's rules."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from lattice.ops.base import CommonParams, OpContext, OpError, OpResult, operation
from lattice.storage.operations import TaskMutationDecision


def resolve_to_relative(lattice_dir: Path, filepath: str) -> str:
    """Resolve a filepath to a path relative to the project root (``.lattice/``'s parent).

    Raises ``ValueError`` if the path escapes the project root (an absolute
    path outside the tree, or a relative path with ``../`` traversal).
    """
    project_root = lattice_dir.parent
    path = Path(filepath)
    if path.is_absolute():
        try:
            rel = str(path.resolve().relative_to(project_root.resolve()))
        except ValueError:
            raise ValueError(f"Path '{filepath}' is outside the project root.") from None
    else:
        normalized = os.path.normpath(filepath)
        if normalized.startswith(".."):
            raise ValueError(f"Path '{filepath}' escapes the project root.")
        rel = normalized
    if rel.startswith("./") or rel.startswith(".\\"):
        rel = rel[2:]
    return rel


def relative_file_paths(lattice_dir: Path, filepaths: tuple[str, ...]) -> list[str]:
    """Check each path for basic safety, then resolve them all project-relative."""
    for path in filepaths:
        if not path or not path.strip():
            raise OpError("VALIDATION_ERROR", "File path must not be empty.")
        if "\x00" in path or any(0 <= ord(c) <= 31 for c in path if c != "\n"):
            raise OpError("VALIDATION_ERROR", f"File path contains control characters: {path!r}.")
    try:
        return [resolve_to_relative(lattice_dir, path) for path in filepaths]
    except ValueError as exc:
        raise OpError("VALIDATION_ERROR", str(exc)) from exc


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
        relative_paths = relative_file_paths(ctx.lattice_dir, p.filepaths)
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
