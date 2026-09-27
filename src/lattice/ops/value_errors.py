"""Map the ``ValueError``s a command used to catch around its write to its code.

Several commands wrapped ``mutate_task`` in ``except ValueError`` and printed
the message under one code: the rule helpers they call inside the lock raise
``ValueError``, and so do the storage placement errors (an archived or missing
task). Their operations keep that mapping so the CLI's output is unchanged.
"""

from __future__ import annotations

from typing import Any

from lattice.core.errors import OpError
from lattice.ops.base import OpContext
from lattice.storage.operations import MutationCallback, TaskMutationResult


def mutate_mapping_value_errors(
    ctx: OpContext, task_id: str, decide: MutationCallback, code: str, **kwargs: Any
) -> TaskMutationResult:
    """``ctx.mutate``, re-raising any plain ``ValueError`` as ``OpError(code)``.

    An ``OpError`` that is also a ``ValueError`` (``StateConflict``) keeps its own code.
    """
    try:
        return ctx.mutate(task_id, decide, **kwargs)
    except OpError:
        raise
    except ValueError as exc:
        raise OpError(code, str(exc)) from exc
