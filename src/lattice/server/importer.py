"""``lattice server project import``: move a local board onto a server (SPEC §11).

The import never modifies its source and never follows a symbolic link inside
it. In order:

1. Check the arguments, and refuse an existing slug, before anything is read.
2. Scan the source's ``.lattice/`` through directory descriptors opened with
   ``O_NOFOLLOW``: every durable or workspace path (SPEC §6.1) must be a real
   directory or a regular file, else ``VALIDATION_ERROR`` naming it. The scan
   records each copied path's identity and lists every path it will not copy.
3. Copy each durable regular file byte for byte into a staging board under
   ``projects/.importing-<slug>-<id>/``, reading it through the same
   descriptor walk and checking its identity before and after the read.
4. Scan the source again; any added, removed, or changed durable path means a
   writer is still running, so the import refuses (``CONFLICT``).
5. Run doctor's board checks on the staged copy, which holds exactly the bytes
   imported; any error refuses (``INTEGRITY_ERROR``) with every finding.
6. Rebuild the task-derived files with the short-ID log floor (SPEC §5).
7. Seal the board (journal at a new epoch, head 0) and, under ``admin.lock``,
   rename it into place if the slug is still free.

Any refusal or failure removes the staging directory, so nothing is created.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from lattice.core.errors import OpError
from lattice.core.ids import generate_instance_id
from lattice.server.admin import (
    admin_lock,
    check_slug,
    project_dir,
    require_root,
    seal_new_board,
)
from lattice.server.config import PROJECTS_DIR
from lattice.server.journal import HOSTED_DIR
from lattice.storage.fs import LATTICE_DIR, atomic_write, ensure_dir
from lattice.storage.integrity import DoctorReport, check_board, repair_task_derived_files
from lattice.storage.operations import AuthoritativeLogError
from lattice.storage.ownership import (
    PathClass,
    classify_path,
    owning_board,
    release_owner_flock,
    try_owner_flock,
)

_COPIED_CLASSES = frozenset({PathClass.DURABLE, PathClass.WORKSPACE})
_PROSE_DIRS = (("plans",), ("notes",), ("archive", "plans"), ("archive", "notes"))
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)


@dataclass(frozen=True)
class _Identity:
    """What the scan saw at a path: a directory's (dev, ino), a file's plus size and mtime."""

    kind: str
    dev: int
    ino: int
    size: int = 0
    mtime_ns: int = 0

    @classmethod
    def of(cls, st: os.stat_result) -> _Identity:
        if stat.S_ISDIR(st.st_mode):
            return cls("dir", st.st_dev, st.st_ino)
        return cls("file", st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


@dataclass
class _Scan:
    #: Every durable and workspace path to copy (relative, POSIX), with its identity.
    copied: dict[str, _Identity] = field(default_factory=dict)
    #: Every other path, with its class: ``(path, class)``; directories end in ``/``.
    not_copied: list[tuple[str, str]] = field(default_factory=list)


def _unsafe(path: str, what: str) -> OpError:
    return OpError(
        "VALIDATION_ERROR",
        f"Import refused: .lattice/{path} {what}. Only real directories and regular files "
        "can be imported; nothing was created.",
        {"path": path},
    )


def _changed(path: str) -> OpError:
    return OpError(
        "CONFLICT",
        f"Import refused: .lattice/{path} changed while it was being copied. Stop every "
        "writer of the board (agents, dashboards, MCP servers) and import again; nothing "
        "was created.",
        {"path": path, "reason": "SOURCE_CHANGED"},
    )


def _unreadable(path: str, exc: OSError) -> OpError:
    return OpError(
        "VALIDATION_ERROR",
        f"Import refused: cannot read .lattice/{path} ({exc.strerror or exc}); nothing was "
        "created.",
        {"path": path},
    )


def _open_dir(parent_fd: int, name: str, rel: str, expected: _Identity | None) -> int:
    """Open one directory component without following a link; check it is the one scanned."""
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.ENOENT):
            raise _changed(rel) from None
        raise _unreadable(rel, exc) from None
    if expected is not None:
        st = os.fstat(fd)
        if (st.st_dev, st.st_ino) != (expected.dev, expected.ino):
            os.close(fd)
            raise _changed(rel)
    return fd


def _scan(lattice_fd: int) -> _Scan:
    """Walk the board below *lattice_fd* without following links (step 2)."""
    scan = _Scan()

    def entries(dir_fd: int, rel: PurePosixPath) -> list[tuple[str, os.stat_result]]:
        try:
            names = sorted(os.listdir(dir_fd))
            return [(name, os.stat(name, dir_fd=dir_fd, follow_symlinks=False)) for name in names]
        except OSError as exc:
            raise _unreadable(rel.as_posix() if rel.parts else ".", exc) from None

    def skipped(dir_fd: int, rel: PurePosixPath, path_class: str) -> None:
        """List everything under a skipped real directory, never following a link."""
        try:
            listing = entries(dir_fd, rel)
        except OpError:
            return  # an unreadable skipped directory is listed by itself
        for name, st in listing:
            child = rel / name
            if stat.S_ISDIR(st.st_mode):
                scan.not_copied.append((f"{child.as_posix()}/", path_class))
                try:
                    fd = os.open(name, _DIR_FLAGS, dir_fd=dir_fd)
                except OSError:
                    continue
                try:
                    skipped(fd, child, path_class)
                finally:
                    os.close(fd)
            else:
                scan.not_copied.append((child.as_posix(), path_class))

    def visit(dir_fd: int, rel: PurePosixPath) -> None:
        for name, st in entries(dir_fd, rel):
            child = rel / name
            path = child.as_posix()
            path_class = classify_path(child)
            is_dir = stat.S_ISDIR(st.st_mode)
            if path_class not in _COPIED_CLASSES:
                scan.not_copied.append((f"{path}/" if is_dir else path, path_class.value))
                if is_dir:
                    try:
                        fd = os.open(name, _DIR_FLAGS, dir_fd=dir_fd)
                    except OSError:
                        continue  # listed by itself; nothing under it is copied
                    try:
                        skipped(fd, child, path_class.value)
                    finally:
                        os.close(fd)
                continue
            if stat.S_ISLNK(st.st_mode):
                raise _unsafe(path, "is a symbolic link")
            if is_dir:
                identity = _Identity.of(st)
                scan.copied[path] = identity
                fd = _open_dir(dir_fd, name, path, identity)
                try:
                    visit(fd, child)
                finally:
                    os.close(fd)
            elif stat.S_ISREG(st.st_mode):
                scan.copied[path] = _Identity.of(st)
            else:
                raise _unsafe(path, "is not a regular file or a directory")

    visit(lattice_fd, PurePosixPath())
    scan.not_copied.sort()
    return scan


def _read_file(lattice_fd: int, path: str, scan: _Scan) -> bytes:
    """Read one scanned file through no-follow descriptors, checking its identity (step 3)."""
    parts = PurePosixPath(path).parts
    fds: list[int] = []
    try:
        dir_fd = lattice_fd
        for depth, name in enumerate(parts[:-1], start=1):
            rel = "/".join(parts[:depth])
            dir_fd = _open_dir(dir_fd, name, rel, scan.copied.get(rel))
            fds.append(dir_fd)
        try:
            fd = os.open(parts[-1], _FILE_FLAGS, dir_fd=dir_fd)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOENT, errno.ENXIO):
                raise _changed(path) from None
            raise _unreadable(path, exc) from None
        fds.append(fd)
        expected = scan.copied[path]
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or _Identity.of(before) != expected:
            raise _changed(path)
        chunks = []
        while chunk := os.read(fd, 1 << 20):
            chunks.append(chunk)
        data = b"".join(chunks)
        if _Identity.of(os.fstat(fd)) != expected or len(data) != expected.size:
            raise _changed(path)
        return data
    finally:
        for fd in reversed(fds):
            os.close(fd)


def _open_source(source: Path) -> int:
    """Check ``--from`` and open its ``.lattice/`` without following a link (step 1)."""
    if not source.exists():
        raise OpError("NOT_FOUND", f"--from {source} does not exist.", {"path": str(source)})
    if not source.is_dir():
        raise OpError(
            "VALIDATION_ERROR", f"--from {source} is not a directory.", {"path": str(source)}
        )
    try:
        st = os.stat(source / LATTICE_DIR, follow_symlinks=False)
    except FileNotFoundError:
        raise OpError(
            "VALIDATION_ERROR",
            f"--from {source} contains no {LATTICE_DIR}/ board.",
            {"path": str(source)},
        ) from None
    if stat.S_ISLNK(st.st_mode):
        raise _unsafe(".", "(the board directory itself) is a symbolic link")
    if not stat.S_ISDIR(st.st_mode):
        raise _unsafe(".", "(the board directory itself) is not a directory")
    try:
        parent = os.open(source, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        raise _unreadable(".", exc) from None
    try:
        return _open_dir(parent, LATTICE_DIR, ".", _Identity.of(st))
    finally:
        os.close(parent)


def _non_canonical(scan: _Scan) -> list[str]:
    """Files under the prose directories that are not ``<task_id>.md`` for a task of the board."""
    task_ids = {
        PurePosixPath(path).stem
        for path, identity in scan.copied.items()
        if identity.kind == "file"
        and PurePosixPath(path).parent.as_posix() in ("events", "archive/events")
        and PurePosixPath(path).name.startswith("task_")
        and path.endswith(".jsonl")
    }
    listed = []
    for path, identity in scan.copied.items():
        if identity.kind != "file":
            continue
        parts = PurePosixPath(path).parts
        for prose in _PROSE_DIRS:
            if parts[: len(prose)] != prose:
                continue
            rest = parts[len(prose) :]
            name = rest[0] if len(rest) == 1 else ""
            if not (name.endswith(".md") and name[:-3] in task_ids):
                listed.append(path)
            break
    return sorted(listed)


def _move_steps(slug: str) -> list[dict]:
    """The guide's move steps (SPEC §11), in order, with the attach command filled in."""
    return [
        {
            "step": 1,
            "text": "Stop every writer of the local board: agents, dashboards, MCP servers.",
            "commands": [],
        },
        {
            "step": 2,
            "text": "Import a copy of the board on the server host (done). Read both lists: "
            "paths not copied stay only in the old board.",
            "commands": [],
        },
        {
            "step": 3,
            "text": "In the checkout, move the old board aside (never delete it) and ignore it; "
            "if board files are tracked in git, stage their removal.",
            "commands": [
                "mv .lattice .lattice.pre-hosted-$(date -u +%Y%m%d-%H%M%S)",
                "echo '/.lattice.pre-hosted-*/' >> .gitignore",
                "git rm -r --cached -q --ignore-unmatch .lattice",
            ],
        },
        {
            "step": 4,
            "text": "Attach the checkout. <alias> is your name for this server "
            "('lattice remote list' shows it).",
            "commands": [f"lattice remote attach <alias> {slug}"],
        },
        {
            "step": 5,
            "text": "Commit the binding, .gitignore, and the staged removal, and push; then "
            "list branches that still track board files.",
            "commands": [
                "git add .lattice-remote.json .gitignore",
                'git commit -m "Move the Lattice board to the server"',
                "git push",
                "lattice remote status",
            ],
        },
    ]


