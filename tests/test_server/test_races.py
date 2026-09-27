"""AC-1 (task.status part): eight concurrent HTTP writers, one outcome."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from click.testing import CliRunner

from lattice.cli.main import cli
from lattice.server.testing import ServerHandle
from tests.test_server.conftest import create_task, mint

WRITERS = 8


def _in_progress_task(server: ServerHandle, token: str, root: Path) -> dict:
    task = create_task(server, token)
    plan = root / "projects" / "alpha" / ".lattice" / "plans" / f"{task['id']}.md"
    plan.write_text("# Plan\n\nDo the thing.\n")
    for status in ("in_planning", "planned", "in_progress"):
        code, _, body = server.op(
            "alpha", "task.status", {"task": task["id"], "new_status": status}, token=token
        )
        assert code == 200, body
    return body["data"]["result"]["task"]


def _race(server: ServerHandle, token: str, envelope: dict, task_id: str) -> list:
    barrier = threading.Barrier(WRITERS)

    def write(_i: int):
        barrier.wait()
        return server.op(
            "alpha",
            "task.status",
            {"task": task_id, "new_status": "review"},
            token=token,
            **envelope,
        )

    with ThreadPoolExecutor(WRITERS) as pool:
        return list(pool.map(write, range(WRITERS)))


def _status_changes_to_review(root: Path, task_id: str) -> int:
    log = root / "projects" / "alpha" / ".lattice" / "events" / f"{task_id}.jsonl"
    events = [json.loads(line) for line in log.read_text().splitlines()]
    return sum(1 for e in events if e["type"] == "status_changed" and e["data"]["to"] == "review")


def _doctor_clean(root: Path) -> None:
    result = CliRunner().invoke(
        cli, ["doctor", "--json"], env={"LATTICE_ROOT": str(root / "projects" / "alpha")}
    )
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert not [f for f in payload["data"]["findings"] if f.get("level") == "error"]


def test_status_race_with_expectations(server: ServerHandle, root: Path) -> None:
    token = mint(root)
    task = _in_progress_task(server, token, root)
    results = _race(
        server, token, {"expect": {"last_event_id": task["last_event_id"]}}, task["id"]
    )
    statuses = sorted(r[0] for r in results)
    assert statuses == [200] + [409] * (WRITERS - 1)
    for status, _, body in results:
        if status == 409:
            assert body["error"]["code"] == "CONFLICT"
            snapshot = body["error"]["details"]["snapshot"]
            assert snapshot["status"] == "review" and snapshot["id"] == task["id"]
    assert _status_changes_to_review(root, task["id"]) == 1
    _doctor_clean(root)


def test_status_race_without_expectations(server: ServerHandle, root: Path) -> None:
    token = mint(root)
    task = _in_progress_task(server, token, root)
    results = _race(server, token, {}, task["id"])
    assert [r[0] for r in results] == [200] * WRITERS
    fresh = [r for r in results if not r[2]["data"]["result"]["idempotent"]]
    assert len(fresh) == 1
    assert _status_changes_to_review(root, task["id"]) == 1
    journal = root / "projects" / "alpha" / ".lattice" / "hosted" / "journal.jsonl"
    lines = [json.loads(line) for line in journal.read_text().splitlines()]
    assert len([x for x in lines if x["op"] == "task.status"]) == 3 + WRITERS
    _doctor_clean(root)
