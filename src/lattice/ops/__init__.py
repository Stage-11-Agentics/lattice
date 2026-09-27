"""Named operations: the one write seam every Lattice front end uses.

See :mod:`lattice.ops.base` for the contract and a complete example.
"""

from lattice.ops.base import (
    HTTP_STATUS,
    Authorizer,
    Caller,
    CommonParams,
    OpContext,
    OpError,
    OpResult,
    StateConflict,
    check_path_component,
    execute,
    get_operation,
    operation,
    parse_params,
    permission_identity,
    registered_operations,
    resolve_actor,
)
from lattice.ops.discovery import OPERATIONS_GROUP, discover

__all__ = [
    "HTTP_STATUS",
    "OPERATIONS_GROUP",
    "Authorizer",
    "Caller",
    "CommonParams",
    "OpContext",
    "OpError",
    "OpResult",
    "StateConflict",
    "check_path_component",
    "discover",
    "execute",
    "get_operation",
    "operation",
    "parse_params",
    "permission_identity",
    "registered_operations",
    "resolve_actor",
]
