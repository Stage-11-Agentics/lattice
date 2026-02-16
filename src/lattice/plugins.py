"""Plugin discovery and loading via importlib.metadata entry points.

Two entry point groups, zero new dependencies:
- ``lattice.cli_plugins`` — register additional Click commands
- ``lattice.template_blocks`` — provide additional CLAUDE.md template sections

Plugin load failures are logged to stderr and never crash the host CLI.
Set LATTICE_DEBUG=1 for full tracebacks on failures.
"""

from __future__ import annotations

import os
import sys
from importlib.metadata import entry_points


# ---------------------------------------------------------------------------
# CLI plugins
# ---------------------------------------------------------------------------

CLI_PLUGIN_GROUP = "lattice.cli_plugins"


def discover_cli_plugins():
    """Return entry points from the ``lattice.cli_plugins`` group."""
    return list(entry_points(group=CLI_PLUGIN_GROUP))


def load_cli_plugins(cli_group) -> None:
    """Load each CLI plugin entry point and call its ``register(cli_group)`` function.

    Failures are logged to stderr but never raise — a broken plugin must not
    prevent built-in commands from working.
    """
    debug = os.environ.get("LATTICE_DEBUG", "")
    for ep in discover_cli_plugins():
        try:
            register_fn = ep.load()
            register_fn(cli_group)
        except Exception as exc:
            print(f"lattice: failed to load CLI plugin '{ep.name}': {exc}", file=sys.stderr)
            if debug:
                import traceback

                traceback.print_exc(file=sys.stderr)


# ---------------------------------------------------------------------------
# Template block plugins
# ---------------------------------------------------------------------------

TEMPLATE_BLOCK_GROUP = "lattice.template_blocks"

_REQUIRED_KEYS = {"marker", "content"}


def discover_template_blocks() -> list[dict]:
    """Return validated template block dicts from all ``lattice.template_blocks`` plugins.

    Each entry point must be a callable returning ``list[dict]`` where each dict
    has at least ``marker`` (str) and ``content`` (str) keys, plus an optional
    ``position`` key (default ``"after_base"``).

    Blocks with ``position: "replace_base"`` are rejected with a warning (v0 constraint).
    Blocks missing required keys are skipped with a warning.
    """
    debug = os.environ.get("LATTICE_DEBUG", "")
    blocks: list[dict] = []

    for ep in entry_points(group=TEMPLATE_BLOCK_GROUP):
        try:
            get_blocks_fn = ep.load()
            raw_blocks = get_blocks_fn()
        except Exception as exc:
            print(
                f"lattice: failed to load template plugin '{ep.name}': {exc}",
                file=sys.stderr,
            )
            if debug:
                import traceback

                traceback.print_exc(file=sys.stderr)
            continue

        if not isinstance(raw_blocks, list):
            print(
                f"lattice: template plugin '{ep.name}' returned {type(raw_blocks).__name__}, "
                "expected list[dict]. Skipping.",
                file=sys.stderr,
            )
            continue

        for i, block in enumerate(raw_blocks):
            if not isinstance(block, dict):
                print(
                    f"lattice: template plugin '{ep.name}' block {i} is not a dict. Skipping.",
                    file=sys.stderr,
                )
                continue

            missing = _REQUIRED_KEYS - block.keys()
            if missing:
                print(
                    f"lattice: template plugin '{ep.name}' block {i} missing keys: "
                    f"{', '.join(sorted(missing))}. Skipping.",
                    file=sys.stderr,
                )
                continue

            position = block.get("position", "after_base")
            if position == "replace_base":
                print(
                    f"lattice: template plugin '{ep.name}' block {i} requested "
                    "'replace_base' — not supported in v0. Skipping.",
                    file=sys.stderr,
                )
                continue

            blocks.append(block)

    return blocks
