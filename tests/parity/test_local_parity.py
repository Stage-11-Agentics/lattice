"""Local parity (AC-29): replay every corpus scenario and compare with its golden.

The goldens under ``golden/`` were recorded from the pre-v2 code (LAT-296). A
failure here means local-mode output or board state changed. If the change is
one of the declared changes of ``docs/hosted/SPEC.md`` G-6, normalize it in
``record.py`` or re-record the affected goldens (``python -m tests.parity.record
<scenario>``) and say so in the PR; otherwise it is a regression.
"""

from __future__ import annotations

import difflib
import json
import re
from pathlib import Path

import pytest

from tests.parity.corpus import REQUIRED_CODES, REQUIRED_COMMANDS, SCENARIOS, Cli
from tests.parity.record import GOLDEN_DIR, MODES, dump, golden_path, run_scenario

CASES = [(s, m) for s in SCENARIOS for m in MODES]


@pytest.mark.parametrize(("scenario", "mode"), CASES, ids=[f"{s.name}.{m}" for s, m in CASES])
def test_scenario_matches_golden(scenario, mode, tmp_path: Path) -> None:
    path = golden_path(scenario.name, mode)
    assert path.exists(), f"missing golden {path.name}; run python -m tests.parity.record"
    expected = path.read_text(encoding="utf-8")
    actual = dump(run_scenario(scenario, tmp_path / "board", mode=mode))
    if actual != expected:
        diff = "".join(
            difflib.unified_diff(
                expected.splitlines(keepends=True),
                actual.splitlines(keepends=True),
                fromfile=f"golden/{path.name}",
                tofile="replay",
                n=3,
            )
        )
        pytest.fail(f"parity drift in {scenario.name} ({mode}):\n{diff}")


def test_no_orphan_goldens() -> None:
    expected = {golden_path(s.name, m).name for s, m in CASES}
    present = {p.name for p in GOLDEN_DIR.glob("*.json")}
    assert present == expected


def _goldens() -> list[dict]:
    return [json.loads(golden_path(s.name, m).read_text()) for s, m in CASES]


def test_corpus_covers_every_rejection_code() -> None:
    """Every SPEC §3.1 code the CLI emits today is recorded at least once."""
    seen: set[str] = set()
    for golden in _goldens():
        for step in golden["steps"]:
            for stream in ("stdout", "response"):
                body = step.get(stream, {}).get("json")
                if isinstance(body, dict) and body.get("ok") is False:
                    seen.add(body["error"]["code"])
                # Some commands print progress before their JSON envelope.
                for line in step.get(stream, {}).get("lines", []):
                    seen.update(re.findall(r'"code": "([A-Z_]+)"', line))
    assert REQUIRED_CODES <= seen, f"codes never recorded: {sorted(REQUIRED_CODES - seen)}"


def test_corpus_covers_every_board_writing_command() -> None:
    """Every board-writing command of SPEC §3.3 (bar the review spawners) is exercised."""
    seen: set[str] = set()
    for scenario in SCENARIOS:
        for step in scenario.steps:
            if isinstance(step, Cli) and step.args:
                seen.add(step.args[0])
                seen.add(" ".join(step.args[:2]))
    assert REQUIRED_COMMANDS <= seen, f"never run: {sorted(REQUIRED_COMMANDS - seen)}"


def test_corpus_covers_forced_status_and_session_actors() -> None:
    steps = [s.args for sc in SCENARIOS for s in sc.steps if isinstance(s, Cli)]
    assert any(a[0] == "status" and "--force" in a and "--reason" in a for a in steps)
    assert any("--name" in a and a[:2] != ("session", "start") for a in steps)


@pytest.mark.parametrize("mode", MODES)
def test_attached_jsonl_payload_keeps_user_origin(mode: str) -> None:
    """Only event logs lose ``origin``; an attached JSONL artifact is user data."""
    board = json.loads(golden_path("artifacts", mode).read_text())["board"]
    payloads = [
        v["jsonl"]
        for k, v in board.items()
        if k.startswith("artifacts/payload/") and k.endswith(".jsonl")
    ]
    assert payloads, "the artifacts scenario attaches a JSONL payload"
    assert [line["origin"] for line in payloads[0]] == ["keep", {"host": "user-host"}]
