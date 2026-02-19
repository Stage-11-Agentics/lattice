"""Tests for `lattice complete` compound operation."""

from __future__ import annotations

import json
from pathlib import Path

from lattice.storage.fs import LATTICE_DIR

from tests.conftest import _add_policies_to_config


_ACTOR = "human:test"


def _create_and_advance_to(invoke, fill_plan, target_status: str) -> str:
    """Create a task and advance it to the given status. Returns task_id."""
    r = invoke("create", "Test task", "--actor", _ACTOR, "--json")
    task_id = json.loads(r.output)["data"]["id"]

    if target_status == "backlog":
        return task_id

    invoke("status", task_id, "in_planning", "--actor", _ACTOR)
    if target_status == "in_planning":
        return task_id

    fill_plan(task_id, "Test task")
    invoke("status", task_id, "planned", "--actor", _ACTOR)
    if target_status == "planned":
        return task_id

    invoke("status", task_id, "in_progress", "--actor", _ACTOR)
    if target_status == "in_progress":
        return task_id

    invoke("status", task_id, "review", "--actor", _ACTOR)
    if target_status == "review":
        return task_id

    return task_id


class TestCompleteBasic:
    """Basic happy-path tests for lattice complete."""

    def test_complete_from_in_progress(self, invoke, initialized_root, fill_plan) -> None:
        task_id = _create_and_advance_to(invoke, fill_plan, "in_progress")

        r = invoke("complete", task_id, "--review", "LGTM. All tests pass.", "--actor", _ACTOR)
        assert r.exit_code == 0
        assert "Completed" in r.output
        assert "4 events" in r.output

        r = invoke("show", task_id, "--json")
        snapshot = json.loads(r.output)["data"]
        assert snapshot["status"] == "done"

    def test_complete_from_review(self, invoke, initialized_root, fill_plan) -> None:
        task_id = _create_and_advance_to(invoke, fill_plan, "review")

        r = invoke(
            "complete", task_id, "--review", "Reviewed cold, looks good.", "--actor", _ACTOR
        )
        assert r.exit_code == 0
        assert "3 events" in r.output

        r = invoke("show", task_id, "--json")
        snapshot = json.loads(r.output)["data"]
        assert snapshot["status"] == "done"

    def test_complete_json_output(self, invoke, initialized_root, fill_plan) -> None:
        task_id = _create_and_advance_to(invoke, fill_plan, "in_progress")

        r = invoke("complete", task_id, "--review", "LGTM", "--actor", _ACTOR, "--json")
        assert r.exit_code == 0
        parsed = json.loads(r.output)
        assert parsed["ok"] is True
        assert parsed["data"]["status"] == "done"

    def test_complete_quiet_output(self, invoke, initialized_root, fill_plan) -> None:
        task_id = _create_and_advance_to(invoke, fill_plan, "in_progress")

        r = invoke("complete", task_id, "--review", "LGTM", "--actor", _ACTOR, "--quiet")
        assert r.exit_code == 0
        assert r.output.strip() == "ok"


class TestCompleteEvents:
    """Verify the event stream produced by lattice complete."""

    def test_produces_four_events_from_in_progress(
        self,
        invoke,
        initialized_root,
        fill_plan,
    ) -> None:
        task_id = _create_and_advance_to(invoke, fill_plan, "in_progress")

        lattice_dir = initialized_root / LATTICE_DIR
        event_path = lattice_dir / "events" / f"{task_id}.jsonl"
        events_before = len(event_path.read_text().strip().splitlines())

        invoke("complete", task_id, "--review", "Review text here", "--actor", _ACTOR)

        all_lines = event_path.read_text().strip().splitlines()
        new_events = [json.loads(line) for line in all_lines[events_before:]]
        assert len(new_events) == 4

        assert new_events[0]["type"] == "comment_added"
        assert new_events[0]["data"]["role"] == "review"
        assert new_events[0]["data"]["body"] == "Review text here"

        assert new_events[1]["type"] == "status_changed"
        assert new_events[1]["data"]["to"] == "review"

        assert new_events[2]["type"] == "artifact_attached"
        assert new_events[2]["data"]["role"] == "review"

        assert new_events[3]["type"] == "status_changed"
        assert new_events[3]["data"]["to"] == "done"

    def test_produces_three_events_from_review(
        self,
        invoke,
        initialized_root,
        fill_plan,
    ) -> None:
        task_id = _create_and_advance_to(invoke, fill_plan, "review")

        lattice_dir = initialized_root / LATTICE_DIR
        event_path = lattice_dir / "events" / f"{task_id}.jsonl"
        events_before = len(event_path.read_text().strip().splitlines())

        invoke(
            "complete",
            task_id,
            "--review",
            "Already in review, finishing.",
            "--actor",
            _ACTOR,
        )

        all_lines = event_path.read_text().strip().splitlines()
        new_events = [json.loads(line) for line in all_lines[events_before:]]
        assert len(new_events) == 3

        assert new_events[0]["type"] == "comment_added"
        assert new_events[1]["type"] == "artifact_attached"
        assert new_events[2]["type"] == "status_changed"
        assert new_events[2]["data"]["to"] == "done"

    def test_artifact_payload_exists(self, invoke, initialized_root, fill_plan) -> None:
        task_id = _create_and_advance_to(invoke, fill_plan, "in_progress")

        r = invoke(
            "complete",
            task_id,
            "--review",
            "Detailed review findings",
            "--actor",
            _ACTOR,
            "--json",
        )
        snapshot = json.loads(r.output)["data"]

        evidence_refs = snapshot.get("evidence_refs", [])
        art_refs = [ref for ref in evidence_refs if ref.get("source_type") == "artifact"]
        assert len(art_refs) >= 1

        art_id = art_refs[0]["id"]
        lattice_dir = initialized_root / LATTICE_DIR

        meta_path = lattice_dir / "artifacts" / "meta" / f"{art_id}.json"
        assert meta_path.exists()
        meta = json.loads(meta_path.read_text())
        assert meta["type"] == "note"
        assert meta["title"] == "Review findings"

        payload_path = lattice_dir / "artifacts" / "payload" / f"{art_id}.md"
        assert payload_path.exists()
        assert payload_path.read_text() == "Detailed review findings"


