"""Default config generation and validation."""

from __future__ import annotations

import json
import re
from typing import TypedDict


class WipLimits(TypedDict, total=False):
    in_progress: int
    review: int


class CompletionPolicy(TypedDict, total=False):
    require_roles: list[str]
    require_assigned: bool


class Workflow(TypedDict, total=False):
    statuses: list[str]
    transitions: dict[str, list[str]]
    universal_targets: list[str]
    wip_limits: WipLimits
    completion_policies: dict[str, CompletionPolicy]
    roles: list[str]
    descriptions: dict[str, str]
    review_cycle_limit: int


class HooksOnConfig(TypedDict, total=False):
    status_changed: str
    task_created: str
    task_archived: str
    task_unarchived: str
    assignment_changed: str
    field_updated: str
    comment_added: str
    comment_edited: str
    comment_deleted: str
    reaction_added: str
    reaction_removed: str
    relationship_added: str
    relationship_removed: str
    artifact_attached: str
    branch_linked: str
    branch_unlinked: str


class HooksConfig(TypedDict, total=False):
    post_event: str
    on: HooksOnConfig
    transitions: dict[str, str | list[str]]


class ResourceDef(TypedDict, total=False):
    description: str
    max_holders: int
    ttl_seconds: int


class ModelTier(TypedDict, total=False):
    primary: str | None
    variations: list[str]


class ModelTiers(TypedDict, total=False):
    high: ModelTier
    medium: ModelTier
    low: ModelTier


class HeartbeatConfig(TypedDict, total=False):
    enabled: bool
    max_advances: int


# ---------------------------------------------------------------------------
# Workflow personality presets
# ---------------------------------------------------------------------------

WORKFLOW_PRESETS: dict[str, dict[str, str]] = {
    "classic": {
        "description": "Standard project management terminology",
        "display_names": {},  # empty = use slug as-is (formatted with underscores → spaces)
    },
    "opinionated": {
        "description": "Human-first status names with personality",
        "display_names": {
            "backlog": "thinking about it",
            "in_planning": "figuring it out",
            "planned": "ready to go",
            "in_progress": "on it",
            "review": "check my work",
            "done": "shipped",
            "blocked": "stuck",
            "needs_human": "ask flesh",
            "cancelled": "never mind",
        },
    },
}


# ---------------------------------------------------------------------------
# Status descriptions — operational contract for each status
# ---------------------------------------------------------------------------
# Unlike display_names (which vary by personality preset), descriptions
# define what each status *means* operationally.  They are the same
# regardless of which preset is active.

STATUS_DESCRIPTIONS: dict[str, str] = {
    "backlog": "Task is captured but no work has started. No planning, no implementation.",
    "in_planning": "Design, dialogue, and scoping underway. No implementation code should be written yet.",
    "planned": "Plan is written and approved. Ready for implementation but work has not started.",
    "in_progress": "Implementation is actively underway. Code is being written, tested, or integrated.",
    "review": "Implementation is complete. Work is being reviewed before it can ship.",
    "done": "Work is reviewed and shipped. No further action needed.",
    "blocked": "Work cannot proceed due to an external dependency or unresolved issue.",
    "needs_human": "A human decision, approval, or input is required before work can continue.",
    "cancelled": "Work has been abandoned. No further action will be taken.",
}


class LatticeConfig(TypedDict, total=False):
    schema_version: int
    default_status: str
    default_priority: str
    default_complexity: str
    task_types: list[str]
    workflow: Workflow
    default_actor: str
    project_code: str
    subproject_code: str
    instance_id: str
    instance_name: str
    hooks: HooksConfig
    members: dict[str, list[str]]
    model_tiers: ModelTiers
    resources: dict[str, ResourceDef]
    heartbeat: HeartbeatConfig
    workflow_preset: str
    project_name: str
    model: str


