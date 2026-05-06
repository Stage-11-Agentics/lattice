"""Tests for the LAT-210 alert filters in lattice list / show / next."""

from __future__ import annotations

import json


_ACTOR = "agent:claude"
_HUMAN = "human:test"


def _create(invoke, title: str) -> str:
    r = invoke("create", title, "--actor", _ACTOR, "--json")
    assert r.exit_code == 0, r.output
    return json.loads(r.output)["data"]["id"]


def _raise(invoke, task_id: str, alert_name: str, short: str = "x") -> None:
    r = invoke(
        "raise",
        task_id,
        alert_name,
        "--short",
        short,
        "--actor",
        _ACTOR,
        "--no-c11",
    )
    assert r.exit_code == 0, r.output


# ---------------------------------------------------------------------------
# lattice list --alerted / --alert / --no-alerts
# ---------------------------------------------------------------------------


class TestListAlertFilters:
    def test_alerted_filter_returns_only_alerted_tasks(self, invoke):
        a = _create(invoke, "Plain task")
        b = _create(invoke, "Alerted task")
        _raise(invoke, b, "needs_human", "Q?")

        r = invoke("list", "--alerted", "--json")
        assert r.exit_code == 0
        data = json.loads(r.output)["data"]
        ids = {t["id"] for t in data}
        assert b in ids
        assert a not in ids

    def test_alert_named_filter(self, invoke):
        a = _create(invoke, "needs_human alert")
        _raise(invoke, a, "needs_human", "Decision?")
        b = _create(invoke, "blocked alert")
        _raise(invoke, b, "blocked", "CI down")

        r = invoke("list", "--alert", "needs_human", "--json")
        data = json.loads(r.output)["data"]
        ids = {t["id"] for t in data}
        assert a in ids
        assert b not in ids

    def test_no_alerts_filter(self, invoke):
        a = _create(invoke, "Quiet task")
        b = _create(invoke, "Loud task")
        _raise(invoke, b, "blocked", "stuck")

        r = invoke("list", "--no-alerts", "--json")
        data = json.loads(r.output)["data"]
        ids = {t["id"] for t in data}
        assert a in ids
        assert b not in ids

    def test_no_alerts_and_alerted_are_mutually_exclusive(self, invoke):
        r = invoke("list", "--alerted", "--no-alerts", "--json")
        assert r.exit_code != 0
        parsed = json.loads(r.output)
        assert parsed["error"]["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# lattice show — Alerts block
# ---------------------------------------------------------------------------


class TestShowAlertsBlock:
    def test_alerts_block_present_when_alerted(self, invoke):
        task_id = _create(invoke, "Task w/ alert")
        _raise(invoke, task_id, "needs_human", "Decide approach")

        r = invoke("show", task_id)
        assert r.exit_code == 0
        assert "Alerts:" in r.output
        assert "[NEEDS_HUMAN]" in r.output
        assert "Decide approach" in r.output

    def test_alerts_block_absent_when_clean(self, invoke):
        task_id = _create(invoke, "Plain")
        r = invoke("show", task_id)
        assert r.exit_code == 0
        assert "Alerts:" not in r.output


# ---------------------------------------------------------------------------
# lattice next — banner
# ---------------------------------------------------------------------------


class TestNextBanner:
    def test_banner_appears_when_alerts_exist(self, invoke):
        a = _create(invoke, "Alerted")
        _raise(invoke, a, "needs_human", "decide")
        # Need a pickable task.
        _create(invoke, "Pickable")

        r = invoke("next")
        assert r.exit_code == 0
        assert "task(s) need attention" in r.output

    def test_no_banner_when_no_alerts(self, invoke):
        _create(invoke, "Pickable")
        r = invoke("next")
        assert r.exit_code == 0
        assert "task(s) need attention" not in r.output

    def test_alerted_task_excluded_from_next(self, invoke):
        task_id = _create(invoke, "Solo")
        _raise(invoke, task_id, "needs_human", "?")

        r = invoke("next", "--json")
        data = json.loads(r.output)
        assert data["data"] is None
        assert data.get("alerted_count") == 1
