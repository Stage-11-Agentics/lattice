"""AC-46 (H-22, client part): retries through the real client (SPEC §8.6, §9.5).

Every case runs the CLI in a bound checkout against an in-process server,
through :class:`Dropper`: an HTTP proxy that forwards an operation request,
lets the server commit it, and then drops the response (closes the
connection without answering), optionally after an intervening write by
another client. ``HostedBoard.execute`` retries with the same ``op_id``; the
server replays the stored result. The command must succeed, apply once, and
render what it would have rendered.

The kill-and-restart case (a real server process killed right after a
commit) is in ``tests/torture/test_client_restart.py`` (marker torture).
"""

from __future__ import annotations

import http.client
import json
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from lattice.remote import acked
from lattice.server import admin, tokens
from tests.test_remote.hosted import (
    PROJECT,
    HostedEnv,
    SpawnRecorder,
    make_repo,
    run_cli,
    walk_to,
)

HOP_BY_HOP = {"connection", "keep-alive", "transfer-encoding", "content-length"}


class Dropper:
    """Forwards everything to the server; drops the response of the next
    ``drops`` operation POSTs after they commit, running ``meanwhile`` first.

    A dropped response is kept in ``captured`` (by ``op_id``): it is exactly
    what the client should have received. With ``answer_captured`` set, an
    operation POST whose ``op_id`` was captured is answered with that original
    response and never reaches the server, so a second run renders the
    original result, for comparison with the replayed run.
    """

    def __init__(self, target: str) -> None:
        self.target = target.removeprefix("http://")
        self.drops = 0
        self.dropped: list[str] = []
        self.captured: dict[str, tuple[int, list[tuple[str, str]], bytes]] = {}
        self.answer_captured = False
        self.meanwhile: Callable[[], None] | None = None
        self.url = ""

    def forward(self, handler: BaseHTTPRequestHandler) -> None:
        length = int(handler.headers.get("Content-Length") or 0)
        body = handler.rfile.read(length) if length else None
        is_op = handler.command == "POST" and "/ops/" in handler.path
        op_id = json.loads(body or b"{}").get("op_id") if is_op else None
        if is_op and self.answer_captured and op_id in self.captured:
            self._answer(handler, *self.captured[op_id])
            return
        headers = {k: v for k, v in handler.headers.items() if k.lower() not in HOP_BY_HOP}
        conn = http.client.HTTPConnection(self.target, timeout=60)
        conn.request(handler.command, handler.path, body=body, headers=headers)
        response = conn.getresponse()
        data = response.read()
        status, response_headers = response.status, response.getheaders()
        conn.close()
        if is_op and self.drops > 0:
            self.drops -= 1
            self.dropped.append(op_id)
            self.captured.setdefault(op_id, (status, response_headers, data))
            if self.meanwhile is not None:
                self.meanwhile()
            handler.close_connection = True
            return  # committed on the server; the client never hears back
        self._answer(handler, status, response_headers, data)

    @staticmethod
    def _answer(
        handler: BaseHTTPRequestHandler, status: int, headers: list[tuple[str, str]], data: bytes
    ) -> None:
        handler.send_response(status)
        for name, value in headers:
            if name.lower() not in HOP_BY_HOP:
                handler.send_header(name, value)
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)


@contextmanager
def pinned_op_id(monkeypatch: pytest.MonkeyPatch, op_id: str) -> Iterator[None]:
    """The next operation the client sends uses *op_id*; later ones get fresh ids."""
    from lattice import boards
    from lattice.core.ids import generate_op_id

    pending = [op_id]
    with monkeypatch.context() as m:
        m.setattr(boards, "generate_op_id", lambda: pending.pop() if pending else generate_op_id())
        yield


@contextmanager
def dropping_proxy(env: HostedEnv) -> Iterator[Dropper]:
    dropper = Dropper(env.url)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_GET(self) -> None:  # noqa: N802
            dropper.forward(self)

        do_POST = do_GET  # noqa: N815

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, args=(0.02,), daemon=True)
    thread.start()
    dropper.url = f"http://127.0.0.1:{httpd.server_address[1]}"
    env.write_remote(url=dropper.url, retry_seconds=10)
    try:
        yield dropper
    finally:
        env.settings.pop("url", None)
        env.write_remote(retry_seconds=1)
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def journal(env: HostedEnv) -> list[dict]:
    raw = (env.board / "hosted" / "journal.jsonl").read_text()
    return [json.loads(line) for line in raw.splitlines()]


