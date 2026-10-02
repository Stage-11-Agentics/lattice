"""The hosted replay's normalization cannot hide a divergence (H-12 review, lens A1).

``declared_differences`` rewrites only SPEC's declared hosted differences, and
only after checking their exact hosted form. These cases feed it hosted steps
that differ in any other way and require that it either raises or leaves the
difference for the golden comparison to catch.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from tests.parity.hosted import (
    CACHE_DOCTOR_LINE,
    UndeclaredDifference,
    comparable,
    declared_differences,
    local_only_step,
)

BINDING = "parity/maintenance-1"
DOCTOR_LINES = ["Checking 1 tasks...", "✓ Short ID aliases consistent", "", "No issues found."]


def capture(*steps: dict[str, Any]) -> dict[str, Any]:
    setup = {"setup": "x"}
    return {"scenario": "s", "mode": "m", "description": "", "steps": [setup, *steps], "board": {}}


def plain(args: list[str], out: list[str], err: list[str] | None = None) -> dict[str, Any]:
    return {
        "args": args,
        "exit_code": 0,
        "stdout": {"lines": out},
        "stderr": {"lines": err or []},
    }


def hosted_doctor(lines: list[str]) -> dict[str, Any]:
    return plain(["doctor"], lines)


def with_cache_line(lines: list[str]) -> list[str]:
    return [*lines[:2], CACHE_DOCTOR_LINE, *lines[2:]]


@pytest.mark.parametrize("args", [["rebuild", "--all"], ["rebuild", "--all", "--json"]])
def test_the_exact_local_only_refusal_is_accepted(args: list[str]) -> None:
    refusal = local_only_step(args, "rebuild", BINDING)
    out = declared_differences(capture(refusal), binding=BINDING)
    assert out["steps"][1] == {"args": args, "local_only": "rebuild"}


def _mutations() -> list[tuple[str, Any]]:
    def extra_stdout(step: dict) -> None:
        step["stdout"] = {"lines": ["Rebuilt 2 tasks"]}

    def extra_stderr(step: dict) -> None:
        step["stderr"]["lines"].append("lattice: cannot reach parity")

    def exception(step: dict) -> None:
        step["exception"] = "RuntimeError: boom"

    def exit_zero(step: dict) -> None:
        step["exit_code"] = 0

    def other_binding(step: dict) -> None:
        step["stderr"]["lines"] = [step["stderr"]["lines"][0].replace(BINDING, "parity/other")]

    return [
        ("extra stdout", extra_stdout),
        ("extra stderr", extra_stderr),
        ("an exception", exception),
        ("exit 0", exit_zero),
        ("another binding", other_binding),
    ]


@pytest.mark.parametrize(("name", "mutate"), _mutations(), ids=[n for n, _ in _mutations()])
def test_a_local_only_step_with_anything_else_is_caught(name: str, mutate: Any) -> None:
    step = local_only_step(["rebuild", "--all"], "rebuild", BINDING)
    mutate(step)
    with pytest.raises(UndeclaredDifference):
        declared_differences(capture(step), binding=BINDING)


@pytest.mark.parametrize(
    "extra",
    [{"details": {"command": "rebuild"}}, {"hint": "x"}],
    ids=["details", "another field"],
)
def test_a_json_refusal_with_an_extra_field_is_caught(extra: dict) -> None:
    step = local_only_step(["rebuild", "--json"], "rebuild", BINDING)
    step["stdout"]["json"]["error"].update(extra)
    with pytest.raises(UndeclaredDifference):
        declared_differences(capture(step), binding=BINDING)
    top = local_only_step(["rebuild", "--json"], "rebuild", BINDING)
    top["stdout"]["json"]["data"] = None
    with pytest.raises(UndeclaredDifference):
        declared_differences(capture(top), binding=BINDING)


def test_exactly_one_doctor_cache_line_is_removed() -> None:
    one = hosted_doctor(with_cache_line(DOCTOR_LINES))
    out = declared_differences(capture(one), binding=BINDING)
    assert out["steps"][1]["stdout"]["lines"] == DOCTOR_LINES
    for lines in (DOCTOR_LINES, [CACHE_DOCTOR_LINE, *with_cache_line(DOCTOR_LINES)]):
        with pytest.raises(UndeclaredDifference):
            declared_differences(capture(hosted_doctor(lines)), binding=BINDING)


def test_other_doctor_output_still_reaches_the_comparison() -> None:
    golden = capture(plain(["doctor"], DOCTOR_LINES))
    hosted = capture(hosted_doctor(with_cache_line([*DOCTOR_LINES, "extra"])))
    actual = comparable(declared_differences(hosted, binding=BINDING))
    assert actual != comparable(golden)


def test_hints_are_rewritten_only_in_their_exact_form() -> None:
    local = "Next: write the plan in plans/task_1.md, then move to planned."
    exact = (
        "Next: write the plan with 'lattice plan write PAR-1 --file <path>', then move to planned."
    )
    hosted = capture(plain(["status", "PAR-1", "in_planning"], ["Status: x", exact]))
    hosted["board"] = {"ids.json": {"json": {"map": {"PAR-1": "task_1"}}}}
    out = declared_differences(copy.deepcopy(hosted), binding=BINDING)
    assert out["steps"][1]["stdout"]["lines"] == ["Status: x", local]
    for variant in (exact + " Also this.", exact.replace("--file", "--stdin")):
        changed = copy.deepcopy(hosted)
        changed["steps"][1]["stdout"]["lines"][1] = variant
        out = declared_differences(changed, binding=BINDING)
        assert out["steps"][1]["stdout"]["lines"][1] != local

    required = (
        "Error: Plan for task_1 is still scaffold. Override with --force --reason."
        " Write the plan with `lattice plan write PAR-1 --file <path>`."
    )
    step = plain(["status", "PAR-1", "in_progress"], [], [required])
    out = declared_differences(capture(step), binding=BINDING)
    assert out["steps"][1]["stderr"]["lines"] == [
        "Error: Plan for task_1 is still scaffold. Override with --force --reason."
    ]
    step = plain(["status", "PAR-1", "in_progress"], [], [required + " More."])
    out = declared_differences(capture(step), binding=BINDING)
    assert out["steps"][1]["stderr"]["lines"][0].endswith(" More.")


def test_hosted_task_type_hint_is_rewritten_only_for_its_slug_and_exact_command() -> None:
    local_message = (
        "Invalid task type: 'research'. Valid types: task, bug, chore. "
        "On a local board, add the type to `.lattice/config.json` `task_types`."
    )
    hosted_message = (
        "Invalid task type: 'research'. Valid types: task, bug, chore. "
        "On a hosted board, ask an admin on the server host to run `lattice server project "
        'config maintenance-1 --set \'task_types=["task","bug","chore","research"]\'`; '
        "this replaces the list, so include the existing values when adding a type."
    )
    step = plain(["create", "Research", "--type", "research"], [], ["Error: " + hosted_message])
    normalized = declared_differences(capture(step), binding=BINDING)
    assert normalized["steps"][1]["stderr"]["lines"] == ["Error: " + local_message]

    for variant in (
        hosted_message.replace("maintenance-1", "other-project"),
        hosted_message.replace("task_types=[", 'task_types=["other",'),
        hosted_message + " Extra.",
    ):
        changed = plain(["create", "Research", "--type", "research"], [], ["Error: " + variant])
        with pytest.raises(UndeclaredDifference):
            declared_differences(capture(changed), binding=BINDING)
