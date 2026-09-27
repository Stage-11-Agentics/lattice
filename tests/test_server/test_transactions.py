"""AC-4 (H-22a part): every server write is wholly applied or wholly absent (SPEC §8.6).

For ``task.create``, ``task.status``, and ``task.archive`` (a task with a plan
and notes, so archive appends, copies, then unlinks), a counting pass lists
every boundary the operation crosses: each undo append and fsync, each board
mutation, each placement step, each strictly durable directory fsync, the
receipt write and fsync, the journal write and fsync, and the finish steps.
The operation is then failed at each one in turn, on a fresh copy of the
server root. Afterward:

- before the journal fsync: wholly absent (durable board files, journal, and
  receipts byte-identical to before); the project keeps serving;
- the journal fsync itself: quarantined (``BOARD_UNAVAILABLE``, nothing more
  written);
- after it: wholly present (committed, indexed, never rolled back), with the
  error still returned to the caller.

In every recovered case no undo log remains, strict discovery and ``lattice
doctor`` are clean, the next operation commits, and a lost-response retry of
it replays. The cases drive :class:`Project` directly, as the op endpoint's
worker does; a few HTTP cases cover what a client sees.
"""

from __future__ import annotations

import asyncio
import errno
import json
import shutil
from types import SimpleNamespace
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from click.testing import CliRunner

from lattice.cli.main import cli
from lattice.core.errors import OpError
from lattice.core.ids import generate_op_id
from lattice.server import transactions
from lattice.server.project import Project
from lattice.server.stream import JOURNAL, Subscriber
from lattice.server.testing import make_root
from lattice.server.transactions import read_undo_log
from lattice.storage import fs
from lattice.storage.operations import discover_task_authorities
from tests.test_server.conftest import board_hash
from tests.test_server.faults import (
    InjectedFault,
    Injector,
    install,
    load_project,
    request,
    run,
)

SLUG = "alpha"


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    base = tmp_path_factory.mktemp("txn-template")
    return make_root(base, projects={SLUG: {"code": "ALP"}})


class Fresh:
    """Fresh copies of the template server root (or of a prepared one), one per case."""

    def __init__(self, template: Path, base: Path) -> None:
        self.template = template
        self.base = base
        self.count = 0
        #: scenario name -> (a root with the scenario's setup done, request builder)
        self.prepared: dict[str, tuple[Path, Callable[[], object]]] = {}

    def __call__(self, source: Path | None = None) -> Path:
        self.count += 1
        root = self.base / f"case-{self.count}"
        shutil.copytree(source or self.template, root, symlinks=True)
        return root


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


def board_of(root: Path) -> Path:
    return root / "projects" / SLUG / ".lattice"


def state(root: Path) -> dict:
    """Everything a transaction may change: durable board files, journal, receipts."""
    hosted = board_of(root) / "hosted"
    receipts = hosted / "receipts"
    return {
        "board": board_hash(root, SLUG),
        "journal": (hosted / "journal.jsonl").read_bytes(),
        "receipts": {
            p.name: p.read_bytes() for p in sorted(receipts.glob("*")) if receipts.is_dir()
        },
    }


def undo_logs(root: Path) -> list[str]:
    undo = board_of(root) / "hosted" / "undo"
    return sorted(p.name for p in undo.iterdir()) if undo.is_dir() else []


def journal_lines(root: Path) -> list[dict]:
    raw = (board_of(root) / "hosted" / "journal.jsonl").read_bytes()
    return [json.loads(x) for x in raw.splitlines()]


def doctor_clean(root: Path) -> None:
    result = CliRunner().invoke(
        cli, ["doctor", "--json"], env={"LATTICE_ROOT": str(root / "projects" / SLUG)}
    )
    payload = json.loads(result.output)
    errors = [f for f in payload["data"]["findings"] if f.get("level") == "error"]
    assert payload["ok"] is True and not errors, result.output


def create(project: Project, title: str = "t", **kw) -> dict:
    return run(project, request("task.create", {"title": title}, **kw)).result_data


