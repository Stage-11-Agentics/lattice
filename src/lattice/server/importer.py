"""``lattice server project import``: move a local board onto a server (SPEC §11).

The import never modifies its source and never follows a symbolic link inside
it. In order:

1. Check the arguments, and refuse an existing slug, before anything is read.
2. Scan the source's ``.lattice/`` through directory descriptors opened with
   ``O_NOFOLLOW``, descending every real directory and classifying every path
   on its own (SPEC §6.1). A durable or workspace path must be a real
   directory or a regular file, else ``VALIDATION_ERROR`` naming it; a
   directory that cannot be read refuses the same way, because the import
   must name every path it does not move. The scan records the identity of
   every path, copied or not, and of the board directory itself.
3. Copy each durable regular file byte for byte into a staging board under
   ``projects/.importing-<slug>-<id>/``, reading it through the same
   descriptor walk and checking its identity before and after the read.
4. Open the source's ``.lattice/`` again and rescan it: any change to the
   board directory or to any path under it means a writer is still running,
   so the import refuses (``CONFLICT``).
5. Run doctor's board checks on the staged copy, which holds exactly the bytes
   imported; any error refuses (``INTEGRITY_ERROR``) with every finding.
6. Rebuild the task-derived files with the short-ID log floor (SPEC §5).
7. Seal the board (journal at a new epoch, head 0), make the staging directory
   its audit repository as ``project create`` does (SPEC §8.10; the first
   commit names the new epoch), and, under ``admin.lock``, rename it into place
   if the slug is still free.

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
    _create_audit_repo,
    admin_lock,
    check_slug,
    project_dir,
    require_root,
    seal_new_board,
)
from lattice.server.config import PROJECTS_DIR, SERVER_JSON, ServerConfigError, load_config
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
_ROOT = "."


@dataclass(frozen=True)
class _Identity:
    """What the scan saw at a path (``lstat``): kind, device, inode, and for
    anything but a directory its size and mtime."""

    kind: str
    dev: int
    ino: int
    size: int = 0
    mtime_ns: int = 0

    @classmethod
    def of(cls, st: os.stat_result) -> _Identity:
        if stat.S_ISDIR(st.st_mode):
            return cls("dir", st.st_dev, st.st_ino)
        kind = (
            "file" if stat.S_ISREG(st.st_mode) else "link" if stat.S_ISLNK(st.st_mode) else "other"
        )
        return cls(kind, st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


@dataclass
class _Scan:
    """Every path under the board (relative, POSIX), with its class and identity.

    ``"."`` is the board directory itself.
    """

    entries: dict[str, tuple[PathClass, _Identity]] = field(default_factory=dict)

    def identity(self, path: str) -> _Identity | None:
        entry = self.entries.get(path)
        return entry[1] if entry else None

    @property
    def copied_files(self) -> list[str]:
        return [
            path
            for path, (path_class, identity) in self.entries.items()
            if path_class in _COPIED_CLASSES and identity.kind == "file"
        ]

    @property
    def copied_dirs(self) -> list[str]:
        """Durable directories, and any directory that holds a copied file, parents first."""
        wanted = {
            path
            for path, (path_class, identity) in self.entries.items()
            if path != _ROOT and path_class in _COPIED_CLASSES and identity.kind == "dir"
        }
        for path in self.copied_files:
            wanted.update(p.as_posix() for p in PurePosixPath(path).parents if p.parts)
        return sorted(wanted, key=lambda p: (p.count("/"), p))

    @property
    def not_copied(self) -> list[tuple[str, str]]:
        """``(path, class)`` for every path the import does not move; directories end in ``/``."""
        created = set(self.copied_dirs)
        rows = []
        for path, (path_class, identity) in self.entries.items():
            if path == _ROOT or path_class in _COPIED_CLASSES or path in created:
                continue
            rows.append((f"{path}/" if identity.kind == "dir" else path, path_class.value))
        return sorted(rows)


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
        f"Import refused: cannot read .lattice/{path} ({exc.strerror or exc}), so the import "
        "cannot list every path it would leave behind; nothing was created.",
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
    """Walk the whole board below *lattice_fd* without following links (step 2)."""
    scan = _Scan()
    scan.entries[_ROOT] = (PathClass.DURABLE, _Identity.of(os.fstat(lattice_fd)))

    def visit(dir_fd: int, rel: PurePosixPath) -> None:
        where = rel.as_posix() if rel.parts else _ROOT
        try:
            names = sorted(os.listdir(dir_fd))
        except OSError as exc:
            raise _unreadable(where, exc) from None
        for name in names:
            child = rel / name
            path = child.as_posix()
            try:
                st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            except FileNotFoundError:
                raise _changed(path) from None
            except OSError as exc:
                raise _unreadable(path, exc) from None
            path_class = classify_path(child)
            identity = _Identity.of(st)
            if path_class in _COPIED_CLASSES:
                if identity.kind == "link":
                    raise _unsafe(path, "is a symbolic link")
                if identity.kind == "other":
                    raise _unsafe(path, "is not a regular file or a directory")
            scan.entries[path] = (path_class, identity)
            if identity.kind == "dir":
                fd = _open_dir(dir_fd, name, path, identity)
                try:
                    visit(fd, child)
                finally:
                    os.close(fd)

    visit(lattice_fd, PurePosixPath())
    return scan


def _read_file(lattice_fd: int, path: str, scan: _Scan) -> bytes:
    """Read one scanned file through no-follow descriptors, checking its identity (step 3)."""
    parts = PurePosixPath(path).parts
    fds: list[int] = []
    try:
        dir_fd = lattice_fd
        for depth, name in enumerate(parts[:-1], start=1):
            rel = "/".join(parts[:depth])
            dir_fd = _open_dir(dir_fd, name, rel, scan.identity(rel))
            fds.append(dir_fd)
        try:
            fd = os.open(parts[-1], _FILE_FLAGS, dir_fd=dir_fd)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOENT, errno.ENXIO):
                raise _changed(path) from None
            raise _unreadable(path, exc) from None
        fds.append(fd)
        expected = scan.identity(path)
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
        raise _unsafe(_ROOT, "(the board directory itself) is a symbolic link")
    if not stat.S_ISDIR(st.st_mode):
        raise _unsafe(_ROOT, "(the board directory itself) is not a directory")
    try:
        parent = os.open(source, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        raise _unreadable(_ROOT, exc) from None
    try:
        return _open_dir(parent, LATTICE_DIR, _ROOT, _Identity.of(st))
    finally:
        os.close(parent)


def _source_unchanged(source: Path, scan: _Scan) -> None:
    """Reopen the board by its path and rescan it; refuse on any difference (step 4)."""
    try:
        fd = _open_source(source)
    except OpError as exc:
        if exc.code == "NOT_FOUND" or exc.details.get("path") == _ROOT:
            raise _changed(_ROOT) from None
        raise
    try:
        rescan = _scan(fd)
    finally:
        os.close(fd)
    if rescan.entries != scan.entries:
        for path in sorted(scan.entries.keys() | rescan.entries.keys()):
            if scan.entries.get(path) != rescan.entries.get(path):
                raise _changed(path)


def _non_canonical(scan: _Scan) -> list[str]:
    """Files under the prose directories that are not ``<task_id>.md`` for a task of the board."""
    files = scan.copied_files
    task_ids = {
        PurePosixPath(path).stem
        for path in files
        if PurePosixPath(path).parent.as_posix() in ("events", "archive/events")
        and PurePosixPath(path).name.startswith("task_")
        and path.endswith(".jsonl")
    }
    listed = []
    for path in files:
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
    try:
        audit_config = load_config(root).audit
    except ServerConfigError as exc:
        raise OpError("VALIDATION_ERROR", f"{root / SERVER_JSON}: {exc}") from exc
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
                for path in scan.copied_dirs:
                    ensure_dir(board / path)
                for path in scan.copied_files:
                    atomic_write(board / path, _read_file(lattice_fd, path, scan))
                _source_unchanged(source, scan)
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
        audit_state = _create_audit_repo(staging, audit_config, epoch=journal.epoch)
        with admin_lock(root):
            if final.exists():
                raise OpError("CONFLICT", f"Project '{slug}' already exists at {final}.")
            os.rename(staging, final)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        os.close(lattice_fd)

    return {
        "slug": slug,
        "path": str(final),
        "source": str(source),
        "project_code": _project_code(final / LATTICE_DIR),
        "epoch": journal.epoch,
        "head_seq": 0,
        "audit": audit_state,
        "copied": len(scan.copied_files),
        "not_copied": [{"path": p, "class": c} for p, c in scan.not_copied],
        "non_canonical": _non_canonical(scan),
        "doctor": {
            "findings": findings,
            "summary": _summary(report),
        },
        "move_steps": _move_steps(slug),
    }


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