def other_client_write(env: HostedEnv) -> Callable[[], None]:
    """An intervening write by another client (its own token)."""
    other = tokens.create_token(
        env.server_root, user="human:bob", machine="m2", projects=[PROJECT]
    )

    def write() -> None:
        assert env.handle is not None
        status, _, body = env.handle.op(
            PROJECT, "task.create", {"title": "meanwhile"}, token=other["token"]
        )
        assert status == 200, body

    return write


@pytest.fixture()
def repo(hosted_env: HostedEnv, tmp_path: Path) -> Path:
    repo = make_repo(tmp_path / "repo")
    assert run_cli(repo, "remote", "attach", "team", PROJECT).exit_code == 0
    assert run_cli(repo, "create", "First", "--actor", "agent:dev").exit_code == 0
    return repo


def _replayed_once(env: HostedEnv, op_id: str) -> None:
    lines = journal(env)
    assert [x["op_id"] for x in lines].count(op_id) == 1
    assert env.handle is not None
    replays = [
        x
        for x in env.handle.log_lines
        if x.get("event") == "request" and x.get("op_id") == op_id and x.get("replayed")
    ]
    assert replays, "the retry was not answered from the stored result"


CASES: dict[str, Callable[[Path], list[str]]] = {
    "comment": lambda repo: [
        "comment",
        "DEM-1",
        "a lost-response comment",
        "--actor",
        "agent:dev",
    ],
    "session start": lambda repo: [
        "session",
        "start",
        "--model",
        "opus",
        "--framework",
        "claude-code",
        "--name",
        "Worker",
    ],
    "resource acquire": lambda repo: ["resource", "acquire", "db", "--actor", "agent:dev"],
    "no-op status": lambda repo: ["status", "DEM-1", "backlog", "--actor", "agent:dev"],
    "config": lambda repo: ["set-project-code", "DEQ", "--force"],
}


#: Every family plain and --json, except set-project-code, which has no --json.
VARIANTS = [
    pytest.param(family, as_json, id=f"{family}-{'json' if as_json else 'plain'}")
    for family in CASES
    for as_json in (False, True)
    if not (family == "config" and as_json)
]


def _acks(repo: Path, op_id: str) -> int:
    return sum(1 for x in acked.read(repo / ".lattice" / "cache") if x["op_id"] == op_id)