def assert_next_write_commits_and_replays(root: Path, project: Project) -> None:
    op_id = generate_op_id()
    before = len(journal_lines(root))
    first = run(project, request("task.create", {"title": "next"}, op_id=op_id))
    assert not first.replayed
    again = run(project, request("task.create", {"title": "next"}, op_id=op_id))
    assert again.replayed and again.seq == first.seq
    assert again.result_data == {**first.result_data, "replayed": True}
    assert len(journal_lines(root)) == before + 1


# ---------------------------------------------------------------------------
# The three operations
# ---------------------------------------------------------------------------


@dataclass
class Scenario:
    name: str
    #: Prepares the loaded project; returns the faulty operation's request builder.
    setup: Callable[[Path, Project], Callable[[], object]]


def _setup_create(root: Path, project: Project) -> Callable[[], object]:
    return lambda: request("task.create", {"title": "faulty"})


def _setup_status(root: Path, project: Project) -> Callable[[], object]:
    task = create(project)["task"]["id"]
    return lambda: request("task.status", {"task": task, "new_status": "in_planning"})


def _setup_archive(root: Path, project: Project) -> Callable[[], object]:
    task = create(project)["task"]["id"]
    board = board_of(root)
    assert (board / "plans" / f"{task}.md").is_file()
    (board / "notes" / f"{task}.md").write_text("working notes\n")
    return lambda: request("task.archive", {"task": task})


def _task_in(project: Project, status: str) -> str:
    """A task walked to *status* (with a real plan, so the plan gate passes)."""
    task = create(project)["task"]["id"]
    path = ["in_planning", "planned", "in_progress", "review", "done"]
    for step in path[: path.index(status) + 1]:
        if step == "planned":
            run(project, request("task.plan_write", {"task": task, "stdin": "# Plan\n\nDo it.\n"}))
        run(project, request("task.status", {"task": task, "new_status": step}))
    return task


def _setup_complete(root: Path, project: Project) -> Callable[[], object]:
    task = _task_in(project, "in_progress")
    return lambda: request("task.complete", {"task": task, "review": "Verified; LGTM."})


def _setup_plan_write(root: Path, project: Project) -> Callable[[], object]:
    task = create(project)["task"]["id"]
    return lambda: request("task.plan_write", {"task": task, "stdin": "# Plan\n\nSteps.\n"})


def _setup_unarchive(root: Path, project: Project) -> Callable[[], object]:
    task = create(project)["task"]["id"]
    (board_of(root) / "notes" / f"{task}.md").write_text("working notes\n")
    run(project, request("task.archive", {"task": task}))
    return lambda: request("task.unarchive", {"task": task})


def _setup_acquire(root: Path, project: Project) -> Callable[[], object]:
    run(project, request("resource.create", {"name": "db"}))
    return lambda: request("resource.acquire", {"name": "db"})


def _setup_session_start(root: Path, project: Project) -> Callable[[], object]:
    params = {"model": "opus", "framework": "claude-code", "name": "Worker"}
    return lambda: request("session.start", params, actor=None)


def _setup_set_project_code(root: Path, project: Project) -> Callable[[], object]:
    # No tasks: doctor rightly flags existing short IDs outside a changed code.
    return lambda: request("board.set_project_code", {"code": "ALQ", "force": True}, actor=None)


SCENARIOS = [
    Scenario("task.create", _setup_create),
    Scenario("task.status", _setup_status),
    Scenario("task.archive", _setup_archive),
    # H-22: the remaining operation families (EVALUATION AC-4, second row).
    Scenario("task.complete", _setup_complete),
    Scenario("task.plan_write", _setup_plan_write),
    Scenario("task.unarchive", _setup_unarchive),
    Scenario("resource.acquire", _setup_acquire),
    Scenario("session.start", _setup_session_start),
    Scenario("board.set_project_code", _setup_set_project_code),
]
SCENARIO = {s.name: s for s in SCENARIOS}


def _prepared(fresh: Fresh, projects: list[Project], scenario: Scenario):  # noqa: ANN202
    """A loaded copy of a root on which *scenario*'s setup has run (once per test)."""
    if scenario.name not in fresh.prepared:
        source = fresh()
        project = load_project(source, SLUG)
        try:
            fresh.prepared[scenario.name] = (source, scenario.setup(source, project))
        finally:
            project.release()
    source, build = fresh.prepared[scenario.name]
    root = fresh(source)
    project = load_project(root, SLUG)
    projects.append(project)
    return root, project, build


