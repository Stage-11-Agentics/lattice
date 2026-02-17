"""ULID generation and validation."""

from __future__ import annotations

import re

from ulid import ULID

# Crockford Base32 alphabet: 0-9 A-Z excluding I, L, O, U
_CROCKFORD_B32_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$", re.IGNORECASE)

_VALID_ACTOR_PREFIXES = frozenset({"agent", "human", "team", "dashboard"})

SHORT_ID_RE = re.compile(r"^[A-Z]{1,5}(?:-[A-Z]{1,5})?-\d+$")

# Pattern for extracting short IDs embedded in arbitrary strings (e.g., branch names).
# Uses word boundaries to avoid partial matches.  Case-insensitive.
_EMBEDDED_SHORT_ID_RE = re.compile(
    r"(?<![A-Za-z])([A-Z]{1,5}(?:-[A-Z]{1,5})?-\d+)(?![A-Za-z])",
    re.IGNORECASE,
)


def validate_short_id(s: str) -> bool:
    """Return ``True`` if *s* matches the short ID pattern (e.g., ``LAT-42``)."""
    return bool(SHORT_ID_RE.match(s))


def parse_short_id(s: str) -> tuple[str, int]:
    """Parse a short ID into (prefix, number). Raises ValueError if invalid."""
    s = s.upper()
    if not SHORT_ID_RE.match(s):
        raise ValueError(f"Invalid short ID format: '{s}'")
    prefix, num_str = s.rsplit("-", 1)
    return prefix, int(num_str)


def is_short_id(s: str) -> bool:
    """Quick check: could *s* be a short ID? Case-insensitive."""
    return bool(SHORT_ID_RE.match(s.upper()))


def extract_short_ids(text: str) -> list[str]:
    """Extract all short IDs embedded in *text* (e.g., a branch name).

    Returns uppercased short IDs in the order they appear, deduplicated.

    Examples::

        >>> extract_short_ids("feat/LAT-42-login-page")
        ['LAT-42']
        >>> extract_short_ids("fix/PROJ-7/hotfix")
        ['PROJ-7']
        >>> extract_short_ids("main")
        []
    """
    seen: set[str] = set()
    result: list[str] = []
    for match in _EMBEDDED_SHORT_ID_RE.finditer(text):
        sid = match.group(1).upper()
        if sid not in seen:
            seen.add(sid)
            result.append(sid)
    return result


def generate_instance_id() -> str:
    """Generate a new instance ID with the inst_ prefix."""
    return f"inst_{ULID()}"


def generate_task_id() -> str:
    """Generate a new task ID with the task_ prefix."""
    return f"task_{ULID()}"


def generate_event_id() -> str:
    """Generate a new event ID with the ev_ prefix."""
    return f"ev_{ULID()}"


def generate_artifact_id() -> str:
    """Generate a new artifact ID with the art_ prefix."""
    return f"art_{ULID()}"


def generate_resource_id() -> str:
    """Generate a new resource ID with the res_ prefix."""
    return f"res_{ULID()}"


def validate_id(id_str: str, expected_prefix: str) -> bool:
    """Validate a ``<prefix>_<ulid>`` identifier.

    The ULID portion must be exactly 26 characters of valid Crockford
    Base32 (0-9, A-Z excluding I, L, O, U -- case insensitive).
    """
    if not isinstance(id_str, str) or not isinstance(expected_prefix, str):
        return False

    parts = id_str.split("_", maxsplit=1)
    if len(parts) != 2:
        return False

    prefix, ulid_part = parts
    if prefix != expected_prefix:
        return False

    return bool(_CROCKFORD_B32_RE.match(ulid_part))


def validate_actor(actor_str: str) -> bool:
    """Validate a ``prefix:identifier`` actor string.

    Valid prefixes are ``agent``, ``human``, and ``team``.  Both the
    prefix and the identifier must be non-empty.
    """
    if not isinstance(actor_str, str):
        return False

    parts = actor_str.split(":", maxsplit=1)
    if len(parts) != 2:
        return False

    prefix, identifier = parts
    if not prefix or not identifier:
        return False

    return prefix in _VALID_ACTOR_PREFIXES