class TestCompleteValidation:
    """Validation and error cases."""

    def test_fails_from_backlog(self, invoke, initialized_root, fill_plan) -> None:
        task_id = _create_and_advance_to(invoke, fill_plan, "backlog")

        r = invoke(
            "complete",
            task_id,
            "--review",
            "Review",
            "--actor",
            _ACTOR,
            "--json",
        )
        assert r.exit_code != 0
        parsed = json.loads(r.output)
        assert parsed["ok"] is False
        assert parsed["error"]["code"] == "INVALID_TRANSITION"

    def test_fails_from_in_planning(self, invoke, initialized_root, fill_plan) -> None:
        task_id = _create_and_advance_to(invoke, fill_plan, "in_planning")

        r = invoke(
            "complete",
            task_id,
            "--review",
            "Review",
            "--actor",
            _ACTOR,
            "--json",
        )
        assert r.exit_code != 0
        parsed = json.loads(r.output)
        assert parsed["ok"] is False
        assert parsed["error"]["code"] == "INVALID_TRANSITION"

    def test_fails_without_review_flag(self, invoke, initialized_root, fill_plan) -> None:
        task_id = _create_and_advance_to(invoke, fill_plan, "in_progress")

        r = invoke("complete", task_id, "--actor", _ACTOR)
        assert r.exit_code != 0
        assert "Missing option '--review'" in r.output

    def test_fails_on_epic(self, invoke, initialized_root) -> None:
        r = invoke(
            "create",
            "Test epic",
            "--type",
            "epic",
            "--actor",
            _ACTOR,
            "--json",
        )
        task_id = json.loads(r.output)["data"]["id"]

        r = invoke(
            "complete",
            task_id,
            "--review",
            "Review",
            "--actor",
            _ACTOR,
            "--json",
        )
        assert r.exit_code != 0
        parsed = json.loads(r.output)
        assert parsed["error"]["code"] == "EPIC_STATUS_REJECTED"

    def test_empty_review_text_fails(self, invoke, initialized_root, fill_plan) -> None:
        task_id = _create_and_advance_to(invoke, fill_plan, "in_progress")

        r = invoke(
            "complete",
            task_id,
            "--review",
            "",
            "--actor",
            _ACTOR,
            "--json",
        )
        assert r.exit_code != 0

    def test_short_id_accepted(self, invoke, initialized_root, fill_plan) -> None:
        lattice_dir = initialized_root / LATTICE_DIR
        config_path = lattice_dir / "config.json"
        config = json.loads(config_path.read_text())
        config["project_code"] = "TST"
        config_path.write_text(json.dumps(config, sort_keys=True, indent=2) + "\n")

        from lattice.storage.short_ids import _default_index, save_id_index

        save_id_index(lattice_dir, _default_index())

        r = invoke("create", "Short ID test", "--actor", _ACTOR, "--json")
        snapshot = json.loads(r.output)["data"]
        task_id = snapshot["id"]
        short_id = snapshot["short_id"]
        assert short_id is not None

        invoke("status", task_id, "in_planning", "--actor", _ACTOR)
        fill_plan(task_id, "Short ID test")
        invoke("status", task_id, "planned", "--actor", _ACTOR)
        invoke("status", task_id, "in_progress", "--actor", _ACTOR)

        r = invoke(
            "complete",
            short_id,
            "--review",
            "LGTM via short ID",
            "--actor",
            _ACTOR,
        )
        assert r.exit_code == 0
        assert "Completed" in r.output


class TestCompleteCompletionPolicy:
    """Verify completion policies are properly enforced/satisfied."""

    def test_satisfies_review_role_policy(
        self,
        invoke_with_policies,
        initialized_root_with_policies,
        fill_plan_with_policies,
    ) -> None:
        task_id = _create_and_advance_to(
            invoke_with_policies,
            fill_plan_with_policies,
            "in_progress",
        )

        r = invoke_with_policies(
            "complete",
            task_id,
            "--review",
            "Review findings",
            "--actor",
            _ACTOR,
        )
        assert r.exit_code == 0
        assert "Completed" in r.output

    def test_fails_unmet_non_review_policy(
        self,
        invoke,
        initialized_root,
        fill_plan,
    ) -> None:
        _add_policies_to_config(
            initialized_root,
            {"done": {"require_roles": ["review", "security"]}},
        )

        lattice_dir = initialized_root / LATTICE_DIR
        config_path = lattice_dir / "config.json"
        config = json.loads(config_path.read_text())
        config["workflow"]["roles"] = ["review", "security"]
        config_path.write_text(json.dumps(config, sort_keys=True, indent=2) + "\n")

        task_id = _create_and_advance_to(invoke, fill_plan, "in_progress")

        r = invoke(
            "complete",
            task_id,
            "--review",
            "Review findings",
            "--actor",
            _ACTOR,
            "--json",
        )
        assert r.exit_code != 0
        parsed = json.loads(r.output)
        assert parsed["error"]["code"] == "COMPLETION_BLOCKED"
        assert "security" in parsed["error"]["message"]