def import_project(root: Path, slug: str, source: Path) -> dict:
    """Import the board at ``<source>/.lattice/`` as project *slug* (SPEC §11)."""
    root = Path(root)
    check_slug(slug)
    require_root(root)
    final = project_dir(root, slug)
    if final.exists():
        raise OpError("CONFLICT", f"Project '{slug}' already exists at {final}.")
    source = Path(source)
    lattice_fd = _open_source(source)
    staging = root / PROJECTS_DIR / f".importing-{slug}-{generate_instance_id()[5:]}"
    board = staging / LATTICE_DIR
    try:
        scan = _scan(lattice_fd)
        with owning_board(board):
            ensure_dir(board / HOSTED_DIR)
            fd = try_owner_flock(board)
            if fd is None:  # a fresh directory nobody else knows about
                raise OpError("BOARD_BUSY", f"could not lock {board}")
            try:
                for path, identity in scan.copied.items():
                    if identity.kind == "dir":
                        ensure_dir(board / path)
                    else:
                        atomic_write(board / path, _read_file(lattice_fd, path, scan))
                rescan = _scan(lattice_fd)
                if rescan.copied != scan.copied:
                    raise _changed(_first_difference(scan, rescan))
                report = check_board(board)
                findings = [_clean(f, board, source / LATTICE_DIR) for f in report.findings]
                if report.errors:
                    raise _doctor_refusal(report, findings)
                try:
                    repair_task_derived_files(board, reconcile_placement=False)
                except AuthoritativeLogError as exc:
                    message = _as_source(str(exc), board, source / LATTICE_DIR)
                    raise OpError(
                        "INTEGRITY_ERROR", f"Import refused: short-ID repair failed: {message}"
                    ) from exc
                journal = seal_new_board(board)
            finally:
                release_owner_flock(fd)
        with admin_lock(root):
            if final.exists():
                raise OpError("CONFLICT", f"Project '{slug}' already exists at {final}.")
            os.rename(staging, final)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        os.close(lattice_fd)

    config_code = _project_code(final / LATTICE_DIR)
    return {
        "slug": slug,
        "path": str(final),
        "source": str(source),
        "project_code": config_code,
        "epoch": journal.epoch,
        "head_seq": 0,
        "copied": sum(1 for i in scan.copied.values() if i.kind == "file"),
        "not_copied": [{"path": p, "class": c} for p, c in scan.not_copied],
        "non_canonical": _non_canonical(scan),
        "doctor": {
            "findings": findings,
            "summary": _summary(report),
        },
        "move_steps": _move_steps(slug),
    }