def default_config(preset: str = "classic") -> LatticeConfig:
    """Return the default Lattice configuration.

    The returned dict, when serialized with
    ``json.dumps(data, sort_keys=True, indent=2) + "\\n"``,
    produces the canonical default config.json.

    *preset* selects the workflow personality ("classic" or "opinionated").
    The opinionated preset adds human-friendly display names for statuses
    while keeping the same underlying slugs and transition graph.
    """
    if preset not in WORKFLOW_PRESETS:
        preset = "classic"

    display_names = WORKFLOW_PRESETS[preset]["display_names"]

    workflow: dict = {
        "statuses": [
            "backlog",
            "in_planning",
            "planned",
            "in_progress",
            "review",
            "done",
            "blocked",
            "needs_human",
            "cancelled",
        ],
        "transitions": {
            "backlog": ["in_planning", "planned", "cancelled"],
            "in_planning": ["planned", "needs_human", "cancelled"],
            "planned": ["in_progress", "review", "blocked", "needs_human", "cancelled"],
            "in_progress": ["review", "blocked", "needs_human", "cancelled"],
            "review": ["done", "in_progress", "in_planning", "needs_human", "cancelled"],
            "done": [],
            "blocked": ["in_planning", "planned", "in_progress", "cancelled"],
            "needs_human": [
                "in_planning",
                "planned",
                "in_progress",
                "review",
                "cancelled",
            ],
            "cancelled": [],
        },
        "universal_targets": ["needs_human", "cancelled"],
        "roles": ["review"],
        "wip_limits": {
            "in_progress": 10,
            "review": 5,
        },
    }

    if display_names:
        workflow["display_names"] = display_names

    workflow["descriptions"] = dict(STATUS_DESCRIPTIONS)

    config: LatticeConfig = {
        "schema_version": 1,
        "default_status": "backlog",
        "default_priority": "medium",
        "task_types": [
            "task",
            "epic",
            "bug",
            "spike",
            "chore",
        ],
        "workflow": workflow,
        "workflow_preset": preset,
    }

    return config


def get_display_name(config: dict, status: str) -> str:
    """Return the display name for a status slug.

    If display_names is configured, returns the mapped name.
    Otherwise, returns the slug with underscores replaced by spaces.
    """
    display_names = config.get("workflow", {}).get("display_names", {})
    if display_names and status in display_names:
        return display_names[status]
    return status.replace("_", " ")


def get_status_description(config: dict, status: str) -> str | None:
    """Return the operational description for a status slug, or None if not defined."""
    return config.get("workflow", {}).get("descriptions", {}).get(status)


def resolve_status_input(config: dict, user_input: str) -> str:
    """Resolve a user-typed status to the canonical slug.

    Accepts either the slug directly (e.g. "in_progress") or a display name
    (e.g. "on it") and returns the canonical slug. Case-insensitive for
    display name matching.
    """
    workflow = config.get("workflow", {})
    statuses = workflow.get("statuses", [])

    # Direct slug match
    if user_input in statuses:
        return user_input

    # Try display name reverse lookup (case-insensitive)
    display_names = workflow.get("display_names", {})
    lower_input = user_input.lower()
    for slug, display in display_names.items():
        if display.lower() == lower_input:
            return slug

    # Fall back to original input (will fail validation downstream)
    return user_input


VALID_PRIORITIES: tuple[str, ...] = ("critical", "high", "medium", "low")
VALID_URGENCIES: tuple[str, ...] = ("immediate", "high", "normal", "low")
VALID_COMPLEXITIES: tuple[str, ...] = ("low", "medium", "high")

_PROJECT_CODE_RE = re.compile(r"^[A-Z]{1,5}$")


def validate_project_code(code: str) -> bool:
    """Return ``True`` if *code* is a valid project code (1-5 uppercase ASCII letters)."""
    return bool(_PROJECT_CODE_RE.match(code))


def validate_subproject_code(code: str) -> bool:
    """Return ``True`` if *code* is a valid subproject code (1-5 uppercase ASCII letters)."""
    return bool(_PROJECT_CODE_RE.match(code))


def serialize_config(config: LatticeConfig | dict[str, object]) -> str:
    """Serialize a config dict to the canonical JSON format."""
    return json.dumps(config, sort_keys=True, indent=2) + "\n"


def load_config(raw: str) -> dict:
    """Parse a JSON config string and return the config dict.

    This is a pure function (no I/O).  The CLI layer reads the file
    and passes the raw string here.
    """
    return json.loads(raw)


