"""The client cache (SPEC §9.4): a read-only mirror of one hosted project board.

Published interface (H-10c and H-11 build on exactly these):

- :func:`catch_up` ``(hosted_root, *, bulk=False) -> SyncOutcome``: bring the
  cache up to the server's head.
- :func:`read_lock` ``(hosted_root)``: the cache's shared read lock. Every
  hosted read holds it from its first directory enumeration through its last
  file read. Never call :func:`catch_up` while holding it: an apply waits for
  every reader to leave.

Everything else here is private to the client.

The cycle, all under ``locks/cache_sync.lock`` held exclusively (one sync at a
time, for the whole cycle):

1. Read ``cache/state.json``. A leftover ``cache/applying`` (a syncer killed
   mid-apply), a missing epoch, or a tamper fingerprint that no longer matches
   the tree forces a reset: the request omits the epoch, so the server sends
   every board file.
2. Fetch and verify everything before touching the board: every path must be a
   synced board path (§6.1: durable or workspace), every ``href`` relative to
   the same server, every file's bytes must match its ``sha256``. Append
   deltas apply to a local copy of exactly ``append_from`` bytes, else the
   whole file is fetched.
3. Apply under ``locks/cache_rw.lock`` held exclusively: write
   ``cache/applying``; on a reset, rescue every local file the reset would
   change or drop into ``cache/rescued/<UTC stamp>/`` (durably, before the
   original is removed); write and remove board files through the storage
   primitives with the syncer flag set; restore modes (files 0400, synced
   directories 0500, ``.lattice/``, ``cache/`` and runtime directories 0700);
   write ``cache/state.json``; remove ``cache/applying`` last.

``.lattice/`` and the client's own directories under it (``cache/`` and its
subdirectories, the runtime directories) are reached through
:mod:`lattice.remote.cache_paths`, never through a symlink: a checkout where one
is a symlink or a file is refused with ``BINDING_CONFLICT`` before anything is
written, walked, or deleted.

Hard failures raise ``OpError`` (``PROXY_REJECTED``, ``PROTOCOL_MISMATCH``, a
non-transient server error, ``NOT_HOSTED``, the remote's first-contact errors,
and ``INTEGRITY_ERROR`` for a delta rejected whole, with ``details.reason``
``UNSAFE_PATH``, ``CROSS_ORIGIN_HREF``, ``MALFORMED_SYNC``, or
``HASH_MISMATCH``). Everything transient is an outcome.

``fcntl`` is imported only inside the functions that take a cache lock, so
local Lattice keeps importing without it (G-6).
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import sys
import time
import urllib.parse
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from lattice.core.errors import OpError
from lattice.remote import cache_paths, http
from lattice.remote.config import resolve_remote
from lattice.storage.fs import atomic_write, ensure_dir, unlink_entry
from lattice.storage.locks import frozen_board
from lattice.storage.ownership import PathClass, classify_path, syncing_board

LATTICE_DIR = ".lattice"
BINDING_FILE = ".lattice-remote.json"

#: The synced classes (SPEC §6.1): durable board data and workspace files. One
#: set, used for verification, the fingerprint, modes, reset, rescue, and doctor.
SYNCED_CLASSES = frozenset({PathClass.DURABLE, PathClass.WORKSPACE})
RUNTIME_DIRS = cache_paths.RUNTIME_DIRS
#: The durable directories of a local board (``ensure_lattice_dirs``), so a cache
#: has the same layout even where the server holds no file (AC-9).
STANDARD_DIRS = (
    "tasks",
    "events",
    "archive",
    "archive/tasks",
    "archive/events",
    "archive/notes",
    "archive/plans",
    "artifacts",
    "artifacts/meta",
    "artifacts/payload",
    "notes",
    "plans",
    "resources",
    "sessions",
    "sessions/archive",
    "templates",
)

FILE_MODE = 0o400
SYNCED_DIR_MODE = 0o500
PRIVATE_DIR_MODE = 0o700

PROBE_SECONDS = 5.0
#: Whole cycles a sync may restart after a hash mismatch or a stale ``href``.
MAX_CYCLES = 3
_SHA256_CHARS = frozenset("0123456789abcdef")
#: A journal line hash (SPEC §8.8): the first 32 hex characters of a SHA-256.
_LINE_HASH_RE = re.compile(r"[0-9a-f]{32}")

OutcomeKind = Literal["applied", "unchanged", "unreachable", "busy", "incomplete"]

#: Test seam: called with a step name at each crash-relevant point of a sync
#: (``sync_ticket`` and ``sync_ticket_taken`` around publishing the ticket,
#: ``applying_written``, ``rescue_copied``, ``rescue_renamed``,
#: ``rescue_dir_synced``, ``rescue_unlinked``, ``file_written``, ``removed``,
#: ``modes_restored``, ``state_written``). ``None`` in production.
_seam: Callable[[str], None] | None = None


def _step(name: str) -> None:
    if _seam is not None:
        _seam(name)


@dataclass(frozen=True)
class SyncOutcome:
    """What one :func:`catch_up` did.

    ``kind``: ``applied`` (a delta or reset applied), ``unchanged`` (already at
    the server's head), ``unreachable`` (no answer in time, or
    ``BOARD_UNAVAILABLE``), ``busy`` (``BOARD_BUSY`` / ``RATE_LIMITED``, or the probe's budget ran
    out waiting for another sync), ``incomplete`` (``cache/applying`` remains:
    an interrupted apply could not be repaired, so reads fail with
    ``CACHE_INCOMPLETE``). ``head_seq`` and ``synced_at`` describe the cache
    after the call (``None`` when it has never completed a sync); ``detail`` is
    a one-line reason for the last three.
    """

    kind: OutcomeKind
    head_seq: int | None
    synced_at: str | None
    detail: str | None = None


# ---------------------------------------------------------------------------
# Identity: which remote and project a hosted root mirrors
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def cache_identity(hosted_root: Path) -> tuple[str, str] | None:
    """``(remote, project)`` from the cache marker, an interrupted apply, or the
    committed binding, in that order; ``None`` when none names both."""
    lattice_dir = Path(hosted_root) / LATTICE_DIR
    for source in (
        lattice_dir / "cache" / "state.json",
        lattice_dir / "cache" / "applying",
        Path(hosted_root) / BINDING_FILE,
    ):
        data = _read_json(source)
        remote, project = data.get("remote"), data.get("project")
        if isinstance(remote, str) and remote and isinstance(project, str) and project:
            return remote, project
    return None


def has_cache_marker(lattice_dir: Path) -> bool:
    cache_dir = Path(lattice_dir) / "cache"
    return (cache_dir / "state.json").exists() or (cache_dir / "applying").exists()


def not_hosted(root: Path) -> OpError:
    return OpError(
        "NOT_HOSTED",
        f"{root} is not a hosted checkout (it holds no cache of a hosted board); "
        "nothing was changed.",
        {"root": str(root)},
    )


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + "Z"


# ---------------------------------------------------------------------------
# The synced tree and the tamper fingerprint (a stat walk, no reads)
# ---------------------------------------------------------------------------


def _walk_synced(lattice_dir: Path) -> tuple[list[tuple[str, os.stat_result]], list[str]]:
    """``(files, directories)`` of the synced classes under *lattice_dir*.

    ``files`` holds ``(relative path, lstat)`` of every non-directory entry
    (symlinks included, never followed), sorted; ``directories`` the relative
    paths of synced directories, parents before children.
    """
    files: list[tuple[str, os.stat_result]] = []
    dirs: list[str] = []
    stack = [("", Path(lattice_dir))]
    while stack:
        prefix, directory = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except (FileNotFoundError, NotADirectoryError):
            continue
        for entry in entries:
            rel = f"{prefix}{entry.name}"
            if classify_path(rel) not in SYNCED_CLASSES:
                continue
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                dirs.append(rel)
                stack.append((rel + "/", Path(entry.path)))
            else:
                files.append((rel, info))
    files.sort()
    dirs.sort(key=lambda d: (d.count("/"), d))
    return files, dirs


def fingerprint(lattice_dir: Path) -> str:
    """SHA-256 over each synced file's path, size, and mtime (lstat; no reads)."""
    digest = hashlib.sha256()
    for rel, info in _walk_synced(lattice_dir)[0]:
        digest.update(f"{rel}\0{info.st_size}\0{info.st_mtime_ns}\n".encode())
    return digest.hexdigest()


