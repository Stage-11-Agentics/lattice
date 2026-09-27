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
from collections.abc import Iterator
from io import StringIO
from pathlib import Path

import pytest

from lattice.core.errors import OpError
from lattice.core.ids import generate_op_id
from lattice.server import admin
from lattice.server.log import ServerLog
from lattice.server.project import Project
from lattice.server.testing import make_root
from lattice.storage.operations import discover_task_authorities
from tests.test_server.faults import CrashSnapshots, install, load_project, request, run
from tests.test_server.test_transactions import (
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