def validate_status(config: dict, status: str) -> bool:
    """Return ``True`` if *status* is a defined status in the workflow."""
    return status in config.get("workflow", {}).get("statuses", [])


def validate_transition(
    config: dict,
    from_status: str,
    to_status: str,
) -> bool:
    """Return ``True`` if the transition from *from_status* to *to_status* is allowed.

    A transition is allowed if *to_status* appears in the explicit transition
    list for *from_status*, **or** if *to_status* is listed in
    ``workflow.universal_targets``.  Universal targets are statuses reachable
    from any other status (e.g. ``needs_human``, ``cancelled``).
    """
    workflow = config.get("workflow", {})
    universal = workflow.get("universal_targets", [])
    if to_status in universal:
        return True
    transitions = workflow.get("transitions", {})
    allowed = transitions.get(from_status, [])
    return to_status in allowed


def get_valid_transitions(config: dict, from_status: str) -> list[str]:
    """Return the list of valid target statuses from *from_status*.

    Includes both explicit transitions and universal targets, deduplicated
    and in config order.
    """
    workflow = config.get("workflow", {})
    universal = workflow.get("universal_targets", [])
    transitions = workflow.get("transitions", {})
    explicit = transitions.get(from_status, [])
    # Merge explicit + universal, preserving order, deduplicating
    seen: set[str] = set()
    result: list[str] = []
    for s in explicit:
        if s not in seen:
            seen.add(s)
            result.append(s)
    for s in universal:
        if s not in seen:
            seen.add(s)
            result.append(s)
    return result


def validate_task_type(config: dict, task_type: str) -> bool:
    """Return ``True`` if *task_type* is listed in the config's task_types."""
    return task_type in config.get("task_types", [])


def get_wip_limit(config: dict, status: str) -> int | None:
    """Return the WIP limit for *status*, or ``None`` if not set."""
    return config.get("workflow", {}).get("wip_limits", {}).get(status)


def get_review_cycle_limit(config: dict) -> int:
    """Return the review cycle limit from workflow config, default 3."""
    return config.get("workflow", {}).get("review_cycle_limit", 3)


def validate_completion_policy(
    config: dict,
    snapshot: dict,
    to_status: str,
) -> tuple[bool, list[str]]:
    """Check whether a transition into *to_status* satisfies completion policies.

    Returns ``(True, [])`` if no policy exists or all requirements are met.
    Returns ``(False, [reason, ...])`` if one or more requirements are not met.

    Universal targets (``needs_human``, ``cancelled``) bypass all policies —
    they are escape hatches.
    """
    from lattice.core.tasks import get_evidence_roles

    workflow = config.get("workflow", {})

    # Universal targets bypass policies
    universal = workflow.get("universal_targets", [])
    if to_status in universal:
        return (True, [])

    policies = workflow.get("completion_policies", {})
    policy = policies.get(to_status)
    if not policy:
        return (True, [])

    failures: list[str] = []

    # Check require_roles — satisfied by any evidence ref with matching role
    require_roles = policy.get("require_roles", [])
    if require_roles:
        present_roles = get_evidence_roles(snapshot)
        for required in require_roles:
            if required not in present_roles:
                failures.append(
                    f"Missing role: {required}. "
                    f"Satisfy with: lattice attach --role {required} "
                    f"or lattice comment --role {required}"
                )

    # Check require_assigned
    if policy.get("require_assigned") and not snapshot.get("assigned_to"):
        failures.append("Task must be assigned")

    return (len(failures) == 0, failures)


def get_configured_roles(config: LatticeConfig) -> set[str]:
    """Collect all valid role strings from explicit ``workflow.roles`` and
    ``require_roles`` across completion policies.

    Returns the union of both sources.  An empty set means no roles are
    configured anywhere — callers should treat any ``--role`` value as valid
    for backward compatibility.
    """
    roles: set[str] = set()
    workflow = config.get("workflow", {})
    # Explicit roles list (the primary source)
    for role in workflow.get("roles", []):
        roles.add(role)
    # Also include roles referenced by completion policies
    policies = workflow.get("completion_policies", {})
    for policy in policies.values():
        for role in policy.get("require_roles", []):
            roles.add(role)
    return roles
