"""Pure task-local acceptance-criterion validation and allocation."""

from __future__ import annotations

import re
from collections.abc import Iterable

CRITERION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
CANONICAL_CRITERION_ID_PATTERN = re.compile(r"^AC-([1-9][0-9]*)$")


def validate_criterion_id(criterion_id: str) -> str:
    """Return a valid opaque task-local criterion ID."""
    if not isinstance(criterion_id, str) or not CRITERION_ID_PATTERN.fullmatch(criterion_id):
        raise ValueError("Criterion ID must match ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$.")
    return criterion_id


def normalize_outcome(outcome: str) -> str:
    """Trim criterion prose and reject an empty result."""
    if not isinstance(outcome, str) or not outcome.strip():
        raise ValueError("Acceptance-criterion outcome must be non-empty.")
    return outcome.strip()


def canonical_criterion_number(criterion_id: str) -> int | None:
    """Return the arbitrary-precision suffix for a canonical ``AC-N`` ID."""
    match = CANONICAL_CRITERION_ID_PATTERN.fullmatch(criterion_id)
    return int(match.group(1)) if match else None


def allocate_criterion_id(criteria: Iterable[dict]) -> str:
    """Allocate ``max(canonical AC-N) + 1`` across active and retired criteria."""
    maximum = 0
    for criterion in criteria:
        number = canonical_criterion_number(str(criterion.get("id", "")))
        if number is not None:
            maximum = max(maximum, number)
    candidate = f"AC-{maximum + 1}"
    if len(candidate) > 64:
        raise ValueError("Acceptance-criterion ID allocation exhausted the 64-character limit.")
    return candidate


def find_criterion(snapshot: dict, criterion_id: str) -> dict | None:
    """Return a criterion record by exact task-local ID."""
    for criterion in snapshot.get("acceptance_criteria", []):
        if criterion.get("id") == criterion_id:
            return criterion
    return None


def normalize_criterion_ids(
    criterion_ids: Iterable[str] | None,
    *,
    snapshot: dict | None = None,
) -> list[str]:
    """Validate and order-deduplicate sparse evidence links."""
    result: list[str] = []
    seen: set[str] = set()
    for raw_id in criterion_ids or ():
        criterion_id = validate_criterion_id(raw_id)
        if criterion_id in seen:
            continue
        if snapshot is not None and find_criterion(snapshot, criterion_id) is None:
            raise ValueError(f"Acceptance criterion {criterion_id} not found on this task.")
        seen.add(criterion_id)
        result.append(criterion_id)
    return result


def criterion_without_history(criterion: dict) -> dict:
    """Return a display copy without its revision array."""
    result = dict(criterion)
    result.pop("revisions", None)
    return result
