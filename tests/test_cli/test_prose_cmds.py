"""``lattice plan`` (group with the legacy read), ``plan write``, ``notes write`` (SPEC §3.9)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from lattice.storage.fs import LATTICE_DIR

A = ("--actor", "agent:t")
PLAN = "# Plan\n\nDo it well.\n"


def _events(root: Path, task_id: str, base: str = "") -> list[dict]:
    log = root / LATTICE_DIR / base / "events" / f"{task_id}.jsonl"
    return [json.loads(line) for line in log.read_text().splitlines()]


@pytest.fixture()
def task(create_task) -> dict:  # noqa: ANN001
    return create_task("Planned")


class TestPlanWrite:
    def test_file_write_event_and_read_back(
        self, invoke, initialized_root: Path, task: dict, tmp_path: Path
    ) -> None:  # noqa: ANN001
        src = tmp_path / "plan.md"
        src.write_bytes(PLAN.encode())
        result = invoke("plan", "write", task["id"], "--file", str(src), *A)
        assert result.exit_code == 0, result.output
        assert f"plans/{task['id']}.md ({len(PLAN)} bytes)" in result.output
        plan_path = initialized_root / LATTICE_DIR / "plans" / f"{task['id']}.md"
        assert plan_path.read_text() == PLAN
        event = _events(initialized_root, task["id"])[-1]
        assert event["type"] == "plan_written"
        assert event["data"] == {
            "sha256": hashlib.sha256(PLAN.encode()).hexdigest(),
            "bytes": len(PLAN),
        }
        assert event["actor"] == "agent:t"
        assert event["origin"]["op"] == "task.plan_write"

        read = invoke("plan", task["id"], "--json")
        assert json.loads(read.output)["data"]["content"] == PLAN
        snapshot = json.loads(invoke("show", task["id"], "--json").output)["data"]
        assert snapshot["last_event_id"] == event["id"]

    def test_stdin_json_and_idempotent_rewrite(
        self, invoke, initialized_root: Path, task: dict
    ) -> None:  # noqa: ANN001
        result = invoke("plan", "write", task["id"], "--stdin", "--json", *A, input=PLAN)
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        assert data == {
            "task_id": task["id"],
            "path": f"plans/{task['id']}.md",
            "sha256": hashlib.sha256(PLAN.encode()).hexdigest(),
            "bytes": len(PLAN),
        }
        count = len(_events(initialized_root, task["id"]))
        again = invoke("plan", "write", task["id"], "--stdin", *A, input=PLAN)
        assert "Plan unchanged" in again.output
        assert len(_events(initialized_root, task["id"])) == count

    def test_expect_sha256(self, invoke, initialized_root: Path, task: dict) -> None:  # noqa: ANN001
        plan_path = initialized_root / LATTICE_DIR / "plans" / f"{task['id']}.md"
        current = hashlib.sha256(plan_path.read_bytes()).hexdigest()
        stale = hashlib.sha256(b"something else").hexdigest()
        refused = invoke(
            "plan", "write", task["id"], "--stdin", "--expect-sha256", stale, "--json", *A,
            input=PLAN,
        )  # fmt: skip
        assert refused.exit_code == 1
        error = json.loads(refused.output)["error"]
        assert error["code"] == "CONFLICT" and current in error["message"]
        ok = invoke(
            "plan", "write", task["id"], "--stdin", "--expect-sha256", current, *A, input=PLAN
        )
        assert ok.exit_code == 0, ok.output
        assert plan_path.read_text() == PLAN

    def test_archived_task_writes_at_its_placement(
        self, invoke, initialized_root: Path, task: dict
    ) -> None:  # noqa: ANN001
        assert invoke("archive", task["id"], *A).exit_code == 0
        result = invoke("plan", "write", task["id"], "--stdin", *A, input=PLAN)
        assert result.exit_code == 0, result.output
        archived = initialized_root / LATTICE_DIR / "archive" / "plans" / f"{task['id']}.md"
        assert archived.read_text() == PLAN
        assert not (initialized_root / LATTICE_DIR / "plans" / f"{task['id']}.md").exists()
        assert _events(initialized_root, task["id"], "archive")[-1]["type"] == "plan_written"

    def test_plan_gate_accepts_a_written_plan(self, invoke, task: dict) -> None:  # noqa: ANN001
        assert invoke("status", task["id"], "in_planning", *A).exit_code == 0
        assert invoke("status", task["id"], "planned", "--no-auto-review", *A).exit_code == 0
        blocked = invoke("status", task["id"], "in_progress", *A)
        assert blocked.exit_code == 1
        assert invoke("plan", "write", task["id"], "--stdin", *A, input=PLAN).exit_code == 0
        assert invoke("status", task["id"], "in_progress", *A).exit_code == 0

    @pytest.mark.parametrize("as_json", [False, True])
    def test_rejections(self, invoke, task: dict, tmp_path: Path, as_json: bool) -> None:  # noqa: ANN001
        src = tmp_path / "p.md"
        src.write_text(PLAN)
        flag = ["--json"] if as_json else []
        cases = [
            (["--file", str(tmp_path)], "VALIDATION_ERROR", "Is a directory"),
            (["--file", str(src), "--stdin"], "VALIDATION_ERROR", "not both"),
            ([], "VALIDATION_ERROR", "Provide the plan as --file PATH or --stdin, for example"),
            (["--file", str(src), "--expect-sha256", "nope"], "VALIDATION_ERROR", "64 hex"),
        ]
        for args, code, needle in cases:
            result = invoke("plan", "write", task["id"], *args, *flag, *A)
            assert result.exit_code == 1, args
            assert needle in result.output
            if as_json:
                assert json.loads(result.output)["error"]["code"] == code
        for raw, code in (("NOPE-9", "NOT_FOUND"), ("bad!", "INVALID_ID")):
            result = invoke("plan", "write", raw, "--file", str(src), *flag, *A)
            assert result.exit_code == 1
            if as_json:
                assert json.loads(result.output)["error"]["code"] == code
        missing = "task_01AAAAAAAAAAAAAAAAAAAAAAAA"
        result = invoke("plan", "write", missing, "--file", str(src), "--json", *A)
        assert json.loads(result.output)["error"] == {
            "code": "NOT_FOUND",
            "message": f"Task {missing} not found.",
        }
        no_actor = invoke("plan", "write", task["id"], "--file", str(src), "--json")
        assert json.loads(no_actor.output)["error"]["code"] == "MISSING_ACTOR"


def test_notes_write(invoke, initialized_root: Path, task: dict) -> None:  # noqa: ANN001
    result = invoke("notes", "write", task["id"], "--stdin", *A, input="scratch\n")
    assert result.exit_code == 0, result.output
    assert result.output.strip().startswith("Notes written for")
    notes = initialized_root / LATTICE_DIR / "notes" / f"{task['id']}.md"
    assert notes.read_text() == "scratch\n"
    event = _events(initialized_root, task["id"])[-1]
    assert event["type"] == "notes_written" and event["data"]["bytes"] == 8


class TestPlanGroupDispatch:
    def test_help_shows_the_group(self, invoke) -> None:  # noqa: ANN001
        result = invoke("plan", "--help")
        assert result.exit_code == 0
        assert "write" in result.output and "TASK_ID" in result.output

    def test_legacy_read_keeps_its_usage_errors(self, invoke) -> None:  # noqa: ANN001
        result = invoke("plan")
        assert result.exit_code == 2
        assert "Missing argument 'TASK_ID'." in result.output

    def test_option_before_task(self, invoke, task: dict) -> None:  # noqa: ANN001
        result = invoke("plan", "--json", task["id"])
        assert json.loads(result.output)["data"]["task_id"] == task["id"]
