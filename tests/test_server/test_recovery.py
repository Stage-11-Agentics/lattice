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

from pathlib import Path

import pytest

from lattice.server.project import Project
from lattice.storage.operations import discover_task_authorities
from tests.test_server.faults import CrashSnapshots, install, load_project, run
from tests.test_server.test_transactions import (
    SCENARIOS,
    SLUG,
    Fresh,
    Scenario,
    _prepared,
    board_of,
    doctor_clean,
    journal_lines,
    state,
    undo_logs,
)
from tests.test_server.test_transactions import (
    fresh as fresh,  # noqa: F401 - fixture
)
from tests.test_server.test_transactions import (
    projects as projects,  # noqa: F401 - fixture
)
from tests.test_server.test_transactions import (
    template as template,  # noqa: F401 - fixture
)


def _receipt_lines(root: Path) -> list[bytes]:
    receipts = board_of(root) / "hosted" / "receipts"
    if not receipts.is_dir():
        return []
    return [line for p in sorted(receipts.glob("*.jsonl")) for line in p.read_bytes().splitlines()]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_a_crash_at_every_boundary_recovers_at_startup(
    scenario: Scenario,
    fresh: Fresh,  # noqa: F811
    projects: list[Project],  # noqa: F811
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
