"""Tests for the `lattice needs-human` flag command (LAT-232)."""

from __future__ import annotations

import json

_ACTOR = "agent:test"


def _create(invoke, title: str = "Flag me") -> str:
    r = invoke("create", title, "--actor", _ACTOR, "--json")
    assert r.exit_code == 0, r.output
    return json.loads(r.output)["data"]["id"]


class TestSetFlag:
    def test_set_in_backlog(self, invoke, initialized_root) -> None:
        task_id = _create(invoke)
        r = invoke("needs-human", task_id, "Need: which DB?", "--actor", _ACTOR, "--json")
        assert r.exit_code == 0, r.output
        data = json.loads(r.output)["data"]
        assert data["status"] == "backlog"  # status untouched
        flag = data["needs_human"]
        assert flag["flagged_by"] == _ACTOR
        assert flag["reason"] == "Need: which DB?"
        assert flag["since"]

    def test_set_in_any_status(self, invoke, initialized_root, fill_plan) -> None:
        """The flag is settable in every status, including done."""
        task_id = _create(invoke)
        invoke("status", task_id, "in_planning", "--actor", _ACTOR)
        fill_plan(task_id)
        for status in ("planned", "in_progress", "review"):
            invoke("status", task_id, status, "--actor", _ACTOR)
        invoke("comment", task_id, "LGTM", "--role", "review", "--actor", _ACTOR)
        invoke("status", task_id, "done", "--actor", _ACTOR)
        r = invoke("needs-human", task_id, "Post-ship question", "--actor", _ACTOR, "--json")
        assert r.exit_code == 0, r.output
        data = json.loads(r.output)["data"]
        assert data["status"] == "done"
        assert data["needs_human"]["reason"] == "Post-ship question"

    def test_reason_required(self, invoke, initialized_root) -> None:
        task_id = _create(invoke)
        r = invoke("needs-human", task_id, "--actor", _ACTOR, "--json")
        assert r.exit_code != 0
        parsed = json.loads(r.output)
        assert parsed["ok"] is False
        assert "REASON is required" in parsed["error"]["message"]

    def test_blank_reason_rejected(self, invoke, initialized_root) -> None:
        task_id = _create(invoke)
        r = invoke("needs-human", task_id, "   ", "--actor", _ACTOR)
        assert r.exit_code != 0

    def test_double_set_rejected(self, invoke, initialized_root) -> None:
        task_id = _create(invoke)
        invoke("needs-human", task_id, "first", "--actor", _ACTOR)
        r = invoke("needs-human", task_id, "second", "--actor", _ACTOR, "--json")
        assert r.exit_code != 0
        parsed = json.loads(r.output)
        assert parsed["error"]["code"] == "FLAG_ALREADY_SET"
        assert "first" in parsed["error"]["message"]

    def test_flag_event_appended(self, invoke, initialized_root) -> None:
        task_id = _create(invoke)
        invoke("needs-human", task_id, "decision needed", "--actor", _ACTOR)
        events_path = initialized_root / ".lattice" / "events" / f"{task_id}.jsonl"
        events = [json.loads(line) for line in events_path.read_text().splitlines() if line]
        flag_events = [e for e in events if e["type"] == "needs_human_flagged"]
        assert len(flag_events) == 1
        assert flag_events[0]["actor"] == _ACTOR
        assert flag_events[0]["data"]["reason"] == "decision needed"

    def test_orthogonal_to_blocked(self, invoke, initialized_root, fill_plan) -> None:
        """A task can be blocked AND flagged simultaneously."""
        task_id = _create(invoke)
        invoke("status", task_id, "in_planning", "--actor", _ACTOR)
        fill_plan(task_id)
        invoke("status", task_id, "planned", "--actor", _ACTOR)
        invoke("status", task_id, "blocked", "--actor", _ACTOR)
        r = invoke("needs-human", task_id, "also need a decision", "--actor", _ACTOR, "--json")
        assert r.exit_code == 0
        data = json.loads(r.output)["data"]
        assert data["status"] == "blocked"
        assert data["needs_human"]["reason"] == "also need a decision"


class TestClearFlag:
    def test_clear(self, invoke, initialized_root) -> None:
        task_id = _create(invoke)
        invoke("needs-human", task_id, "question", "--actor", _ACTOR)
        r = invoke(
            "needs-human", task_id, "--clear", "--note", "answered", "--actor", "human:t", "--json"
        )
        assert r.exit_code == 0, r.output
        data = json.loads(r.output)["data"]
        assert data["needs_human"] is None

    def test_clear_event_appended(self, invoke, initialized_root) -> None:
        task_id = _create(invoke)
        invoke("needs-human", task_id, "question", "--actor", _ACTOR)
        invoke("needs-human", task_id, "--clear", "--note", "resolved", "--actor", "human:t")
        events_path = initialized_root / ".lattice" / "events" / f"{task_id}.jsonl"
        events = [json.loads(line) for line in events_path.read_text().splitlines() if line]
        clear_events = [e for e in events if e["type"] == "needs_human_cleared"]
        assert len(clear_events) == 1
        assert clear_events[0]["actor"] == "human:t"
        assert clear_events[0]["data"]["note"] == "resolved"

    def test_clear_when_not_set_rejected(self, invoke, initialized_root) -> None:
        task_id = _create(invoke)
        r = invoke("needs-human", task_id, "--clear", "--actor", _ACTOR, "--json")
        assert r.exit_code != 0
        assert json.loads(r.output)["error"]["code"] == "FLAG_NOT_SET"

    def test_clear_with_reason_rejected(self, invoke, initialized_root) -> None:
        task_id = _create(invoke)
        invoke("needs-human", task_id, "question", "--actor", _ACTOR)
        r = invoke("needs-human", task_id, "oops", "--clear", "--actor", _ACTOR)
        assert r.exit_code != 0

    def test_set_clear_set_again(self, invoke, initialized_root) -> None:
        task_id = _create(invoke)
        invoke("needs-human", task_id, "q1", "--actor", _ACTOR)
        invoke("needs-human", task_id, "--clear", "--actor", _ACTOR)
        r = invoke("needs-human", task_id, "q2", "--actor", _ACTOR, "--json")
        assert r.exit_code == 0
        assert json.loads(r.output)["data"]["needs_human"]["reason"] == "q2"


