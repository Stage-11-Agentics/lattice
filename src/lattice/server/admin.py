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

from lattice.core.config import serialize_config, validate_project_code, validate_subproject_code
from lattice.core.errors import OpError
from lattice.core.ids import generate_instance_id
from lattice.server import control
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

#: ``project config`` keys and their allowed values: ``init``'s choices (SPEC §8.2).
CONFIG_CHOICES: dict[str, tuple[str, ...]] = {
    "review_mode": ("inline", "single", "triple"),
    "plan_review_mode": ("inline", "single", "triple"),
    "plan_approval": ("auto", "human"),
    "auto_code_review_on_transition": ("true", "false"),
    "auto_plan_review_on_transition": ("true", "false"),
}
_BOOL_KEYS = ("auto_code_review_on_transition", "auto_plan_review_on_transition")

#: What ``server init`` writes to a new ``server.json``: SPEC §8.1's defaults.
DEFAULT_SERVER_JSON: dict[str, Any] = {
    "bind": "127.0.0.1",
    "port": 8740,
    "trusted_proxy": False,
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
    }


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
# Review workflow configuration (SPEC §8.2 "project config")
# ---------------------------------------------------------------------------


def parse_config_assignments(assignments: list[str] | tuple[str, ...]) -> dict[str, str]:
    """``KEY=VALUE`` strings to a dict (validation is :func:`validate_config_changes`)."""
    changes: dict[str, str] = {}
    for item in assignments:
        key, sep, value = item.partition("=")
        if not sep:
            raise OpError("VALIDATION_ERROR", f"Expected KEY=VALUE, got {item!r}.")
        changes[key.strip()] = value.strip()
    return changes


def validate_config_changes(raw: Any) -> dict[str, Any]:
    """Refuse any key or value outside the allowlist; return typed values."""
    if not isinstance(raw, dict) or not raw:
        raise OpError("VALIDATION_ERROR", "Give at least one --set KEY=VALUE.")
    typed: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in CONFIG_CHOICES:
            raise OpError(
                "VALIDATION_ERROR",
                f"'{key}' cannot be set with project config; allowed keys: "
                f"{', '.join(CONFIG_CHOICES)}.",
                {"key": key},
            )
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
    """Change a project's review workflow (SPEC §8.2).

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
        config.update(typed)
        atomic_write(board / "config.json", serialize_config(config))
    return {"via": "offline", "project": slug, "set": typed, "maintenance": True}
