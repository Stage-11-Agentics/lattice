"""ULID generation and validation."""

from __future__ import annotations

import re

from ulid import ULID

# Crockford Base32 alphabet: 0-9 A-Z excluding I, L, O, U
_CROCKFORD_B32_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$", re.IGNORECASE)

_VALID_ACTOR_PREFIXES = frozenset({"agent", "human", "team", "dashboard"})


def generate_task_id() -> str:
    """Generate a new task ID with the task_ prefix."""
    return f"task_{ULID()}"


def generate_event_id() -> str:
    """Generate a new event ID with the ev_ prefix."""
    return f"ev_{ULID()}"


def generate_artifact_id() -> str:
    """Generate a new artifact ID with the art_ prefix."""
    return f"art_{ULID()}"


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