def _first_difference(before: _Scan, after: _Scan) -> str:
    for path in sorted(set(before.copied) | set(after.copied)):
        if before.copied.get(path) != after.copied.get(path):
            return path
    return "."


def _project_code(board: Path) -> str | None:
    try:
        return json.loads((board / "config.json").read_text(encoding="utf-8")).get("project_code")
    except (OSError, ValueError, AttributeError):
        return None


def _as_source(text: str, staged: Path, source: Path) -> str:
    """Name the source board, not the staging copy that doctor read (and that is removed)."""
    for form in {str(staged.resolve()), str(staged)}:
        text = text.replace(form, str(source))
    return text


def _clean(finding: dict, staged: Path, source: Path) -> dict:
    """A doctor finding as ``lattice doctor --json`` prints it, with source paths."""
    return {
        "level": finding["level"],
        "check": finding["check"],
        "message": _as_source(finding["message"], staged, source),
        "task_id": finding.get("task_id"),
    }


def _summary(report: DoctorReport) -> dict:
    return {
        "tasks": report.task_count,
        "events": report.event_count,
        "artifacts": report.artifact_count,
        "resources": report.resource_count,
        "warnings": report.warnings,
        "errors": report.errors,
    }


def _doctor_refusal(report: DoctorReport, findings: list[dict]) -> OpError:
    lines = [f"  {f['level']}: {f['message']}" for f in findings]
    noun = "error" if report.errors == 1 else "errors"
    return OpError(
        "INTEGRITY_ERROR",
        f"Import refused: the board fails lattice doctor ({report.errors} {noun}); "
        "nothing was created. Findings:\n" + "\n".join(lines),
        {"findings": findings, "summary": _summary(report)},
    )