@pytest.mark.parametrize(("family", "as_json"), VARIANTS)
def test_a_lost_response_is_retried_replayed_and_rendered_as_the_original(
    family: str,
    as_json: bool,
    hosted_env: HostedEnv,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drop the response, write from another client, retry with the same op_id:
    replayed verbatim, applied once. The command's output equals what the
    original response renders (the captured response, answered to a second run
    under the same op_id), and the same client effects run (the ack)."""
    from lattice.core.ids import generate_op_id

    if family == "resource acquire":
        assert run_cli(repo, "resource", "create", "db", "--actor", "agent:dev").exit_code == 0
    args = CASES[family](repo) + (["--json"] if as_json else [])
    op_id = generate_op_id()
    with dropping_proxy(hosted_env) as dropper:
        dropper.drops = 1
        dropper.meanwhile = other_client_write(hosted_env)
        with pinned_op_id(monkeypatch, op_id):
            replayed = run_cli(repo, *args)
        assert replayed.exit_code == 0, replayed.output
        assert dropper.dropped == [op_id]
        _replayed_once(hosted_env, op_id)
        assert _acks(repo, op_id) == 1

        dropper.answer_captured = True  # what the lost response would have shown
        with pinned_op_id(monkeypatch, op_id):
            original = run_cli(repo, *args)
        assert original.exit_code == 0, original.output
    assert replayed.stdout == original.stdout
    if as_json:
        assert json.loads(replayed.stdout)["ok"] is True
    assert _acks(repo, op_id) == 2  # the client effect ran in both, once each
    assert [x["op_id"] for x in journal(hosted_env)].count(op_id) == 1


def test_a_replayed_create_renders_exactly_the_committed_result(
    hosted_env: HostedEnv, repo: Path
) -> None:
    with dropping_proxy(hosted_env) as dropper:
        dropper.drops = 1
        dropper.meanwhile = other_client_write(hosted_env)
        result = run_cli(repo, "create", "Lost response", "--actor", "agent:dev", "--json")
    assert result.exit_code == 0, result.output
    (op_id,) = dropper.dropped
    _replayed_once(hosted_env, op_id)
    shown = run_cli(repo, "remote", "op-status", op_id, "--json")
    stored = json.loads(shown.stdout)["data"]["result"]["task"]
    assert json.loads(result.stdout)["data"] == stored  # the stored result, verbatim


def test_a_status_that_records_an_auto_review_uses_two_op_ids(
    hosted_env: HostedEnv, repo: Path, spawns: SpawnRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The status's response is lost and replayed; its client effects (the review
    spawn and the auto-review record, under a second op_id) run as they would
    have after the original response, and the output is the original's."""
    from lattice.core.ids import generate_op_id

    admin.set_project_config(
        hosted_env.server_root, PROJECT, {"auto_code_review_on_transition": "true"}
    )
    walk_to(repo, "DEM-1", "in_planning", "planned", "in_progress")
    status_op = generate_op_id()
    args = ("status", "DEM-1", "review", "--actor", "agent:dev", "--json")
    before = spawns.review_types.count("code-review")
    records_before = len(journal(hosted_env))
    with dropping_proxy(hosted_env) as dropper:
        dropper.drops = 1
        with pinned_op_id(monkeypatch, status_op):
            replayed = run_cli(repo, *args)
        assert replayed.exit_code == 0, replayed.output
        _replayed_once(hosted_env, status_op)
        after_replay = journal(hosted_env)
        assert spawns.review_types.count("code-review") == before + 1

        dropper.answer_captured = True
        with pinned_op_id(monkeypatch, status_op):
            original = run_cli(repo, *args)
        assert original.exit_code == 0, original.output
    rendered = [json.loads(r.stdout)["data"] for r in (replayed, original)]
    for data in rendered:
        # Fields each run's own client effect sets: the review it spawned (its
        # pid, from the spawn stub, and its spawned_at) and the auto-review
        # record it then wrote (the task's last event and its updated_at). The
        # timestamps differ when the two runs straddle a second. Everything
        # from the status result matches.
        data["auto_review"].pop("pid")
        data["auto_review"].pop("spawned_at")
        data["next_steps"].pop("pid")
        data.pop("last_event_id")
        data.pop("updated_at")
    assert rendered[0] == rendered[1]
    assert spawns.review_types.count("code-review") == before + 2  # once per run
    tail = after_replay[-2:]
    assert [x["op"] for x in tail] == ["task.status", "task.record_auto_review"]
    assert tail[0]["op_id"] == status_op and tail[1]["op_id"] != status_op
    records = [
        x for x in journal(hosted_env)[records_before:] if x["op"] == "task.record_auto_review"
    ]
    assert len({x["op_id"] for x in records}) == len(records) == 2  # one per run, both applied


def test_outcome_unknown_names_the_op_and_op_status_finds_it(
    hosted_env: HostedEnv, repo: Path
) -> None:
    """The request is sent and committed, then the server goes down (in place of
    its answer) and stays down past retry_seconds: OUTCOME_UNKNOWN naming the
    op_id. With the server back, op-status reports it committed, once."""
    with dropping_proxy(hosted_env) as dropper:
        hosted_env.write_remote(url=dropper.url, retry_seconds=1)
        dropper.drops = 1
        dropper.meanwhile = hosted_env.stop  # down right after the commit
        result = run_cli(repo, "create", "Unknown", "--actor", "agent:dev", "--json")
        down_retries = len(dropper.dropped)
        hosted_env.start()  # back, on a new port (the proxy still points at the old one)
    assert result.exit_code == 1
    error = json.loads(result.stdout)["error"]
    assert error["code"] == "OUTCOME_UNKNOWN"
    (op_id,) = dropper.dropped
    assert down_retries == 1
    assert op_id in error["message"] and "lattice remote op-status" in error["message"]
    assert "retrying operation " + op_id in result.stderr  # it did retry, same op_id
    shown = run_cli(repo, "remote", "op-status", op_id, "--json")
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.stdout)["data"]["state"] == "committed"
    assert [x["op_id"] for x in journal(hosted_env)].count(op_id) == 1
    # Never acknowledged, so not in the ledger verify checks.
    assert op_id not in {x["op_id"] for x in acked.read(repo / ".lattice" / "cache")}