def synced_files(lattice_dir: Path) -> list[str]:
    """Relative paths of every synced (durable or workspace) non-directory entry."""
    return [rel for rel, _info in _walk_synced(lattice_dir)[0]]


def _sha256_file(path: Path) -> str | None:
    """The file's SHA-256, or ``None`` for anything but a readable regular file."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            while chunk := fh.read(1 << 20):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Delta verification (SPEC §9.4)
# ---------------------------------------------------------------------------


def unsafe_path_reason(rel: object, lattice_dir: Path) -> str | None:
    """Why a server-supplied path may not be written, or ``None`` if it may.

    It must be a relative POSIX path of a synced class with no empty, ``.``, or
    ``..`` component, no backslash or NUL, and no existing symlink along it
    under *lattice_dir*.
    """
    if not isinstance(rel, str) or not rel:
        return "not a path"
    if "\\" in rel or "\0" in rel:
        return "contains a backslash or NUL"
    if rel.startswith("/"):
        return "is absolute"
    parts = rel.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return "has an empty, '.', or '..' component"
    path_class = classify_path(PurePosixPath(rel))
    if path_class not in SYNCED_CLASSES:
        return f"is not a synced board path ({path_class.value})"
    current = Path(lattice_dir)
    for part in parts:
        current = current / part
        try:
            info = os.lstat(current)
        except (FileNotFoundError, NotADirectoryError):
            break
        if stat.S_ISLNK(info.st_mode):
            return "passes through a symlink"
    return None


def _integrity(reason: str, message: str, **details: Any) -> OpError:
    return OpError("INTEGRITY_ERROR", message, {"reason": reason, **details})


@dataclass(frozen=True)
class _Entry:
    rel: str
    sha256: str
    size: int
    content: bytes | None = None
    href: str | None = None
    append_from: int | None = None


@dataclass
class _Delta:
    epoch: str
    head_seq: int
    head_hash: str | None
    reset: bool
    files: list[_Entry]
    removed: list[str]
    server_version: str | None


def _parse_delta(
    remote: http.Remote, body: Any, lattice_dir: Path, server_version: str | None
) -> _Delta:
    """Check a sync body's shape and every path and ``href`` in it, before
    anything is fetched or written (the delta is rejected whole)."""

    def malformed(what: str) -> OpError:
        return _integrity("MALFORMED_SYNC", f"the server's sync answer is malformed: {what}")

    if not isinstance(body, dict):
        raise malformed("not an object")
    epoch, head, head_hash = body.get("epoch"), body.get("head_seq"), body.get("head_hash")
    files, removed, reset = body.get("files", {}), body.get("removed", []), body.get("reset")
    if not isinstance(epoch, str) or not epoch:
        raise malformed("no epoch")
    if not isinstance(head, int) or isinstance(head, bool) or head < 0:
        raise malformed("no head_seq")
    if head > 0 and not (isinstance(head_hash, str) and _LINE_HASH_RE.fullmatch(head_hash)):
        raise malformed("head_hash must be 32 lowercase hex characters at a nonzero head")
    if head == 0 and "head_hash" in body:  # absent, not even null (SPEC §8.8)
        raise malformed("head_hash must be absent at head 0")
    if not isinstance(reset, bool) or not isinstance(files, dict) or not isinstance(removed, list):
        raise malformed("reset, files, or removed has the wrong type")
    entries: list[_Entry] = []
    for rel in [*files, *removed]:
        reason = unsafe_path_reason(rel, lattice_dir)
        if reason is not None:
            raise _integrity(
                "UNSAFE_PATH",
                f"the server's sync answer names the path {rel!r}, which {reason}; "
                "the whole delta was rejected and nothing was changed.",
                path=str(rel),
            )
    for rel, spec in sorted(files.items()):
        if not isinstance(spec, dict):
            raise malformed(f"{rel}: not an object")
        digest, size = spec.get("sha256"), spec.get("size")
        if not (isinstance(digest, str) and len(digest) == 64 and set(digest) <= _SHA256_CHARS):
            raise malformed(f"{rel}: bad sha256")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise malformed(f"{rel}: bad size")
        href = spec.get("href")
        if href is not None and http.href_url(remote, href) is None:
            raise _integrity(
                "CROSS_ORIGIN_HREF",
                f"the server's sync answer points {rel} at {href!r}, which is not a relative "
                "path on the same server; the whole delta was rejected and nothing was sent.",
                path=rel,
            )
        content = None
        if "content_b64" in spec:
            try:
                content = base64.b64decode(spec["content_b64"], validate=True)
            except (binascii.Error, TypeError, ValueError):
                raise malformed(f"{rel}: content_b64 is not base64") from None
        append_from = spec.get("append_from")
        if append_from is not None and (
            not isinstance(append_from, int)
            or isinstance(append_from, bool)
            or append_from < 0
            or content is None
            or href is None
        ):
            raise malformed(f"{rel}: bad append delta")
        if content is None and href is None:
            raise malformed(f"{rel}: neither content nor href")
        entries.append(_Entry(rel, digest, size, content, href, append_from))
    return _Delta(
        epoch=epoch,
        head_seq=head,
        head_hash=head_hash,
        reset=reset,
        files=entries,
        removed=sorted(str(r) for r in removed),
        server_version=server_version,
    )


# ---------------------------------------------------------------------------
# Locks (fcntl only here: hosted-only functions)
# ---------------------------------------------------------------------------


def _fcntl():
    try:
        import fcntl
    except ImportError as exc:
        raise OpError(
            "HOSTED_UNSUPPORTED_PLATFORM",
            "Hosted mode needs a POSIX platform (macOS or Linux).",
        ) from exc
    return fcntl


def _dir_fd(lattice_dir: Path, rel: str = "", *, create: bool = True) -> int:
    """A descriptor of ``.lattice/`` or a cache-control or runtime directory
    under it (never board data), each component a real directory made 0700 and
    never followed (:mod:`lattice.remote.cache_paths`). A symlink or a file
    there is ``BINDING_CONFLICT`` (``UNSAFE_CACHE_PATH``). The caller closes it."""
    parts = [lattice_dir.name, *(part for part in rel.split("/") if part)]
    try:
        return cache_paths.open_dir(lattice_dir.parent, *parts, create=create)
    except cache_paths.UnsafeCachePath as exc:
        raise cache_paths.layout_error(lattice_dir.parent, Path(exc.filename)) from None


def _private_dir(lattice_dir: Path, rel: str = "") -> None:
    """Create ``.lattice/`` or a cache-control or runtime directory under it, 0700
    (see :func:`_dir_fd`)."""
    os.close(_dir_fd(lattice_dir, rel))


def _write_cache_file(lattice_dir: Path, name: str, text: str) -> None:
    """Replace ``cache/<name>`` atomically, never through a symlink."""
    fd = _dir_fd(lattice_dir, "cache")
    try:
        with cache_paths.naming(lattice_dir / "cache" / name):
            cache_paths.write_file(fd, name, text.encode("utf-8"))
    finally:
        os.close(fd)


def _remove_cache_file(lattice_dir: Path, name: str) -> None:
    """Unlink ``cache/<name>`` itself; nothing to do without a ``cache/``."""
    try:
        fd = _dir_fd(lattice_dir, "cache", create=False)
    except FileNotFoundError:
        return
    try:
        with cache_paths.naming(lattice_dir / "cache" / name):
            cache_paths.remove_file(fd, name)
    finally:
        os.close(fd)


def _lock(path: Path, exclusive: bool, deadline: float | None) -> int | None:
    """``flock`` *path*; returns the descriptor, or ``None`` if *deadline* passed.

    After locking it checks that *path* still names the locked file, so a
    lock file deleted and recreated meanwhile (``cache clear``) cannot split
    two lockers onto different files.
    """
    fcntl = _fcntl()
    mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    lattice_dir, name = path.parent.parent, path.name
    while True:
        dir_fd = _dir_fd(lattice_dir, path.parent.name)
        try:
            with cache_paths.naming(path):
                fd = os.open(
                    name, os.O_RDWR | os.O_CREAT | cache_paths.NOFOLLOW, 0o600, dir_fd=dir_fd
                )
        except BaseException:
            os.close(dir_fd)
            raise
        try:
            if deadline is None:
                fcntl.flock(fd, mode)
            else:
                while True:
                    try:
                        fcntl.flock(fd, mode | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            os.close(fd)
                            return None
                        time.sleep(0.02)
            try:
                with cache_paths.naming(path):
                    here = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                same = os.fstat(fd).st_ino == here.st_ino
            except FileNotFoundError:
                same = False
        except BaseException:
            os.close(fd)
            raise
        finally:
            os.close(dir_fd)
        if same:
            return fd
        os.close(fd)


@contextlib.contextmanager
def read_lock(hosted_root: Path) -> Iterator[Path]:
    """Hold the cache's read lock (``LOCK_SH`` on ``locks/cache_rw.lock``); yields
    the cache's ``.lattice/``.

    Raises ``CACHE_INCOMPLETE`` when ``cache/applying`` is present once the
    lock is held: no live syncer can be mid-apply then, so a syncer died
    mid-apply and the tree is mixed.
    """
    lattice_dir = Path(hosted_root) / LATTICE_DIR
    cache_paths.require_safe_layout(Path(hosted_root))
    if not lattice_dir.is_dir():
        yield lattice_dir
        return
    locks = lattice_dir / "locks"
    fd = _lock(locks / "cache_rw.lock", exclusive=False, deadline=None)
    try:
        if (lattice_dir / "cache" / "applying").exists():
            identity = cache_identity(Path(hosted_root))
            name = f"{identity[0]}/{identity[1]}" if identity else "the server"
            raise OpError(
                "CACHE_INCOMPLETE",
                "the cache was interrupted mid-update and the server is unreachable; "
                f"run `lattice sync` when {name} is back.",
                {"root": str(hosted_root)},
            )
        # Only the syncer's exclusive apply (and ``cache clear``) changes a
        # cache, so while this lock is held the per-task storage locks have no
        # writer to exclude.
        with frozen_board(locks):
            yield lattice_dir
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# The sync ticket: one sync serves every caller that waited for it (SPEC §9.4)
# ---------------------------------------------------------------------------

#: ``locks/cache_sync.json``: coordination between live processes, written only
#: by the holder of ``cache_sync.lock`` (atomic replace, never fsynced).
TICKET_FILE = "cache_sync.json"
SUCCESS_KINDS: frozenset[str] = frozenset({"applied", "unchanged"})
ANY_KIND: frozenset[str] = frozenset({"applied", "unchanged", "unreachable", "busy", "incomplete"})
#: How often a caller waiting for another process's sync looks again.
POLL_SECONDS = 0.01


@dataclass(frozen=True)
class _Ticket:
    """The ticket record. ``started`` counts sync requests: a syncer takes the
    next number just before it sends each sync request. ``finished`` is the
    number of the last sync that ended with an outcome (``kind``, ``detail``).
    ``generation`` changes whenever the counter could go back (a missing or
    unreadable record, another binding, ``cache clear``), so a caller never
    compares numbers across two counters."""

    generation: str
    remote: str
    project: str
    started: int
    finished: int
    kind: str | None
    detail: str | None


def _count(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _read_ticket(lattice_dir: Path) -> _Ticket | None:
    """The current record, or ``None`` when it is missing or not a valid one."""
    try:
        fd = _dir_fd(lattice_dir, "locks", create=False)
    except (OSError, OpError):
        return None
    try:
        raw = cache_paths.read_file(fd, TICKET_FILE)
        data = json.loads(raw) if raw is not None else None
    except (OSError, ValueError, RecursionError):
        return None
    finally:
        os.close(fd)
    if not isinstance(data, dict):
        return None
    started, finished = _count(data.get("started")), _count(data.get("finished"))
    kind, detail = data.get("kind"), data.get("detail")
    generation, remote, project = data.get("generation"), data.get("remote"), data.get("project")
    if (
        not (isinstance(generation, str) and generation)
        or not (isinstance(remote, str) and remote)
        or not (isinstance(project, str) and project)
        or started is None
        or finished is None
        or finished > started
        or not (kind is None or (isinstance(kind, str) and kind in ANY_KIND))
        or not (detail is None or isinstance(detail, str))
    ):
        return None
    return _Ticket(generation, remote, project, started, finished, kind, detail)


def _write_ticket(lattice_dir: Path, ticket: _Ticket) -> None:
    fd = _dir_fd(lattice_dir, "locks")
    try:
        text = json.dumps(ticket.__dict__, sort_keys=True, indent=2) + "\n"
        cache_paths.write_file(fd, TICKET_FILE, text.encode("utf-8"), fsync=False)
    finally:
        os.close(fd)


def _rotate_ticket(lattice_dir: Path, remote: str, project: str) -> _Ticket | None:
    """Start a new generation (the caller holds ``cache_sync.lock``); ``None``
    if it could not be written."""
    ticket = _Ticket(secrets.token_hex(8), remote, project, 0, 0, None, None)
    try:
        _write_ticket(lattice_dir, ticket)
    except (OSError, OpError):
        return None
    return ticket


def _remove_ticket(lattice_dir: Path) -> None:
    try:
        fd = _dir_fd(lattice_dir, "locks", create=False)
    except (OSError, OpError):
        return
    try:
        with contextlib.suppress(OSError):
            cache_paths.remove_file(fd, TICKET_FILE)
    finally:
        os.close(fd)


def _adoptable(lattice_dir: Path, sample: _Ticket | None, kinds: frozenset[str]) -> _Ticket | None:
    """The record, if a sync that sent its request after *sample* was read has
    finished with an outcome in *kinds*; else ``None``. A caller that sampled
    no valid record, or finds another generation, identity, or a counter that
    went back, never adopts: it syncs itself."""
    if sample is None:
        return None
    now = _read_ticket(lattice_dir)
    if (
        now is None
        or now.generation != sample.generation
        or (now.remote, now.project) != (sample.remote, sample.project)
        or now.started < sample.started
    ):
        return None
    if now.finished > sample.started and now.kind in kinds:
        return now
    return None


def sample_ticket(hosted_root: Path) -> _Ticket | None:
    """The ticket record now, for :func:`open_window_in_order`."""
    return _read_ticket(Path(hosted_root) / LATTICE_DIR)


def open_window_in_order(
    hosted_root: Path, since: _Ticket | None, open_window: Callable[[], None]
) -> bool:
    """Run *open_window* (open the offline window) for a request that failed to
    reach the server and began when the record was *since*, unless that would
    put an older outcome over a newer success. It runs under ``cache_sync.lock``
    (created if missing: a first sync may be starting in another process), and
    not at all while a sync is in flight (whose outcome decides) or when a sync
    that took its ticket after *since* last finished ``applied`` or
    ``unchanged``. Returns whether it ran."""
    return _in_ticket_order(hosted_root, since, open_window, skip_if=SUCCESS_KINDS)


def close_window_in_order(
    hosted_root: Path, since: _Ticket | None, close_window: Callable[[], None]
) -> bool:
    """Run *close_window* (end the offline window) for a request that reached the
    server and began when the record was *since*, in the same order as
    :func:`open_window_in_order`: not while a sync is in flight, and not when a
    sync that took its ticket after *since* last finished ``unreachable`` (it
    may have opened the window after this request's success; the server was
    unreachable then anyway). A successful sync ends the window itself, under
    the lock. Always under ``cache_sync.lock``, created if missing, as for
    opening. Returns whether it ran."""
    return _in_ticket_order(hosted_root, since, close_window, skip_if=frozenset({"unreachable"}))


def _in_ticket_order(
    hosted_root: Path,
    since: _Ticket | None,
    action: Callable[[], None],
    *,
    skip_if: frozenset[str],
) -> bool:
    lattice_dir = Path(hosted_root) / LATTICE_DIR
    sync_lock = lattice_dir / "locks" / "cache_sync.lock"
    try:
        fd = _lock(sync_lock, exclusive=True, deadline=time.monotonic())
    except (OSError, OpError):
        return False
    if fd is None:
        return False
    try:
        now = _read_ticket(lattice_dir)
        if now is not None and now.kind in skip_if:
            newer = (
                since is None or now.generation != since.generation or now.finished > since.started
            )
            if newer:
                return False
        action()
        return True
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------


def _chmod(path: Path, mode: int) -> None:
    with contextlib.suppress(FileNotFoundError):
        if not os.path.islink(path):
            os.chmod(path, mode)


def _restore_private_modes(lattice_dir: Path) -> None:
    _chmod(lattice_dir, PRIVATE_DIR_MODE)
    for name in ("cache", *RUNTIME_DIRS):
        _chmod(lattice_dir / name, PRIVATE_DIR_MODE)


def _restore_all_modes(lattice_dir: Path) -> None:
    """Every synced file 0400, every synced directory 0500, the rest 0700."""
    files, dirs = _walk_synced(lattice_dir)
    for rel, info in files:
        if stat.S_ISREG(info.st_mode):
            _chmod(lattice_dir / rel, FILE_MODE)
    for rel in dirs:
        _chmod(lattice_dir / rel, SYNCED_DIR_MODE)
    _restore_private_modes(lattice_dir)


# ---------------------------------------------------------------------------
# The syncer
# ---------------------------------------------------------------------------


#: Server error codes that mean "not now" rather than "wrong": the only ones a
#: catch-up (and doctor) turns into an outcome instead of raising. A project
#: quarantined as ``BOARD_UNAVAILABLE`` (SPEC §8.6) cannot serve the cache until
#: its next load, so the cache reads as if the server were unreachable. Every
#: other code, whatever its HTTP status (a 500 ``INTEGRITY_ERROR`` included),
#: raises with its own code.
AVAILABILITY_CODES: dict[str, OutcomeKind] = {
    "BOARD_BUSY": "busy",
    "RATE_LIMITED": "busy",
    "BOARD_UNAVAILABLE": "unreachable",
}


def availability(exc: http.ServerError) -> OutcomeKind | None:
    """``busy`` or ``unreachable`` for an availability code, else ``None``."""
    return AVAILABILITY_CODES.get(exc.code)


class _Restart(Exception):
    """Discard this cycle's fetches and sync again from the current state."""

    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


