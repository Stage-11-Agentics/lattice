"""``board.file_write``: ``lattice board write`` (SPEC §3.9, §6.1).

Writes one file of the workspace: anything under ``orchestration/`` (creating
missing directories), or a loose file directly under ``plans/`` or ``notes/``
that is not a task's own ``<task_id>.md``. The path is checked as a string,
relative to ``.lattice/``; the client normalizes what the user typed. The
storage primitives still confine the write to the board (symlinks included).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from lattice.core.ids import validate_id
from lattice.ops.base import OpContext, OpError, OpResult, operation
from lattice.ops.prose_common import ContentParams, check_expectation, confine, written_value
from lattice.storage.fs import atomic_write, ensure_dir
from lattice.storage.locks import lattice_lock

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_MAX_PATH = 1024
RULES = (
    "a board write path must be under orchestration/, or a loose file directly under "
    "plans/ or notes/ that is not a task's <task_id>.md"
)


def _refuse(path: str, why: str) -> OpError:
    return OpError(
        "VALIDATION_ERROR",
        f"Invalid board path {path!r}: {why}; {RULES}.",
        {"reason": "BOARD_PATH", "param": "path"},
    )


def check_board_path(path: str) -> tuple[str, ...]:
    """The parts of a writable workspace path, or ``VALIDATION_ERROR`` (SPEC §3.9)."""
    if not path or len(path) > _MAX_PATH:
        raise _refuse(path, "empty or too long")
    if _CONTROL_RE.search(path) or "\\" in path:
        raise _refuse(path, "control character or backslash")
    if path.startswith("/"):
        raise _refuse(path, "absolute")
    parts = tuple(path.split("/"))
    for part in parts:
        if part in ("", ".", ".."):
            raise _refuse(path, "empty, '.' or '..' component")
        if part.startswith(".tmp."):
            raise _refuse(path, "temporary file name")
    head = parts[0]
    if head == "orchestration" and len(parts) >= 2:
        return parts
    if head in ("plans", "notes") and len(parts) == 2:
        return parts
    raise _refuse(path, "outside the workspace")


def _task_named(lattice_dir, name: str) -> str | None:  # noqa: ANN001
    """The task whose own prose file is called *name*, when the board has it."""
    stem = PurePosixPath(name)
    if stem.suffix != ".md" or not validate_id(stem.stem, "task"):
        return None
    task_id = stem.stem
    for prefix in ("", "archive/"):
        for kind in ("events", "tasks"):
            suffix = "jsonl" if kind == "events" else "json"
            if (lattice_dir / f"{prefix}{kind}/{task_id}.{suffix}").exists():
                return task_id
    return None


@dataclass(frozen=True, kw_only=True)
class FileWriteParams(ContentParams):
    path: str  # relative to .lattice/
    expect_sha256: str | None = None

    def check(self) -> None:
        check_board_path(self.path)
        super().check()


@operation("board.file_write")
class FileWrite:
    """No actor, like ``board.context_write``."""

    Params = FileWriteParams
    no_actor = True

    def run(self, ctx: OpContext, p: FileWriteParams) -> OpResult:
        parts = check_board_path(p.path)
        lattice_dir = ctx.lattice_dir
        if parts[0] in ("plans", "notes"):
            task_id = _task_named(lattice_dir, parts[1])
            if task_id is not None:
                raise _refuse(
                    p.path,
                    f"it is the {parts[0][:-1] if parts[0] == 'plans' else 'notes'} file of "
                    f"task {task_id} (use 'lattice {'plan' if parts[0] == 'plans' else 'notes'} "
                    "write')",
                )
        target = lattice_dir.joinpath(*parts)
        # Resolve and confine before any read or idempotency decision.
        confine(target)
        for depth in range(1, len(parts)):
            ancestor = lattice_dir.joinpath(*parts[:depth])
            if ancestor.exists() and not ancestor.is_dir():
                raise _refuse(p.path, f"'{'/'.join(parts[:depth])}' is a file")
        if target.is_dir():
            raise _refuse(p.path, "it is a directory")
        data = p.content.encode("utf-8")
        value = written_value(p.path, data)
        with lattice_lock(lattice_dir / "locks", "board_files"):
            confine(target)  # again under the lock: the path may have changed
            found = check_expectation(target, p.expect_sha256, f".lattice/{p.path}")
            if found == value["sha256"]:
                return OpResult(value=value, idempotent=True)
            ensure_dir(target.parent)
            atomic_write(target, data)
        return OpResult(value=value)