def wire_publication(project: Project) -> list[int]:
    """Put the ``publication`` fault seam in front of the real publication hook;
    returns the seqs whose failed publication closed the project's streams."""
    closed: list[int] = []
    real_publish = project._publish

    def publish(line: dict) -> None:
        transactions._fault("publication", seq=line["seq"])
        real_publish(line)

    def close_streams() -> None:
        closed.append(project.journal.head_seq)
        project.broadcaster.close_all()

    project.publish = publish
    project.close_streams = close_streams
    return closed


class Follower:
    """A connected follower of *project*, without HTTP: a broadcaster subscriber
    whose every accepted entry counts as delivered (``abort()`` clears only the
    undelivered queue), and which, once its stream is ended, reconnects from
    its ``Last-Event-ID`` through the stream endpoint's own resume path
    (``app._stream_start``: subscribe, then replay under the work lock)."""

    LIMITS = SimpleNamespace(max_stream_subscribers_per_project=64, replay_reset_entries=1000)

    def __init__(self, project: Project) -> None:
        self.project = project
        #: The head when it connected: it follows from here.
        self.start = project.journal.head_seq if project.journal else 0
        self.loop = asyncio.new_event_loop()
        self.delivered: list[int] = []
        self.reconnects = 0
        self.subscriber = self._subscribe()

    def _new_subscriber(self) -> Subscriber:
        follower = self

        class Recording(Subscriber):
            def offer(self, item: tuple[str, int, bytes]) -> bool:
                accepted = super().offer(item)
                if accepted and item[0] == JOURNAL:
                    follower.delivered.append(item[1])
                return accepted

        return Recording(self.loop, 1000)

    def _subscribe(self) -> Subscriber:
        subscriber = self._new_subscriber()
        assert self.project.broadcaster.subscribe(subscriber, 64)
        return subscriber

    def reconnect_if_ended(self) -> None:
        """Resume from the last delivered entry, as a real follower would."""
        if not self.subscriber.aborted:
            return
        from lattice.server.app import _stream_start

        self.reconnects += 1
        project = self.project
        journal = project.journal
        since = self.delivered[-1] if self.delivered else self.start
        subscriber = self._new_subscriber()
        with project.locked():
            resume = (True, journal.epoch, since, journal.hash_at(since))
            frames, _head = _stream_start(project, self.LIMITS, subscriber, resume)
        for frame in frames:
            text = frame.decode()
            assert "event: reset" not in text, "a resume inside the epoch must replay"
            ident = next(x for x in text.splitlines() if x.startswith("id: "))
            self.delivered.append(int(ident.split(":")[2]))
        self.subscriber = subscriber

    def close(self) -> None:
        self.project.broadcaster.unsubscribe(self.subscriber)
        self.loop.close()


def assert_follower_missed_no_seq(follower: Follower, head: int, case: str) -> None:
    """Every committed seq after the follower connected reached it, in order, with
    no gap and no duplicate, through the final head, reconnecting after any
    ended stream (SPEC §8.9, AC-4)."""
    assert follower.delivered == list(range(follower.start + 1, head + 1)), case