class _Transient(Exception):
    """The server could not give an answer now: an outcome, not an error."""

    def __init__(self, kind: OutcomeKind, detail: str):
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


@dataclass
class _Syncer:
    root: Path
    remote: http.Remote
    project: str
    bulk: bool
    deadline: float | None  # the probe's monotonic budget; None in bulk
    staged: dict[str, Path] = field(default_factory=dict)
    #: The ticket this syncer published last (``None``: none, or not published).
    ticket: _Ticket | None = None

    @property
    def lattice_dir(self) -> Path:
        return self.root / LATTICE_DIR

    @property
    def cache_dir(self) -> Path:
        return self.lattice_dir / "cache"

    @property
    def label(self) -> str:
        return f"{self.remote.alias}/{self.project}"

    def state(self) -> dict:
        return _read_json(self.cache_dir / "state.json")

    def outcome(self, kind: OutcomeKind, detail: str | None = None) -> SyncOutcome:
        state = self.state()
        head = state.get("head_seq") if state.get("epoch") else None
        return SyncOutcome(kind, head, state.get("synced_at"), detail)

    def _policy(self) -> http.Policy:
        if self.deadline is None:
            return http.BULK.with_progress(f"syncing {self.label}")
        remaining = max(0.05, self.deadline - time.monotonic())
        return http.Policy(min(2.0, remaining), remaining, f"syncing {self.label}")

    # -- the cycle ------------------------------------------------------------

    def run(self) -> SyncOutcome:
        restart: _Restart | None = None
        stale_restarts = 0
        cycles = 0
        while True:
            cycles += 1
            try:
                return self._cycle()
            except _Transient as exc:
                interrupted = (self.cache_dir / "applying").exists()
                return self.outcome("incomplete" if interrupted else exc.kind, exc.detail)
            except _Restart as exc:
                restart = exc
            finally:
                self._clear_staging()
            if restart.reason == "STALE_VERSION":
                stale_restarts += 1
                if stale_restarts >= 5:
                    return self.outcome("busy", "the board kept changing during the sync")
            elif cycles >= MAX_CYCLES:
                raise _integrity(
                    restart.reason,
                    f"{self.label}: {restart.detail} after {MAX_CYCLES} attempts; the cache "
                    "was left as it was.",
                )

    def _cycle(self) -> SyncOutcome:
        lattice_dir = self.lattice_dir
        state = self.state()
        interrupted = (self.cache_dir / "applying").exists()
        if interrupted:
            _restore_all_modes(lattice_dir)
        forced = (
            interrupted
            or not state.get("epoch")
            or state.get("fingerprint") != fingerprint(lattice_dir)
        )
        query: dict[str, Any] = {"since": 0}
        if not forced:
            query = {"since": state.get("head_seq", 0), "epoch": state["epoch"]}
            if state.get("head_hash"):
                query["hash"] = state["head_hash"]
        path = f"/v1/projects/{urllib.parse.quote(self.project, safe='')}/sync?" + (
            urllib.parse.urlencode(query)
        )
        self._take_ticket()
        response = self._get(path, what="sync")
        delta = _parse_delta(self.remote, _data(response), lattice_dir, response.server_version)
        if forced and not delta.reset:
            raise _integrity(
                "MALFORMED_SYNC",
                f"{self.label}: the server answered a full-sync request with a delta",
            )
        if (
            not delta.reset
            and not delta.files
            and not delta.removed
            and delta.epoch == state.get("epoch")
            and delta.head_seq == state.get("head_seq")
        ):
            self._mark_unchanged(state, delta.server_version)
            return self.outcome("unchanged")
        contents = self._resolve(delta)
        self._apply(delta, contents)
        return self.outcome("applied")

    def _take_ticket(self) -> None:
        """Publish the next ticket number, just before the sync request is sent:
        every caller that read the record before now may adopt this sync."""
        _step("sync_ticket")
        current = _read_ticket(self.lattice_dir)
        if current is None or (current.remote, current.project) != (
            self.remote.alias,
            self.project,
        ):
            current = _Ticket(
                secrets.token_hex(8), self.remote.alias, self.project, 0, 0, None, None
            )
        ticket = replace(current, started=current.started + 1)
        try:
            _write_ticket(self.lattice_dir, ticket)
        except (OSError, OpError):
            self.ticket = None  # unpublished: nobody may adopt this sync
            return
        self.ticket = ticket
        _step("sync_ticket_taken")

    def finish_ticket(self, outcome: SyncOutcome) -> None:
        """Record this sync's outcome under its own ticket, unless the record
        is no longer the one it published (deleted or replaced meanwhile)."""
        mine = self.ticket
        if mine is None or _read_ticket(self.lattice_dir) != mine:
            return
        done = replace(mine, finished=mine.started, kind=outcome.kind, detail=outcome.detail)
        with contextlib.suppress(OSError, OpError):
            _write_ticket(self.lattice_dir, done)

    def _get(
        self, path: str, *, what: str, expect: str = "json", sink: Callable | None = None
    ) -> http.Response:
        try:
            return http.request(
                self.remote,
                "GET",
                path,
                expect=expect,
                policy=self._policy()
                if expect == "json"
                else http.BULK.with_progress(f"syncing {self.label}"),
                sink=sink,
                what=what,
            )
        except http.Unreachable as exc:
            raise _Transient(
                "unreachable", f"cannot reach {self.remote.alias}: {exc.reason}"
            ) from None
        except http.ServerError as exc:
            if exc.code == "STALE_VERSION" and expect == "bytes":
                raise _Restart("STALE_VERSION", exc.message) from None
            kind = availability(exc)
            if kind is not None:
                raise _Transient(kind, f"{self.remote.alias} answered {exc.code}") from None
            raise

    # -- fetch and verify -----------------------------------------------------

    def _resolve(self, delta: _Delta) -> dict[str, bytes | Path]:
        """Every file's verified bytes (in memory, or staged under
        ``cache/incoming/`` for fetched files), keyed by path."""
        contents: dict[str, bytes | Path] = {}
        for entry in delta.files:
            local = self.lattice_dir / entry.rel
            if entry.append_from is not None and entry.content is not None:
                data = self._append(local, entry)
                if data is not None:
                    contents[entry.rel] = data
                    continue
            elif entry.content is not None:
                if _matches(entry.content, entry):
                    contents[entry.rel] = entry.content
                    continue
                raise _Restart("HASH_MISMATCH", f"{entry.rel} did not match its sha256")
            elif delta.reset and _sha256_file(local) == entry.sha256:
                # A reset that re-sends a file this cache already holds
                # byte for byte: nothing to fetch.
                contents[entry.rel] = local
                continue
            contents[entry.rel] = self._fetch(entry)
        return contents

    def _append(self, local: Path, entry: _Entry) -> bytes | None:
        """Local bytes plus the appended bytes, if the local copy is exactly
        ``append_from`` long and the result matches; else ``None`` (fetch whole)."""
        try:
            info = os.lstat(local)
            if not stat.S_ISREG(info.st_mode) or info.st_size != entry.append_from:
                return None
            data = local.read_bytes()
        except OSError:
            return None
        if len(data) != entry.append_from:
            return None
        combined = data + (entry.content or b"")
        return combined if _matches(combined, entry) else None

    def _fetch(self, entry: _Entry) -> Path:
        assert entry.href is not None
        incoming = self.cache_dir / "incoming"
        _private_dir(self.lattice_dir, "cache/incoming")
        staged = incoming / f"{len(self.staged):06d}"
        digest = hashlib.sha256()
        size = 0
        with open(staged, "wb") as fh:

            def sink(chunk: bytes) -> None:
                nonlocal size
                digest.update(chunk)
                size += len(chunk)
                fh.write(chunk)

            self.staged[entry.rel] = staged
            self._get(entry.href, what=f"fetch of {entry.rel}", expect="bytes", sink=sink)
        if digest.hexdigest() != entry.sha256 or size != entry.size:
            raise _Restart("HASH_MISMATCH", f"{entry.rel} did not match its sha256")
        return staged

    def _clear_staging(self) -> None:
        self.staged.clear()
        try:
            fd = _dir_fd(self.lattice_dir, "cache", create=False)
        except (OSError, OpError):
            return
        try:
            shutil.rmtree("incoming", ignore_errors=True, dir_fd=fd)
        finally:
            os.close(fd)

    # -- apply ----------------------------------------------------------------

    def _mark_unchanged(self, state: dict, server_version: str | None) -> None:
        state = {**state, "synced_at": _utc_now()}
        if server_version:
            state["server_version"] = server_version
        with syncing_board(self.lattice_dir):
            self._write_state(state)
        self._clear_unreachable()

    def _write_state(self, state: dict) -> None:
        _write_cache_file(
            self.lattice_dir, "state.json", json.dumps(state, sort_keys=True, indent=2) + "\n"
        )

    def _clear_unreachable(self) -> None:
        _remove_cache_file(self.lattice_dir, "unreachable_until")

    def _apply(self, delta: _Delta, contents: dict[str, bytes | Path]) -> None:
        lattice_dir = self.lattice_dir
        # Once, at the start of the apply (reset and rescue included): its base
        # directories are real directories, never a symlink (cache_paths).
        cache_paths.require_safe_layout(self.root)
        fd = _lock(lattice_dir / "locks" / "cache_rw.lock", exclusive=True, deadline=None)
        try:
            with syncing_board(lattice_dir):
                self._apply_locked(delta, contents)
        except OSError as exc:
            # Out of disk, a permission surprise: cache/applying stays behind,
            # so reads refuse the mixed tree and the next sync resets it.
            raise _Transient("incomplete", f"the cache update failed: {exc}") from None
        finally:
            os.close(fd)

    def _apply_locked(self, delta: _Delta, contents: dict[str, bytes | Path]) -> None:
        lattice_dir = self.lattice_dir
        _write_cache_file(
            lattice_dir,
            "applying",
            json.dumps(
                {
                    "remote": self.remote.alias,
                    "project": self.project,
                    "started_at": _utc_now(),
                    "pid": os.getpid(),
                    "kind": "reset" if delta.reset else "delta",
                    "epoch": delta.epoch,
                    "target_head_seq": delta.head_seq,
                },
                sort_keys=True,
                indent=2,
            )
            + "\n",
        )
        _step("applying_written")
        if delta.reset:
            for name in RUNTIME_DIRS:
                _private_dir(lattice_dir, name)
            _files, dirs = _walk_synced(lattice_dir)
            for rel in dirs:
                _chmod(lattice_dir / rel, PRIVATE_DIR_MODE)
            for rel in STANDARD_DIRS:
                self._ensure_synced_dir(lattice_dir / rel)
            wanted = {entry.rel: entry.sha256 for entry in delta.files}
            self._rescue(wanted)
        written: list[str] = []
        opened: set[Path] = set()
        for entry in delta.files:
            target = lattice_dir / entry.rel
            self._open_parents(target, opened)
            data = contents[entry.rel]
            if isinstance(data, Path):
                if data == target:
                    # A reset re-sent a file this cache already holds; if it was
                    # edited since it was checked, it was rescued: start over.
                    if _sha256_file(target) == entry.sha256:
                        continue
                    raise _Restart("HASH_MISMATCH", f"{entry.rel} changed during the sync")
                data = data.read_bytes()
            atomic_write(target, data)
            os.chmod(target, FILE_MODE)
            written.append(entry.rel)
            _step("file_written")
        for rel in delta.removed:
            target = lattice_dir / rel
            if os.path.lexists(target) and not stat.S_ISDIR(os.lstat(target).st_mode):
                self._open_parents(target, opened)
                self._remove(target)
                _step("removed")
        if delta.reset:
            _restore_all_modes(lattice_dir)
        else:
            for directory in sorted(opened, key=lambda p: -len(p.parts)):
                _chmod(directory, SYNCED_DIR_MODE)
            _restore_private_modes(lattice_dir)
        _step("modes_restored")
        self._write_state(
            {
                "remote": self.remote.alias,
                "project": self.project,
                "epoch": delta.epoch,
                "head_seq": delta.head_seq,
                "head_hash": delta.head_hash,
                "server_version": delta.server_version,
                "synced_at": _utc_now(),
                "fingerprint": fingerprint(lattice_dir),
            }
        )
        _step("state_written")
        self._clear_unreachable()
        _remove_cache_file(lattice_dir, "applying")

    def _ensure_synced_dir(self, directory: Path) -> None:
        """Create a synced directory and its missing parents, each writable for
        the apply (modes are restored at its end)."""
        missing: list[Path] = []
        current = directory
        while not current.is_dir() and current != self.lattice_dir:
            missing.append(current)
            current = current.parent
        for path in reversed(missing):
            ensure_dir(path)
            os.chmod(path, PRIVATE_DIR_MODE)

    def _open_parents(self, target: Path, opened: set[Path]) -> None:
        """Make every synced directory above *target* writable for this apply."""
        parent = target.parent
        chain: list[Path] = []
        while parent != self.lattice_dir and self.lattice_dir in parent.parents:
            chain.append(parent)
            parent = parent.parent
        for directory in reversed(chain):
            if directory in opened:
                continue
            if directory.is_dir():
                os.chmod(directory, PRIVATE_DIR_MODE)
            else:
                self._ensure_synced_dir(directory)
            opened.add(directory)

    def _remove(self, target: Path) -> None:
        """Remove a synced entry itself, never following it: a planted symlink
        goes, its target (possibly outside the board) stays."""
        unlink_entry(target)

    # -- rescue (SPEC §9.4: a reset never discards a local edit) --------------

    def _rescue(self, wanted: dict[str, str]) -> None:
        """Move every synced file the reset would change or drop into
        ``cache/rescued/<UTC stamp>/``, durably, before it is replaced."""
        lattice_dir = self.lattice_dir
        victims = [
            rel
            for rel, _info in _walk_synced(lattice_dir)[0]
            if rel not in wanted or _sha256_file(lattice_dir / rel) != wanted[rel]
        ]
        if not victims:
            return
        rescue_dir = self._new_rescue_dir()
        for rel in victims:
            self._rescue_one(lattice_dir / rel, rescue_dir / rel)
        print(
            f"lattice: {len(victims)} locally edited board file(s) moved to {rescue_dir}; "
            "the cache is read-only. Write a plan with: lattice plan write <task> "
            "--file <path> (notes: lattice notes write)",
            file=sys.stderr,
        )

    def _new_rescue_dir(self) -> Path:
        base = self.cache_dir / "rescued"
        _private_dir(self.lattice_dir, "cache/rescued")
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        candidate, n = base / stamp, 1
        while os.path.lexists(candidate):
            n += 1
            candidate = base / f"{stamp}-{n}"
        _private_dir(self.lattice_dir, f"cache/rescued/{candidate.name}")
        return candidate

    def _rescue_one(self, source: Path, dest: Path) -> None:
        _private_dir(self.lattice_dir, dest.parent.relative_to(self.lattice_dir).as_posix())
        tmp = dest.parent / f".rescue-{secrets.token_hex(6)}.tmp"
        mode = os.lstat(source).st_mode
        if stat.S_ISLNK(mode):
            os.symlink(os.readlink(source), tmp)
        elif not stat.S_ISREG(mode):
            self._remove(source)  # a socket or FIFO holds no edit to keep
            return
        else:
            with open(source, "rb") as src, open(tmp, "wb") as out:
                shutil.copyfileobj(src, out, 1 << 20)
                out.flush()
                os.fsync(out.fileno())
        _step("rescue_copied")
        os.replace(tmp, dest)
        _step("rescue_renamed")
        _fsync_dir(dest.parent)
        _step("rescue_dir_synced")
        self._remove(source)
        _step("rescue_unlinked")