def test_remote_verify_confirms_every_acknowledged_write(
    hosted_env: HostedEnv, repo: Path
) -> None:
    assert run_cli(repo, "comment", "DEM-1", "hello", "--actor", "agent:dev").exit_code == 0
    lines = acked.read(repo / ".lattice" / "cache")
    assert len(lines) == 2  # the create and the comment
    assert all(x["epoch"] and isinstance(x["seq"], int) for x in lines)
    plain = run_cli(repo, "remote", "verify")
    assert plain.exit_code == 0, plain.output
    assert "2 acknowledged write(s) checked; the server holds all." in plain.stdout
    as_json = run_cli(repo, "remote", "verify", "--json")
    data = json.loads(as_json.stdout)["data"]
    assert data == {"checked": 2, "confirmed": 2, "dropped": 0, "missing": []}
    assert all("confirmed_at" in x for x in acked.read(repo / ".lattice" / "cache"))


def test_remote_verify_drops_lines_older_than_90_days(
    hosted_env: HostedEnv, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import timedelta

    later = acked._now() + timedelta(days=91)
    monkeypatch.setattr(acked, "_now", lambda: later)
    data = json.loads(run_cli(repo, "remote", "verify", "--json").stdout)["data"]
    assert data == {"checked": 0, "confirmed": 0, "dropped": 1, "missing": []}
    assert acked.read(repo / ".lattice" / "cache") == []


def test_remote_verify_unreachable_changes_nothing(hosted_env: HostedEnv, repo: Path) -> None:
    path = repo / ".lattice" / "cache" / acked.ACKED_FILE
    before = path.read_bytes()
    with hosted_env.stopped():
        result = run_cli(repo, "remote", "verify", "--json")
        assert result.exit_code == 1
        assert json.loads(result.stdout)["error"]["code"] == "SERVER_UNREACHABLE"
    assert path.read_bytes() == before


def test_a_torn_acked_line_is_skipped(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    acked.record(cache, op_id="op_01J9Z0000000000000000000AA", project="p", epoch="ep_x", seq=1)
    with open(cache / acked.ACKED_FILE, "ab") as fh:
        fh.write(b'{"op_id": "op_torn')
    assert [x["op_id"] for x in acked.read(cache)] == ["op_01J9Z0000000000000000000AA"]


# ---------------------------------------------------------------------------
# The ledger itself (review round 2, push 2)
# ---------------------------------------------------------------------------


def test_the_ack_is_recorded_before_the_post_write_sync(
    hosted_env: HostedEnv, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A client that dies during the post-write sync has already recorded the
    acknowledged write, so verify still checks it."""
    from lattice.remote import session

    class Died(BaseException):
        pass

    def die(*_args: Any, **_kwargs: Any) -> bool:
        raise Died("killed during the post-write sync")

    before = len(acked.read(repo / ".lattice" / "cache"))
    with monkeypatch.context() as m:
        m.setattr(session, "catch_up_and_report", die)
        with pytest.raises(Died):
            run_cli(repo, "create", "Dies syncing", "--actor", "agent:dev")
    lines = acked.read(repo / ".lattice" / "cache")
    assert len(lines) == before + 1
    assert lines[-1]["op_id"] == journal(hosted_env)[-1]["op_id"]


def test_a_record_after_a_torn_tail_keeps_both_writes(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    first, second = "op_01J9Z0000000000000000000AA", "op_01J9Z0000000000000000000BB"
    acked.record(cache, op_id=first, project="p", epoch="ep_x", seq=1)
    with open(cache / acked.ACKED_FILE, "ab") as fh:
        fh.write(b'{"op_id":"op_torn')  # a client killed mid-append
    acked.record(cache, op_id=second, project="p", epoch="ep_x", seq=2)
    assert [x["op_id"] for x in acked.read(cache)] == [first, second]
    report = acked.verify(cache, lambda _op: {"state": "committed"})
    assert report.checked == report.confirmed == 2


def test_a_torn_only_ledger_is_cut_before_the_next_record(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / acked.ACKED_FILE).write_bytes(b'{"op_id":"op_torn')
    acked.record(cache, op_id="op_01J9Z0000000000000000000CC", project="p", epoch=None, seq=3)
    assert [x["op_id"] for x in acked.read(cache)] == ["op_01J9Z0000000000000000000CC"]


def test_an_odd_cache_state_never_fails_a_write(
    hosted_env: HostedEnv, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from lattice.boards import resolve_board

    board = resolve_board(repo)
    state = repo / ".lattice" / "cache" / "state.json"
    original = state.read_bytes()
    mode = state.stat().st_mode
    state.chmod(0o600)
    try:
        state.write_text("[]\n")  # valid JSON, not an object
        board._record_ack("op_01J9Z0000000000000000000DD", 7)
    finally:
        state.write_bytes(original)
        state.chmod(mode & 0o777)
    line = acked.read(repo / ".lattice" / "cache")[-1]
    assert line["op_id"] == "op_01J9Z0000000000000000000DD" and line["epoch"] is None
    assert capsys.readouterr().err == ""


def test_a_failing_ledger_is_one_warning_line_and_the_write_succeeds(
    hosted_env: HostedEnv, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_args: Any, **_kwargs: Any) -> None:
        raise ValueError("ledger unusable")

    monkeypatch.setattr(acked, "record", broken)
    result = run_cli(repo, "create", "Still written", "--actor", "agent:dev", "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["data"]["title"] == "Still written"
    warnings = [x for x in result.stderr.splitlines() if "cache/acked.jsonl" in x]
    assert len(warnings) == 1 and "lattice remote verify will not check it" in warnings[0]


def test_verify_asks_op_status_once_per_ledger_line(tmp_path: Path) -> None:
    """Two lines for one op_id (a replayed write acknowledged twice) are two
    lookups, each line judged on its own."""
    cache = tmp_path / "cache"
    cache.mkdir()
    op = "op_01J9Z0000000000000000000EE"
    acked.record(cache, op_id=op, project="p", epoch="ep_x", seq=1)
    acked.record(cache, op_id=op, project="p", epoch="ep_x", seq=1)
    acked.record(cache, op_id="op_01J9Z0000000000000000000FF", project="p", epoch=None, seq=2)
    asked: list[str] = []

    def status(op_id: str) -> dict:
        asked.append(op_id)
        return {"state": "committed"}

    report = acked.verify(cache, status)
    assert asked == [op, op, "op_01J9Z0000000000000000000FF"]
    assert report.checked == report.confirmed == 3


# ---------------------------------------------------------------------------
# The first write from a checkout that has never synced (LAT-335)
# ---------------------------------------------------------------------------


def _assert_bootstrapped_and_clean(repo: Path) -> None:
    lattice_dir = repo / ".lattice"
    state = json.loads((lattice_dir / "cache" / "state.json").read_text())
    assert state["epoch"] and not (lattice_dir / "cache" / "applying").exists()
    for directory in (lattice_dir, lattice_dir / "cache"):
        assert directory.stat().st_mode & 0o777 == 0o700
    doctor = run_cli(repo, "doctor", "--json")
    assert doctor.exit_code == 0, doctor.output
    findings = json.loads(doctor.stdout)["data"]["findings"]
    assert [f for f in findings if f["check"].startswith("cache_")] == []


def test_the_first_write_from_an_unsynced_checkout_is_recorded(
    hosted_env: HostedEnv, tmp_path: Path
) -> None:
    """A fresh clone's first command is a write (no read catches the cache up
    first): the ledger creates the cache directory, so the ack is recorded
    before the post-write sync bootstraps the cache."""
    repo = hosted_env.bind(make_repo(tmp_path / "fresh"))
    assert not (repo / ".lattice").exists()
    result = run_cli(repo, "create", "First", "--actor", "agent:dev")
    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    lines = acked.read(repo / ".lattice" / "cache")
    assert [x["op_id"] for x in lines] == [journal(hosted_env)[-1]["op_id"]]
    assert lines[0]["epoch"] is None  # acknowledged before any sync
    _assert_bootstrapped_and_clean(repo)
    data = json.loads(run_cli(repo, "remote", "verify", "--json").stdout)["data"]
    assert data == {"checked": 1, "confirmed": 1, "dropped": 0, "missing": []}


def test_a_ledger_only_cache_is_still_bootstrapped_by_the_next_command(
    hosted_env: HostedEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A client that dies in the post-write sync of its first write leaves a
    ``.lattice/`` holding only the ledger: not a cache marker, not board data.
    The next command bootstraps the cache with a reset, as for a fresh clone."""
    repo = hosted_env.bind(make_repo(tmp_path / "fresh"))
    _first_write_dies_syncing(hosted_env, repo, monkeypatch)
    lattice_dir = repo / ".lattice"
    left = sorted(p.relative_to(lattice_dir).as_posix() for p in lattice_dir.rglob("*"))
    assert left == ["cache", "cache/acked.jsonl", "cache/acked.lock"]
    _assert_next_command_bootstraps(repo)


def test_runtime_leftovers_are_made_private_by_the_first_write(
    hosted_env: HostedEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A teammate's clone after the move keeps ignored runtime leftovers in a
    0755 ``.lattice/`` (SPEC §9.3). A first write that dies in its post-write
    sync still leaves ``.lattice/`` and ``cache/`` 0700 (SPEC §9.4)."""
    repo = hosted_env.bind(make_repo(tmp_path / "fresh"))
    lattice_dir = repo / ".lattice"
    for leftover in (lattice_dir, lattice_dir / "locks", lattice_dir / ".daemon"):
        leftover.mkdir()
        leftover.chmod(0o755)
    (lattice_dir / ".daemon" / "dashboard.log").write_text("old\n")
    _first_write_dies_syncing(hosted_env, repo, monkeypatch)
    for directory in (lattice_dir, lattice_dir / "cache"):
        assert directory.stat().st_mode & 0o777 == 0o700
    _assert_next_command_bootstraps(repo)


def _first_write_dies_syncing(env: HostedEnv, repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``create`` as the checkout's first command, killed in its post-write
    sync; the acknowledged write is in the ledger all the same."""
    from lattice.remote import session

    class Died(BaseException):
        pass

    def die(*_args: Any, **_kwargs: Any) -> bool:
        raise Died("killed during the post-write sync")

    with monkeypatch.context() as m:
        m.setattr(session, "catch_up_and_report", die)
        with pytest.raises(Died):
            run_cli(repo, "create", "Dies syncing", "--actor", "agent:dev")
    lines = acked.read(repo / ".lattice" / "cache")
    assert [x["op_id"] for x in lines] == [journal(env)[-1]["op_id"]]
    session.reset_process_state()


def _assert_next_command_bootstraps(repo: Path) -> None:
    listed = run_cli(repo, "list", "--json")
    assert listed.exit_code == 0, listed.output
    assert [t["title"] for t in json.loads(listed.stdout)["data"]] == ["Dies syncing"]
    _assert_bootstrapped_and_clean(repo)
    data = json.loads(run_cli(repo, "remote", "verify", "--json").stdout)["data"]
    assert data == {"checked": 1, "confirmed": 1, "dropped": 0, "missing": []}


def test_the_ledger_creates_missing_directories_private(tmp_path: Path) -> None:
    cache = tmp_path / "checkout" / ".lattice" / "cache"
    (tmp_path / "checkout").mkdir()
    acked.record(cache, op_id="op_01J9Z0000000000000000000GG", project="p", epoch=None, seq=1)
    assert [x["op_id"] for x in acked.read(cache)] == ["op_01J9Z0000000000000000000GG"]
    for directory in (cache.parent, cache):
        assert directory.stat().st_mode & 0o777 == 0o700
        directory.chmod(0o755)
    acked.record(cache, op_id="op_01J9Z0000000000000000000HH", project="p", epoch=None, seq=2)
    for directory in (cache.parent, cache):
        assert directory.stat().st_mode & 0o777 == 0o700
