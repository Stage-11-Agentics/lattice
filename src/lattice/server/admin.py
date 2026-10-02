"""Admin actions behind ``lattice server`` (SPEC §8.2), without Click.

Every action that edits a server file holds ``<server_root>/admin.lock`` and
writes with ``atomic_write``. Admin actions never write a board a server owns:
they go through a control request (:mod:`lattice.server.control`), or, with
the project's owner flock free, take the flock themselves.

Errors are ``OpError`` so the CLI renders them in the usual envelope.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from filelock import FileLock

from lattice.core.config import (
    merge_config_changes,
    serialize_config,
    valid_git_branch_name,
    validate_project_code,
    validate_subproject_code,
)
from lattice.core.errors import OpError
from lattice.core.ids import generate_instance_id
from lattice.server import audit, control, doctor_media
from lattice.server.config import (
    ADMIN_LOCK,
    PROJECTS_DIR,
    SERVER_JSON,
    STATUS_JSON,
    TOKENS_JSON,
    ServerConfigError,
    load_config,
)
from lattice.server.journal import (
    HOSTED_DIR,
    JOURNAL,
    JOURNAL_META,
    ROTATION,
    Journal,
    JournalError,
    finish_rotation,
    now_ms,
    rotate_epoch,
)
from lattice.storage.board_init import create_board
from lattice.storage.fs import atomic_write, ensure_dir, strict_durability, unlink_path
from lattice.storage.ownership import (
    offline_maintenance,
    owning_board,
    release_owner_flock,
    try_owner_flock,
)

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")

#: Enum-valued ``project config`` keys (SPEC §8.2); typed string/integer keys
#: are listed separately below.
CONFIG_CHOICES: dict[str, tuple[str, ...]] = {
    "review_mode": ("inline", "single", "triple"),
    "plan_review_mode": ("inline", "single", "triple"),
    "plan_approval": ("auto", "human"),
    "auto_code_review_on_transition": ("true", "false"),
    "auto_plan_review_on_transition": ("true", "false"),
    "issues.enabled": ("true", "false"),
}
_BOOL_KEYS = (
    "auto_code_review_on_transition",
    "auto_plan_review_on_transition",
    "issues.enabled",
)
_STRING_CONFIG_KEYS = {"review_base_branch"}
_LIST_CONFIG_KEYS = {"review_integration_branches"}
_INTEGER_CONFIG_MINIMUMS = {
    "review_timeout_seconds": 1,
    "review_max_diff_lines": 0,
    "review_max_diff_chars": 0,
}

#: What ``server init`` writes to a new ``server.json``: SPEC §8.1's defaults.
DEFAULT_SERVER_JSON: dict[str, Any] = {
    "bind": "127.0.0.1",
    "port": 8740,
    "trusted_proxies": [],
    "public_origins": [],
    "log_level": "info",
    "audit": {"enabled": True, "debounce_seconds": 5, "max_interval_seconds": 60, "push": None},
    "limits": {
        "max_body_bytes": 16777216,
        "inline_file_bytes": 1048576,
        "lock_timeout_seconds": 30,
        "max_inflight_per_token": 8,
        "token_ops_per_minute": 600,
        "token_body_bytes_per_minute": 268435456,
        "max_event_data_bytes": 65536,
        "max_stream_subscribers_per_project": 64,
        "stream_queue_entries": 1000,
        "replay_reset_entries": 1000,
        "min_free_disk_bytes": 1073741824,
        "max_issue_media_file_bytes": 104857600,
        "max_issue_media_issue_bytes": 262144000,
        "max_issue_media_project_bytes": 10737418240,
    },
    "stream": {"heartbeat_seconds": 2},
}


# ---------------------------------------------------------------------------
# Root
# ---------------------------------------------------------------------------


@contextmanager
def admin_lock(root: Path) -> Iterator[None]:
    """Hold ``<root>/admin.lock`` (serializes admin commands on one host)."""
    with FileLock(str(Path(root) / ADMIN_LOCK)):
        yield


def require_root(root: Path) -> None:
    if not (Path(root) / TOKENS_JSON).is_file() or not (Path(root) / PROJECTS_DIR).is_dir():
        raise OpError(
            "NOT_INITIALIZED",
            f"{root} is not a Lattice server root; run 'lattice server init --root {root}'.",
        )


def init_root(root: Path) -> dict:
    """Create the root, ``server.json``, empty ``tokens.json`` (0600). Idempotent."""
    root = Path(root)
    if not root.exists():
        root.mkdir(parents=True)
        os.chmod(root, 0o700)
    created: list[str] = []
    with admin_lock(root):
        ensure_dir(root / PROJECTS_DIR)
        if not (root / SERVER_JSON).exists():
            atomic_write(root / SERVER_JSON, json.dumps(DEFAULT_SERVER_JSON, indent=2) + "\n")
            created.append(SERVER_JSON)
        if not (root / TOKENS_JSON).exists():
            atomic_write(root / TOKENS_JSON, json.dumps({"tokens": []}, indent=2) + "\n")
            created.append(TOKENS_JSON)
        os.chmod(root / TOKENS_JSON, 0o600)
    try:
        load_config(root)
    except ServerConfigError as exc:
        raise OpError("VALIDATION_ERROR", f"{root / SERVER_JSON}: {exc}") from exc
    return {"root": str(root), "created": created}


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


def project_dir(root: Path, slug: str) -> Path:
    check_slug(slug)
    return Path(root) / PROJECTS_DIR / slug


def check_slug(slug: str) -> None:
    if not isinstance(slug, str) or not SLUG_RE.fullmatch(slug):
        raise OpError(
            "VALIDATION_ERROR",
            f"Invalid project slug {slug!r}: use 1-63 lowercase letters, digits, and "
            "hyphens, starting with a letter or digit.",
        )


def existing_project(root: Path, slug: str) -> Path:
    directory = project_dir(root, slug)
    if not (directory / ".lattice" / "config.json").is_file():
        raise OpError("NOT_FOUND", f"No project '{slug}' under {Path(root) / PROJECTS_DIR}.")
    return directory


def project_slugs(root: Path) -> list[str]:
    """Every project directory's slug, sorted (half-built ``.creating-*`` ones excluded)."""
    base = Path(root) / PROJECTS_DIR
    try:
        names = os.listdir(base)
    except OSError:
        return []
    return sorted(
        n
        for n in names
        if SLUG_RE.fullmatch(n) and (base / n / ".lattice" / "config.json").is_file()
    )