def _matches(data: bytes, entry: _Entry) -> bool:
    return len(data) == entry.size and hashlib.sha256(data).hexdigest() == entry.sha256


def _data(response: http.Response) -> Any:
    try:
        return response.data()
    except (ValueError, KeyError):
        raise _integrity("MALFORMED_SYNC", "the server's sync answer is not JSON") from None


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# The published entry point
# ---------------------------------------------------------------------------


def catch_up(
    hosted_root: Path,
    *,
    bulk: bool = False,
    adopt: frozenset[str] = SUCCESS_KINDS,
    on_unreachable: Callable[[], None] | None = None,
) -> SyncOutcome:
    """Bring the cache at *hosted_root* (the checkout holding ``.lattice/``) up to
    the server's head; see the module docstring for the cycle.

    ``bulk=False`` is a command's catch-up probe: waiting for another sync's
    lock and waiting for the server's answer to start share one 5-second
    budget (``busy`` / ``unreachable`` when it runs out); once the answer
    starts, the transfer runs under the bulk budget. ``bulk=True`` (``lattice
    sync``, ``remote attach``, the follower) waits for the lock as long as it
    takes and uses the bulk policy.

    **Coalescing.** A caller is served by any sync whose request was sent after
    the caller read the ticket record, whoever ran it: while another process
    holds ``cache_sync.lock``, the caller polls the record and returns that
    sync's outcome when its ``kind`` is in *adopt* (default: ``applied`` or
    ``unchanged``; an ordinary read passes :data:`ANY_KIND`). Otherwise it
    takes the lock and syncs itself. So it waits for at most the sync in
    flight when it arrived and the next one. *on_unreachable* runs under
    ``cache_sync.lock`` when this caller's own sync ends ``unreachable`` (the
    offline window is opened in ticket order, never after a newer success).
    """
    _fcntl()
    root = Path(hosted_root)
    identity = cache_identity(root)
    if identity is None:
        raise not_hosted(root)
    alias, project = identity
    lattice_dir = root / LATTICE_DIR
    cache_paths.require_safe_layout(root)
    if lattice_dir.is_dir() and not has_cache_marker(lattice_dir) and synced_files(lattice_dir):
        raise OpError(
            "BINDING_CONFLICT",
            f"{root} holds a local board beside its hosted binding; a sync would replace it. "
            "Move the board to the server first (the hosted guide's move steps).",
            {"root": str(root)},
        )
    remote = resolve_remote(alias)
    deadline = None if bulk else time.monotonic() + PROBE_SECONDS
    _private_dir(lattice_dir)
    syncer = _Syncer(root, remote, project, bulk, deadline)
    sample = _read_ticket(lattice_dir)
    sync_lock = lattice_dir / "locks" / "cache_sync.lock"
    while True:
        fd = _lock(sync_lock, exclusive=True, deadline=time.monotonic())
        adopted = _adoptable(lattice_dir, sample, adopt)
        if adopted is not None:
            if fd is not None:
                os.close(fd)
            return syncer.outcome(adopted.kind, adopted.detail)  # type: ignore[arg-type]
        if fd is not None:
            break
        if deadline is not None and time.monotonic() >= deadline:
            return syncer.outcome("busy", "another sync of this cache is in progress")
        time.sleep(POLL_SECONDS)
    try:
        # A `cache clear --forget` may have run while this waited for the lock:
        # route by what is on disk now, never by what was read before it.
        now = cache_identity(root)
        if now is None:
            raise not_hosted(root)
        if now != identity:
            syncer = _Syncer(root, resolve_remote(now[0]), now[1], bulk, deadline)
        _private_dir(lattice_dir, "cache")
        outcome = syncer.run()
        if outcome.kind == "unreachable" and on_unreachable is not None:
            on_unreachable()
        syncer.finish_ticket(outcome)
        return outcome
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# lattice cache clear
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClearResult:
    root: Path
    remote: str
    project: str
    forgot: bool
    rescued: Path | None


