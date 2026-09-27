"""Audit history: each project directory is a git repository (SPEC §8.10).

With ``audit.enabled`` and ``git`` on ``PATH``, ``projects/<slug>/`` is a git
repository whose history holds the board's durable data (SPEC §6.1) and
nothing else.

**What a commit holds.** Staging never asks git which files to take: it lists
the board's durable and workspace files itself (:func:`durable_files`),
hashes each one's raw bytes (``git hash-object --no-filters``, so no
``.gitattributes`` filter or line-ending rule can change them), and builds the
index from that listing alone (``read-tree --empty``, ``update-index
--index-info``). No ignore file is read, so a board's own ``.gitignore`` can
hide nothing, and ``hosted/``, runtime, temporary, and unmanaged paths can
never enter. The project directory's ``.gitignore`` is still the SPEC's
allowlist, for a human running ``git status``; the committer does not depend
on it. A stat cache means only changed files are rehashed. The listing and
hashing run in a stage worker process (:class:`Stager`), never on a server
thread, where each file's syscalls would wait behind the request threads for
the GIL while the work lock is held.

**The committer.** One :class:`AuditCommitter` per loaded project:

- :meth:`AuditCommitter.notify` is called under the project's work lock for
  every journaled line;
- ``debounce_seconds`` after the last write, and at most
  ``max_interval_seconds`` after the first uncommitted one, its thread
  prehashes the changed files with the board still live, then takes the work
  lock and stages, hashing only what changed since the prehash. An operation
  holds that lock for its whole transaction, so staging never sees a partial
  one. ``audit_commit`` reports ``prehash_ms``, ``lock_wait_ms``, ``stage_ms``
  (the lock hold), and ``commit_ms``;
- outside the lock it commits (``audit: seq <a>-<b> (<n> ops)``, with
  ``Lattice-Epoch`` and ``Lattice-Seq`` trailers naming the last journaled
  line it covers). A failed stage or commit is requeued and retried with
  backoff;
- ``git gc --auto`` and the push run on a separate maintenance thread, so a
  slow push or gc never delays the next commit. A failed push is logged and
  retried after the next commit; nothing here ever blocks or fails a write.

**Load, shutdown, unload.** At load, :meth:`AuditCommitter.reconcile` compares
the last commit's trailers with the journal and schedules a commit for
anything journaled since (a crash between the journal fsync and
:meth:`~AuditCommitter.notify`, or a failed commit before a restart), and
staging picks up a board changed while the server was down. Shutdown and
unload run, in order: drain operations (admission); :meth:`~AuditCommitter.stage`
under the work lock; release the lock; :meth:`~AuditCommitter.commit_and_stop`;
``clean_shutdown``; release the lease. :meth:`~AuditCommitter.drain` never
takes the work lock.

**Git's environment.** Every call ignores system and user configuration
(``GIT_CONFIG_NOSYSTEM``, ``GIT_CONFIG_GLOBAL=/dev/null``, injected
``GIT_CONFIG_*`` removed), runs no hook (G-5), never signs, never prompts, and
is bounded by a timeout. URLs never reach the log.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from lattice.core.errors import OpError
from lattice.server import control
from lattice.server.config import check_push
from lattice.storage.ownership import (
    DURABLE_DIRS,
    DURABLE_FILES,
    RECORDED_CLASSES,
    classify_path,
)

if TYPE_CHECKING:
    from lattice.server.config import AuditConfig
    from lattice.server.log import ServerLog

AUTHOR_NAME = "Lattice Hosted"
AUTHOR_EMAIL = "lattice-hosted@localhost"
BRANCH = "main"
#: The per-project push override, written by ``lattice server project audit``.
AUDIT_JSON = "audit.json"
#: Wall-clock bound on one git call; a push gets the same.
GIT_TIMEOUT_SECONDS = 120.0
PUSH_TIMEOUT_SECONDS = 120.0
#: How long shutdown waits for the last gc and push before leaving them behind.
FINAL_MAINTENANCE_SECONDS = 20.0
#: Retry backoff after a failed stage or commit: from min(1 s, debounce), doubling.
MAX_BACKOFF_SECONDS = 60.0

#: Paths per ``hash-object`` call (keeps the argument list far below ARG_MAX).
HASH_BATCH = 256

EPOCH_TRAILER = "Lattice-Epoch"
SEQ_TRAILER = "Lattice-Seq"

#: Workspace paths (SPEC §6.1) are durable in every respect but their name.
WORKSPACE_DIRS = ("orchestration",)

#: ``-c`` settings on every call; they outrank the repository's own config.
_GIT_SETTINGS = (
    "core.hooksPath=/dev/null",
    "core.excludesFile=/dev/null",
    "core.attributesFile=/dev/null",
    "core.autocrlf=false",
    "core.safecrlf=false",
    "core.fsmonitor=false",
    "core.quotePath=false",
    "commit.gpgSign=false",
    "gc.autoDetach=false",
    f"init.defaultBranch={BRANCH}",
)

_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s'\"]+")


def gitignore_text() -> str:
    """The allowlist ``.gitignore`` of a project directory (for humans; see the
    module docstring: staging lists files itself and reads no ignore file)."""
    lines = [
        "# Lattice audit history (SPEC §8.10): an allowlist. Only the board's durable",
        "# and workspace paths are recorded; hosted/, runtime, temporary, and",
        "# unmanaged paths never are. Written by the server; do not edit.",
        "/*",
        "!/.lattice/",
        "/.lattice/*",
    ]
    lines += [f"!/.lattice/{name}/" for name in sorted(DURABLE_DIRS) + list(WORKSPACE_DIRS)]
    lines += [f"!/.lattice/{name}" for name in sorted(DURABLE_FILES)]
    lines += ["# atomic_write temporaries (a crashed write can leave one behind)", ".tmp.*"]
    return "\n".join(lines) + "\n"


def git_executable() -> str | None:
    import shutil

    return shutil.which("git")


def availability(config: AuditConfig) -> tuple[bool, str | None]:
    """``(active, reason)``: whether this server keeps audit histories, and why not."""
    if not config.enabled:
        return False, "audit.enabled is false in server.json"
    if git_executable() is None:
        return False, "git is not on PATH"
    return True, None


# ---------------------------------------------------------------------------
# Running git
# ---------------------------------------------------------------------------


class GitError(Exception):
    def __init__(self, args: list[str], returncode: int | None, stderr: str) -> None:
        self.git_args = args
        self.returncode = returncode
        self.stderr = redact(stderr)
        super().__init__(f"git {args[0] if args else ''} failed ({returncode}): {self.stderr}")


def redact(text: str) -> str:
    """git's first ``fatal:`` or ``error:`` line (else its last line), every URL
    replaced by ``<url>``, at most 300 characters."""
    lines = [line.strip() for line in str(text).strip().splitlines() if line.strip()]
    errors = [line for line in lines if line.startswith(("fatal:", "error:"))]
    line = errors[0] if errors else (lines[-1] if lines else "")
    return _URL.sub("<url>", line)[:300]


def _env() -> dict[str, str]:
    """The inherited environment without anything that points git at another
    repository or configuration, plus the audit identity."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("GIT_") or k in ("GIT_SSH", "GIT_SSH_COMMAND", "GIT_SSH_VARIANT")
    }
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": AUTHOR_NAME,
            "GIT_AUTHOR_EMAIL": AUTHOR_EMAIL,
            "GIT_COMMITTER_NAME": AUTHOR_NAME,
            "GIT_COMMITTER_EMAIL": AUTHOR_EMAIL,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "/bin/false" if os.path.exists("/bin/false") else "false",
            "SSH_ASKPASS": "",
            "LC_ALL": "C",
        }
    )
    return env