def _write_owner_marker(board: Path, server_id: str) -> None:
    atomic_write(
        board / HOSTED_DIR / "owner.json",
        json.dumps(
            {
                "server_id": server_id,
                "host": socket.gethostname(),
                "pid": os.getpid(),
                "started_at": now_ms(),
            },
            sort_keys=True,
            indent=2,
        )
        + "\n",
    )


def seal_new_board(board: Path) -> Journal:
    """Finish a board built for a new project (``create`` and ``import``).

    Starts the journal at a new epoch, head 0, with a baseline of the board's
    current log lengths; creates the control-request directory; writes the
    owner marker. The caller holds the board's owner flock under
    :func:`owning_board`. The audit repository (SPEC §8.10) belongs here too.
    """
    journal = Journal.create(board)
    ensure_dir(board / HOSTED_DIR / control.CONTROL_DIR)
    _write_owner_marker(board, "lattice-server-admin")
    return journal


def create_project(
    root: Path,
    slug: str,
    *,
    code: str | None = None,
    subproject_code: str | None = None,
    review_mode: str | None = None,
    plan_review_mode: str | None = None,
    plan_approval: str | None = None,
    auto_code_review: bool | None = None,
    auto_plan_review: bool | None = None,
) -> dict:
    """Create ``projects/<slug>/.lattice/`` as ``lattice init`` would, then its journal.

    Built under ``projects/.creating-<slug>-<id>/`` and renamed into place, so a
    running server never sees a half-built project. The board carries
    ``hosted/owner.json`` from birth, so no local command writes it by mistake;
    the server that first loads it takes the (free) lease over.
    """
    root = Path(root)
    require_root(root)
    check_slug(slug)
    if code is not None:
        code = code.upper()
        if not validate_project_code(code):
            raise OpError(
                "VALIDATION_ERROR",
                f"Invalid project code: '{code}'. Must be 1-5 uppercase letters/digits, "
                "starting with a letter.",
            )
    if subproject_code is not None:
        if code is None:
            raise OpError("VALIDATION_ERROR", "Cannot set --subproject-code without --code.")
        subproject_code = subproject_code.upper()
        if not validate_subproject_code(subproject_code):
            raise OpError("VALIDATION_ERROR", f"Invalid subproject code: '{subproject_code}'.")
    for key, value in (
        ("review_mode", review_mode),
        ("plan_review_mode", plan_review_mode),
        ("plan_approval", plan_approval),
    ):
        if value is not None and value not in CONFIG_CHOICES[key]:
            raise OpError("VALIDATION_ERROR", f"Invalid {key} {value!r}.")

    try:
        audit_config = load_config(root).audit
    except ServerConfigError as exc:
        raise OpError("VALIDATION_ERROR", f"{root / SERVER_JSON}: {exc}") from exc

    with admin_lock(root):
        final = project_dir(root, slug)
        if final.exists():
            raise OpError("CONFLICT", f"Project '{slug}' already exists at {final}.")
        staging = root / PROJECTS_DIR / f".creating-{slug}-{generate_instance_id()[5:]}"
        board = staging / ".lattice"
        try:
            with owning_board(board):
                ensure_dir(board / HOSTED_DIR)
                fd = try_owner_flock(board)
                if fd is None:  # a fresh directory nobody else knows about
                    raise OpError("BOARD_BUSY", f"could not lock {board}")
                try:
                    config = create_board(
                        staging,
                        project_code=code,
                        subproject_code=subproject_code,
                        review_mode=review_mode,
                        plan_review_mode=plan_review_mode,
                        plan_approval=plan_approval,
                    )
                    toggles = {
                        "auto_code_review_on_transition": auto_code_review,
                        "auto_plan_review_on_transition": auto_plan_review,
                    }
                    if any(v is not None for v in toggles.values()):
                        config.update({k: v for k, v in toggles.items() if v is not None})
                        atomic_write(board / "config.json", serialize_config(config))
                    journal = seal_new_board(board)
                finally:
                    release_owner_flock(fd)
            audit_state = _create_audit_repo(staging, audit_config, epoch=journal.epoch)
            os.rename(staging, final)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
    return {
        "slug": slug,
        "path": str(final),
        "project_code": config.get("project_code"),
        "epoch": journal.epoch,
        "head_seq": 0,
        "audit": audit_state,
    }


