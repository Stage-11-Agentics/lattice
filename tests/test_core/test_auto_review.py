"""Tests for the pure auto-review gating logic (LAT-211)."""

from __future__ import annotations

import pytest

from lattice.core.auto_review import (
    AUTO_REVIEW_ACTOR,
    DAEMON_DIR_NAME,
    format_skip_reason,
    resolve_mode,
    review_type_for_status,
    should_auto_fire,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_constants_have_expected_values() -> None:
    assert DAEMON_DIR_NAME == ".daemon"
    assert AUTO_REVIEW_ACTOR == "agent:lattice-auto-review"


# ---------------------------------------------------------------------------
# review_type_for_status
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("review", "code-review"),
        ("planned", "plan-review"),
        ("in_progress", None),
        ("done", None),
        ("backlog", None),
        ("needs_human", None),
    ],
)
def test_review_type_for_status(status: str, expected: str | None) -> None:
    assert review_type_for_status(status) == expected


# ---------------------------------------------------------------------------
# resolve_mode
# ---------------------------------------------------------------------------


def test_resolve_mode_review_default_is_single() -> None:
    assert resolve_mode({}, "review") == "single"


def test_resolve_mode_planned_default_is_inline() -> None:
    assert resolve_mode({}, "planned") == "inline"


def test_resolve_mode_review_reads_review_mode() -> None:
    assert resolve_mode({"review_mode": "triple"}, "review") == "triple"
    assert resolve_mode({"review_mode": "inline"}, "review") == "inline"


def test_resolve_mode_planned_reads_plan_review_mode() -> None:
    assert resolve_mode({"plan_review_mode": "triple"}, "planned") == "triple"
    assert resolve_mode({"plan_review_mode": "single"}, "planned") == "single"


# ---------------------------------------------------------------------------
# should_auto_fire — happy paths
# ---------------------------------------------------------------------------


class TestShouldAutoFireHappyPaths:
    def test_review_default_config_fires(self) -> None:
        ok, reason = should_auto_fire({}, "review", no_auto_review_flag=False)
        assert ok is True
        assert reason is None

    def test_planned_with_triple_mode_fires(self) -> None:
        # plan_review_mode defaults to inline, which would skip; explicit
        # triple unblocks the planned gate.
        ok, reason = should_auto_fire(
            {"plan_review_mode": "triple"},
            "planned",
            no_auto_review_flag=False,
        )
        assert ok is True
        assert reason is None

    def test_review_explicit_triple_fires(self) -> None:
        ok, reason = should_auto_fire(
            {"review_mode": "triple"},
            "review",
            no_auto_review_flag=False,
        )
        assert ok is True
        assert reason is None


# ---------------------------------------------------------------------------
# should_auto_fire — skip paths
# ---------------------------------------------------------------------------


class TestShouldAutoFireSkipPaths:
    def test_skip_irrelevant_status(self) -> None:
        ok, reason = should_auto_fire({}, "in_progress", no_auto_review_flag=False)
        assert ok is False
        assert reason == "not_a_review_gate"

    def test_skip_when_no_auto_review_flag_set(self) -> None:
        ok, reason = should_auto_fire({}, "review", no_auto_review_flag=True)
        assert ok is False
        assert reason == "no_auto_review_flag"

    def test_no_auto_review_flag_takes_priority_over_config(self) -> None:
        # Even if config disables, the flag-skip wins so the message can
        # tell the operator their flag was honored rather than redundant.
        ok, reason = should_auto_fire(
            {"auto_code_review_on_transition": False},
            "review",
            no_auto_review_flag=True,
        )
        assert ok is False
        assert reason == "no_auto_review_flag"

    def test_skip_disabled_code_review_in_config(self) -> None:
        ok, reason = should_auto_fire(
            {"auto_code_review_on_transition": False},
            "review",
            no_auto_review_flag=False,
        )
        assert ok is False
        assert reason == "disabled_in_config"

    def test_skip_disabled_plan_review_in_config(self) -> None:
        ok, reason = should_auto_fire(
            {
                "plan_review_mode": "triple",  # would otherwise fire
                "auto_plan_review_on_transition": False,
            },
            "planned",
            no_auto_review_flag=False,
        )
        assert ok is False
        assert reason == "disabled_in_config"

    def test_disabled_only_affects_matching_gate(self) -> None:
        # Disabling code-review must not affect planned, and vice versa.
        ok, reason = should_auto_fire(
            {
                "auto_code_review_on_transition": False,
                "plan_review_mode": "triple",
            },
            "planned",
            no_auto_review_flag=False,
        )
        assert ok is True
        assert reason is None

    def test_skip_inline_review_mode(self) -> None:
        ok, reason = should_auto_fire(
            {"review_mode": "inline"},
            "review",
            no_auto_review_flag=False,
        )
        assert ok is False
        assert reason == "inline_mode"

    def test_skip_inline_plan_review_mode(self) -> None:
        # plan_review_mode default is inline, so this also exercises the
        # default-skip path for the planned gate.
        ok, reason = should_auto_fire({}, "planned", no_auto_review_flag=False)
        assert ok is False
        assert reason == "inline_mode"


# ---------------------------------------------------------------------------
# format_skip_reason
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reason", "fragment"),
    [
        ("inline_mode", "inline mode"),
        ("disabled_in_config", "disabled in config"),
        ("no_auto_review_flag", "--no-auto-review"),
        ("not_a_review_gate", "not a review gate"),
        ("executable_not_found", "executable not found"),
        ("review_in_flight", "already in flight"),
        ("spawn_failed:OSError boom", "spawn failed: OSError boom"),
    ],
)
def test_format_skip_reason_strings(reason: str, fragment: str) -> None:
    rendered = format_skip_reason(reason)
    assert "auto-review skipped" in rendered
    assert fragment in rendered


def test_format_skip_reason_includes_holder_pid() -> None:
    rendered = format_skip_reason("review_in_flight", holder_pid=4242)
    assert "pid 4242" in rendered


def test_format_skip_reason_unknown_falls_through() -> None:
    rendered = format_skip_reason("custom_reason")
    assert "auto-review skipped" in rendered
    assert "custom_reason" in rendered
