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
                "--set",
                'task_types=["task","bug","chore","research"]',
                "review_integration_branches=v2,release/next",
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
        assert config["task_types"] == ["task", "bug", "chore", "research"]
        assert config["review_integration_branches"] == ["v2", "release/next"]
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
        # Written atomically, as the admin writes every request (temp file, then
        # rename): a plain write_text could be scanned half-written (CI flake).
        control._write_private(
            request_path,
            json.dumps({"action": "set-config", "set": {"plan_approval": "human"}}).encode(),
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

    from tests.test_server.server_ops import Gate

    token = mint(root)
    Gate.reset()
    with running_server(root) as server:
        board = root / "projects" / "alpha" / ".lattice"
        outcome: list[object] = []

        def gated() -> None:
            try:
                outcome.append(server.op("alpha", "xtest.gate", {}, token=token)[0])
            except BaseException as exc:  # noqa: BLE001 - surfaced by the assert below
                outcome.append(repr(exc))

        slow = threading.Thread(target=gated)
        slow.start()
        try:
            # Inside the operation, past its admission check: the edit lands during it.
            assert Gate.entered.wait(10)
            (board / "context.md").write_text("# Edited while the op ran\n")
        finally:
            Gate.released.set()
            slow.join(timeout=30)  # a safety bound only
        assert not slow.is_alive(), "the gated operation never finished"
        assert outcome == [200]
        create_task(server, token)
        entries = [(x["op"], x["paths"]) for x in _journal(root)]
        assert entries[0][0] == "xtest.gate"
        assert ("external", ["context.md"]) in entries
        assert entries.index(("external", ["context.md"])) == 1


# ---------------------------------------------------------------------------
# H-22: unload, load, reload, doctor (SPEC §8.2), at the registry layer
# ---------------------------------------------------------------------------


def _admin(root: Path, *args: str, env: dict | None = None) -> tuple[int, dict]:
    result = CliRunner().invoke(
        cli, ["server", "project", *args, "--root", str(root), "--json"], env=env
    )
    return result.exit_code, json.loads(result.output)


def _lease_free(root: Path, slug: str) -> bool:
    return admin.try_owner_flock_free(root / "projects" / slug / ".lattice")


def _steady_writer(server, token: str, slug: str, stop):  # noqa: ANN001, ANN202
    """Write to *slug* until *stop* is set; returns (statuses, slowest seconds). An
    exception in the writer lands in *statuses* as its ``repr``."""
    statuses: list[int | str] = []
    slowest = [0.0]

    def loop() -> None:
        try:
            while not stop.is_set():
                started = time.monotonic()
                status, _, _ = server.op(slug, "task.create", {"title": "steady"}, token=token)
                slowest[0] = max(slowest[0], time.monotonic() - started)
                statuses.append(status)
        except BaseException as exc:  # noqa: BLE001 - surfaced by the caller's assert
            statuses.append(repr(exc))

    return loop, statuses, slowest


def test_unload_offline_maintenance_then_load_while_others_serve(root: Path) -> None:
    """AC-15: 'project unload', offline maintenance on that project, then 'project
    load', all while the server keeps serving the others; 'load' reaches the
    running server although the project's own flock is free."""
    import threading

    token = mint(root)
    with running_server(root) as server:
        create_task(server, token, "alpha")
        stop = threading.Event()
        loop, statuses, slowest = _steady_writer(server, token, "beta", stop)
        writer = threading.Thread(target=loop)
        writer.start()
        try:
            code, out = _admin(root, "unload", "alpha")
            assert code == 0 and out["data"]["lease"] == "released", out
            assert _lease_free(root, "alpha")  # free the moment unload returns
            status, _, body = server.op("alpha", "task.create", {"title": "x"}, token=token)
            assert status == 503 and body["error"]["code"] == "BOARD_UNAVAILABLE"
            assert "unloaded" in body["error"]["message"]
            _, _, health = server.request("GET", "/healthz")
            assert health["projects"]["unloaded"] == 1
            listed = CliRunner().invoke(
                cli, ["server", "project", "list", "--root", str(root), "--json"]
            )
            states = {r["slug"]: r["state"] for r in json.loads(listed.output)["data"]}
            assert states["alpha"] == "unloaded" and states["beta"] == "loaded"

            fixed = CliRunner().invoke(
                cli,
                ["doctor", "--fix", "--offline-maintenance", "--json"],
                env={"LATTICE_ROOT": str(root / "projects" / "alpha")},
            )
            assert fixed.exit_code == 0, fixed.output
            epoch = _meta(root)["epoch"]

            code, out = _admin(root, "load", "alpha")
            assert code == 0 and out["data"]["state"] == "loaded", out
            assert out["data"]["epoch"] != epoch  # the maintenance record rotated it
            assert not _lease_free(root, "alpha")
            create_task(server, token, "alpha")
        finally:
            stop.set()
            writer.join(timeout=30)  # a safety bound only
    assert not writer.is_alive(), "the background writer never stopped"
    assert statuses and set(statuses) == {200}
    assert slowest[0] < 1.0


def test_an_unloaded_project_stays_unloaded_until_load(root: Path) -> None:
    token = mint(root)
    with running_server(root) as server:
        assert _admin(root, "unload", "alpha")[0] == 0
        for _ in range(3):  # neither requests nor the poller load it again
            status, _, _ = server.op("alpha", "task.create", {"title": "x"}, token=token)
            assert status == 503
            time.sleep(0.06)
        assert server.project("alpha").state == "unloaded"
        assert _lease_free(root, "alpha")
        code, out = _admin(root, "reload", "alpha")
        assert code == 0 and out["data"]["state"] == "loaded"
        create_task(server, token, "alpha")


def test_reload_releases_and_retakes_the_lease(root: Path) -> None:
    token = mint(root)
    with running_server(root) as server:
        create_task(server, token)
        head = server.project("alpha").journal.head_seq
        code, out = _admin(root, "reload", "alpha")
        assert code == 0 and out["data"]["head_seq"] == head
        assert server.project("alpha").state == "loaded"
        events = [x["event"] for x in server.log_lines]
        assert "project_unload" in events and events.count("project_load") >= 3


def test_lifecycle_commands_need_a_running_server(root: Path) -> None:
    for action in ("unload", "load", "reload"):
        code, out = _admin(root, action, "alpha")
        assert code == 1 and out["error"]["code"] == "CONFLICT"
        assert "no Lattice server is running" in out["error"]["message"]
    plain = CliRunner().invoke(cli, ["server", "project", "load", "alpha", "--root", str(root)])
    assert plain.exit_code == 1 and "no Lattice server is running" in plain.output


def test_project_doctor_through_the_server_and_directly(root: Path) -> None:
    token = mint(root)
    with running_server(root) as server:
        create_task(server, token)
        code, out = _admin(root, "doctor", "alpha")
        assert code == 0, out
        data = out["data"]
        assert data["via"] == "server" and data["summary"]["errors"] == 0
        assert data["summary"]["tasks"] == 1
        # An unloaded project: the server takes its owner flock for the check.
        assert _admin(root, "unload", "alpha")[0] == 0
        code, out = _admin(root, "doctor", "alpha")
        assert code == 0 and out["data"]["summary"]["errors"] == 0
        assert _lease_free(root, "alpha")
        plain = CliRunner().invoke(
            cli, ["server", "project", "doctor", "alpha", "--root", str(root)]
        )
        assert plain.exit_code == 0 and "no issues found" in plain.output
    code, out = _admin(root, "doctor", "alpha")  # no server: direct, under the flock
    assert code == 0 and out["data"]["via"] == "offline"


def test_project_doctor_reports_errors_and_exits_1(root: Path) -> None:
    token = mint(root)
    with running_server(root) as server:
        task = create_task(server, token)
    log = root / "projects" / "alpha" / ".lattice" / "events" / f"{task['id']}.jsonl"
    log.write_bytes(b"{not json}\n" + log.read_bytes())  # corrupt mid-log, not a torn tail
    code, out = _admin(root, "doctor", "alpha")
    assert code == 1 and out["data"]["summary"]["errors"] >= 1, out


def test_direct_doctor_refuses_while_another_process_holds_the_project(root: Path) -> None:
    from lattice.storage.ownership import release_owner_flock, try_owner_flock

    fd = try_owner_flock(root / "projects" / "alpha" / ".lattice")
    assert fd is not None
    try:
        code, out = _admin(root, "doctor", "alpha")
        assert code == 1 and out["error"]["code"] == "BOARD_BUSY"
    finally:
        release_owner_flock(fd)


def test_recover_refuses_while_the_server_holds_the_project(root: Path) -> None:
    with running_server(root):
        code, out = _admin(root, "recover", "alpha", "--rollback")
        assert code == 1 and out["error"]["code"] == "BOARD_BUSY"
    code, out = _admin(root, "recover", "alpha", "--keep")
    assert code == 0 and out["data"]["undo_logs"] == 0


def test_graceful_shutdown_records_clean_shutdown(root: Path) -> None:
    token = mint(root)
    with running_server(root) as server:
        create_task(server, token)
        epoch = server.project("alpha").journal.epoch
    meta = _meta(root)
    assert meta["clean_shutdown"]["head_seq"] == 1
    with running_server(root) as server:
        assert server.project("alpha").journal.epoch == epoch
        assert _meta(root)["clean_shutdown"] is None


def _meta(root: Path, slug: str = "alpha") -> dict:
    path = root / "projects" / slug / ".lattice" / "hosted" / "journal_meta.json"
    return json.loads(path.read_text())


def _record_phases(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, bool, bool]]:
    """Wrap the shutdown phases to record ``(name, work lock held, lease held)``.
    The audit-commit stand-in takes the work lock itself, as H-16's committer
    will: holding it there would deadlock (review round 1)."""
    from lattice.server import registry as registry_module

    seen: list[tuple[str, bool, bool]] = []
    wrapped = []
    for name, phase, locked in registry_module.SHUTDOWN_PHASES:

        def recorder(reg, project, _name=name, _phase=phase):  # noqa: ANN001, ANN202
            seen.append((_name, project.work.locked(), project.holds_lease))
            if _name == "audit_commit":
                assert project.work.acquire(timeout=2), "audit commit cannot take the work lock"
                project.work.release()
            _phase(reg, project)

        wrapped.append((name, recorder, locked))
    monkeypatch.setattr(registry_module, "SHUTDOWN_PHASES", tuple(wrapped))
    return seen


