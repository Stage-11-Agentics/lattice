"""AC-4 (H-22 part), AC-23, AC-46: startup recovery (SPEC §8.7).

The crash cases kill no process: :class:`CrashSnapshots` copies the server
root at every seam an operation crosses (and, at each server-control write,
a torn variant with half the line on disk), which is exactly what a process
killed there leaves behind. Each copy is then loaded by a fresh
:class:`Project`, as a restarted server would, and must hold the operation
wholly present (its journal line complete) or wholly absent, with no undo
log left, strict discovery and doctor clean, and a retry of the same
``op_id`` applied exactly once.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from io import StringIO
from dataclasses import replace
from pathlib import Path

import pytest

from lattice.core.errors import OpError
from lattice.core.ids import generate_op_id
from lattice.server import admin
from lattice.server.log import ServerLog
from lattice.server.project import Project
from lattice.server.testing import make_root
from lattice.server.transactions import receipt_file_name
from lattice.storage.ownership import offline_maintenance
from lattice.storage.operations import discover_task_authorities
from tests.test_server.conftest import board_hash
from tests.test_server.faults import (
    CrashSnapshots,
    Injector,
    install,
    load_project,
    request,
    run,
)
from tests.test_server.test_transactions import (
    SCENARIO,
    SCENARIOS,
    SLUG,
    Fresh,
    Scenario,
    _prepared,
    board_of,
    create,
    doctor_clean,
    journal_lines,
    state,
    undo_logs,
)


@pytest.fixture(scope="module")
def template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return make_root(
        tmp_path_factory.mktemp("recovery-template"), projects={SLUG: {"code": "ALP"}}
    )


@pytest.fixture()
def fresh(template: Path, tmp_path: Path) -> Fresh:
    return Fresh(template, tmp_path)


@pytest.fixture()
def projects() -> Iterator[list[Project]]:
    """Projects a test loads; their owner leases are released at teardown."""
    loaded: list[Project] = []
    yield loaded
    for project in loaded:
        project.release()


def _receipt_lines(root: Path) -> list[bytes]:
    receipts = board_of(root) / "hosted" / "receipts"
    if not receipts.is_dir():
        return []
    return [line for p in sorted(receipts.glob("*.jsonl")) for line in p.read_bytes().splitlines()]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_a_crash_at_every_boundary_recovers_at_startup(
    scenario: Scenario,
    fresh: Fresh,
    projects: list[Project],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root, project, build = _prepared(fresh, projects, scenario)
    before = state(root)
    before_receipts = _receipt_lines(root)
    write = build()
    op_id = write.caller.origin["op_id"]
    crashes = CrashSnapshots(root, tmp_path / "crashes")
    with monkeypatch.context() as m:
        install(m, crashes)  # type: ignore[arg-type]
        run(project, write)
    project.release()
    projects.remove(project)
    after = state(root)
    assert crashes.snapshots

    for label, crashed in crashes.snapshots:
        case = f"{scenario.name} killed at {label}"
        restarted = load_project(crashed, SLUG)
        try:
            lines = journal_lines(crashed)
            committed = bool(lines) and lines[-1].get("op_id") == op_id
            assert undo_logs(crashed) == [], case
            recovered = state(crashed)
            if committed:
                assert recovered["board"] == after["board"], case
                assert len(lines) == before["journal"].count(b"\n") + 1, case
            else:
                assert recovered["board"] == before["board"], case
                assert recovered["journal"] == before["journal"], case
                assert _receipt_lines(crashed) == before_receipts, case  # orphans removed
            discover_task_authorities(board_of(crashed))
            doctor_clean(crashed)
            retry = run(restarted, write)
            assert retry.replayed is committed, case
            ops = [x["op_id"] for x in journal_lines(crashed)]
            assert ops.count(op_id) == 1, case
        finally:
            restarted.release()


# ---------------------------------------------------------------------------
# Helpers for the targeted cases
# ---------------------------------------------------------------------------


def _loaded(fresh: Fresh, projects: list[Project]) -> tuple[Path, Project]:
    root = fresh()
    project = load_project(root, SLUG)
    projects.append(project)
    return root, project


def _restart(root: Path, project: Project | None = None) -> Project:
    """Release *project* (as if its process died) and load *root* afresh."""
    if project is not None:
        project.release()
    restarted = Project(SLUG, root / "projects" / SLUG, ServerLog("debug", StringIO()), "srv2")
    restarted.load()
    return restarted


def _log_events(project: Project) -> list[dict]:
    stream = project.log._stream  # noqa: SLF001
    return [json.loads(line) for line in stream.getvalue().splitlines() if line]


class Crash(BaseException):
    """Stops a write dead at a seam, skipping in-process recovery (a killed process)."""


def _kill_at(monkeypatch: pytest.MonkeyPatch, point: str, occurrence: int = 1) -> None:
    """Make the process 'die' at *point*: the write stops there, and nothing in
    this process may touch the board afterwards (the caller releases the lease)."""
    import lattice.server.transactions as transactions

    seen = {"n": 0}

    def fault(name: str, **_ctx: object) -> None:
        if name == point:
            seen["n"] += 1
            if seen["n"] == occurrence:
                raise Crash(point)

    def no_recovery(self: transactions.Transaction) -> None:
        raise transactions.Quarantine("killed")  # a dead process recovers nothing

    monkeypatch.setattr(transactions, "_fault", fault)
    monkeypatch.setattr(transactions.Transaction, "recover", no_recovery)


def _killed_write(
    monkeypatch: pytest.MonkeyPatch,
    project: Project,
    write: object,
    point: str,
    occurrence: int = 1,
) -> None:
    with monkeypatch.context() as m:
        _kill_at(m, point, occurrence)
        with pytest.raises(BaseException):  # noqa: B017 - the kill, or its quarantine
            run(project, write)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Undo-log identity (plan review finding 1)
# ---------------------------------------------------------------------------


def test_undo_headers_name_the_seq_they_will_commit_at(
    fresh: Fresh,
    projects: list[Project],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, project = _loaded(fresh, projects)
    create(project)
    headers: list[dict] = []

    def spy(point: str, **_ctx: object) -> None:
        if point == "board.mutation" and not headers:
            (log,) = (board_of(root) / "hosted" / "undo").iterdir()
            headers.append(json.loads(log.read_text().splitlines()[0]))

    with monkeypatch.context() as m:
        install(m, spy)  # type: ignore[arg-type]
        outcome = run(project, request("task.create", {"title": "second"}))
    assert headers[0]["seq"] == outcome.seq == 2
    assert set(headers[0]) == {"epoch", "seq", "token_id", "op_id"}


def test_a_crashed_retry_after_receipt_expiry_is_rolled_back_not_taken_as_committed(
    fresh: Fresh,
    projects: list[Project],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An op_id may run again once its receipt expires. A crash in that second
    attempt must roll back even though the journal already holds a line with the
    same token and op_id (the first attempt's, at another seq)."""
    root, project = _loaded(fresh, projects)
    op_id = generate_op_id()
    first = run(project, request("task.create", {"title": "first run"}, op_id=op_id))
    project.release()
    projects.remove(project)
    receipts = board_of(root) / "hosted" / "receipts"
    for path in receipts.glob("*.jsonl"):
        path.rename(receipts / "2000-01-01.jsonl")  # past retention
    project = _restart(root)
    projects.append(project)
    assert not list(receipts.glob("*.jsonl"))  # expired at load
    assert (("tok_alice", op_id)) not in project.index
    before = state(root)

    retry = request("task.create", {"title": "second run"}, op_id=op_id)
    _killed_write(monkeypatch, project, retry, "receipt.write")
    assert undo_logs(root)  # the dead attempt's log
    header = json.loads(
        next((board_of(root) / "hosted" / "undo").iterdir()).read_text().split("\n")[0]
    )
    assert header["op_id"] == op_id and header["seq"] == first.seq + 1

    project.release()
    projects.remove(project)
    restarted = _restart(root)
    projects.append(restarted)
    assert restarted.state == "loaded", restarted.reason
    assert undo_logs(root) == []
    assert state(root) == before  # rolled back, not mistaken for seq 1
    events = [e["event"] for e in _log_events(restarted)]
    assert "recovery_rollback" in events
    doctor_clean(root)


