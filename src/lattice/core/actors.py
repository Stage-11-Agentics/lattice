"""Actor identity primitive — structured identity for agents and humans.

An actor is a collection of attributes representing any entity that can
take action in Lattice.  The ``model`` field determines the actor type:
``model="human"`` indicates a human; any other value indicates an agent.

Required attributes (all actors):
    name     – Self-chosen name + system-assigned serial (e.g., "Argus-3")
    session  – Auto-generated ULID, unique per runtime invocation
    model    – What intelligence is acting ("human", "claude-opus-4", etc.)

Required for agents only:
    framework – What tool drives the actor ("claude-code", "codex-cli", etc.)

Optional (open extension — unknown keys are preserved, never dropped):
    prompt     – Active skill/workflow
    parent     – Who launched this agent
    agent_type – Categorical type for grouping ("advance", "review", etc.)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_NAME_SERIAL_RE = re.compile(r"^(.+)-(\d+)$")

# Base name must be non-empty, no whitespace, no slashes (filesystem safety).
_BASE_NAME_RE = re.compile(r"^[^\s/\\]+$")


@dataclass(frozen=True)
class ActorIdentity:
    """Structured actor identity.

    This is the core primitive.  All coordination (ownership, assignment,
    locking) keys off the ``session`` ULID.  The ``name`` (with serial)
    is the human-readable display key.
    """

    name: str  # Disambiguated name with serial, e.g. "Argus-3"
    base_name: str  # Self-chosen part, e.g. "Argus"
    serial: int  # System-assigned serial number (≥ 1)
    session: str  # ULID, unique per runtime (e.g. "sess_01KH...")
    model: str  # "human" or an LLM identifier

    # Required for agents, optional for humans
    framework: str | None = None

    # Optional for all
    agent_type: str | None = None
    prompt: str | None = None
    parent: str | None = None

    # Open extension — preserved but not interpreted
    extra: dict = field(default_factory=dict)

    @property
    def is_human(self) -> bool:
        """True if this actor represents a human."""
        return self.model == "human"

    def to_dict(self) -> dict:
        """Serialize to a dict for event storage / JSON output.

        Only includes non-None fields.  Extension fields are merged
        at the top level.
        """
        d: dict = {
            "name": self.name,
            "base_name": self.base_name,
            "serial": self.serial,
            "session": self.session,
            "model": self.model,
        }
        if self.framework is not None:
            d["framework"] = self.framework
        if self.agent_type is not None:
            d["agent_type"] = self.agent_type
        if self.prompt is not None:
            d["prompt"] = self.prompt
        if self.parent is not None:
            d["parent"] = self.parent
        if self.extra:
            d.update(self.extra)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> ActorIdentity:
        """Deserialize from a dict (event or session file).

        Unknown keys are captured in ``extra``.
        """
        known = {
            "name", "base_name", "serial", "session", "model",
            "framework", "agent_type", "prompt", "parent",
        }
        extra = {k: v for k, v in d.items() if k not in known}
        return cls(
            name=d["name"],
            base_name=d["base_name"],
            serial=d["serial"],
            session=d["session"],
            model=d["model"],
            framework=d.get("framework"),
            agent_type=d.get("agent_type"),
            prompt=d.get("prompt"),
            parent=d.get("parent"),
            extra=extra,
        )

    def to_legacy_actor(self) -> str:
        """Return a legacy ``prefix:identifier`` string for backward compat.

        Human actors → ``human:<base_name>``
        Agent actors → ``agent:<base_name>``
        """
        prefix = "human" if self.is_human else "agent"
        return f"{prefix}:{self.base_name}"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_base_name(name: str) -> str | None:
    """Validate a base name.  Returns an error message or None if valid."""
    if not name:
        return "Name cannot be empty."
    if not _BASE_NAME_RE.match(name):
        return f"Invalid name '{name}': must not contain whitespace or slashes."
    if _NAME_SERIAL_RE.match(name):
        return (
            f"Name '{name}' looks like it already has a serial number. "
            "Provide just the base name (e.g., 'Argus' not 'Argus-3')."
        )
    return None


def parse_disambiguated_name(name: str) -> tuple[str, int] | None:
    """Parse a disambiguated name like 'Argus-3' into ('Argus', 3).

    Returns None if the name doesn't match the pattern.
    """
    m = _NAME_SERIAL_RE.match(name)
    if m is None:
        return None
    return m.group(1), int(m.group(2))


def validate_session_creation(
    *,
    model: str,
    framework: str | None,
) -> str | None:
    """Validate required fields for session creation.

    Returns an error message or None if valid.
    """
    if not model:
        return "Model is required."
    if model != "human" and not framework:
        return "Framework is required for agent actors (e.g., --framework claude-code)."
    return None


# ---------------------------------------------------------------------------
# Legacy actor parsing
# ---------------------------------------------------------------------------


def parse_legacy_actor(actor_str: str) -> dict:
    """Parse a legacy ``prefix:identifier`` actor string into a partial dict.

    Returns a dict with ``name`` (= identifier) and ``model`` (inferred from
    prefix).  Used for backward compatibility when ``--actor`` is provided
    instead of ``--name``.
    """
    parts = actor_str.split(":", maxsplit=1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(f"Invalid legacy actor format: '{actor_str}'")

    prefix, identifier = parts
    return {
        "name": identifier,
        "model": "human" if prefix == "human" else identifier,
    }
