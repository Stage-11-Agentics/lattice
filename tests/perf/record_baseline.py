"""Time ``show``, ``list``, ``status``, and ``create`` on the perf board; record the baseline.

Each command runs as a real ``lattice`` subprocess against a freshly generated
board (``make_board.py``): one warm-up run, then the median of 5 timed runs.
``status`` moves a different task each run (to ``cancelled``, reachable from
every status); ``create`` adds a task each run.

Record on the reference machine (the operator's laptop, G-9) when it is quiet::

    uv run python -m tests.perf.record_baseline          # writes tests/perf/baseline.json

The file records the machine's load and CPU next to the timings, because other
agents share the reference machine. ``test_local_latency.py`` compares later
code against it.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from tests.perf.make_board import build_board

BASELINE_PATH = Path(__file__).parent / "baseline.json"
RUNS = 5
COMMANDS = ("show", "list", "status", "create")


def lattice_argv() -> list[str]:
    """The ``lattice`` console script of the running interpreter's environment."""
    script = Path(sys.executable).parent / "lattice"
    if script.exists():
        return [str(script)]
    return [sys.executable, "-m", "lattice.cli.main"]


def _env(root: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("C11_", "CMUX_", "LATTICE_"))}
    env.update({"LATTICE_ROOT": str(root), "LATTICE_NO_UPDATE_CHECK": "1"})
    return env


def _args(command: str, run: int) -> list[str]:
    if command == "show":
        return ["show", "PERF-500"]
    if command == "list":
        return ["list"]
    if command == "status":
        return ["status", f"PERF-{101 + run}", "cancelled", "--actor", "human:atin"]
    if command == "create":
        return ["create", f"Perf create {run}", "--actor", "human:atin"]
    raise ValueError(command)


def time_commands(root: Path) -> dict[str, dict[str, object]]:
    """Median and samples (ms) of each command on the board at *root*."""
    base = lattice_argv()
    env = _env(root)
    results: dict[str, dict[str, object]] = {}
    for command in COMMANDS:
        samples: list[float] = []
        for run in range(RUNS + 1):  # run 0 warms caches and is discarded
            argv = base + _args(command, run)
            start = time.perf_counter()
            proc = subprocess.run(argv, cwd=root, env=env, capture_output=True, text=True)
            elapsed = (time.perf_counter() - start) * 1000
            if proc.returncode != 0:
                raise RuntimeError(f"{' '.join(argv)} failed: {proc.stderr or proc.stdout}")
            if run:
                samples.append(round(elapsed, 1))
        results[command] = {
            "median_ms": round(statistics.median(samples), 1),
            "samples_ms": samples,
        }
    return results


def machine() -> dict[str, object]:
    def run(*argv: str) -> str:
        try:
            return subprocess.run(argv, capture_output=True, text=True, check=False).stdout.strip()
        except OSError:
            return ""

    cpu = run("sysctl", "-n", "machdep.cpu.brand_string") or platform.processor()
    return {
        "cpu": cpu,
        "cores": os.cpu_count(),
        "memory_bytes": int(run("sysctl", "-n", "hw.memsize") or 0) or None,
        "platform": platform.platform(),
        "python": platform.python_version(),
        "uptime": run("uptime"),
        "loadavg": [round(x, 2) for x in os.getloadavg()],
    }


def measure_fresh_board() -> dict[str, dict[str, object]]:
    tmp = Path(tempfile.mkdtemp(prefix="lattice-perf-"))
    try:
        root = build_board(tmp / "board")
        return time_commands(root)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    before = machine()
    timings = measure_fresh_board()
    after = machine()
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False
    ).stdout.strip()
    doc = {
        "recorded_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "git_sha": sha,
        "board": "tests/perf/make_board.py: 1000 active tasks (~45 events each), 300 archived",
        "method": f"lattice subprocess, 1 warm-up then median of {RUNS} runs",
        "machine": {**before, "loadavg_after": after["loadavg"], "uptime_after": after["uptime"]},
        "commands": timings,
    }
    BASELINE_PATH.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    print(json.dumps(doc["commands"], indent=2, sort_keys=True))
    print(f"load before {before['loadavg']}, after {after['loadavg']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
