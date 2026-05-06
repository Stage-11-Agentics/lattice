"""Tests for `lattice raise` / `lattice clear` (LAT-210 alerts mechanism)."""

from __future__ import annotations

import json
from pathlib import Path

from lattice.storage.fs import LATTICE_DIR


_ACTOR_AGENT = "agent:claude"
_ACTOR_HUMAN = "human:test"


def _read_events(root: Path, task_id: str) -> list[dict]:
    path = root / LATTICE_DIR / "events" / f"{task_id}.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _read_snapshot(root: Path, task_id: str) -> dict:
    return json.loads(
        (root / LATTICE_DIR / "tasks" / f"{task_id}.json").read_text()
    )


def _create_task(invoke, title: str = "Test task") -> str:
    r = invoke("create", title, "--actor", _ACTOR_AGENT, "--json")
    assert r.exit_code == 0, r.output
    return json.loads(r.output)["data"]["id"]


# ---------------------------------------------------------------------------
# Raise — happy path
# ---------------------------------------------------------------------------


class TestRaiseHappyPath:
    def test_raise_writes_event_and_snapshot(self, invoke, initialized_root):
        task_id = _create_task(invoke)

        r = invoke(
            "raise",
            task_id,
            "needs_human",
            "--short",
            "Decision: REST or GraphQL?",
            "--actor",
            _ACTOR_AGENT,
            "--no-c11",
            "--json",
        )
        assert r.exit_code == 0, r.output

        events = _read_events(initialized_root, task_id)
        raise_events = [e for e in events if e["type"] == "alert_raised"]
        assert len(raise_events) == 1
        assert raise_events[0]["data"]["name"] == "needs_human"
        assert raise_events[0]["data"]["short"] == "Decision: REST or GraphQL?"

        snap = _read_snapshot(initialized_root, task_id)
        assert "needs_human" in snap["alerts"]
        assert snap["alerts"]["needs_human"]["short"] == "Decision: REST or GraphQL?"
        assert snap["alerts"]["needs_human"]["raised_by"] == _ACTOR_AGENT

    def test_raise_with_long_and_prompt(self, invoke, initialized_root):
        task_id = _create_task(invoke)

        r = invoke(
            "raise",
            task_id,
            "blocked",
            "--short",
            "CI failing",
            "--long",
            "The pipeline cannot resolve dependency X.",
            "--prompt",
            "lattice clear LAT-1 blocked --answer fixed",
            "--actor",
            _ACTOR_AGENT,
            "--no-c11",
        )
        assert r.exit_code == 0

        snap = _read_snapshot(initialized_root, task_id)
        alert = snap["alerts"]["blocked"]
        assert alert["long"].startswith("The pipeline")
        assert alert["prompt"].startswith("lattice clear")

    def test_raise_with_evidence_ref(self, invoke, initialized_root):
        task_id = _create_task(invoke)

        r = invoke(
            "raise",
            task_id,
            "needs_human",
            "--short",
            "Plan needs approval",
            "--evidence-ref",
            "art_01HEXAMPLE",
            "--actor",
            _ACTOR_AGENT,
            "--no-c11",
        )
        assert r.exit_code == 0
        snap = _read_snapshot(initialized_root, task_id)
        assert snap["alerts"]["needs_human"]["evidence_ref"] == "art_01HEXAMPLE"


# ---------------------------------------------------------------------------
# Double-raise + idempotent re-clear
# ---------------------------------------------------------------------------


class TestDoubleRaiseAndIdempotentClear:
    def test_double_raise_writes_two_events_and_overwrites_snapshot(
        self, invoke, initialized_root
    ):
        task_id = _create_task(invoke)

        invoke(
            "raise", task_id, "needs_human",
            "--short", "Q1", "--actor", _ACTOR_AGENT, "--no-c11",
        )
        invoke(
            "raise", task_id, "needs_human",
            "--short", "Q2", "--actor", _ACTOR_AGENT, "--no-c11",
        )

        events = _read_events(initialized_root, task_id)
        raises = [e for e in events if e["type"] == "alert_raised"]
        assert len(raises) == 2

        snap = _read_snapshot(initialized_root, task_id)
        assert snap["alerts"]["needs_human"]["short"] == "Q2"

    def test_idempotent_reclear_no_event_no_op_success(self, invoke, initialized_root):
        task_id = _create_task(invoke)

        # Clear before raising — should be a no-op success.
        r = invoke(
            "clear",
            task_id,
            "needs_human",
            "--actor",
            _ACTOR_HUMAN,
            "--no-c11",
            "--json",
        )
        assert r.exit_code == 0, r.output
        parsed = json.loads(r.output)
        assert parsed["ok"] is True
        assert parsed["data"]["cleared"] is False

        events = _read_events(initialized_root, task_id)
        clears = [e for e in events if e["type"] == "alert_cleared"]
        assert len(clears) == 0


