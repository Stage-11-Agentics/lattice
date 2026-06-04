"""Default config generation and validation."""

from __future__ import annotations

import json
import re
from typing import Literal, TypedDict


class WipLimits(TypedDict, total=False):
    in_progress: int
    review: int
    in_validation: int
    pr_open: int


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
            "in_validation": "seeing it work",
            "pr_open": "PR up",
            "done": "shipped",
            "blocked": "stuck",
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
    "todo": "Selected for work; not started.",
    "in_progress": "Implementation is actively underway. Code is being written, tested, or integrated.",
    "in_review": "A human or agent is reviewing the change.",
    "review": "Local review is underway. A review sub-agent is examining the diff before validation begins.",
    "in_validation": (
        "End-to-end validation is underway. The change is being exercised against a "
        "running system — browser automation, simulator flows, curl — before the PR "
        "opens. The bar: 'I saw it work,' not 'I think it should work.'"
    ),
    "pr_open": (
        "PR is open and awaiting human review, CI, or merge. "
        "Local review and validation artifacts are recorded."
    ),
    "done": "Work is reviewed, merged, and shipped. No further action needed.",
    "blocked": "Work cannot proceed due to an external dependency or unresolved issue.",
    "cancelled": "Work has been abandoned. No further action will be taken.",
}


class LatticeConfig(TypedDict, total=False):
    schema_version: int
    default_status: str
    default_priority: str
    default_complexity: str
    task_types: list[str]
    workflow: Workflow
    status_preset: str
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
    dashboard_port: int
    review_mode: Literal["inline", "single", "triple"]
    plan_review_mode: Literal["inline", "single", "triple"]
    plan_approval: Literal["auto", "human"]
    review_timeout_seconds: int
    auto_code_review_on_transition: bool
    auto_plan_review_on_transition: bool
    done_display: Literal["all", "recent", "grouped"]
    project_type: Literal["standard", "structure"]


# ---------------------------------------------------------------------------
# Status presets — which workflow statuses an instance uses
# ---------------------------------------------------------------------------
# Orthogonal to WORKFLOW_PRESETS (display-name personality): a status preset
# decides WHICH statuses exist; the personality decides what they're called.
# ``stage11`` is the full agentic flow (the default), ``linear`` the familiar
# minimal board, ``custom`` an interview-composed subset of the stage11 spine.

STATUS_PRESET_VALUES: tuple[str, ...] = ("stage11", "linear", "custom")

#: Role vocabulary shared by every status preset.  Roles without completion
#: policies are inert, so even minimal presets keep the full list — attach
#: ``--role`` works everywhere.
_FULL_ROLES: list[str] = ["review", "plan-review", "review-individual", "validation"]

#: Shipped WIP limit per in-flight status; presets include only the entries
#: whose status exists.
_WIP_DEFAULTS: dict[str, int] = {
    "in_progress": 10,
    "review": 5,
    "in_validation": 5,
    "pr_open": 10,
}


def stage11_workflow() -> dict:
    """The full Stage 11 agentic workflow (the shipped default).

    This is a hand-tuned literal — the transition graph carries deliberate
    skip-forward edges (``backlog → planned``, ``planned → review``,
    ``review → pr_open/done``, ``pr_open → review``) that no clean derivation
    rule reproduces.  Do not re-express it through :func:`compose_workflow`.
    """
    statuses = [
        "backlog",
        "in_planning",
        "planned",
        "in_progress",
        "review",
        "in_validation",
        "pr_open",
        "done",
        "blocked",
        "cancelled",
    ]
    return {
        "statuses": statuses,
        "transitions": {
            "backlog": ["in_planning", "planned", "cancelled"],
            "in_planning": ["planned", "cancelled"],
            "planned": ["in_progress", "review", "blocked", "cancelled"],
            "in_progress": ["review", "blocked", "cancelled"],
            "review": [
                "in_validation",
                "pr_open",
                "done",
                "in_progress",
                "in_planning",
                "cancelled",
            ],
            "in_validation": [
                "pr_open",
                "in_progress",
                "in_planning",
                "blocked",
                "cancelled",
            ],
            "pr_open": [
                "done",
                "in_progress",
                "review",
                "blocked",
                "cancelled",
            ],
            "done": [],
            "blocked": [
                "in_planning",
                "planned",
                "in_progress",
                "in_validation",
                "pr_open",
                "cancelled",
            ],
            "cancelled": [],
        },
        "universal_targets": ["cancelled"],
        "roles": list(_FULL_ROLES),
        "wip_limits": dict(_WIP_DEFAULTS),
        "completion_policies": {
            "done": {"require_roles": ["review"]},
            "pr_open": {"require_roles": ["validation"]},
        },
        "descriptions": {s: STATUS_DESCRIPTIONS[s] for s in statuses},
    }


