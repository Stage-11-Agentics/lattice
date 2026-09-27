"""SPEC §8.2 control requests and §8.7 load-time rules that H-9 owns: project config
through a running server (AC-49), maintenance rotation, external edits, unknown types."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from lattice.cli.main import cli
from lattice.core.errors import OpError
from lattice.server import admin, control
from lattice.server.journal import Journal
from lattice.server.testing import running_server, wait_for
from tests.test_server.conftest import create_task, mint


def _journal(root: Path, slug: str = "alpha") -> list[dict]:
    path = root / "projects" / slug / ".lattice" / "hosted" / "journal.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_project_config_through_a_running_server(root: Path) -> None:
    with running_server(root) as server:
        assert control.server_running(root)
        result = CliRunner().invoke(
            cli,
            [
                "server",
                "project",
                "config",
                "alpha",
                "--set",
                "review_mode=triple",
                "--set",
                "auto_code_review_on_transition=false",
                "--root",
                str(root),
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        assert data["via"] == "server" and data["paths"] == ["config.json"]
        line = _journal(root)[-1]
        assert line["op"] == "server.set_config" and line["token_id"] is None
        assert line["paths"] == ["config.json"] and line["seq"] == data["seq"]
        assert line["op_id"].startswith("op_") and line["lengths"] == {}
        config = json.loads((root / "projects" / "alpha" / ".lattice" / "config.json").read_text())
        assert config["review_mode"] == "triple"
        assert config["auto_code_review_on_transition"] is False
        assert not (
            root / "projects" / "alpha" / ".lattice" / "hosted" / "maintenance.json"
        ).exists()
        assert not list(
            (root / "projects" / "alpha" / ".lattice" / "hosted" / "control").iterdir()
        )
        assert any(x["event"] == "control_request" and x["ok"] for x in server.log_lines)
    assert not control.server_running(root)


def test_control_requests_run_at_admission(root: Path) -> None:
    token = mint(root)
    with running_server(root) as server:
        board = root / "projects" / "alpha" / ".lattice"
        request_path = board / "hosted" / "control" / "01J9Z0000000000000000000AA.json"
        request_path.write_text(
            json.dumps({"action": "set-config", "set": {"plan_approval": "human"}})
        )
        create_task(server, token)  # the admission check runs it before the write
        done = request_path.with_suffix(".done")
        assert json.loads(done.read_text())["ok"] is True
        ops = [line["op"] for line in _journal(root)]
        assert ops == ["server.set_config", "task.create"]


def test_bad_control_requests_are_answered_not_run(root: Path) -> None:
    with running_server(root):
        board = root / "projects" / "alpha" / ".lattice"
        for action, payload in (("set-config", {"set": {"hooks": "x"}}), ("no-such-action", {})):
            answer = control.send_request(board, action, payload, wait_seconds=5)
            assert answer["ok"] is False and answer["error"]["code"] == "VALIDATION_ERROR"
        assert _journal(root) == []


def test_admin_wait_times_out_cleanly(root: Path) -> None:
    board = root / "projects" / "alpha" / ".lattice"
    started = time.monotonic()
    with pytest.raises(OpError) as exc:
        control.send_request(
            board, "set-config", {"set": {"review_mode": "single"}}, wait_seconds=0.2
        )
    assert exc.value.code == "BOARD_BUSY" and time.monotonic() - started < 2
    # the request stays queued and the next server runs it
    with running_server(root):
        assert wait_for(lambda: len(_journal(root)) == 1, timeout=5)
        assert _journal(root)[0]["op"] == "server.set_config"


def test_offline_config_then_load_rotates_the_epoch(root: Path) -> None:
    board = root / "projects" / "alpha" / ".lattice"
    first_epoch = Journal.load(board).epoch
    admin.set_project_config(root, "alpha", {"review_mode": "triple"})
    assert (board / "hosted" / "maintenance.json").exists()
    with running_server(root) as server:
        epoch = server.project("alpha").journal.epoch
        assert epoch != first_epoch
        assert not (board / "hosted" / "maintenance.json").exists()
        assert (board / "hosted" / f"journal.{first_epoch}.jsonl").exists()
        assert any(x["event"] == "maintenance_rotation" for x in server.log_lines)


def test_hand_edits_of_config_and_context_are_journaled(root: Path) -> None:
    token = mint(root)
    with running_server(root) as server:
        create_task(server, token)
        board = root / "projects" / "alpha" / ".lattice"
        (board / "context.md").write_text("# Edited by hand\n")
        create_task(server, token)
        ops = [(line["op"], line["paths"]) for line in _journal(root)]
        assert ops[1] == ("external", ["context.md"])
        assert ops[2][0] == "task.create"
        assert any(x["event"] == "external_change" for x in server.log_lines)
        create_task(server, token)
        assert [line["op"] for line in _journal(root)].count("external") == 1


def test_missing_journal_starts_a_new_epoch(root: Path) -> None:
    board = root / "projects" / "alpha" / ".lattice"
    (board / "hosted" / "journal_meta.json").unlink()
    with running_server(root) as server:
        assert server.project("alpha").state == "loaded"
        assert Journal.load(board).head_seq == 0
        assert any(x["event"] == "journal_missing" for x in server.log_lines)


def test_a_corrupt_log_makes_only_that_project_unavailable(root: Path) -> None:
    token = mint(root)
    with running_server(root) as server:
        create_task(server, token, "alpha")
    log = next((root / "projects" / "alpha" / ".lattice" / "events").glob("task_*.jsonl"))
    log.write_bytes(log.read_bytes() + b'{"truncated": ')
    with running_server(root) as server:
        assert server.project("alpha").state == "unavailable"
        status, _, body = server.op("alpha", "task.create", {"title": "x"}, token=token)
        assert status == 503 and body["error"]["code"] == "BOARD_UNAVAILABLE"
        create_task(server, token, "beta")
        _, _, health = server.request("GET", "/healthz")
        assert health["projects"]["unavailable"] == 1 and health["projects"]["loaded"] == 1
        listed = CliRunner().invoke(
            cli, ["server", "project", "list", "--root", str(root), "--json"]
        )
        states = {r["slug"]: r["state"] for r in json.loads(listed.output)["data"]}
        assert states == {"alpha": "unavailable", "beta": "loaded"}


def test_unknown_event_types_are_logged_once(root: Path) -> None:
    token = mint(root)
    with running_server(root) as server:
        task = create_task(server, token)
    log = root / "projects" / "alpha" / ".lattice" / "events" / f"{task['id']}.jsonl"
    event = json.loads(log.read_text().splitlines()[0])
    event.update(id="ev_01J9Z0000000000000000000ZZ", type="future_type", data={})
    event.pop("origin", None)
    with open(log, "a") as fh:
        fh.write(json.dumps(event, sort_keys=True) + "\n")
    with running_server(root) as server:
        for _ in range(3):
            server.request("GET", f"/v1/projects/alpha/tasks/{task['id']}", token=token)
        warnings = [x for x in server.log_lines if x["event"] == "unknown_event_type"]
        assert len(warnings) == 1 and warnings[0]["type"] == "future_type"
        assert warnings[0]["project"] == "alpha"


def test_a_hand_edit_before_a_queued_control_request_is_journaled(root: Path) -> None:
    """B1: the edit gets its own ``external`` entry before set-config rewrites the file."""
    with running_server(root) as server:
        board = root / "projects" / "alpha" / ".lattice"
        config = json.loads((board / "config.json").read_text())
        config["project_name"] = "Edited by hand"
        (board / "config.json").write_text(json.dumps(config, sort_keys=True, indent=2) + "\n")
        (board / "context.md").write_text("# Also edited\n")
        answer = control.send_request(
            board, "set-config", {"set": {"review_mode": "triple"}}, wait_seconds=5
        )
        assert answer["ok"] is True
        entries = [(x["op"], x["paths"]) for x in _journal(root)]
        assert entries == [
            ("external", ["config.json", "context.md"]),
            ("server.set_config", ["config.json"]),
        ]
        final = json.loads((board / "config.json").read_text())
        assert final["project_name"] == "Edited by hand" and final["review_mode"] == "triple"
        assert server.project("alpha").state == "loaded"


def test_a_hand_edit_during_an_operation_is_not_adopted(root: Path) -> None:
    """B1: a commit re-baselines only the watched files it journaled."""
    import threading

    token = mint(root)
    with running_server(root) as server:
        board = root / "projects" / "alpha" / ".lattice"
        slow = threading.Thread(
            target=server.op, args=("alpha", "xtest.sleep", {"ms": 400}), kwargs={"token": token}
        )
        slow.start()
        assert wait_for(lambda: server.project("alpha").work.locked())
        (board / "context.md").write_text("# Edited while the op ran\n")
        slow.join()
        create_task(server, token)
        entries = [(x["op"], x["paths"]) for x in _journal(root)]
        assert entries[0][0] == "xtest.sleep"
        assert ("external", ["context.md"]) in entries
        assert entries.index(("external", ["context.md"])) == 1
