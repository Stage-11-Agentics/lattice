#!/usr/bin/env python3
"""Status Haikus — one poem for every state a task can live in.

Reads the workflow statuses from .lattice/config.json so it adapts
to custom workflows. Each haiku is 5-7-5 and tries to capture what
it actually feels like to be a task in that state.
"""

import json
import sys
from pathlib import Path

# Keyed by status name. Each tuple is (line1, line2, line3) in 5-7-5.
HAIKUS = {
    "backlog": (
        "Waiting in the dark",
        "a name without a heartbeat",
        "someday never comes",
    ),
    "in_planning": (
        "The blank page breathes in",
        "tracing edges of the shape",
        "before the first cut",
    ),
    "planned": (
        "All the lines are drawn",
        "the architect steps aside",
        "now the hands must move",
    ),
    "in_progress": (
        "Sawdust fills the air",
        "each commit a driven nail",
        "the house is rising",
    ),
    "review": (
        "Stand back from the work",
        "another pair of eyes asks",
        "is this what we meant",
    ),
    "done": (
        "The door closes soft",
        "what was asked for now exists",
        "silence after rain",
    ),
    "blocked": (
        "Roots hit solid rock",
        "I cannot grow past this wall",
        "someone move the stone",
    ),
    "needs_human": (
        "I have reached the edge",
        "of what a machine can choose",
        "your turn to decide",
    ),
    "cancelled": (
        "You were possible",
        "the world just moved on without",
        "finishing your name",
    ),
}

FALLBACK_HAIKU = (
    "A status unknown",
    "no poem was written here",
    "you are on your own",
)


def find_lattice_root():
    """Walk up from cwd to find .lattice/, like git finds .git/."""
    current = Path.cwd()
    while current != current.parent:
        if (current / ".lattice").is_dir():
            return current / ".lattice"
        current = current.parent
    return None


def load_statuses(lattice_root):
    """Read workflow statuses from config.json."""
    config_path = lattice_root / "config.json"
    with open(config_path) as f:
        config = json.load(f)
    return config.get("workflow", {}).get("statuses", [])


def format_haiku(status, lines):
    """Format a single haiku with its status header."""
    out = []
    out.append(f"  {status}")
    out.append(f"    {lines[0]}")
    out.append(f"    {lines[1]}")
    out.append(f"    {lines[2]}")
    return "\n".join(out)


def main():
    lattice_root = find_lattice_root()
    if lattice_root is None:
        print("No .lattice/ directory found.", file=sys.stderr)
        print("Run this from within a Lattice-tracked project.", file=sys.stderr)
        sys.exit(1)

    statuses = load_statuses(lattice_root)
    if not statuses:
        print("No statuses found in config.json.", file=sys.stderr)
        sys.exit(1)

    print()
    print("  ── Status Haikus ──")
    print()

    for status in statuses:
        haiku = HAIKUS.get(status, FALLBACK_HAIKU)
        print(format_haiku(status, haiku))
        print()


if __name__ == "__main__":
    main()