# ---------------------------------------------------------------------------
# Clear — happy path
# ---------------------------------------------------------------------------


class TestClearHappyPath:
    def test_clear_after_raise_removes_alert(self, invoke, initialized_root):
        task_id = _create_task(invoke)

        invoke(
            "raise", task_id, "needs_human",
            "--short", "Q?", "--actor", _ACTOR_AGENT, "--no-c11",
        )
        r = invoke(
            "clear",
            task_id,
            "needs_human",
            "--answer",
            "REST.",
            "--actor",
            _ACTOR_HUMAN,
            "--no-c11",
            "--json",
        )
        assert r.exit_code == 0
        parsed = json.loads(r.output)
        assert parsed["data"]["cleared"] is True

        snap = _read_snapshot(initialized_root, task_id)
        assert "needs_human" not in snap["alerts"]

        events = _read_events(initialized_root, task_id)
        clears = [e for e in events if e["type"] == "alert_cleared"]
        assert len(clears) == 1
        assert clears[0]["data"]["answer"] == "REST."


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_unknown_alert_name_rejected(self, invoke, initialized_root):
        task_id = _create_task(invoke)
        r = invoke(
            "raise",
            task_id,
            "totally_made_up",
            "--short",
            "x",
            "--actor",
            _ACTOR_AGENT,
            "--no-c11",
            "--json",
        )
        assert r.exit_code != 0
        parsed = json.loads(r.output)
        assert parsed["error"]["code"] == "VALIDATION_ERROR"

    def test_oversized_short_rejected(self, invoke, initialized_root):
        task_id = _create_task(invoke)
        r = invoke(
            "raise",
            task_id,
            "needs_human",
            "--short",
            "x" * 1000,
            "--actor",
            _ACTOR_AGENT,
            "--no-c11",
            "--json",
        )
        assert r.exit_code != 0

    def test_terminal_status_rejected(self, invoke, cli_runner, cli_env, fill_plan, initialized_root):
        from lattice.cli.main import cli

        task_id = _create_task(invoke)
        # Fast-track to done via cancelled (universal target).
        r = cli_runner.invoke(
            cli,
            ["status", task_id, "cancelled", "--actor", _ACTOR_HUMAN],
            env=cli_env,
        )
        assert r.exit_code == 0, r.output

        r = invoke(
            "raise",
            task_id,
            "needs_human",
            "--short",
            "x",
            "--actor",
            _ACTOR_AGENT,
            "--no-c11",
            "--json",
        )
        assert r.exit_code != 0
        parsed = json.loads(r.output)
        assert parsed["error"]["code"] == "task_terminal"


# ---------------------------------------------------------------------------
# Snapshot rebuild determinism (replay survival)
# ---------------------------------------------------------------------------


class TestReplayDeterminism:
    def test_oversize_long_event_truncated_on_replay(self, initialized_root):
        """A historical event with an oversize ``long`` must replay cleanly."""
        from lattice.core.events import ALERT_LONG_MAX, create_event
        from lattice.core.tasks import apply_event_to_snapshot

        # Build a minimal task_created snapshot.
        created = create_event(
            type="task_created",
            task_id="task_01TESTREPLAY00000000000",
            actor=_ACTOR_AGENT,
            data={"title": "x", "status": "backlog"},
        )
        snap = apply_event_to_snapshot(None, created)

        # Synthesize an oversize alert_raised event (bypassing CLI validation).
        oversize = "y" * (ALERT_LONG_MAX + 500)
        ev = create_event(
            type="alert_raised",
            task_id=created["task_id"],
            actor=_ACTOR_AGENT,
            data={"name": "needs_human", "short": "ok", "long": oversize},
        )
        snap = apply_event_to_snapshot(snap, ev)

        # Mutation handler must truncate, not crash.
        assert "needs_human" in snap["alerts"]
        assert len(snap["alerts"]["needs_human"]["long"]) == ALERT_LONG_MAX
