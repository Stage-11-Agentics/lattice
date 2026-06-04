"""Tests for workflow status presets (LAT-234).

stage11 is locked as a literal; linear is its own literal; compose_workflow
serves the custom interview path and must always produce a connected,
policy-consistent graph.
"""

from __future__ import annotations

import itertools

import pytest

from lattice.core.config import (
    STATUS_PRESET_VALUES,
    compose_workflow,
    default_config,
    linear_workflow,
    stage11_workflow,
)

# The previously-shipped default workflow, locked byte-for-byte (AC8a).
# Do NOT regenerate this from code under test — it is the regression anchor.
_SHIPPED_STAGE11 = {
    "statuses": [
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
    ],
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
    "roles": ["review", "plan-review", "review-individual", "validation"],
    "wip_limits": {
        "in_progress": 10,
        "review": 5,
        "in_validation": 5,
        "pr_open": 10,
    },
    "completion_policies": {
        "done": {"require_roles": ["review"]},
        "pr_open": {"require_roles": ["validation"]},
    },
}


def _reachable(transitions: dict[str, list[str]], universal: list[str], start: str) -> set[str]:
    seen = {start}
    frontier = [start]
    while frontier:
        current = frontier.pop()
        for target in [*transitions.get(current, []), *universal]:
            if target not in seen:
                seen.add(target)
                frontier.append(target)
    return seen


class TestStage11Literal:
    def test_matches_shipped_literal(self) -> None:
        wf = stage11_workflow()
        descriptions = wf.pop("descriptions")
        assert wf == _SHIPPED_STAGE11
        assert set(descriptions) == set(_SHIPPED_STAGE11["statuses"])

    def test_default_config_workflow_is_stage11(self) -> None:
        config = default_config()
        assert config["status_preset"] == "stage11"
        wf = dict(config["workflow"])
        wf.pop("descriptions")
        assert wf == _SHIPPED_STAGE11

    def test_needs_human_is_not_a_status(self) -> None:
        for preset in STATUS_PRESET_VALUES:
            assert (
                "needs_human" not in default_config(status_preset=preset)["workflow"]["statuses"]
            )


class TestLinearPreset:
    def test_statuses(self) -> None:
        wf = linear_workflow()
        assert wf["statuses"] == [
            "backlog",
            "todo",
            "in_progress",
            "in_review",
            "done",
            "cancelled",
        ]

    def test_machinery_light(self) -> None:
        wf = linear_workflow()
        assert wf["wip_limits"] == {}
        assert wf["completion_policies"] == {}
        # Slug-keyed machinery statuses are absent by design.
        for slug in ("in_planning", "planned", "review", "in_validation", "pr_open", "blocked"):
            assert slug not in wf["statuses"]

    def test_descriptions_cover_exactly_the_statuses(self) -> None:
        wf = linear_workflow()
        assert set(wf["descriptions"]) == set(wf["statuses"])

    def test_connected(self) -> None:
        wf = linear_workflow()
        reachable = _reachable(wf["transitions"], wf["universal_targets"], "backlog")
        assert reachable == set(wf["statuses"])

    def test_default_config_linear(self) -> None:
        config = default_config(status_preset="linear")
        assert config["status_preset"] == "linear"
        assert config["workflow"]["statuses"] == linear_workflow()["statuses"]

    def test_opinionated_display_names_filtered(self) -> None:
        config = default_config(preset="opinionated", status_preset="linear")
        display_names = config["workflow"]["display_names"]
        assert set(display_names) <= set(config["workflow"]["statuses"])
        assert "in_validation" not in display_names
        assert display_names["done"] == "shipped"


class TestComposeWorkflow:
    @pytest.mark.parametrize(
        "include_review,include_validation,include_pr_open",
        list(itertools.product([True, False], repeat=3)),
    )
    def test_connected_and_policy_consistent(
        self, include_review: bool, include_validation: bool, include_pr_open: bool
    ) -> None:
        wf = compose_workflow(
            include_review=include_review,
            include_validation=include_validation,
            include_pr_open=include_pr_open,
        )
        statuses = wf["statuses"]
        transitions = wf["transitions"]
        universal = wf["universal_targets"]

        # Spine is always present; gates appear iff opted in.
        for spine in (
            "backlog",
            "in_planning",
            "planned",
            "in_progress",
            "done",
            "blocked",
            "cancelled",
        ):
            assert spine in statuses
        assert ("review" in statuses) == include_review
        assert ("in_validation" in statuses) == include_validation
        assert ("pr_open" in statuses) == include_pr_open
        assert "needs_human" not in statuses

        # Every status reachable from backlog (AC8b).
        assert _reachable(transitions, universal, "backlog") == set(statuses)

        # done reachable from every non-terminal status.
        for status in statuses:
            if status in ("done", "cancelled"):
                continue
            assert "done" in _reachable(transitions, universal, status), status

        # Transition targets are all defined statuses.
        for source, targets in transitions.items():
            assert source in statuses
            for target in targets:
                assert target in statuses, f"{source} -> {target} undefined"

        # WIP limits only for present statuses.
        assert set(wf["wip_limits"]) <= set(statuses)

        # Policy rules (R3): done gated iff review exists; pr_open gated iff
        # pr_open AND in_validation exist.
        policies = wf["completion_policies"]
        assert ("done" in policies) == include_review
        assert ("pr_open" in policies) == (include_pr_open and include_validation)
        if include_review:
            assert policies["done"] == {"require_roles": ["review"]}
        if include_pr_open and include_validation:
            assert policies["pr_open"] == {"require_roles": ["validation"]}

        # Descriptions cover exactly the present statuses.
        assert set(wf["descriptions"]) == set(statuses)

    def test_all_gates_on_uses_immediate_forward_edges(self) -> None:
        # compose_workflow is for custom subsets — it does NOT reproduce the
        # stage11 literal's skip-forward edges, and that is deliberate.
        wf = compose_workflow(include_review=True, include_validation=True, include_pr_open=True)
        assert wf["transitions"]["backlog"] == ["in_planning", "cancelled"]
        assert wf["transitions"]["review"][0] == "in_validation"

    def test_validation_off_routes_review_to_pr_open(self) -> None:
        wf = compose_workflow(include_review=True, include_validation=False, include_pr_open=True)
        assert wf["transitions"]["review"][0] == "pr_open"
        assert "pr_open" not in wf["completion_policies"]


class TestStatusPresetFallback:
    def test_invalid_status_preset_falls_back_to_stage11(self) -> None:
        config = default_config(status_preset="nonsense")
        assert config["status_preset"] == "stage11"
        wf = dict(config["workflow"])
        wf.pop("descriptions")
        assert wf == _SHIPPED_STAGE11