def linear_workflow() -> dict:
    """The familiar minimal board: backlog / todo / in progress / in review / done.

    Machinery-light by design: the literal ``todo`` / ``in_review`` slugs mean
    the slug-keyed agentic machinery (auto-fired reviews on ``review`` /
    ``planned``, plan gating, rework-cycle counting) is naturally inert.  No
    WIP limits, no completion policies.
    """
    statuses = ["backlog", "todo", "in_progress", "in_review", "done", "cancelled"]
    return {
        "statuses": statuses,
        "transitions": {
            "backlog": ["todo", "in_progress", "cancelled"],
            "todo": ["in_progress", "cancelled"],
            "in_progress": ["in_review", "todo", "cancelled"],
            "in_review": ["done", "in_progress", "cancelled"],
            "done": [],
            "cancelled": [],
        },
        "universal_targets": ["cancelled"],
        "roles": list(_FULL_ROLES),
        "wip_limits": {},
        "completion_policies": {},
        "descriptions": {s: STATUS_DESCRIPTIONS[s] for s in statuses},
    }


def compose_workflow(
    *,
    include_review: bool,
    include_validation: bool,
    include_pr_open: bool,
) -> dict:
    """Compose a custom workflow from the Stage 11 spine with opt-in gates.

    Used by the ``lattice init`` custom interview path only — the named
    presets are literals (:func:`stage11_workflow`, :func:`linear_workflow`).
    Forward edges target the immediate next present stage; rework and blocked
    edges mirror the stage11 graph for the statuses that exist.

    Policy rules: ``done`` requires ``review``-role evidence iff the review
    stage exists; ``pr_open`` requires ``validation``-role evidence iff BOTH
    ``pr_open`` and ``in_validation`` exist (declining the validation stage at
    init time is human intent that this project has no validation ritual).
    """
    gates = []
    if include_review:
        gates.append("review")
    if include_validation:
        gates.append("in_validation")
    if include_pr_open:
        gates.append("pr_open")

    chain = ["backlog", "in_planning", "planned", "in_progress", *gates, "done"]
    statuses = [*chain, "blocked", "cancelled"]

    # Forward chain: each non-terminal status targets the immediate next stage.
    transitions: dict[str, list[str]] = {
        status: [chain[i + 1]] for i, status in enumerate(chain[:-1])
    }
    transitions["done"] = []

    # Rework edges mirroring stage11: gates route back to in_progress, and
    # review/in_validation additionally to in_planning.
    for gate in gates:
        transitions[gate].append("in_progress")
        if gate in ("review", "in_validation"):
            transitions[gate].append("in_planning")

    # Blocked edges mirroring stage11, restricted to present statuses.
    for status in ("planned", "in_progress", "in_validation", "pr_open"):
        if status in chain:
            transitions[status].append("blocked")
    transitions["blocked"] = [
        s
        for s in ("in_planning", "planned", "in_progress", "in_validation", "pr_open")
        if s in chain
    ]

    # Cancelled closes out every non-terminal status (it is also a universal
    # target; the explicit edges match the stage11 literal's style).
    for status in [*chain[:-1], "blocked"]:
        transitions[status].append("cancelled")
    transitions["cancelled"] = []

    completion_policies: dict[str, dict] = {}
    if include_review:
        completion_policies["done"] = {"require_roles": ["review"]}
    if include_pr_open and include_validation:
        completion_policies["pr_open"] = {"require_roles": ["validation"]}

    return {
        "statuses": statuses,
        "transitions": transitions,
        "universal_targets": ["cancelled"],
        "roles": list(_FULL_ROLES),
        "wip_limits": {s: v for s, v in _WIP_DEFAULTS.items() if s in statuses},
        "completion_policies": completion_policies,
        "descriptions": {s: STATUS_DESCRIPTIONS[s] for s in statuses},
    }


