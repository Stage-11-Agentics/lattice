"""Map the rule ``ValueError``s a command used to catch around its write to its code.

Several commands wrapped ``mutate_task`` in ``except ValueError`` and printed
the message under one code, because the rule helpers they call inside the lock
raise ``ValueError``. Their operations keep that mapping for those rules.
Storage errors are not rule errors: they pass through to ``execute``, which
maps them as SPEC §3.1 says (a placement error to ``NOT_FOUND`` with its
message, any other ``AuthoritativeLogError`` to ``INTEGRITY_ERROR``; G-6).
"""

from __future__ import annotations

from typing import Any

from lattice.core.errors import OpError
from lattice.ops.base import OpContext
from lattice.storage.operations import (
    AuthoritativeLogError,
    MutationCallback,
    TaskMutationResult,
    read_task_authority,
)


def mutate_mapping_value_errors(
    ctx: OpContext, task_id: str, decide: MutationCallback, code: str, **kwargs: Any
) -> TaskMutationResult:
    """``ctx.mutate``, re-raising a rule ``ValueError`` as ``OpError(code)``.

    An ``OpError`` that is also a ``ValueError`` (``StateConflict``) keeps its
    own code, and a storage ``AuthoritativeLogError`` propagates to ``execute``.
    A task with no log at all is ``NOT_FOUND`` (``mutate_task`` reports that
    case as a bare ``AuthoritativeLogError``, which would read as corruption).
    """
    if read_task_authority(ctx.lattice_dir, task_id, allow_missing=True) is None:
        raise OpError("NOT_FOUND", f"Task {task_id} does not exist.")
    try:
        return ctx.mutate(task_id, decide, **kwargs)
    except (OpError, AuthoritativeLogError):
        raise
    except ValueError as exc:
        raise OpError(code, str(exc)) from exc
