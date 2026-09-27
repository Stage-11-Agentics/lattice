"""``task.file_unlink``: the ``lattice file-unlink`` command's rules."""

from __future__ import annotations

from dataclasses import dataclass

from lattice.ops.base import CommonParams, OpContext, OpError, OpResult, operation
from lattice.ops.task_file_link import check_file_paths
from lattice.storage.operations import TaskMutationDecision


@dataclass(frozen=True, kw_only=True)
class FileUnlinkParams(CommonParams):
    task: str
    filepaths: tuple[str, ...]

    def check(self) -> None:  # Click requires at least one; an API caller may not
        if not self.filepaths:
            raise OpError("VALIDATION_ERROR", "At least one file path is required.")


@operation("task.file_unlink")
class FileUnlink:
    Params = FileUnlinkParams

    def run(self, ctx: OpContext, p: FileUnlinkParams) -> OpResult:
        task_id = ctx.resolve_task(p.task)
        relative_paths = check_file_paths(p.filepaths)
        ctx.require_active(task_id)

        def decide(context):  # noqa: ANN001, ANN202
            existing = set(context.snapshot.get("linked_files", []))
            to_remove = [path for path in relative_paths if path in existing]
            if not to_remove:
                raise OpError("NOT_FOUND", "None of the specified files are linked to this task.")
            return TaskMutationDecision(
                events=[ctx.event("file_unlinked", task_id, {"paths": to_remove}, p)]
            )

        result = ctx.mutate(task_id, decide)
        return OpResult(task=result.snapshot, events=result.appended_events, value=result.snapshot)
