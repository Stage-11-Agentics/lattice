"""Local latency against the v1 baseline (EVALUATION §1, SPEC §5). Marker ``perf``.

Runs only on the reference machine (the operator's laptop), never in the default
suite or CI: ``uv run pytest -m perf tests/perf -q``. Each command's median must
stay within 15% or 20 ms of ``baseline.json``, whichever is larger; ``create``
may spend up to 100 ms more (the short-ID floor's budget).
"""

from __future__ import annotations

import json

import pytest

from tests.perf.record_baseline import BASELINE_PATH, COMMANDS, measure_fresh_board

pytestmark = [pytest.mark.perf, pytest.mark.timeout(600)]


def allowance_ms(command: str, baseline_ms: float) -> float:
    allowed = max(0.15 * baseline_ms, 20.0)
    if command == "create":
        allowed = max(allowed, 100.0)
    return allowed


def test_allowance_rule() -> None:
    assert allowance_ms("show", 100.0) == 20.0
    assert allowance_ms("list", 400.0) == 60.0
    assert allowance_ms("create", 100.0) == 100.0
    assert allowance_ms("create", 1000.0) == 150.0


def test_latency_within_baseline() -> None:
    baseline = json.loads(BASELINE_PATH.read_text())["commands"]
    measured = measure_fresh_board()
    failures = []
    for command in COMMANDS:
        base = baseline[command]["median_ms"]
        now = measured[command]["median_ms"]
        limit = base + allowance_ms(command, base)
        if now > limit:
            failures.append(f"{command}: {now} ms > {limit:.1f} ms (baseline {base} ms)")
    assert not failures, "; ".join(failures)
