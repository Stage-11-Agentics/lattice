"""Tiny stand-in for claude/codex/gemini used by agent_spawn integration tests.

Reads ``$LATTICE_AGENT_PROMPT``, writes a deterministic block to
``$LATTICE_AGENT_OUTPUT``, and exits 0. Honors the ``$LATTICE_FAKE_BEHAVIOR``
env var for failure-mode coverage:

- unset / "ok"      → write output, exit 0
- "stdout"          → write nothing to file, print to stdout, exit 0
- "fail"            → exit 1 with stderr message
- "sleep:<n>"       → sleep <n> seconds before writing (timeout coverage)
- "noop"            → exit 0 without writing anything
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path


def main() -> int:
    behavior = os.environ.get("LATTICE_FAKE_BEHAVIOR", "ok")
    prompt_file = os.environ.get("LATTICE_AGENT_PROMPT", "")
    output_file = os.environ.get("LATTICE_AGENT_OUTPUT", "")

    if not output_file:
        sys.stderr.write("fake_agent: LATTICE_AGENT_OUTPUT not set\n")
        return 2

    prompt_text = ""
    if prompt_file and Path(prompt_file).exists():
        prompt_text = Path(prompt_file).read_text(encoding="utf-8")

    if behavior.startswith("sleep:"):
        try:
            secs = float(behavior.split(":", 1)[1])
        except ValueError:
            secs = 0.0
        time.sleep(secs)
        Path(output_file).write_text(f"slept {secs}s\n{prompt_text}", encoding="utf-8")
        return 0

    if behavior == "fail":
        sys.stderr.write("fake_agent: simulated failure\n")
        return 1

    if behavior == "noop":
        return 0

    if behavior == "stdout":
        sys.stdout.write(f"FAKE-AGENT-STDOUT-OUTPUT\nprompt={len(prompt_text)} bytes\n")
        return 0

    # Default "ok" path.
    Path(output_file).write_text(
        f"FAKE-AGENT-OUTPUT\nprompt={len(prompt_text)} bytes\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