def test_an_undo_log_without_a_seq_quarantines_until_project_recover(
    fresh: Fresh,
    projects: list[Project],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, project = _loaded(fresh, projects)
    create(project)
    before = state(root)
    epoch = project.journal.epoch
    _killed_write(monkeypatch, project, request("task.create", {"title": "dies"}), "receipt.write")
    project.release()
    projects.remove(project)
    (log,) = (board_of(root) / "hosted" / "undo").iterdir()
    lines = log.read_text().split("\n")
    header = json.loads(lines[0])
    del header["seq"]  # an undo log written before headers carried a seq
    log.write_text("\n".join([json.dumps(header), *lines[1:]]))

    restarted = _restart(root)
    assert restarted.state == "unavailable"
    assert "project recover alpha" in (restarted.reason or "")
    assert undo_logs(root)  # untouched until an admin decides

    with pytest.raises(OpError) as refused:
        admin.recover_project(root, SLUG, None)  # the journal cannot decide: the admin must
    assert refused.value.details["reason"] == "RECOVER_MODE_REQUIRED"
    assert undo_logs(root)

    result = admin.recover_project(root, SLUG, "rollback")
    assert len(result["rolled_back"]) == 1 and undo_logs(root) == []
    assert state(root)["board"] == before["board"]
    loaded = _restart(root)
    projects.append(loaded)
    assert loaded.state == "loaded" and loaded.journal.epoch != epoch  # maintenance
    doctor_clean(root)


# ---------------------------------------------------------------------------
# Startup rollback is strictly durable and resumable (plan review finding 2)
# ---------------------------------------------------------------------------


def _dead_write(
    fresh: Fresh, projects: list[Project], monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, dict, object]:
    """A root whose last write died after changing board files (an archive with
    notes: appends, copies, and an unlink), its lease released. Returns the root,
    the state before that write, and the write."""
    root, project, build = _prepared(fresh, projects, SCENARIO["task.archive"])
    before = state(root)
    write = build()
    _killed_write(monkeypatch, project, write, "receipt.write")
    assert undo_logs(root) and state(root)["board"] != before["board"]
    project.release()
    projects.remove(project)
    return root, before, write


def test_a_directory_fsync_failure_during_startup_rollback_keeps_the_undo_log(
    fresh: Fresh, projects: list[Project], monkeypatch: pytest.MonkeyPatch
) -> None:
    import lattice.storage.fs as fs

    root, before, _write = _dead_write(fresh, projects, monkeypatch)
    hosted = board_of(root) / "hosted"
    real = fs._fsync_directory

    def failing(path: Path, *, strict: bool = False) -> None:
        if not Path(path).resolve().is_relative_to(hosted.resolve()):
            raise OSError(5, "injected directory fsync failure")
        real(path, strict=strict)

    with monkeypatch.context() as m:
        m.setattr(fs, "_fsync_directory", failing)
        failed = _restart(root)
    assert failed.state == "unavailable"
    assert undo_logs(root)  # never deleted before every restoration is durable
    loaded = _restart(root)  # the next load resumes the rollback
    projects.append(loaded)
    assert loaded.state == "loaded", loaded.reason
    assert undo_logs(root) == [] and state(root) == before
    doctor_clean(root)


def test_a_restart_in_the_middle_of_a_startup_rollback_resumes_it(
    fresh: Fresh, projects: list[Project], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root, before, write = _dead_write(fresh, projects, monkeypatch)
    crashes = CrashSnapshots(root, tmp_path / "mid-rollback")
    with monkeypatch.context() as m:
        install(m, crashes)  # type: ignore[arg-type]
        recovering = _restart(root)
    recovering.release()
    labels = [label for label, _ in crashes.snapshots]
    # Distinct disk states only: the one before the undo log's deletion equals
    # the one after the last restoration's directory fsync.
    assert any(label.startswith("recover.rollback") for label in labels)
    assert len(labels) > 3
    for label, crashed in crashes.snapshots:
        restarted = _restart(crashed)
        try:
            assert restarted.state == "loaded", (label, restarted.reason)
            assert undo_logs(crashed) == [], label
            assert state(crashed) == before, label
            retry = run(restarted, write)  # type: ignore[arg-type]
            assert not retry.replayed, label
        finally:
            restarted.release()


# ---------------------------------------------------------------------------
# Rotation, torn tails, missing journal
# ---------------------------------------------------------------------------


def test_a_crash_after_each_rotation_step_is_completed_at_startup(
    fresh: Fresh, projects: list[Project], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import shutil

    import lattice.server.journal as journal_module

    root, project = _loaded(fresh, projects)
    write = request("task.create", {"title": "before rotation"})
    committed = run(project, write)
    old_epoch = project.journal.epoch
    snapshots: list[tuple[str, Path]] = []

    def after(name: str, fn):  # noqa: ANN001, ANN202
        def wrapped(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            result = fn(*args, **kwargs)
            target = tmp_path / f"rot-{len(snapshots)}"
            shutil.copytree(root, target, symlinks=True)
            snapshots.append((f"after {name} {Path(args[0]).name}", target))
            return result

        return wrapped

    with monkeypatch.context() as m:
        for name in ("atomic_write", "_rename", "unlink_path"):
            m.setattr(journal_module, name, after(name, getattr(journal_module, name)))
        with project.locked():
            project.journal = project.journal.rotate()
    new_epoch = project.journal.epoch
    assert [label.split()[1] for label, _ in snapshots] == [
        "atomic_write",  # 1. rotation.json
        "_rename",  # 2. journal.jsonl -> journal.<old>.jsonl
        "atomic_write",  # 3. journal_meta.json
        "atomic_write",  # 4. journal.jsonl
        "unlink_path",  # 5. rotation.json removed
    ]
    for label, crashed in snapshots:
        restarted = _restart(crashed)
        try:
            assert restarted.state == "loaded", (label, restarted.reason)
            assert restarted.journal.epoch == new_epoch, label
            hosted = board_of(crashed) / "hosted"
            assert not (hosted / "rotation.json").exists(), label
            assert (hosted / f"journal.{old_epoch}.jsonl").exists(), label
            replay = run(restarted, write)
            assert replay.replayed and replay.seq == committed.seq, label
        finally:
            restarted.release()


@pytest.mark.parametrize("which", ["journal", "receipt", "undo"])
def test_torn_tails_are_dropped_at_startup(
    which: str, fresh: Fresh, projects: list[Project], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, project = _loaded(fresh, projects)
    create(project)
    before = state(root)
    point = {"journal": "journal.write", "receipt": "receipt.write", "undo": "undo.write"}[which]
    # The second undo write is the first entry after the header: a torn entry
    # guards a change that was never made.
    occurrence = 2 if which == "undo" else 1

    import lattice.server.transactions as transactions

    seen = {"n": 0}

    def torn(name: str, **ctx: object) -> None:
        if name == point:
            seen["n"] += 1
            if seen["n"] == occurrence:
                data = ctx["data"]
                os.write(ctx["fd"], data[: len(data) // 2])  # type: ignore[arg-type, index]
                raise Crash(point)

    with monkeypatch.context() as m:
        m.setattr(transactions, "_fault", torn)
        m.setattr(
            transactions.Transaction,
            "recover",
            lambda self: (_ for _ in ()).throw(transactions.Quarantine("killed")),
        )
        with pytest.raises(BaseException):  # noqa: B017
            run(project, request("task.create", {"title": "torn"}))
    project.release()
    projects.remove(project)
    restarted = _restart(root)
    projects.append(restarted)
    assert restarted.state == "loaded", restarted.reason
    assert state(root) == before and undo_logs(root) == []
    doctor_clean(root)


def test_a_missing_journal_with_undo_logs_waits_for_recover_keep(
    fresh: Fresh, projects: list[Project], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _before, _write = _dead_write(fresh, projects, monkeypatch)
    hosted = board_of(root) / "hosted"
    changed = board_hash(root, SLUG)
    (hosted / "journal.jsonl").unlink()
    quarantined = _restart(root)
    assert quarantined.state == "unavailable"
    assert "--rollback | --keep" in (quarantined.reason or "")
    assert undo_logs(root)

    result = admin.recover_project(root, SLUG, "keep")
    assert len(result["kept"]) == 1 and undo_logs(root) == []
    assert board_hash(root, SLUG) == changed  # kept as they were
    assert (hosted / "maintenance.json").exists()
    loaded = _restart(root)
    projects.append(loaded)
    assert loaded.state == "loaded", loaded.reason
    assert loaded.journal.head_seq == 0


def test_a_missing_journal_without_undo_logs_rotates(
    fresh: Fresh, projects: list[Project]
) -> None:
    root, project = _loaded(fresh, projects)
    create(project)
    project.release()
    projects.remove(project)
    (board_of(root) / "hosted" / "journal.jsonl").unlink()
    loaded = _restart(root)
    projects.append(loaded)
    assert loaded.state == "loaded" and loaded.journal.head_seq == 0
    assert "journal_missing" in [e["event"] for e in _log_events(loaded)]


# ---------------------------------------------------------------------------
# Maintenance, restores, foreign appends (AC-23)
# ---------------------------------------------------------------------------


def test_a_restart_after_offline_maintenance_rotates_the_epoch(
    fresh: Fresh, projects: list[Project]
) -> None:
    root, project = _loaded(fresh, projects)
    write = request("task.create", {"title": "kept"})
    run(project, write)
    old_epoch = project.journal.epoch
    project.release()
    projects.remove(project)
    board = board_of(root)
    with offline_maintenance(board, "doctor --fix"):
        pass  # the record alone: the maintenance command changed nothing here
    loaded = _restart(root)
    projects.append(loaded)
    assert loaded.journal.epoch != old_epoch
    assert not (board / "hosted" / "maintenance.json").exists()
    assert run(loaded, write).replayed  # rotation does not affect deduplication


def _shut_down_cleanly(project: Project) -> None:
    with project.locked():
        project.write_clean_shutdown()
    project.release()


def test_clean_shutdown_is_recorded_and_cleared(fresh: Fresh, projects: list[Project]) -> None:
    root, project = _loaded(fresh, projects)
    create(project)
    epoch = project.journal.epoch
    projects.remove(project)
    _shut_down_cleanly(project)
    meta = json.loads((board_of(root) / "hosted" / "journal_meta.json").read_text())
    assert meta["clean_shutdown"]["head_seq"] == 1
    assert len(meta["clean_shutdown"]["tree_fingerprint"]) == 32
    loaded = _restart(root)
    projects.append(loaded)
    assert loaded.journal.epoch == epoch  # nothing changed: same history
    meta = json.loads((board_of(root) / "hosted" / "journal_meta.json").read_text())
    assert meta["clean_shutdown"] is None


def test_a_changed_tree_after_a_clean_shutdown_rotates(
    fresh: Fresh, projects: list[Project]
) -> None:
    root, project = _loaded(fresh, projects)
    task = create(project)["task"]["id"]
    epoch = project.journal.epoch
    projects.remove(project)
    _shut_down_cleanly(project)
    plan = board_of(root) / "plans" / f"{task}.md"
    os.utime(plan, ns=(plan.stat().st_atime_ns, plan.stat().st_mtime_ns + 1_000_000_000))
    loaded = _restart(root)
    projects.append(loaded)
    assert loaded.journal.epoch != epoch
    assert "restore_rotation" in [e["event"] for e in _log_events(loaded)]


def test_a_restore_with_mtimes_preserved_keeps_the_epoch(
    fresh: Fresh, projects: list[Project], tmp_path: Path
) -> None:
    import shutil

    root, project = _loaded(fresh, projects)
    create(project)
    epoch = project.journal.epoch
    projects.remove(project)
    _shut_down_cleanly(project)
    restored = tmp_path / "restored"
    shutil.copytree(root, restored, symlinks=True)  # copy2: mtimes preserved
    loaded = _restart(restored)
    projects.append(loaded)
    assert loaded.journal.epoch == epoch


def test_a_foreign_append_after_a_crash_is_journaled_as_external(
    fresh: Fresh, projects: list[Project]
) -> None:
    root, project = _loaded(fresh, projects)
    task = create(project)["task"]["id"]
    project.release()  # no clean shutdown: as after a crash
    projects.remove(project)
    log = board_of(root) / "events" / f"{task}.jsonl"
    event = json.loads(log.read_text().splitlines()[0])
    event.update(id="ev_01J9Z0000000000000000000ZZ", type="comment_added", data={"body": "x"})
    with open(log, "a") as fh:
        fh.write(json.dumps(event, sort_keys=True) + "\n")
    loaded = _restart(root)
    projects.append(loaded)
    line = journal_lines(root)[-1]
    rel = f"events/{task}.jsonl"
    assert line["op"] == "external" and line["paths"] == [rel]
    assert line["lengths"] == {rel: log.stat().st_size}
    assert "external_change" in [e["event"] for e in _log_events(loaded)]
    again = _restart(root, loaded)  # now known: journaled once
    projects.remove(loaded)
    projects.append(again)
    assert [x["op"] for x in journal_lines(root)].count("external") == 1


def test_an_archived_then_appended_log_is_not_foreign(
    fresh: Fresh, projects: list[Project]
) -> None:
    """A log relocated by a journaled operation has no append base; its later,
    journaled appends must not look foreign."""
    root, project = _loaded(fresh, projects)
    task = create(project)["task"]["id"]
    run(project, request("task.archive", {"task": task}))
    run(project, request("task.unarchive", {"task": task}))
    run(project, request("task.comment", {"task": task, "text": "after"}))
    head = project.journal.head_seq
    loaded = _restart(root, project)
    projects.remove(project)
    projects.append(loaded)
    assert loaded.journal.head_seq == head


# ---------------------------------------------------------------------------
# Quarantine, then reload or restart; receipt retention
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("recovery_point", ["recover.rollback", "recover.undo_delete"])
@pytest.mark.parametrize("how", ["reload", "restart"])
def test_a_quarantined_project_recovers_on_its_next_load(
    recovery_point: str,
    how: str,
    fresh: Fresh,
    projects: list[Project],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, project, build = _prepared(fresh, projects, SCENARIO["task.archive"])
    before = state(root)
    with monkeypatch.context() as m:
        install(m, Injector("receipt.fsync").also(recovery_point))
        with pytest.raises(OpError):
            run(project, build())
    assert project.state == "unavailable" and undo_logs(root)
    if how == "reload":
        project.load()  # what 'project reload' runs after its unload
        loaded = project
    else:
        projects.remove(project)
        loaded = _restart(root, project)
        projects.append(loaded)
    assert loaded.state == "loaded", loaded.reason
    assert undo_logs(root) == [] and state(root) == before
    doctor_clean(root)
    run(loaded, build())


def _eight_days_later(monkeypatch: pytest.MonkeyPatch) -> None:
    """Move the retention clock past the seven-day window (the server keeps running)."""
    from datetime import timedelta

    from lattice.server import recovery

    later = recovery.utc_today() + timedelta(days=8)
    monkeypatch.setattr(recovery, "utc_today", lambda: later)


def test_an_expired_retry_that_is_the_first_request_after_rollover_runs_again(
    fresh: Fresh, projects: list[Project], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review round 1: the replay lookup must not see an expired receipt, even
    when no other write has run retention since the day rolled over."""
    root, project = _loaded(fresh, projects)
    old = request("task.create", {"title": "old"})
    first = run(project, old)
    assert run(project, old).replayed  # within retention: a replay
    _eight_days_later(monkeypatch)
    retried = run(project, old)  # the first request after rollover
    assert not retried.replayed and retried.seq == first.seq + 1
    receipts = board_of(root) / "hosted" / "receipts"
    key = (old.token_id, old.caller.origin["op_id"])
    status = project.op_status(*key)
    assert status["seq"] == retried.seq  # the new run's receipt, not the expired one
    # Retention deleted the expired file; the retry's receipt starts a new one
    # (receipt files are named by the real clock).
    assert [p.name for p in receipts.glob("*.jsonl")] == [receipt_file_name()]


def test_receipts_past_retention_are_deleted_on_the_first_write_of_a_day(
    fresh: Fresh, projects: list[Project], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, project = _loaded(fresh, projects)
    old = request("task.create", {"title": "old"})
    run(project, old)
    receipts = board_of(root) / "hosted" / "receipts"
    (today,) = receipts.glob("*.jsonl")
    today.rename(receipts / "2000-01-01.jsonl")
    key = (old.token_id, old.caller.origin["op_id"])
    project.index[key] = replace(project.index[key], receipt="2000-01-01.jsonl")
    _eight_days_later(monkeypatch)
    run(project, request("task.create", {"title": "today"}))
    assert not (receipts / "2000-01-01.jsonl").exists()
    assert key not in project.index
    retried = run(project, old)  # past retention: it runs again (SPEC §8.6)
    assert not retried.replayed


def test_a_failed_receipt_deletion_never_lets_the_expired_operation_replay(
    fresh: Fresh, projects: list[Project], monkeypatch: pytest.MonkeyPatch
) -> None:
    import lattice.server.project as project_module

    root, project = _loaded(fresh, projects)
    project.log = ServerLog("debug", StringIO())
    old = request("task.create", {"title": "old"})
    run(project, old)
    receipts = board_of(root) / "hosted" / "receipts"
    (today,) = receipts.glob("*.jsonl")
    expired = receipts / "2000-01-01.jsonl"
    today.rename(expired)
    key = (old.token_id, old.caller.origin["op_id"])
    project.index[key] = replace(project.index[key], receipt=expired.name)
    real_unlink = project_module.unlink_path

    def failing_unlink(path: Path, **kw: object) -> None:
        if Path(path).parent == receipts:
            raise OSError(5, "injected unlink failure")
        real_unlink(path, **kw)  # type: ignore[arg-type]

    _eight_days_later(monkeypatch)
    with monkeypatch.context() as m:
        m.setattr(project_module, "unlink_path", failing_unlink)
        retried = run(project, old)  # retention fails; the write does not
    assert not retried.replayed
    assert expired.exists()  # deletion failed, but the index forgot it anyway
    assert all(v.receipt != expired.name for v in project.index.values())
    assert "receipt_retention_failed" in [e["event"] for e in _log_events(project)]
    run(project, request("task.create", {"title": "next"}))  # retention retried
    assert not expired.exists()


def test_op_status_at_rollover_never_loses_an_entry_committed_meanwhile(
    fresh: Fresh, projects: list[Project], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review round 2: op status runs without the work lock. Paused in the middle
    of its expiry check at a day rollover while a write commits, it must not
    discard that write's index entry: the write's retry replays, applied once."""
    import threading

    from lattice.server import recovery

    root, project = _loaded(fresh, projects)
    old = request("task.create", {"title": "old"})
    run(project, old)
    receipts = board_of(root) / "hosted" / "receipts"
    (today,) = receipts.glob("*.jsonl")
    today.rename(receipts / "2000-01-01.jsonl")  # the old operation is past retention
    old_key = (old.token_id, old.caller.origin["op_id"])
    project.index[old_key] = replace(project.index[old_key], receipt="2000-01-01.jsonl")
    project._index_day = None  # noqa: SLF001 - the day rolled over; no expiry has run

    paused, resume = threading.Event(), threading.Event()
    real_expired = recovery.receipt_expired

    def gated(name: str, today=None) -> bool:  # noqa: ANN001
        if threading.current_thread().name == "op-status" and not paused.is_set():
            paused.set()
            assert resume.wait(5)
        return real_expired(name, today)

    monkeypatch.setattr(recovery, "receipt_expired", gated)
    status: dict = {}
    reader = threading.Thread(
        target=lambda: status.update(project.op_status(*old_key)), name="op-status"
    )
    reader.start()
    assert paused.wait(5)  # op status is mid-check, holding no lock
    new = request("task.create", {"title": "committed meanwhile"})
    first = run(project, new)  # commits and inserts its index entry now
    resume.set()
    reader.join(timeout=5)

    retry = run(project, new)
    assert retry.replayed and retry.seq == first.seq
    ops = [x["op_id"] for x in journal_lines(root)]
    assert ops.count(new.caller.origin["op_id"]) == 1
    assert status["state"] == "committed" and "result" not in status  # expired: no result
    assert old_key not in project.index