def _create_audit_repo(
    directory: Path, config: Any, *, epoch: str | None = None, head_seq: int = 0
) -> dict:
    """Make a new (or imported) project directory its audit repository (SPEC §8.10).

    Audit off or git missing: no repository, and the reason. A git failure is
    reported the same way rather than failing the create; the server makes the
    repository when it next loads the project. *epoch* and *head_seq* name the
    journal line the first commit covers (``project import`` passes its new epoch).
    """
    active, reason = audit.availability(config)
    if not active:
        return {"repo": False, "reason": reason}
    try:
        audit.init_repo(directory, epoch=epoch, head_seq=head_seq)
    except audit.GitError as exc:
        return {"repo": False, "reason": str(exc)}
    return {"repo": True, "reason": None}


def _read_owner(board: Path) -> dict:
    try:
        owner = json.loads((board / HOSTED_DIR / "owner.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return owner if isinstance(owner, dict) else {}


def _owner_state(board: Path) -> tuple[str, dict]:
    owner = _read_owner(board)
    fd = try_owner_flock(board)
    if fd is None:
        return "loaded", owner
    release_owner_flock(fd)
    return "unloaded", owner


def _head_seq(board: Path) -> int:
    try:
        with open(board / HOSTED_DIR / JOURNAL, "rb") as fh:
            return sum(1 for line in fh if line.endswith(b"\n"))
    except OSError:
        return 0


def _task_count(board: Path) -> int:
    total = 0
    for events in (board / "events", board / "archive" / "events"):
        try:
            total += sum(
                1 for n in os.listdir(events) if n.startswith("task_") and n.endswith(".jsonl")
            )
        except OSError:
            continue
    return total


def list_projects(root: Path) -> list[dict]:
    """Slug, project code, head seq, task count, state, owner for every project.

    With a server running, the state is the one it publishes in
    ``server_status.json``; without one, every project is ``unloaded`` unless
    another process (offline maintenance) holds its lease.
    """
    require_root(root)
    running = control.server_running(root)
    status: dict = {}
    if running:
        try:
            status = json.loads((Path(root) / STATUS_JSON).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            status = {}
    rows = []
    for slug in project_slugs(root):
        board = project_dir(root, slug) / ".lattice"
        try:
            config = json.loads((board / "config.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            config = {}
        if running:
            state = (status.get("projects") or {}).get(slug, {}).get("state", "unloaded")
            owner = _read_owner(board)
        else:
            state, owner = _owner_state(board)
        rows.append(
            {
                "slug": slug,
                "project_code": config.get("project_code"),
                "head_seq": _head_seq(board),
                "task_count": _task_count(board),
                "state": state,
                "owner": owner or None,
            }
        )
    return rows


def unlock_project(root: Path, slug: str) -> dict:
    """Remove a stale owner marker (SPEC §6.2); refused while any process holds the lease."""
    board = existing_project(root, slug) / ".lattice"
    with admin_lock(root):
        fd = try_owner_flock(board)
        if fd is None:
            raise OpError(
                "BOARD_BUSY",
                f"a running process holds project '{slug}'; stop the server first.",
            )
        try:
            with owning_board(board):
                marker = board / HOSTED_DIR / "owner.json"
                existed = marker.exists()
                unlink_path(marker, missing_ok=True)
        finally:
            release_owner_flock(fd)
    return {"slug": slug, "removed": existed}


# ---------------------------------------------------------------------------
# Epoch rotation (SPEC §8.2 "project rotate-epoch")
# ---------------------------------------------------------------------------


def rotate_project_epoch(root: Path, slug: str, *, wait_seconds: float = 30.0) -> dict:
    """Start a new journal epoch so every cache resyncs.

    With a server running: a control request; the server rotates under the
    project's locks and broadcasts ``reset`` to its streams. With no server:
    take the owner flock and rotate directly, refused while any undo log exists
    (only a load's recovery, or ``project recover``, may settle those).
    """
    root = Path(root)
    board = existing_project(root, slug) / ".lattice"
    if control.server_running(root):
        answer = control.send_request(board, "rotate-epoch", {}, wait_seconds=wait_seconds)
        if not answer.get("ok"):
            error = answer.get("error") or {}
            raise OpError(
                error.get("code", "INTERNAL_ERROR"),
                error.get("message", "rejected"),
                error.get("details"),
            )
        return {"via": "server", **(answer.get("result") or {})}
    with admin_lock(root):
        fd = try_owner_flock(board)
        if fd is None:
            raise OpError(
                "BOARD_BUSY",
                f"a running process holds project '{slug}'; stop it first.",
            )
        try:
            with owning_board(board), strict_durability():
                undo_logs = _undo_logs(board)
                if undo_logs:
                    raise OpError(
                        "CONFLICT",
                        f"project '{slug}' has {len(undo_logs)} undo log(s) from an unfinished "
                        "operation; load the project once so recovery settles them, or run "
                        f"'lattice server project recover {slug} --rollback | --keep', "
                        "then rotate.",
                        {"reason": "UNDO_LOGS_PRESENT", "undo_logs": len(undo_logs)},
                    )
                if (board / HOSTED_DIR / ROTATION).exists():
                    finish_rotation(board)
                old_epoch = _current_epoch(board)
                journal = rotate_epoch(board, old_epoch=old_epoch)
        finally:
            release_owner_flock(fd)
    return {"via": "offline", "project": slug, "old_epoch": old_epoch, "epoch": journal.epoch}


def _undo_logs(board: Path) -> list[str]:
    try:
        names = os.listdir(board / HOSTED_DIR / "undo")
    except OSError:
        return []
    return sorted(n for n in names if not n.startswith("."))


def _current_epoch(board: Path) -> str | None:
    try:
        meta = json.loads((board / HOSTED_DIR / JOURNAL_META).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    epoch = meta.get("epoch") if isinstance(meta, dict) else None
    return epoch if isinstance(epoch, str) and epoch.startswith("ep_") else None


# ---------------------------------------------------------------------------
# Project admin configuration (SPEC §8.2 "project config")
# ---------------------------------------------------------------------------


def parse_config_assignments(assignments: list[str] | tuple[str, ...]) -> dict[str, Any]:
    """Parse ``KEY=VALUE`` assignments, decoding ``task_types`` as JSON."""
    changes: dict[str, Any] = {}
    for item in assignments:
        key, sep, value = item.partition("=")
        if not sep:
            raise OpError("VALIDATION_ERROR", f"Expected KEY=VALUE, got {item!r}.")
        key = key.strip()
        value = value.strip()
        if key == "task_types":
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise OpError(
                    "VALIDATION_ERROR", "task_types must be a JSON array of strings."
                ) from exc
        changes[key] = value
    return changes


def _validate_task_types(value: Any) -> list[str]:
    """Validate a complete replacement list of task types."""
    if not isinstance(value, list) or not value:
        raise OpError("VALIDATION_ERROR", "task_types must be a non-empty JSON array of strings.")
    if any(not isinstance(item, str) or not item or item != item.strip() for item in value):
        raise OpError(
            "VALIDATION_ERROR", "task_types must contain only non-empty, stripped strings."
        )
    if len(set(value)) != len(value):
        raise OpError("VALIDATION_ERROR", "task_types must not contain duplicate values.")
    if "task" not in value:
        raise OpError("VALIDATION_ERROR", "task_types must include 'task'.")
    return list(value)


def validate_config_changes(raw: Any) -> dict[str, Any]:
    """Refuse any key or value outside the allowlist; return typed values."""
    if not isinstance(raw, dict) or not raw:
        raise OpError("VALIDATION_ERROR", "Give at least one --set KEY=VALUE.")
    typed: dict[str, Any] = {}
    allowed_keys = (
        set(CONFIG_CHOICES)
        | _STRING_CONFIG_KEYS
        | _LIST_CONFIG_KEYS
        | set(_INTEGER_CONFIG_MINIMUMS)
        | {"task_types"}
    )
    for key, value in raw.items():
        if key == "task_types":
            typed[key] = _validate_task_types(value)
            continue
        if key not in allowed_keys:
            raise OpError(
                "VALIDATION_ERROR",
                f"'{key}' cannot be set with project config; allowed keys: "
                f"{', '.join(sorted(allowed_keys))}.",
                {"key": key},
            )
        if key in _STRING_CONFIG_KEYS:
            if (
                not isinstance(value, str)
                or not value.strip()
                or any(char in value for char in "\0\r\n")
            ):
                raise OpError(
                    "VALIDATION_ERROR",
                    f"Invalid value {value!r} for {key}; provide a non-empty branch name.",
                    {"key": key},
                )
            typed[key] = value.strip()
            continue
        if key in _LIST_CONFIG_KEYS:
            if isinstance(value, str):
                if not value.strip() or any(char in value for char in "\0\r\n"):
                    branches = []
                else:
                    branches = [branch.strip() for branch in value.split(",")]
            elif isinstance(value, list) and all(isinstance(item, str) for item in value):
                branches = list(value)
            else:
                branches = []
            if not branches or any(
                "," in branch or not valid_git_branch_name(branch) for branch in branches
            ):
                raise OpError(
                    "VALIDATION_ERROR",
                    f"Invalid value {value!r} for {key}; provide comma-separated, valid Git branch names.",
                    {"key": key},
                )
            if any(not branch for branch in branches) or len(set(branches)) != len(branches):
                raise OpError(
                    "VALIDATION_ERROR",
                    f"Invalid value {value!r} for {key}; provide comma-separated non-empty, unique branch names in precedence order.",
                    {"key": key},
                )
            typed[key] = branches
            continue
        if key in _INTEGER_CONFIG_MINIMUMS:
            if isinstance(value, bool):
                integer = None
            elif isinstance(value, int):
                integer = value
            elif isinstance(value, str) and value.isdecimal():
                try:
                    integer = int(value)
                except ValueError:
                    integer = None
            else:
                integer = None
            minimum = _INTEGER_CONFIG_MINIMUMS[key]
            if integer is None or integer < minimum:
                qualifier = "positive" if minimum else "non-negative"
                raise OpError(
                    "VALIDATION_ERROR",
                    f"Invalid value {value!r} for {key}; provide a {qualifier} integer.",
                    {"key": key},
                )
            typed[key] = integer
            continue
        text = str(value).lower() if isinstance(value, bool) else value
        if text not in CONFIG_CHOICES[key]:
            raise OpError(
                "VALIDATION_ERROR",
                f"Invalid value {value!r} for {key}; choose one of "
                f"{', '.join(CONFIG_CHOICES[key])}.",
                {"key": key},
            )
        typed[key] = (text == "true") if key in _BOOL_KEYS else text
    return typed


def set_project_config(
    root: Path, slug: str, changes: dict[str, Any], *, wait_seconds: float = 30.0
) -> dict:
    """Change a project's admin configuration (SPEC §8.2).

    With a server running: a control request, applied by the server as a
    journaled change. With no server: take the owner flock, rewrite
    ``config.json``, and record ``hosted/maintenance.json`` so the next load
    rotates the epoch and every cache resyncs.
    """
    root = Path(root)
    board = existing_project(root, slug) / ".lattice"
    typed = validate_config_changes(changes)
    if control.server_running(root):
        answer = control.send_request(
            board, "set-config", {"set": typed}, wait_seconds=wait_seconds
        )
        if not answer.get("ok"):
            error = answer.get("error") or {}
            raise OpError(error.get("code", "VALIDATION_ERROR"), error.get("message", "rejected"))
        return {"via": "server", **(answer.get("result") or {})}
    with admin_lock(root), offline_maintenance(board, "project config"):
        config = json.loads((board / "config.json").read_text(encoding="utf-8"))
        config = merge_config_changes(config, typed)
        atomic_write(board / "config.json", serialize_config(config))
    return {"via": "offline", "project": slug, "set": typed, "maintenance": True}


# ---------------------------------------------------------------------------
# Project lifecycle, doctor, and recover (SPEC §8.2)
# ---------------------------------------------------------------------------

#: How long a direct ``project doctor`` subprocess may run.
DOCTOR_TIMEOUT_SECONDS = 300.0


def _control_answer(answer: dict) -> dict:
    if not answer.get("ok"):
        error = answer.get("error") or {}
        raise OpError(
            error.get("code", "INTERNAL_ERROR"),
            error.get("message", "rejected"),
            error.get("details"),
        )
    return answer.get("result") or {}


def project_lifecycle(root: Path, slug: str, action: str, *, wait_seconds: float = 30.0) -> dict:
    """``project unload | load | reload``: a control request to the running server.

    With no server holding ``server.lock`` they fail: there is no lease to
    release or take (SPEC §8.2).
    """
    root = Path(root)
    board = existing_project(root, slug) / ".lattice"
    if action not in ("unload", "load", "reload"):
        raise OpError("VALIDATION_ERROR", f"unknown lifecycle action {action!r}")
    if not control.server_running(root):
        raise OpError(
            "CONFLICT",
            f"no Lattice server is running on {root}; 'project {action}' acts on a "
            "running server only (start it with 'lattice server serve').",
            {"reason": "SERVER_NOT_RUNNING"},
        )
    answer = control.send_request(board, action, {}, wait_seconds=wait_seconds)
    return {"via": "server", **_control_answer(answer)}


def run_doctor(board: Path) -> dict:
    """``lattice doctor --json`` (read-only) on *board*, in a subprocess of this
    interpreter, so its output never mixes with a server's. The caller holds
    the project's work lock or owner flock. Returns doctor's ``data``."""
    import subprocess
    import sys

    project = Path(board).parent
    env = {k: v for k, v in os.environ.items() if not k.startswith("LATTICE_")}
    env["LATTICE_ROOT"] = str(project)
    try:
        done = subprocess.run(
            [sys.executable, "-c", "from lattice.cli.main import cli; cli()", "doctor", "--json"],
            cwd=project,
            env=env,
            capture_output=True,
            timeout=DOCTOR_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise OpError(
            "INTEGRITY_ERROR", f"doctor did not finish within {DOCTOR_TIMEOUT_SECONDS:g} s"
        ) from exc
    try:
        payload = json.loads(done.stdout)
    except ValueError as exc:
        stderr = done.stderr.decode("utf-8", "replace").strip()[-500:]
        raise OpError(
            "INTEGRITY_ERROR", f"doctor failed (exit {done.returncode}): {stderr}"
        ) from exc
    if not payload.get("ok"):
        error = payload.get("error") or {}
        raise OpError(error.get("code", "INTEGRITY_ERROR"), error.get("message", "doctor failed"))
    return payload.get("data") or {}


def project_doctor(
    root: Path, slug: str, *, wait_seconds: float = 120.0, verify_media: bool = False
) -> dict:
    """``project doctor``: doctor's read-only checks, never racing a transaction.

    Through the running server (under the project's work lock, or its owner
    flock when the server does not hold the project); with no server, directly,
    holding the owner flock so a starting server cannot race it. A media pass
    (``server/doctor_media.py``) checks every media file a snapshot lists, and
    with *verify_media* hashes each original; the server runs the hash pass
    after releasing the work lock.
    """
    root = Path(root)
    board = existing_project(root, slug) / ".lattice"
    if control.server_running(root):
        answer = control.send_request(
            board,
            "doctor",
            {"verify_media": True} if verify_media else {},
            wait_seconds=wait_seconds,
        )
        return {"via": "server", **_control_answer(answer)}
    fd = try_owner_flock(board)
    if fd is None:
        raise OpError(
            "BOARD_BUSY",
            f"another process holds project '{slug}' (offline maintenance?); retry when it ends.",
        )
    try:
        data = run_doctor(board)
        scan = doctor_media.scan_media(board, board.parent)
        data = doctor_media.merge_media(data, doctor_media.finish_media(scan, verify=verify_media))
    finally:
        release_owner_flock(fd)
    return {"via": "offline", "project": slug, **data}


def recover_project(root: Path, slug: str, mode: str | None) -> dict:
    """``project recover <slug> --rollback | --keep`` (SPEC §8.2, §8.7 step 3).

    Needs the owner flock free (a server holds it only for a loaded project; a
    quarantined one has released it). Every undo log the journal can classify
    is settled exactly as startup recovery would, whatever the flag: committed
    ones are deleted, uncommitted ones rolled back. *mode* decides only the
    logs it cannot classify (a missing journal, or a log without ``seq``):
    ``rollback`` restores their pre-images, ``keep`` leaves the files as they
    are and deletes the logs. Writes ``hosted/maintenance.json``, so the next
    load rotates the epoch and every cache resyncs.
    """
    from lattice.server import recovery

    if mode not in (None, "rollback", "keep"):
        raise OpError("VALIDATION_ERROR", f"unknown recover mode {mode!r}")
    root = Path(root)
    board = existing_project(root, slug) / ".lattice"
    with admin_lock(root):
        if try_owner_flock_free(board) is False:
            raise OpError(
                "BOARD_BUSY",
                f"a running process holds project '{slug}'; unload it "
                f"('lattice server project unload {slug}') or stop the server first.",
            )
        if not recovery.undo_log_paths(board):
            return {
                "project": slug,
                "committed": [],
                "rolled_back": [],
                "kept": [],
                "undo_logs": 0,
            }
        with (
            offline_maintenance(board, f"project recover --{mode or 'auto'}"),
            strict_durability(),
        ):
            if (board / HOSTED_DIR / ROTATION).exists():
                finish_rotation(board)
            recovery.drop_torn_tails(board)
            try:
                journal: Journal | None = Journal.load(board)
            except JournalError:
                journal = None
            try:
                settled = recovery.settle_undo_logs(board, journal, unclassified=mode)
            except recovery.NeedsRecover as exc:
                raise OpError(
                    "VALIDATION_ERROR",
                    f"{exc}; choose --rollback (restore what they changed) or --keep "
                    "(keep the files as they are).",
                    {"reason": "RECOVER_MODE_REQUIRED"},
                ) from exc
    return {
        "project": slug,
        "committed": settled.committed,
        "rolled_back": settled.rolled_back,
        "kept": settled.kept,
        "undo_logs": len(settled.committed) + len(settled.rolled_back) + len(settled.kept),
        "maintenance": True,
    }


def try_owner_flock_free(board: Path) -> bool:
    """Whether the project's owner flock is free right now (probe and release)."""
    fd = try_owner_flock(board)
    if fd is None:
        return False
    release_owner_flock(fd)
    return True


# ---------------------------------------------------------------------------
# Audit push target (SPEC §8.2 "project audit", §8.10)
# ---------------------------------------------------------------------------


def set_project_audit(
    root: Path, slug: str, push: dict | None, *, wait_seconds: float = 30.0
) -> dict:
    """Write ``.lattice/hosted/audit.json``: the project's push target, overriding
    ``audit.push`` (``None`` turns pushing off for this project).

    With a server running: a control request, which the server applies under
    the project's locks. With none: take the owner flock and write it. It is
    server control data, not board data, so no epoch rotation follows.
    """
    root = Path(root)
    directory = existing_project(root, slug)
    board = directory / ".lattice"
    settings = audit.validate_push_settings({"push": push})
    if control.server_running(root):
        answer = control.send_request(board, "set-audit", settings, wait_seconds=wait_seconds)
        if not answer.get("ok"):
            error = answer.get("error") or {}
            raise OpError(error.get("code", "VALIDATION_ERROR"), error.get("message", "rejected"))
        return {"via": "server", "slug": slug, "push": settings["push"]}
    audit.check_remote(directory, settings)
    with admin_lock(root):
        fd = try_owner_flock(board)
        if fd is None:
            raise OpError(
                "BOARD_BUSY",
                f"a running process holds project '{slug}'; retry when it is done.",
            )
        try:
            with owning_board(board):
                audit.write_settings(board, settings)
        finally:
            release_owner_flock(fd)
    return {"via": "offline", "slug": slug, "push": settings["push"]}
