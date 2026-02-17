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
    transitions: dict[str, str]


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


def default_config() -> LatticeConfig:
    """Return the default Lattice configuration.

    The returned dict, when serialized with
    ``json.dumps(data, sort_keys=True, indent=2) + "\\n"``,
    produces the canonical default config.json.
    """
    return {
        "schema_version": 1,
        "default_status": "backlog",
        "default_priority": "medium",
        "task_types": [
            "task",
            "ticket",
            "epic",
            "bug",
            "spike",
            "chore",
        ],
        "workflow": {
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
                "review": ["done", "in_progress", "needs_human", "cancelled"],
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
            "wip_limits": {
                "in_progress": 10,
                "review": 5,
            },
        },
    }


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


def validate_task_type(config: dict, task_type: str) -> bool:
    """Return ``True`` if *task_type* is listed in the config's task_types."""
    return task_type in config.get("task_types", [])


def get_wip_limit(config: dict, status: str) -> int | None:
    """Return the WIP limit for *status*, or ``None`` if not set."""
    return config.get("workflow", {}).get("wip_limits", {}).get(status)


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
    from lattice.core.tasks import get_artifact_roles

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

    # Check require_roles
    require_roles = policy.get("require_roles", [])
    if require_roles:
        roles = get_artifact_roles(snapshot)
        present_roles = {r for r in roles.values() if r is not None}
        for required in require_roles:
            if required not in present_roles:
                failures.append(f"Missing artifact with role: {required}")

    # Check require_assigned
    if policy.get("require_assigned") and not snapshot.get("assigned_to"):
        failures.append("Task must be assigned")

    return (len(failures) == 0, failures)