def test_shutdown_phases_run_in_the_ruled_order(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drain, audit staged under the work lock, audit committed outside it,
    clean_shutdown, then the lease released (H-16 coordination ruling)."""
    token = mint(root)
    seen = _record_phases(monkeypatch)
    with running_server(root) as server:
        create_task(server, token)
    per_project = [
        ("audit_stage", True, True),
        ("audit_commit", False, True),
        ("clean_shutdown", True, True),
    ]
    assert seen == per_project * 2  # alpha, then beta
    assert _meta(root)["clean_shutdown"]["head_seq"] == 1
    assert _lease_free(root, "alpha")


def test_unload_runs_the_same_phases_before_releasing_the_lease(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = mint(root)
    with running_server(root) as server:
        create_task(server, token)
        seen = _record_phases(monkeypatch)
        assert _admin(root, "unload", "alpha")[0] == 0
        assert [name for name, _, _ in seen] == ["audit_stage", "audit_commit", "clean_shutdown"]
        assert [locked for _, locked, _ in seen] == [True, False, True]
        assert all(lease for _, _, lease in seen)
        assert _lease_free(root, "alpha")
        assert _meta(root)["clean_shutdown"]["head_seq"] == 1
        code, out = _admin(root, "load", "alpha")
        assert code == 0 and out["data"]["head_seq"] == 1  # unchanged tree: same epoch


def test_a_half_written_request_waits_until_it_is_complete(root: Path) -> None:
    """A non-atomic writer's partial request is never answered as malformed while
    it is being written; once complete it runs."""
    with running_server(root):
        board = root / "projects" / "alpha" / ".lattice"
        request = json.dumps({"action": "set-config", "set": {"review_mode": "triple"}})
        partial = board / "hosted" / "control" / "01J9Z0000000000000000000AB.json"
        partial.write_text(request[:20])  # half written
        # The server has seen it unparseable, and polled a few more times (10 ms each).
        assert wait_for(lambda: partial in control._first_seen_incomplete, timeout=5)
        time.sleep(0.05)
        assert not partial.with_suffix(".done").exists()
        assert control.pending_requests(board) == []
        partial.write_text(request)  # the writer finishes
        assert wait_for(lambda: partial.with_suffix(".done").exists(), timeout=5)
        assert json.loads(partial.with_suffix(".done").read_text())["ok"] is True


def test_a_malformed_request_expires_however_its_mtime_moves(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review round 2 (push 2): the grace runs on this process's monotonic clock
    from first sight. A malformed oldest request with a far-future mtime, touched
    again and again, is still answered once the grace passes, and a later valid
    request is processed at once meanwhile.

    The grace is held open until the valid request is answered, then shortened
    to 0.3 s: touching every 0.05 s would keep a grace that restarted on a touch
    (or ran from the mtime) from ever passing."""
    import os

    monkeypatch.setattr(control, "INCOMPLETE_GRACE_SECONDS", 1e9)
    with running_server(root):
        folder = root / "projects" / "alpha" / ".lattice" / "hosted" / "control"
        folder.mkdir(exist_ok=True)
        bad = folder / "01J9Z0000000000000000000AC.json"  # the oldest request
        bad.write_text("{not json")
        future = time.time() + 10 * 365 * 86400
        os.utime(bad, (future, future))
        good = folder / "01J9Z0000000000000000000AD.json"
        control._write_private(
            good, json.dumps({"action": "set-config", "set": {"review_mode": "triple"}}).encode()
        )
        assert wait_for(lambda: good.with_suffix(".done").exists(), timeout=5)
        assert json.loads(good.with_suffix(".done").read_text())["ok"] is True
        assert not bad.with_suffix(".done").exists()  # still within its grace

        monkeypatch.setattr(control, "INCOMPLETE_GRACE_SECONDS", 0.3)
        deadline = time.monotonic() + 10  # a safety bound only
        while not bad.with_suffix(".done").exists():
            assert time.monotonic() < deadline, "the malformed request was never answered"
            if bad.exists():
                os.utime(bad, (future + 1, time.time() + 3600))  # touched: no extension
            time.sleep(0.05)
        answer = json.loads(bad.with_suffix(".done").read_text())
        assert answer["ok"] is False and answer["error"]["code"] == "VALIDATION_ERROR"