class TestQueueAndSurfaces:
    def test_list_needs_human_filter(self, invoke, initialized_root) -> None:
        flagged_id = _create(invoke, "Flagged task")
        _create(invoke, "Unflagged task")
        invoke("needs-human", flagged_id, "decision", "--actor", _ACTOR)

        r = invoke("list", "--needs-human", "--json")
        assert r.exit_code == 0
        data = json.loads(r.output)["data"]
        assert len(data) == 1
        assert data[0]["id"] == flagged_id
        assert data[0]["needs_human"]["reason"] == "decision"

    def test_list_human_output_marks_flagged(self, invoke, initialized_root) -> None:
        flagged_id = _create(invoke, "Flagged task")
        invoke("needs-human", flagged_id, "decision", "--actor", _ACTOR)
        r = invoke("list")
        assert r.exit_code == 0
        flagged_line = next(line for line in r.output.splitlines() if "Flagged task" in line)
        assert flagged_line.startswith(">>> ")
        assert "needs human: decision" in flagged_line

    def test_show_renders_flag(self, invoke, initialized_root) -> None:
        task_id = _create(invoke)
        invoke("needs-human", task_id, "which key?", "--actor", _ACTOR)
        r = invoke("show", task_id)
        assert r.exit_code == 0
        assert "NEEDS HUMAN" in r.output
        assert "which key?" in r.output

    def test_next_skips_flagged(self, invoke, initialized_root) -> None:
        flagged_id = _create(invoke, "Flagged")
        other_id = _create(invoke, "Pickable")
        invoke("needs-human", flagged_id, "decision", "--actor", _ACTOR)
        r = invoke("next", "--json")
        assert r.exit_code == 0
        data = json.loads(r.output)["data"]
        assert data["id"] == other_id

    def test_weather_surfaces_flag_reason(self, invoke, initialized_root) -> None:
        task_id = _create(invoke, "Stormy")
        invoke("needs-human", task_id, "pick a vendor", "--actor", _ACTOR)
        r = invoke("weather", "--json")
        assert r.exit_code == 0
        data = json.loads(r.output)["data"]
        nh_items = [i for i in data["attention"] if i["type"] == "needs_human"]
        assert len(nh_items) == 1
        assert nh_items[0]["detail"] == "pick a vendor"


class TestRebuildDeterminism:
    def test_rebuild_reproduces_flag(self, invoke, initialized_root) -> None:
        task_id = _create(invoke)
        invoke("needs-human", task_id, "decision", "--actor", _ACTOR)
        snap_path = initialized_root / ".lattice" / "tasks" / f"{task_id}.json"
        before = snap_path.read_text()
        r = invoke("rebuild", "--all")
        assert r.exit_code == 0, r.output
        assert snap_path.read_text() == before

    def test_rebuild_reproduces_cleared_flag(self, invoke, initialized_root) -> None:
        task_id = _create(invoke)
        invoke("needs-human", task_id, "decision", "--actor", _ACTOR)
        invoke("needs-human", task_id, "--clear", "--actor", _ACTOR)
        snap_path = initialized_root / ".lattice" / "tasks" / f"{task_id}.json"
        before = snap_path.read_text()
        r = invoke("rebuild", "--all")
        assert r.exit_code == 0, r.output
        assert snap_path.read_text() == before


class TestLegacyStatusInstances:
    """Instances whose config still carries the needs_human STATUS keep working."""

    @staticmethod
    def _add_legacy_status(initialized_root) -> None:
        config_path = initialized_root / ".lattice" / "config.json"
        config = json.loads(config_path.read_text())
        wf = config["workflow"]
        wf["statuses"].append("needs_human")
        wf["transitions"]["needs_human"] = ["in_planning", "in_progress", "cancelled"]
        wf["transitions"]["in_planning"].insert(0, "needs_human")
        wf["universal_targets"] = ["needs_human", "cancelled"]
        config_path.write_text(json.dumps(config, sort_keys=True, indent=2) + "\n")

    def test_status_transitions_still_work(self, invoke, initialized_root) -> None:
        """Transition into and out of the legacy status — no crash, sane snapshot."""
        self._add_legacy_status(initialized_root)
        task_id = _create(invoke)
        invoke("status", task_id, "in_planning", "--actor", _ACTOR)

        r = invoke("status", task_id, "needs_human", "--actor", _ACTOR, "--json")
        assert r.exit_code == 0, r.output
        assert json.loads(r.output)["data"]["status"] == "needs_human"

        r = invoke("status", task_id, "in_planning", "--actor", _ACTOR, "--json")
        assert r.exit_code == 0, r.output
        data = json.loads(r.output)["data"]
        assert data["status"] == "in_planning"
        # needs_human is unranked in the default order — backward detection
        # must degrade gracefully (no spurious reopen counting, no crash).
        assert isinstance(data.get("reopened_count", 0), int)
