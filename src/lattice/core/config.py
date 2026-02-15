"""Default config generation and validation."""

from __future__ import annotations

import json
import re
from typing import TypedDict


class WipLimits(TypedDict, total=False):
    in_implementation: int
    in_review: int


class Workflow(TypedDict):
    statuses: list[str]
    transitions: dict[str, list[str]]
    wip_limits: WipLimits


class HooksOnConfig(TypedDict, total=False):
    status_changed: str
    task_created: str
    task_archived: str
    task_unarchived: str
    assignment_changed: str
    field_updated: str
    comment_added: str
    relationship_added: str
    relationship_removed: str
    artifact_attached: str


class HooksConfig(TypedDict, total=False):
    post_event: str
    on: HooksOnConfig


class LatticeConfig(TypedDict, total=False):
    schema_version: int
    default_status: str
    default_priority: str
    task_types: list[str]
    workflow: Workflow
    default_actor: str
    project_code: str
    hooks: HooksConfig


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
                "in_implementation",
                "implemented",
                "in_review",
                "done",
                "cancelled",
            ],
            "transitions": {
                "backlog": ["in_planning", "cancelled"],
                "in_planning": ["planned", "cancelled"],
                "planned": ["in_implementation", "cancelled"],
                "in_implementation": ["implemented", "cancelled"],
                "implemented": ["in_review", "cancelled"],
                "in_review": ["done", "in_implementation", "cancelled"],
                "done": [],
                "cancelled": [],
            },
            "wip_limits": {
                "in_implementation": 10,
                "in_review": 5,
            },
        },
    }


VALID_PRIORITIES: tuple[str, ...] = ("critical", "high", "medium", "low")
VALID_URGENCIES: tuple[str, ...] = ("immediate", "high", "normal", "low")

_PROJECT_CODE_RE = re.compile(r"^[A-Z]{1,5}$")


def validate_project_code(code: str) -> bool:
    """Return ``True`` if *code* is a valid project code (1-5 uppercase ASCII letters)."""
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
    """Return ``True`` if the transition from *from_status* to *to_status* is allowed."""
    transitions = config.get("workflow", {}).get("transitions", {})
    allowed = transitions.get(from_status, [])
    return to_status in allowed


def validate_task_type(config: dict, task_type: str) -> bool:
    """Return ``True`` if *task_type* is listed in the config's task_types."""
    return task_type in config.get("task_types", [])


def get_wip_limit(config: dict, status: str) -> int | None:
    """Return the WIP limit for *status*, or ``None`` if not set."""
    return config.get("workflow", {}).get("wip_limits", {}).get(status)