def git(
    directory: Path,
    *args: str,
    check: bool = True,
    timeout: float = GIT_TIMEOUT_SECONDS,
    input: bytes | None = None,  # noqa: A002 - mirrors subprocess.run
) -> subprocess.CompletedProcess[bytes]:
    """Run ``git -C <directory> <args>`` with the audit settings (bytes in and out)."""
    command = ["git"]
    for setting in _GIT_SETTINGS:
        command += ["-c", setting]
    command += ["-C", str(directory), *args]
    try:
        done = subprocess.run(
            command,
            input=input,
            stdin=None if input is not None else subprocess.DEVNULL,
            capture_output=True,
            env=_env(),
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitError(list(args), None, f"timed out after {timeout:g} s") from exc
    except OSError as exc:
        raise GitError(list(args), None, str(exc)) from exc
    if check and done.returncode != 0:
        raise GitError(list(args), done.returncode, _text(done.stderr or done.stdout))
    return done


def _text(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def git_text(directory: Path, *args: str, check: bool = True) -> str:
    return _text(git(directory, *args, check=check).stdout).strip()


def is_repo(directory: Path) -> bool:
    return (Path(directory) / ".git").exists()


# ---------------------------------------------------------------------------
# Staging from an explicit listing
# ---------------------------------------------------------------------------


def durable_files(board: Path) -> Iterator[tuple[str, Path, os.stat_result]]:
    """``(path relative to .lattice/, path, stat)`` for every regular file of a
    durable or workspace class (SPEC §6.1). Symlinks and special files are not
    board data and are skipped."""
    board = Path(board)
    for dirpath, dirnames, filenames in os.walk(board):
        base = Path(dirpath)
        rel_dir = base.relative_to(board)
        if rel_dir == Path("."):
            dirnames[:] = [d for d in dirnames if classify_path(d) in RECORDED_CLASSES]
        dirnames.sort()
        for name in sorted(filenames):
            rel = (rel_dir / name).as_posix()
            if classify_path(rel) not in RECORDED_CLASSES:
                continue
            path = base / name
            try:
                st = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISREG(st.st_mode):
                yield rel, path, st


def _stat_key(st: os.stat_result) -> tuple[int, int, int, int]:
    return (st.st_mtime_ns, st.st_ctime_ns, st.st_size, st.st_ino)


class Stager:
    """Builds a tree of exactly the board's durable files and bytes, rehashing only
    files whose stat changed since the last stage.

    :meth:`stage` runs in a worker process that lives as long as this object
    (:func:`_stage_worker`, started on first use and again if it dies) and keeps
    the stat cache. The walk makes one GIL-releasing syscall per file, and inside
    a busy server each one waits behind the request threads to get the GIL back:
    on Atlas under AC-42 a 0.15 s stage of 3,700 files took 9 s, all of it under
    the work lock, so every write queued and readers' catch-ups timed out
    (LAT-340). The worker has its own GIL; the calling thread only waits for its
    reply. :meth:`stage_here` does the same work in this process.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.board = self.directory / ".lattice"
        self._cache: dict[str, tuple[tuple[int, int, int, int], str]] = {}
        self._lock = threading.Lock()
        self._worker: subprocess.Popen[bytes] | None = None

    def stage(self) -> str:
        """Rebuild the index from the board and return its tree id. Call with the
        board quiescent (under the project's work lock)."""
        return self._ask(b"stage")["tree"]

    def prehash(self) -> None:
        """Hash the files changed since the last stage into the worker's cache,
        with the board still changing (outside the work lock), so the stage
        under the lock hashes only what changed after this. A file that changes
        or vanishes meanwhile is simply hashed again by the stage."""
        self._ask(b"prehash")

    def _ask(self, command: bytes) -> dict[str, Any]:
        with self._lock:
            worker = self._worker
            if worker is None or worker.poll() is not None:
                worker = self._worker = self._start_worker()
            assert worker.stdin is not None and worker.stdout is not None
            try:
                worker.stdin.write(command + b"\n")
                worker.stdin.flush()
                line = worker.stdout.readline()
            except OSError:
                line = b""
            try:
                reply = json.loads(line) if line else None
            except ValueError:
                reply = None
            if not isinstance(reply, dict):
                self._stop_worker(wait=True)
                raise GitError(["stage"], worker.poll(), "the stage process ended without a reply")
            if "error" in reply:
                error = reply["error"]
                raise GitError(error["args"], error["returncode"], error["stderr"])
            return reply

    def close(self) -> None:
        """End the worker (it also ends when this process does). Never waits."""
        with self._lock:
            self._stop_worker(wait=False)

    def _start_worker(self) -> subprocess.Popen[bytes]:
        try:
            return subprocess.Popen(
                [sys.executable, "-c", _STAGE_WORKER, str(self.directory)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
            )
        except OSError as exc:
            raise GitError(["stage"], None, str(exc)) from exc

    def _stop_worker(self, *, wait: bool) -> None:
        worker, self._worker = self._worker, None
        if worker is None:
            return
        with contextlib.suppress(OSError):
            assert worker.stdin is not None
            worker.stdin.close()  # end of input: the worker exits
        if wait:
            try:
                worker.wait(5)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait()

    def stage_here(self) -> str:
        """:meth:`stage`'s work in this process (the worker runs it)."""
        listing = self._hash_changed(settled=True)
        index_info = b"".join(
            b"100644 " + sha.encode() + b"\t" + os.fsencode(".lattice/" + rel) + b"\0"
            for rel, sha in sorted(listing)
        )
        git(self.directory, "read-tree", "--empty")
        git(self.directory, "update-index", "-z", "--index-info", input=index_info)
        return git_text(self.directory, "write-tree")

    def _hash_changed(self, *, settled: bool) -> list[tuple[str, str]]:
        """Hash every file whose stat changed since it was cached; return the
        ``(rel, sha)`` listing. Each cache key is the stat taken before hashing,
        so a file that changes while it is hashed never matches it again.
        Unless *settled* (the board may be changing), a failed batch is left
        for the next call."""
        listing: list[tuple[str, str]] = []
        to_hash: list[tuple[str, Path, tuple[int, int, int, int]]] = []
        for rel, path, st in durable_files(self.board):
            key = _stat_key(st)
            cached = self._cache.get(rel)
            if cached is not None and cached[0] == key:
                listing.append((rel, cached[1]))
            else:
                to_hash.append((rel, path, key))
        for start in range(0, len(to_hash), HASH_BATCH):
            batch = to_hash[start : start + HASH_BATCH]
            # Paths as arguments after "--": any name a POSIX file may have (a
            # newline, a leading "-") passes intact; --stdin-paths is newline-delimited.
            try:
                out = git(
                    self.directory,
                    "hash-object",
                    "-w",
                    "--no-filters",
                    "--",
                    *(str(path) for _, path, _ in batch),
                )
            except GitError:
                if settled:
                    raise
                continue
            shas = _text(out.stdout).split()
            if len(shas) != len(batch):
                if not settled:
                    continue
                raise GitError(["hash-object"], 0, "hash-object returned a short listing")
            for (rel, _path, key), sha in zip(batch, shas, strict=True):
                self._cache[rel] = (key, sha)
                listing.append((rel, sha))
        if settled:
            present = {rel for rel, _ in listing}
            for rel in [r for r in self._cache if r not in present]:
                del self._cache[rel]
        return listing


_STAGE_WORKER = "from lattice.server.audit import _stage_worker; _stage_worker()"


def _stage_worker() -> None:
    """The stage worker (``sys.argv[1]`` is the directory): each input line is
    ``stage`` or ``prehash`` and gets one JSON line back, ``{"tree"}`` (``null``
    for a prehash) or ``{"error"}`` (a :class:`GitError`). It exits at end of
    input."""
    stager = Stager(Path(sys.argv[1]))
    for request in sys.stdin.buffer:
        try:
            if request.strip() == b"prehash":
                stager._hash_changed(settled=False)
                reply: dict[str, Any] = {"tree": None}
            else:
                reply = {"tree": stager.stage_here()}
        except GitError as exc:
            reply = {
                "error": {"args": exc.git_args, "returncode": exc.returncode, "stderr": exc.stderr}
            }
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()


def head_commit(directory: Path) -> str | None:
    out = git_text(directory, "rev-parse", "--verify", "--quiet", "HEAD^{commit}", check=False)
    return out or None


def _commit_on_head(directory: Path, tree: str, message: str) -> str:
    parent = head_commit(directory)
    args = ["commit-tree", tree] + (["-p", parent] if parent else [])
    commit = _text(git(directory, *args, input=message.encode("utf-8")).stdout).strip()
    git(directory, "update-ref", "-m", "audit", "HEAD", commit, parent or "0" * len(commit))
    return commit


def commit_tree(directory: Path, tree: str, message: str) -> str | None:
    """Commit *tree* on ``HEAD``; ``None`` (no commit) when it equals ``HEAD``'s tree."""
    parent = head_commit(directory)
    if parent is not None and git_text(directory, "rev-parse", f"{parent}^{{tree}}") == tree:
        return None
    return _commit_on_head(directory, tree, message)


def commit_message(subject: str, epoch: str | None, seq: int) -> str:
    message = subject + "\n"
    if epoch:
        message += f"\n{EPOCH_TRAILER}: {epoch}\n{SEQ_TRAILER}: {seq}\n"
    return message


def last_audited(directory: Path) -> tuple[str | None, int | None]:
    """The ``(epoch, seq)`` trailers of the latest commit that has them."""
    out = git_text(
        directory, "log", "-1", f"--grep=^{EPOCH_TRAILER}: ", "--format=%B", check=False
    )
    epoch = seq = None
    for line in out.splitlines():
        key, _, value = line.partition(": ")
        if key == EPOCH_TRAILER:
            epoch = value.strip()
        elif key == SEQ_TRAILER and value.strip().isdigit():
            seq = int(value.strip())
    return epoch, seq


def init_repo(
    directory: Path,
    *,
    epoch: str | None = None,
    head_seq: int = 0,
    gc: bool = True,
    message: str = "audit: project created",
) -> bool:
    """Make *directory* (a ``projects/<slug>/``) an audit repository, commit its
    board as the first commit, then ``git gc --auto``. Returns ``False`` if it
    already was one (the ``.gitignore`` is rewritten either way).

    ``project create`` and ``project import`` call this before the project is
    renamed into place; a load of a project without one (created while audit was
    off) calls it with ``gc=False`` and leaves the gc to the maintenance thread.
    """
    directory = Path(directory)
    created = not is_repo(directory)
    if created:
        git(directory, "init", "--quiet", f"--initial-branch={BRANCH}")
    _write_gitignore(directory)
    if created:
        tree = Stager(directory).stage_here()  # once, before the project serves
        _commit_on_head(directory, tree, commit_message(message, epoch, head_seq))
        if gc:
            git(directory, "gc", "--auto", "--quiet")
    return created


def _write_gitignore(directory: Path) -> None:
    """Plain write: the project directory's ``.gitignore`` is outside the board."""
    path = directory / ".gitignore"
    text = gitignore_text()
    try:
        if path.read_text(encoding="utf-8") == text:
            return
    except OSError:
        pass
    tmp = directory / f".gitignore.tmp.{os.getpid()}"
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Push target
# ---------------------------------------------------------------------------


class PushConfigError(Exception):
    pass


def push_target(board: Path, config: AuditConfig) -> dict | None:
    """The push target: the project's ``hosted/audit.json`` override, else
    ``audit.push``. Raises :class:`PushConfigError` for an invalid override."""
    path = Path(board) / "hosted" / AUDIT_JSON
    try:
        override = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        override = None
    except (OSError, ValueError) as exc:
        raise PushConfigError(f"{AUDIT_JSON} is unreadable: {type(exc).__name__}") from exc
    if override is not None:
        if not isinstance(override, dict) or "push" not in override:
            raise PushConfigError(f'{AUDIT_JSON} must hold {{"push": ...}}')
        if override["push"] is None:
            return None
        try:
            return check_push(override["push"])
        except ValueError as exc:
            raise PushConfigError(f"{AUDIT_JSON}: {exc}") from exc
    return check_push(config.push) if config.push is not None else None


# ---------------------------------------------------------------------------
# The committer
# ---------------------------------------------------------------------------


@dataclass
class _Pending:
    first_seq: int
    last_seq: int
    ops: int
    first_at: float
    last_at: float
    epoch: str | None = None

    def merge(self, later: _Pending) -> None:
        """Fold in *later* (newer lines). A new epoch restarts seq numbering, so the
        range becomes the newer epoch's; a zero-op entry never widens a range."""
        if self.ops == 0 or (later.epoch is not None and later.epoch != self.epoch):
            # A zero-op entry (the load-time check) carries no range of its own.
            self.first_seq, self.last_seq, self.epoch = (
                later.first_seq,
                later.last_seq,
                later.epoch,
            )
        elif later.ops == 0:
            pass
        else:
            self.first_seq = min(self.first_seq, later.first_seq)
            self.last_seq = max(self.last_seq, later.last_seq)
        self.ops += later.ops
        self.first_at = min(self.first_at, later.first_at)
        self.last_at = max(self.last_at, later.last_at)

    def message(self) -> str:
        subject = f"audit: seq {self.first_seq}-{self.last_seq} ({self.ops} ops)"
        return commit_message(subject, self.epoch, self.last_seq)


@dataclass
class Staged:
    """What :meth:`AuditCommitter.stage` captured under the work lock."""

    tree: str
    pending: _Pending | None


class AuditCommitter:
    """The committer of one project (see the module docstring).

    *lock* is the project's work lock. :meth:`notify`, :meth:`reconcile`, and
    :meth:`stage` are called while holding it; :meth:`drain` and :meth:`abandon`
    with or without it; :meth:`commit_and_stop` without it (or with
    ``join=False``).
    """

    def __init__(
        self,
        slug: str,
        directory: Path,
        lock: threading.Lock,
        config: AuditConfig,
        log: ServerLog,
    ) -> None:
        self.slug = slug
        self.directory = Path(directory)
        self.board = self.directory / ".lattice"
        self.lock = lock
        self.config = config
        self.log = log
        self.stager = Stager(self.directory)
        self._cond = threading.Condition()
        self._pending: _Pending | None = None
        self._stopping = False
        #: True while the thread stages or commits; :meth:`drain` waits for it.
        self._active = False
        self._retry_at = 0.0
        self._backoff = 0.0
        self._thread: threading.Thread | None = None
        self.maintenance = _Maintenance(self)
        #: Commits made (tests and diagnostics).
        self.commits = 0
        #: True while the thread waits for the work lock to stage.
        self.waiting_for_lock = False

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        self.maintenance.start()
        self._thread = threading.Thread(
            target=self._run, name=f"lattice-audit-{self.slug}", daemon=True
        )
        self._thread.start()

    def notify(self, seq: int, epoch: str | None = None) -> None:
        """A line was journaled (the caller holds the work lock)."""
        now = time.monotonic()
        self._add(_Pending(seq, seq, 1, now, now, epoch))

    def reconcile(self, epoch: str | None, head_seq: int) -> None:
        """At load (under the work lock): schedule a commit covering everything
        journaled after the last audited line, and a staging that records any
        change made to the board while no committer ran."""
        try:
            last_epoch, last_seq = last_audited(self.directory)
        except GitError as exc:
            self.log.warning("audit_reconcile_failed", project=self.slug, error=str(exc))
            last_epoch, last_seq = None, None
        first = last_seq + 1 if last_epoch == epoch and last_seq is not None else 1
        ops = max(0, head_seq - first + 1)
        if ops == 0:
            first = head_seq
        now = time.monotonic()
        self._add(_Pending(first, head_seq, ops, now, now, epoch))
        if ops:
            self.log.info(
                "audit_reconcile", project=self.slug, first_seq=first, last_seq=head_seq, ops=ops
            )

    def _add(self, pending: _Pending) -> None:
        with self._cond:
            if self._pending is None:
                self._pending = pending
            else:
                self._pending.merge(pending)
            self._cond.notify_all()

    def drain(self, timeout: float = 60.0) -> None:
        """Stop scheduling and wait for a stage or commit in progress to finish.
        Never takes the work lock, so it is safe with or without it held: a thread
        waiting for the lock is not active, and stages nothing once it gets it."""
        deadline = time.monotonic() + timeout
        with self._cond:
            self._stopping = True
            self._cond.notify_all()
            while self._active:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.log.warning("audit_drain_timeout", project=self.slug)
                    return
                self._cond.wait(remaining)

    def stage(self) -> Staged | None:
        """Shutdown and unload, step 1 (under the work lock, after operations are
        drained): drain the committer, then stage the board. ``None`` when staging
        failed (logged; the next load's reconcile records the board)."""
        self.drain()
        with self._cond:
            pending, self._pending = self._pending, None
        try:
            return Staged(self.stager.stage(), pending)
        except GitError as exc:
            self.log.error("audit_failed", project=self.slug, step="stage", error=str(exc))
            return None

    def commit_and_stop(self, staged: Staged | None, *, join: bool = True) -> None:
        """Shutdown and unload, step 2 (outside the work lock): commit what
        :meth:`stage` captured, stop the thread, and give the last gc and push up
        to ``FINAL_MAINTENANCE_SECONDS``. *join* ``False`` is for a caller that
        still holds the work lock (the thread exits once it gets the lock)."""
        self.drain()
        if staged is not None:
            try:
                self._commit(staged.tree, staged.pending, final=True)
            except GitError as exc:
                self.log.error("audit_failed", project=self.slug, step="commit", error=str(exc))
        thread = self._thread
        if join and thread is not None and thread is not threading.current_thread():
            thread.join(10)
        self.stager.close()
        self.maintenance.stop(flush=True, timeout=FINAL_MAINTENANCE_SECONDS)

    def abandon(self) -> None:
        """Stop without committing (a quarantined board is not trusted). Never waits."""
        with self._cond:
            self._stopping = True
            self._pending = None
            self._cond.notify_all()
        self.stager.close()
        self.maintenance.stop(flush=False, timeout=0)

    # -- the thread ------------------------------------------------------------

    def _run(self) -> None:
        while True:
            with self._cond:
                if not self._wait_until_due():
                    return
            self._cycle()

    def _wait_until_due(self) -> bool:
        """Wait (holding ``_cond``) until a commit is due; ``False`` means exit."""
        while True:
            if self._stopping:
                return False
            pending = self._pending
            if pending is None:
                self._cond.wait()
                continue
            due = min(
                pending.last_at + self.config.debounce_seconds,
                pending.first_at + self.config.max_interval_seconds,
            )
            remaining = max(due, self._retry_at) - time.monotonic()
            if remaining <= 0:
                return True
            self._cond.wait(remaining)

    def _cycle(self) -> None:
        """Prehash, stage under the work lock, then commit outside it; requeue on
        failure."""
        prehash_started = time.monotonic()
        try:
            self.stager.prehash()
        except Exception as exc:  # noqa: BLE001 - the stage hashes whatever this missed
            self.log.debug("audit_prehash_failed", project=self.slug, error=redact(str(exc)))
        self.waiting_for_lock = True
        started = time.monotonic()
        try:
            self.lock.acquire()
        finally:
            self.waiting_for_lock = False
        locked_at = time.monotonic()
        pending = None
        try:
            with self._cond:
                if self._stopping:
                    return  # shutdown's own stage() records the board
                pending, self._pending = self._pending, None
                if pending is None:
                    return
                self._active = True
            tree = self.stager.stage()
        except Exception as exc:  # noqa: BLE001 - audit never takes the server down
            self._failed(pending, "stage", exc)
            return
        finally:
            self.lock.release()
        timings = {
            "prehash_ms": round((started - prehash_started) * 1000, 1),
            "lock_wait_ms": round((locked_at - started) * 1000, 1),
            "stage_ms": round((time.monotonic() - locked_at) * 1000, 1),
        }
        try:
            self._commit(tree, pending, timings=timings)
        except Exception as exc:  # noqa: BLE001
            self._failed(pending, "commit", exc)
            return
        with self._cond:
            self._active = False
            self._backoff = 0.0
            self._retry_at = 0.0
            self._cond.notify_all()

    def _failed(self, pending: _Pending | None, step: str, exc: Exception) -> None:
        """Requeue *pending* ahead of anything journaled since, and back off."""
        self.log.error(
            "audit_failed",
            project=self.slug,
            step=step,
            error=redact(f"{type(exc).__name__}: {exc}"),
            retry="requeued",
        )
        with self._cond:
            if pending is not None:
                if self._pending is not None:
                    pending.merge(self._pending)
                self._pending = pending
            first = min(1.0, max(0.05, float(self.config.debounce_seconds)))
            self._backoff = min(MAX_BACKOFF_SECONDS, self._backoff * 2 if self._backoff else first)
            self._retry_at = time.monotonic() + self._backoff
            self._active = False
            self._cond.notify_all()

    def _commit(
        self,
        tree: str,
        pending: _Pending | None,
        *,
        final: bool = False,
        timings: dict[str, float] | None = None,
    ) -> bool:
        """Commit *tree*; ``False`` when it matches ``HEAD`` (nothing changed).
        *timings* (the cycle's ``prehash_ms``, ``lock_wait_ms``, and
        ``stage_ms``, the time it held the work lock) join ``commit_ms`` on the
        ``audit_commit`` line."""
        if pending is None:
            pending = _Pending(0, 0, 0, 0.0, 0.0, None)
        started = time.monotonic()
        commit = commit_tree(self.directory, tree, pending.message())
        if commit is None:
            self.log.debug(
                "audit_nothing_to_commit",
                project=self.slug,
                first_seq=pending.first_seq,
                last_seq=pending.last_seq,
            )
            return False
        self.commits += 1
        self.log.info(
            "audit_commit",
            project=self.slug,
            first_seq=pending.first_seq,
            last_seq=pending.last_seq,
            ops=pending.ops,
            final=final,
            **(timings or {}),
            commit_ms=round((time.monotonic() - started) * 1000, 1),
        )
        self.maintenance.request()
        return True


class _Maintenance:
    """``git gc --auto`` and the push, on their own thread.

    Each commit queues one gc: however long a push stalls, gc runs once for every
    commit (late, never fewer times). Pushes coalesce: after each batch of gc
    runs it pushes ``HEAD``, which carries every earlier commit (so the push after
    the next commit retries a failed one)."""

    def __init__(self, committer: AuditCommitter) -> None:
        self.committer = committer
        self._cond = threading.Condition()
        #: Commits not yet followed by their gc run.
        self._gc_owed = 0
        self._stopping = False
        self._thread: threading.Thread | None = None
        self._push_failing = False
        self._bad_target: str | None = None
        #: Completed gc runs and push attempts (tests and diagnostics).
        self.gc_runs = 0
        self.push_attempts = 0

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name=f"lattice-audit-maint-{self.committer.slug}", daemon=True
        )
        self._thread.start()

    def request(self) -> None:
        with self._cond:
            self._gc_owed += 1
            self._cond.notify_all()

    def stop(self, *, flush: bool, timeout: float) -> None:
        with self._cond:
            if not flush:
                self._gc_owed = 0
            self._stopping = True
            self._cond.notify_all()
        thread = self._thread
        if timeout > 0 and thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
            if thread.is_alive():
                self.committer.log.warning(
                    "audit_maintenance_abandoned",
                    project=self.committer.slug,
                    waited_seconds=timeout,
                )

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._gc_owed and not self._stopping:
                    self._cond.wait()
                if not self._gc_owed:
                    return
                owed, self._gc_owed = self._gc_owed, 0
            for _ in range(owed):
                self._guarded(self._gc)
            self._guarded(self._push)

    def _guarded(self, step: Any) -> None:
        try:
            step()
        except Exception as exc:  # noqa: BLE001 - never takes the server down
            self.committer.log.error(
                "audit_maintenance_failed",
                project=self.committer.slug,
                error=redact(f"{type(exc).__name__}: {exc}"),
            )

    def _gc(self) -> None:
        c = self.committer
        try:
            git(c.directory, "gc", "--auto", "--quiet")
        except GitError as exc:
            c.log.warning("audit_gc_failed", project=c.slug, error=str(exc))
        finally:
            self.gc_runs += 1

    def _push(self) -> None:
        c = self.committer
        try:
            target = push_target(c.board, c.config)
        except PushConfigError as exc:
            if self._bad_target != str(exc):
                self._bad_target = str(exc)
                c.log.warning("audit_push_config_invalid", project=c.slug, error=redact(str(exc)))
            return
        self._bad_target = None
        if target is None:
            return
        remote, branch = target["remote"], target["branch"]
        self.push_attempts += 1
        try:
            git(
                c.directory,
                "push",
                "--quiet",
                "--no-verify",
                "--",
                remote,
                f"HEAD:refs/heads/{branch}",
                timeout=PUSH_TIMEOUT_SECONDS,
            )
        except GitError as exc:
            self._push_failing = True
            c.log.warning(
                "audit_push_failed",
                project=c.slug,
                remote=remote,
                branch=branch,
                returncode=exc.returncode,
                error=exc.stderr,
                retry="after the next commit",
            )
            return
        if self._push_failing:
            self._push_failing = False
            c.log.info("audit_push_recovered", project=c.slug, remote=remote, branch=branch)


# ---------------------------------------------------------------------------
# ``lattice server project audit`` (SPEC §8.2)
# ---------------------------------------------------------------------------


def validate_push_settings(request: Any) -> dict:
    """The ``hosted/audit.json`` object for a request ``{"push": null | {remote, branch}}``."""
    if not isinstance(request, dict) or "push" not in request:
        raise OpError("VALIDATION_ERROR", "Give --push-remote NAME --branch B, or --no-push.")
    push = request["push"]
    if push is None:
        return {"push": None}
    try:
        return {"push": check_push(push)}
    except ValueError as exc:
        raise OpError("VALIDATION_ERROR", f"Invalid push target: {exc}.") from exc


def write_settings(board: Path, settings: dict) -> None:
    """Write ``hosted/audit.json`` (the caller is the board's owner)."""
    from lattice.storage.fs import atomic_write

    atomic_write(
        Path(board) / "hosted" / AUDIT_JSON, json.dumps(settings, sort_keys=True, indent=2) + "\n"
    )


def check_remote(directory: Path, settings: dict) -> None:
    """Refuse a push remote the project's audit repository does not have."""
    push = settings.get("push")
    if push is None:
        return
    if git_executable() is None:
        raise OpError("VALIDATION_ERROR", "git is not on PATH; audit history is disabled.")
    if not is_repo(directory):
        raise OpError(
            "VALIDATION_ERROR",
            f"{directory} is not an audit repository yet (audit was off when it was "
            "created); the server makes it when it next loads the project.",
        )
    try:
        remotes = git_text(directory, "remote").split()
    except GitError as exc:
        raise OpError("VALIDATION_ERROR", f"cannot list git remotes: {exc}") from exc
    if push["remote"] not in remotes:
        raise OpError(
            "VALIDATION_ERROR",
            f"No git remote '{push['remote']}' in {directory}; add it first with "
            f"'git -C {directory} remote add {push['remote']} <url>'.",
            {"remotes": remotes},
        )


@control.action("set-audit")
def _set_audit_action(project: Any, request: dict) -> dict:
    settings = validate_push_settings(request)
    check_remote(project.directory, settings)
    write_settings(project.board, settings)
    return {"project": project.slug, **settings}