def clear_cache(hosted_root: Path, *, forget: bool = False) -> ClearResult:
    """Delete the cache at *hosted_root* (SPEC §9.4 "Clearing a cache").

    Keeps ``cache/rescued/``, the runtime ``locks/`` (so every waiter stays on
    the same lock files), and, unless *forget*, a routing marker
    ``cache/state.json`` holding only ``{remote, project}``. Refuses with
    ``NOT_HOSTED``, deleting nothing, unless ``.lattice/`` carries a cache
    marker, so it can never delete a local board.
    """
    _fcntl()
    root = Path(hosted_root)
    lattice_dir = root / LATTICE_DIR
    bad = cache_paths.unsafe_component(root)
    if bad is not None:
        # Never walk, chmod, or delete through it; a checkout nothing routes
        # to the server stays NOT_HOSTED, as before.
        if cache_identity(root) is None:
            raise not_hosted(root)
        raise cache_paths.layout_error(root, bad)
    if not lattice_dir.is_dir() or not has_cache_marker(lattice_dir):
        raise not_hosted(root)
    identity = cache_identity(root)
    if identity is None:
        raise not_hosted(root)
    remote, project = identity
    locks = lattice_dir / "locks"
    sync_fd = _lock(locks / "cache_sync.lock", exclusive=True, deadline=None)
    try:
        # A new generation: no caller waiting from before the clear adopts a
        # sync that ran before it (the ticket file itself stays under locks/).
        # If the new record cannot be written, remove the old one: a missing
        # record makes every caller sync itself too.
        if _rotate_ticket(lattice_dir, remote, project) is None:
            _remove_ticket(lattice_dir)
        rw_fd = _lock(locks / "cache_rw.lock", exclusive=True, deadline=None)
        try:
            # Both locks are held for the whole clear, and ``locks/`` (runtime
            # state) is never deleted, so every sync and reader waits on the
            # same lock files and none starts on a half-cleared tree.
            for dirpath, dirnames, _files in os.walk(lattice_dir):
                for name in dirnames:
                    _chmod(Path(dirpath) / name, PRIVATE_DIR_MODE)
            _chmod(lattice_dir, PRIVATE_DIR_MODE)
            for entry in list(lattice_dir.iterdir()):
                if entry.name == "locks" and entry.is_dir() and not entry.is_symlink():
                    continue
                if entry.name == "cache" and entry.is_dir() and not entry.is_symlink():
                    for sub in list(entry.iterdir()):
                        if sub.name != "rescued":
                            _delete(sub)
                else:
                    _delete(entry)
            _step("clear_deleted")
            rescued = lattice_dir / "cache" / "rescued"
            kept = rescued if rescued.is_dir() and any(rescued.iterdir()) else None
            if not forget:
                _write_cache_file(
                    lattice_dir,
                    "state.json",
                    json.dumps({"project": project, "remote": remote}, sort_keys=True, indent=2)
                    + "\n",
                )
            if forget and kept is None:
                with contextlib.suppress(OSError):
                    (lattice_dir / "cache").rmdir()
            _step("clear_finished")
        finally:
            os.close(rw_fd)
    finally:
        os.close(sync_fd)
    return ClearResult(root, remote, project, forget, kept)


