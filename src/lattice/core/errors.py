"""Typed business-rule errors raised by operations and the write path.

``OpError`` lives in ``core`` (not ``lattice.ops``) because ``storage`` raises
it too, and ``storage`` must not import the ``lattice.ops`` package.
``lattice.ops`` re-exports both names; that is the public spelling.
"""

from __future__ import annotations

from typing import Any

# SPEC §3.1: the HTTP status each code maps to on a server. The CLI exits 1
# for every error, as it always has.
HTTP_STATUS: dict[str, int] = {
    "VALIDATION_ERROR": 400,
    "MISSING_ARGS": 400,
    "INVALID_ID": 400,
    "INVALID_ROLE": 400,
    "INVALID_ACTOR": 400,
    "MISSING_ACTOR": 400,
    "LOCAL_ONLY": 400,
    "PROTOCOL_MISMATCH": 400,
    "UNSUPPORTED_PARAM": 400,
    "CLIENT_TOO_OLD": 400,
    "UNAUTHENTICATED": 401,
    "FORBIDDEN": 403,
    "ACTOR_NOT_PERMITTED": 403,
    "NOT_FOUND": 404,
    "NOT_INITIALIZED": 404,
    "PLAN_NOT_FOUND": 404,
    "SESSION_NOT_FOUND": 404,
    "UNKNOWN_OP": 404,
    "CONFLICT": 409,
    "ALREADY_CLAIMED": 409,
    "RESOURCE_HELD": 409,
    "NOT_HELD": 409,
    "EXPIRED": 409,
    "FLAG_ALREADY_SET": 409,
    "FLAG_NOT_SET": 409,
    "ISSUES_DISABLED": 409,
    "STALE_VERSION": 412,
    "PAYLOAD_TOO_LARGE": 413,
    "INVALID_TRANSITION": 422,
    "PLAN_REQUIRED": 422,
    "COMPLETION_BLOCKED": 422,
    "REVIEW_CYCLE_LIMIT": 422,
    "TASK_ERASED": 422,
    "RATE_LIMITED": 429,
    "INTEGRITY_ERROR": 500,
    "BOARD_BUSY": 503,
    "BOARD_UNAVAILABLE": 503,
    "STORAGE_LOW": 507,
}


class OpError(Exception):
    """A business-rule rejection: a stable ``code``, the message users see, and details.

    ``details`` is structured context for machine callers (the server puts it
    in the error envelope); the CLI prints only ``code`` and ``message``.
    """

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    @classmethod
    def task_state(cls, code: str, message: str, snapshot: dict | None) -> OpError:
        """A rejection about a task's state, carrying the task's current compact
        snapshot in ``details.snapshot`` (SPEC §3.1) so a caller can re-decide."""
        return cls(code, message, {"snapshot": task_state_snapshot(snapshot)})

    @property
    def http_status(self) -> int:
        return HTTP_STATUS.get(self.code, 400)

    def to_dict(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            error["details"] = self.details
        return error

    def __repr__(self) -> str:
        return f"OpError({self.code!r}, {self.message!r})"


class HostedReadError(OpError):
    """A hosted checkout's read phase failed (the cache could not be caught up
    or is interrupted). Raised from ``HostedBoard.lattice_dir`` wherever a
    command reads; the CLI renders it as that command's error."""


def task_state_snapshot(snapshot: dict | None) -> dict | None:
    """The compact snapshot a task-state rejection carries, plus ``last_event_id``."""
    if snapshot is None:
        return None
    from lattice.core.tasks import compact_snapshot

    return {**compact_snapshot(snapshot), "last_event_id": snapshot.get("last_event_id")}


class StateConflict(OpError, ValueError):
    """``CONFLICT`` raised by the write path itself (a ``from`` mismatch or a
    failed ``expect_last_event_id``).

    Also a ``ValueError`` so callers not yet converted to operations, which
    catch ``ValueError`` and show ``str(exc)``, keep today's behavior.
    """

    def __init__(self, message: str, snapshot: dict | None):
        super().__init__("CONFLICT", message, {"snapshot": task_state_snapshot(snapshot)})


class TaskErased(OpError):
    """``TASK_ERASED``: a write to a tombstoned task (SPEC §7).

    Raised by the write path itself, so a command that is not yet an operation
    meets it too; the CLI's root group renders it like any command error.
    """

    def __init__(self, snapshot: dict):
        display = snapshot.get("short_id") or snapshot.get("id")
        reason = snapshot.get("tombstone_reason")
        message = f"Task {display} is erased"
        if reason:
            message += f" ({reason})"
        message += f". Restore it with 'lattice unerase {display} --reason TEXT'."
        super().__init__("TASK_ERASED", message, {"snapshot": task_state_snapshot(snapshot)})


class BoardWriteError(OpError):
    """A storage write primitive refused a path before touching disk (SPEC §6.2).

    Raised by ``lattice.storage.fs``; nothing was written when it propagates.
    """


class BoardIsCache(BoardWriteError):
    """``BOARD_IS_CACHE``: a durable write into a client cache from outside its
    syncer, or (``details.reason`` ``CACHE_ACCESS``) a path in the cache this
    process cannot use."""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__("BOARD_IS_CACHE", message, details)


class BoardIsHosted(BoardWriteError):
    """``BOARD_IS_HOSTED``: a durable write into a server-owned board from outside
    the owning server and outside offline maintenance."""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__("BOARD_IS_HOSTED", message, details)


class BoardPathError(BoardWriteError):
    """A write whose resolved path is not under the board's ``.lattice/``.

    ``VALIDATION_ERROR`` on every surface (SPEC §3.1): the backstop behind the
    path-bearing input checks.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__("VALIDATION_ERROR", message, details)