# Every-boundary fault walk with real fsyncs: slow CI runners need more than the 15 s default.
@pytest.mark.timeout(120)
@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_every_boundary_leaves_the_operation_wholly_present_or_absent(
    scenario: Scenario,
    fresh: Fresh,
    projects: list[Project],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The counting pass: every boundary this operation crosses, in order.
    root, project, build = _prepared(fresh, projects, scenario)
    wire_publication(project)
    with monkeypatch.context() as m:
        counter = install(m, Injector())
        run(project, build())
    boundaries = counter.occurrences()
    points = {p for p, _ in boundaries}
    for required in (
        "undo.write",
        "undo.fsync",
        "board.mutation",
        "dir_fsync",
        "receipt.write",
        "receipt.fsync",
        "receipt.close",
        "journal.write",
        "journal.fsync",
        "journal.close",
        "finish.accept",
        "finish.memory",
        "finish.memory.manifest",
        "finish.index",
        "undo.close",
        "finish.undo_delete",
        "publication",
    ):
        assert required in points, (scenario.name, required)
    if scenario.name in ("task.archive", "task.unarchive"):
        assert "placement.source_event_removed" in points
    commit_at = boundaries.index(("journal.fsync", 1))

    for position, (point, occurrence) in enumerate(boundaries):
        case = f"{scenario.name} failing at {point} #{occurrence}"
        root, project, build = _prepared(fresh, projects, scenario)
        closed = wire_publication(project)
        before = state(root)
        write = build()
        error: BaseException | None = None
        stream = Follower(project)
        with monkeypatch.context() as m:
            injector = install(m, Injector(point, occurrence, short=point.endswith(".write")))
            try:
                run(project, write)
            except Exception as exc:  # noqa: BLE001 - inspected below
                error = exc
        assert injector.fired, case

        if position == commit_at or point.startswith("finish.memory"):
            # Durability unknown, or committed but memory could not be finalized
            # (H-10a: never left half-updated): quarantine, nothing more is written.
            assert isinstance(error, OpError) and error.code == "BOARD_UNAVAILABLE", case
            assert project.state == "unavailable", case
            after = state(root)
            with pytest.raises(OpError) as again:
                run(project, request("task.create", {"title": "blocked"}))
            assert again.value.code == "BOARD_UNAVAILABLE"
            assert state(root) == after, case
            stream.close()
            continue

        assert error is not None, case
        assert project.state == "loaded", case
        assert undo_logs(root) == [], case
        after = state(root)
        if position < commit_at:
            assert after == before, case  # wholly absent
            assert not isinstance(error, OpError) or error.code != "BOARD_UNAVAILABLE", case
        else:
            lines = journal_lines(root)  # wholly present: committed, never rolled back
            assert len(lines) == before["journal"].count(b"\n") + 1, case
            assert lines[-1]["op_id"] == write.caller.origin["op_id"], case
            assert (write.token_id, write.caller.origin["op_id"]) in project.index, case
            assert project.journal.head_seq == lines[-1]["seq"], case
            assert after["board"] != before["board"], case
            # A failed publication closes the streams so followers replay.
            assert closed == ([lines[-1]["seq"]] if point == "publication" else []), case
        # A follower whose stream a failed publication ended reconnects now.
        stream.reconnect_if_ended()
        assert stream.reconnects == (1 if point == "publication" else 0), case
        discover_task_authorities(board_of(root))
        doctor_clean(root)
        assert_next_write_commits_and_replays(root, project)
        # In the in-process branch a connected follower misses no seq (AC-4, H-22).
        assert_follower_missed_no_seq(stream, project.journal.head_seq, case)
        stream.close()


@pytest.mark.parametrize("name", ["task.archive", "task.unarchive"])
def test_placement_failing_right_after_the_source_log_unlink_rolls_back(
    name: str, fresh: Fresh, projects: list[Project], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, project, build = _prepared(fresh, projects, SCENARIO[name])
    before = state(root)
    with monkeypatch.context() as m:
        install(m, Injector("placement.source_event_removed"))
        with pytest.raises(InjectedFault):
            run(project, build())
    assert state(root) == before
    assert undo_logs(root) == []
    doctor_clean(root)
    run(project, build())  # the placement itself now commits
    doctor_clean(root)


# ---------------------------------------------------------------------------
# Quarantine: a failed recovery
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "recovery_point", ["recover.truncate", "recover.rollback", "recover.undo_delete"]
)
def test_a_failed_rollback_quarantines_the_project(
    recovery_point: str,
    fresh: Fresh,
    projects: list[Project],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, project, build = _prepared(fresh, projects, SCENARIOS[2])
    with monkeypatch.context() as m:
        install(m, Injector("receipt.fsync").also(recovery_point))
        with pytest.raises(OpError) as caught:
            run(project, build())
    assert caught.value.code == "BOARD_UNAVAILABLE"
    assert project.state == "unavailable"
    assert undo_logs(root)  # left for startup recovery (H-22)
    after = state(root)
    with pytest.raises(OpError) as again:
        run(project, request("task.create", {"title": "blocked"}))
    assert again.value.code == "BOARD_UNAVAILABLE"
    assert state(root) == after


def test_a_committed_operation_whose_finish_cannot_complete_quarantines_without_truncating(
    fresh: Fresh, projects: list[Project], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Journal.accept failure after the journal fsync never truncates: the line is
    committed on disk, so recovery finishes it or quarantines."""
    root, project, build = _prepared(fresh, projects, SCENARIOS[0])
    write = build()
    with monkeypatch.context() as m:
        install(m, Injector("finish.accept", sticky=True))
        with pytest.raises(OpError) as caught:
            run(project, write)
    assert caught.value.code == "BOARD_UNAVAILABLE"
    assert journal_lines(root)[-1]["op_id"] == write.caller.origin["op_id"]


def test_a_journal_close_failure_after_a_good_fsync_is_committed_not_quarantined(
    fresh: Fresh, projects: list[Project], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, project, build = _prepared(fresh, projects, SCENARIOS[1])
    write = build()
    with monkeypatch.context() as m:
        install(m, Injector("journal.close"))
        with pytest.raises(InjectedFault):
            run(project, write)  # SPEC §8.6: the error still reaches the caller
    assert project.state == "loaded"
    line = journal_lines(root)[-1]
    assert line["op_id"] == write.caller.origin["op_id"]
    assert project.journal.head_seq == line["seq"] and undo_logs(root) == []
    replay = run(project, write)
    assert replay.replayed and replay.seq == line["seq"]
    doctor_clean(root)


def test_a_one_off_accept_failure_is_finished_by_recovery(
    fresh: Fresh, projects: list[Project], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, project, build = _prepared(fresh, projects, SCENARIOS[0])
    write = build()
    with monkeypatch.context() as m:
        install(m, Injector("finish.accept"))
        with pytest.raises(InjectedFault):
            run(project, write)  # SPEC §8.6: the error still reaches the caller
    line = journal_lines(root)[-1]
    assert project.journal.head_seq == line["seq"]
    replay = run(project, write)
    assert replay.replayed and replay.seq == line["seq"]


# ---------------------------------------------------------------------------
# Receipts, publication, sessions, durability, server-started writes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("existing", [False, True], ids=["new-receipt-file", "existing"])
def test_rollback_restores_the_receipt_file_exactly(
    existing: bool,
    fresh: Fresh,
    projects: list[Project],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = fresh()
    project = load_project(root, SLUG)
    projects.append(project)
    if existing:
        create(project)
    receipts = board_of(root) / "hosted" / "receipts"
    before = sorted(p.name for p in receipts.glob("*")) if receipts.is_dir() else []
    assert bool(before) is existing
    snapshot = state(root)
    with monkeypatch.context() as m:
        install(m, Injector("receipt.write", short=True))
        with pytest.raises(InjectedFault):
            run(project, request("task.create", {"title": "x"}))
    assert sorted(p.name for p in receipts.glob("*")) == before
    assert state(root) == snapshot


def test_a_failed_publication_keeps_the_commit_and_closes_the_streams(
    fresh: Fresh, projects: list[Project]
) -> None:
    root, project, build = _prepared(fresh, projects, SCENARIOS[0])
    published: list[int] = []
    closed: list[bool] = []

    def publish(line: dict) -> None:
        if not published:
            published.append(-1)
            raise RuntimeError("broadcaster down")
        published.append(line["seq"])

    project.publish = publish
    project.close_streams = lambda: closed.append(True)
    write = build()
    with pytest.raises(RuntimeError):
        run(project, write)
    assert closed == [True]
    assert journal_lines(root)[-1]["op_id"] == write.caller.origin["op_id"]
    assert undo_logs(root) == []
    outcome = run(project, request("task.create", {"title": "after"}))
    assert published[-1] == outcome.seq  # later commits publish in seq order


def test_a_rejected_operation_leaves_the_session_it_touched_untouched(
    fresh: Fresh, projects: list[Project]
) -> None:
    root = fresh()
    project = load_project(root, SLUG)
    projects.append(project)
    task = create(project)["task"]["id"]
    started = run(
        project,
        request("session.start", {"model": "opus", "framework": "claude-code", "name": "Worker"}),
    )
    name = started.result_data["value"]["name"]
    session = board_of(root) / "sessions" / f"{name}.json"
    before = state(root)
    assert session.is_file()
    with pytest.raises(OpError) as caught:
        run(
            project,
            request("task.status", {"task": task, "new_status": "done"}, actor_name=name),
        )
    assert caught.value.code == "INVALID_TRANSITION"
    assert state(root) == before
    assert undo_logs(root) == []


def test_a_failed_directory_fsync_rolls_back_on_the_server_and_is_silent_locally(
    tmp_path: Path,
    fresh: Fresh,
    projects: list[Project],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Local mode: today's behavior exactly, a failed directory fsync is ignored.
    real_open = fs.os.open

    def no_dir_fsync(path, flags, *args):  # noqa: ANN001, ANN202
        if flags == fs.os.O_RDONLY and Path(path).is_dir():
            raise OSError(errno.EINVAL, "directory fsync unsupported")
        return real_open(path, flags, *args)

    fs.ensure_lattice_dirs(tmp_path / "local")
    target = tmp_path / "local" / ".lattice" / "context.md"
    with monkeypatch.context() as m:
        m.setattr(fs.os, "open", no_dir_fsync)
        fs.atomic_write(target, "local\n")
        with fs.strict_durability(), pytest.raises(OSError):
            fs.atomic_write(target, "strict\n")
    assert target.read_text() == "strict\n"  # the rename landed; only durability failed

    # Server: the same failure inside a transaction is a failed write, rolled back.
    root, project, build = _prepared(fresh, projects, SCENARIOS[1])
    before = state(root)
    with monkeypatch.context() as m:
        install(m, Injector("dir_fsync", 3))
        with pytest.raises(InjectedFault):
            run(project, build())
    assert state(root) == before


def test_server_control_writes_are_strictly_durable_outside_a_transaction(
    fresh: Fresh, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SPEC §8.6: a failed fsync of any server-control write propagates."""
    from lattice.storage.ownership import owning_board

    root = fresh()
    board = board_of(root)
    with monkeypatch.context() as m, owning_board(board):
        install(m, Injector("dir_fsync"))
        with pytest.raises(InjectedFault):
            fs.atomic_write(board / "hosted" / "owner.json", "{}\n")
    with monkeypatch.context() as m, owning_board(board):
        injector = install(m, Injector("dir_fsync"))
        (board / "hosted" / "control" / "x.json").parent.mkdir(exist_ok=True)
        (board / "hosted" / "control" / "x.json").write_text("{}")
        with pytest.raises(InjectedFault):
            fs.unlink_path(board / "hosted" / "control" / "x.json")
        assert injector.fired


def test_set_config_is_a_server_transaction(
    fresh: Fresh, projects: list[Project], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = fresh()
    project = load_project(root, SLUG)
    projects.append(project)
    config = board_of(root) / "config.json"
    before = state(root)
    with monkeypatch.context() as m:
        install(m, Injector("receipt.fsync"))
        with project.locked(), pytest.raises(InjectedFault):
            project.set_config({"review_mode": "inline"})
    assert state(root) == before

    seen: list[list[str]] = []
    undo = board_of(root) / "hosted" / "undo"

    def spy(point: str, **_ctx: object) -> None:
        if point == "board.mutation":
            seen.append(sorted(p.name for p in undo.iterdir()))

    with monkeypatch.context() as m:
        install(m, spy)  # type: ignore[arg-type]
        with project.locked():
            result = project.set_config({"review_mode": "inline"})
    assert json.loads(config.read_text())["review_mode"] == "inline"
    assert len(seen) == 1 and len(seen[0]) == 1 and seen[0][0].startswith("server--op_")
    line = journal_lines(root)[-1]
    assert line["seq"] == result["seq"] and line["token_id"] is None
    assert undo_logs(root) == []


def test_undo_log_reader_ignores_a_torn_final_line(tmp_path: Path) -> None:
    path = tmp_path / "u.jsonl"
    header = {"epoch": "ep_x", "token_id": "tok", "op_id": "op_x"}
    entry = {"path": "ids.json", "kind": "content", "existed": False, "content_b64": None}
    path.write_text(json.dumps(header) + "\n" + json.dumps(entry) + "\n" + '{"path": "ta')
    assert read_undo_log(path) == [entry]
    path.write_text(json.dumps(header) + "\n")
    assert read_undo_log(path) == []