def _delete(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# lattice doctor on a cache: compare with the server's manifest (SPEC §9.6)
# ---------------------------------------------------------------------------


def _fetch_manifest(root: Path, alias: str, project: str) -> tuple[dict | None, dict | None]:
    """Catch up, then fetch the server's manifest: ``(manifest, None)``, or
    ``(None, warning)`` when the server is unreachable or busy. Every other
    failure raises with its own code."""
    outcome = catch_up(root)
    if outcome.kind in ("unreachable", "busy"):
        return None, _unavailable(f"{outcome.kind}: {outcome.detail}")
    if outcome.kind == "incomplete":
        return None, None  # read_lock refuses the mixed tree with CACHE_INCOMPLETE
    remote = resolve_remote(alias)
    path = f"/v1/projects/{urllib.parse.quote(project, safe='')}/sync?since=0&manifest=1"
    try:
        response = http.request(remote, "GET", path, policy=http.BULK, what="manifest")
    except http.Unreachable as exc:
        return None, _unavailable(f"cannot reach {alias}: {exc.reason}")
    except http.ServerError as exc:
        if availability(exc) is None:
            raise
        return None, _unavailable(f"{alias} answered {exc.code}")
    manifest = _data(response)
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(files, dict) or not all(isinstance(v, dict) for v in files.values()):
        raise _integrity("MALFORMED_SYNC", "the server's manifest is malformed")
    return manifest, None


@contextlib.contextmanager
def cache_check(hosted_root: Path, *, attempts: int = 3) -> Iterator[list[dict]]:
    """Hold the cache's read lock for doctor, yielding the manifest findings.

    Catches up and fetches the manifest outside the lock (an apply waits for
    readers, so catching up inside it could deadlock), then takes the lock and
    checks that the manifest's head is the locked ``state.json`` head; on a
    mismatch it releases and retries the whole sequence (bounded). The caller
    runs every other check inside the block, so all enumeration, reads, and
    hashing see one tree. An unreachable or busy server yields one warning;
    every other failure raises with its own code.
    """
    root = Path(hosted_root)
    identity = cache_identity(root)
    if identity is None:
        raise not_hosted(root)
    alias, project = identity
    for attempt in range(attempts):
        manifest, warning = _fetch_manifest(root, alias, project)
        with read_lock(root) as lattice_dir:
            if manifest is not None:
                state = _read_json(lattice_dir / "cache" / "state.json")
                heads = (manifest.get("epoch"), manifest.get("head_seq"))
                if heads != (state.get("epoch"), state.get("head_seq")):
                    if attempt + 1 < attempts:
                        continue
                    warning = _unavailable(
                        "the board kept changing while doctor compared it with the server"
                    )
            if warning is not None:
                yield [warning]
            elif manifest is None:
                yield []
            else:
                yield _compare(lattice_dir, manifest["files"])
            return


def manifest_findings(hosted_root: Path, *, attempts: int = 3) -> list[dict]:
    """The manifest comparison alone (see :func:`cache_check`)."""
    with cache_check(hosted_root, attempts=attempts) as findings:
        return findings


def _unavailable(detail: str) -> dict:
    return {
        "level": "warning",
        "check": "cache_manifest_unavailable",
        "message": f"Could not compare the cache with the server's manifest ({detail}).",
        "task_id": None,
    }


def _compare(lattice_dir: Path, files: dict) -> list[dict]:
    findings: list[dict] = []
    local = set(synced_files(lattice_dir))
    for rel in sorted(local | set(files)):
        spec = files.get(rel)
        if rel not in local:
            findings.append(
                {
                    "level": "error",
                    "check": "cache_missing_file",
                    "message": f"{rel} is on the server but missing from the cache.",
                    "task_id": None,
                }
            )
        elif not isinstance(spec, dict):
            findings.append(
                {
                    "level": "error",
                    "check": "cache_extra_file",
                    "message": f"{rel} is in the cache but not on the server.",
                    "task_id": None,
                }
            )
        elif _sha256_file(lattice_dir / rel) != spec.get("sha256"):
            findings.append(
                {
                    "level": "error",
                    "check": "cache_local_modification",
                    "message": f"{rel} differs from the server's copy (modified locally).",
                    "task_id": None,
                }
            )
    return findings
