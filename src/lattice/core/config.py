"""Default config generation and validation."""

from __future__ import annotations

import json
from typing import TypedDict


class WipLimits(TypedDict, total=False):
    in_progress: int
    review: int


class Workflow(TypedDict):
    statuses: list[str]
    transitions: dict[str, list[str]]
    wip_limits: WipLimits


class LatticeConfig(TypedDict, total=False):
    schema_version: int
    default_status: str
    default_priority: str
    task_types: list[str]
    workflow: Workflow
    default_actor: str


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
                "ready",
                "in_progress",
                "review",
                "done",
                "blocked",
                "cancelled",
            ],
            "transitions": {
                "backlog": ["ready", "cancelled"],
                "ready": ["in_progress", "blocked", "cancelled"],
                "in_progress": ["review", "blocked", "cancelled"],
                "review": ["done", "in_progress", "cancelled"],
                "done": [],
                "cancelled": [],
                "blocked": ["ready", "in_progress", "cancelled"],
            },
            "wip_limits": {
                "in_progress": 10,
                "review": 5,
            },
        },
    }


VALID_PRIORITIES: tuple[str, ...] = ("critical", "high", "medium", "low")
VALID_URGENCIES: tuple[str, ...] = ("immediate", "high", "normal", "low")


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
