"""Tests for `lattice migrate needs-human` (LAT-232)."""

from __future__ import annotations

import json

_ACTOR = "agent:test"


def _make_legacy_instance(invoke, initialized_root, *, with_comment: bool = True) -> str:
    """Reshape the initialized root into a pre-LAT-232 instance.

    Restores the needs_human status to the config and parks one task in it
    (via in_planning, so the migration has a return status to find).
    Returns the task ID.
    """
    config_path = initialized_root / ".lattice" / "config.json"
    config = json.loads(config_path.read_text())
    wf = config["workflow"]
    wf["statuses"].insert(wf["statuses"].index("cancelled"), "needs_human")
    wf["transitions"]["needs_human"] = ["in_planning", "in_progress", "cancelled"]
    wf["transitions"]["in_planning"].insert(0, "needs_human")
    wf["universal_targets"] = ["needs_human", "cancelled"]
    wf["descriptions"]["needs_human"] = "A human decision is required."
    config_path.write_text(json.dumps(config, sort_keys=True, indent=2) + "\n")

    r = invoke("create", "Legacy stuck task", "--actor", _ACTOR, "--json")
    task_id = json.loads(r.output)["data"]["id"]
    invoke("status", task_id, "in_planning", "--actor", _ACTOR)
    if with_comment:
        invoke("comment", task_id, "Need: pick an auth provider", "--actor", _ACTOR)
    r = invoke("status", task_id, "needs_human", "--actor", _ACTOR, "--json")
    assert r.exit_code == 0, r.output
    return task_id


def _read_snap(initialized_root, task_id: str) -> dict:
    return json.loads((initialized_root / ".lattice" / "tasks" / f"{task_id}.json").read_text())


def _read_config(initialized_root) -> dict:
    return json.loads((initialized_root / ".lattice" / "config.json").read_text())


class TestMigrateNeedsHuman:
    def test_migrates_task_and_config(self, invoke, initialized_root) -> None:
        task_id = _make_legacy_instance(invoke, initialized_root)

        r = invoke("migrate", "needs-human", "--json")
        assert r.exit_code == 0, r.output
        data = json.loads(r.output)["data"]
        assert len(data["tasks_migrated"]) == 1
        assert data["tasks_migrated"][0]["return_status"] == "in_planning"

        snap = _read_snap(initialized_root, task_id)
        assert snap["status"] == "in_planning"
        assert snap["needs_human"]["reason"] == "Need: pick an auth provider"

        wf = _read_config(initialized_root)["workflow"]
        assert "needs_human" not in wf["statuses"]
        assert "needs_human" not in wf["transitions"]
        assert all("needs_human" not in targets for targets in wf["transitions"].values())
        assert wf["universal_targets"] == ["cancelled"]
        assert "needs_human" not in wf.get("descriptions", {})

    def test_fallback_reason_without_comment(self, invoke, initialized_root) -> None:
        task_id = _make_legacy_instance(invoke, initialized_root, with_comment=False)
        r = invoke("migrate", "needs-human")
        assert r.exit_code == 0, r.output
        snap = _read_snap(initialized_root, task_id)
        assert snap["needs_human"]["reason"] == "Migrated from needs_human status"

    def test_idempotent(self, invoke, initialized_root) -> None:
        task_id = _make_legacy_instance(invoke, initialized_root)
        invoke("migrate", "needs-human")
        snap_after_first = _read_snap(initialized_root, task_id)
        config_after_first = _read_config(initialized_root)

        r = invoke("migrate", "needs-human", "--json")
        assert r.exit_code == 0, r.output
        data = json.loads(r.output)["data"]
        assert data["tasks_migrated"] == []
        assert data["config_changes"] == []
        assert _read_snap(initialized_root, task_id) == snap_after_first
        assert _read_config(initialized_root) == config_after_first

    def test_dry_run_writes_nothing(self, invoke, initialized_root) -> None:
        task_id = _make_legacy_instance(invoke, initialized_root)
        snap_before = _read_snap(initialized_root, task_id)
        config_before = _read_config(initialized_root)

        r = invoke("migrate", "needs-human", "--dry-run", "--json")
        assert r.exit_code == 0, r.output
        data = json.loads(r.output)["data"]
        assert data["dry_run"] is True
        assert len(data["tasks_migrated"]) == 1
        assert data["config_changes"]

        assert _read_snap(initialized_root, task_id) == snap_before
        assert _read_config(initialized_root) == config_before

    def test_migration_events_carry_provenance(self, invoke, initialized_root) -> None:
        task_id = _make_legacy_instance(invoke, initialized_root)
        invoke("migrate", "needs-human")
        events_path = initialized_root / ".lattice" / "events" / f"{task_id}.jsonl"
        events = [json.loads(line) for line in events_path.read_text().splitlines() if line]
        flag_event = next(e for e in events if e["type"] == "needs_human_flagged")
        assert "LAT-232 migration" in flag_event["provenance"]["reason"]
        route_event = events[-1]
        assert route_event["type"] == "status_changed"
        assert route_event["data"] == {"from": "needs_human", "to": "in_planning"}
        assert "LAT-232 migration" in route_event["provenance"]["reason"]

    def test_rebuild_reproduces_migrated_snapshot(self, invoke, initialized_root) -> None:
        """AC #10: the migrated snapshot is exactly what event replay produces."""
        task_id = _make_legacy_instance(invoke, initialized_root)
        invoke("migrate", "needs-human")
        snap_path = initialized_root / ".lattice" / "tasks" / f"{task_id}.json"
        before = snap_path.read_text()
        r = invoke("rebuild", "--all")
        assert r.exit_code == 0, r.output
        assert snap_path.read_text() == before

    def test_nothing_to_migrate(self, invoke, initialized_root) -> None:
        r = invoke("migrate", "needs-human")
        assert r.exit_code == 0
        assert "Nothing to migrate" in r.output