def default_config(preset: str = "classic", status_preset: str = "stage11") -> LatticeConfig:
    """Return the default Lattice configuration.

    The returned dict, when serialized with
    ``json.dumps(data, sort_keys=True, indent=2) + "\\n"``,
    produces the canonical default config.json.

    *preset* selects the workflow personality ("classic" or "opinionated").
    The opinionated preset adds human-friendly display names for statuses
    while keeping the same underlying slugs and transition graph.

    *status_preset* selects WHICH statuses the workflow uses ("stage11",
    "linear", or "custom").  For "custom" the caller composes the workflow
    itself via :func:`compose_workflow` and overrides ``config["workflow"]``
    — this function only stamps the ``status_preset`` key and installs the
    stage11 workflow as the base.
    """
    if preset not in WORKFLOW_PRESETS:
        preset = "classic"
    if status_preset not in STATUS_PRESET_VALUES:
        status_preset = "stage11"

    workflow = linear_workflow() if status_preset == "linear" else stage11_workflow()

    # Personality display names apply only to statuses that exist in the set.
    display_names = {
        slug: name
        for slug, name in WORKFLOW_PRESETS[preset]["display_names"].items()
        if slug in workflow["statuses"]
    }
    if display_names:
        # Keep key order consistent with prior serialization: display_names
        # sits between completion_policies and descriptions semantically, but
        # sort_keys=True serialization makes insertion order irrelevant.
        workflow["display_names"] = display_names

    config: LatticeConfig = {
        "schema_version": 1,
        "default_status": "backlog",
        "default_priority": "medium",
        "task_types": [
            "task",
            "bug",
            "spike",
            "chore",
        ],
        "workflow": workflow,
        "workflow_preset": preset,
        "status_preset": status_preset,
        "review_mode": "single",
        "plan_review_mode": "single",
        "plan_approval": "auto",
        "review_timeout_seconds": 600,
        "auto_code_review_on_transition": True,
        "auto_plan_review_on_transition": True,
        "done_display": "grouped",
    }

    return config


VALID_PROJECT_TYPES: tuple[str, ...] = ("standard", "structure")


def get_project_type(config: dict) -> str:
    """Return the project type, defaulting to ``"standard"`` when unset.

    Lazy migration: projects initialized before the ``project_type`` field
    existed are treated as ``standard`` without rewriting their config.
    """
    value = config.get("project_type")
    if value in VALID_PROJECT_TYPES:
        return value  # type: ignore[return-value]
    return "standard"


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

_PROJECT_CODE_RE = re.compile(r"^[A-Z][A-Z0-9]{0,4}$")


def validate_project_code(code: str) -> bool:
    """Return ``True`` if *code* is a valid project code.

    Rules: 1-5 chars, uppercase ASCII letters and digits, must start with a
    letter. The leading-letter requirement keeps the prefix unambiguous
    against the trailing numeric sequence in short IDs like ``C11-42``.
    """
    return bool(_PROJECT_CODE_RE.match(code))


def validate_subproject_code(code: str) -> bool:
    """Return ``True`` if *code* is a valid subproject code.

    Same rules as project code: 1-5 chars, uppercase ASCII letters and
    digits, must start with a letter.
    """
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
    from any other status (e.g. ``cancelled``).
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

    Universal targets (``cancelled``) bypass all policies —
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
