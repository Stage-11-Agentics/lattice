"""Hosted checkouts for the H-10c command tests.

:class:`RealHostedBoard` is the happy path: H-10a's real server
(:func:`lattice.server.testing.serve_board`) and a checkout bound to it the way
H-10b's own tests bind one (``.lattice-remote.json`` plus
``LATTICE_REMOTE_TEAM_*``), synced by H-10b's real ``catch_up`` and read under
its real ``read_lock``. Nothing is patched: the commands route to it because
the cache marker names its remote.

:class:`StubHostedBoard` is for forced failures the real server cannot be made
to give (malformed answers, a given status on every sync): the stream stub,
with the stub syncer patched in as ``cache.catch_up``.
"""

from __future__ import annotations

import contextlib
import json
import threading
from collections.abc import Iterator
from pathlib import Path

from click.testing import CliRunner

from lattice.cli.main import cli
from lattice.remote import cache
from lattice.server.testing import BoardServer, serve_board
from lattice.storage.fs import LATTICE_DIR
from tests.test_remote.conftest import bind
from tests.test_remote.stream_stub import TOKEN, StubServer, StubSyncer
from lattice.core.config import default_config, serialize_config
from lattice.storage.fs import atomic_write, ensure_lattice_dirs


class RealHostedBoard:
    def __init__(self, server: BoardServer, client: Path) -> None:
        self.server = server
        self.b = client

    def op(self, name: str, params: dict) -> dict:
        return self.server.op(name, params)

    def create(self, title: str) -> dict:
        return self.op("task.create", {"title": title})["task"]

    def status(self, task_id: str, new_status: str) -> dict:
        return self.op("task.status", {"task": task_id, "new_status": new_status})

    def server_events(self, task_id: str) -> list[dict]:
        """The task's events on the server, as local ``watch`` yields them."""
        path = self.server.board / "events" / f"{task_id}.jsonl"
        events = []
        for line in path.read_text("utf-8").splitlines():
            event = json.loads(line)
            event["task_id"] = task_id
            events.append(event)
        return events

    def cli(self, *args: str):
        return CliRunner().invoke(cli, list(args), env={"LATTICE_ROOT": str(self.b)})


@contextlib.contextmanager
def real_hosted_board(
    tmp_path: Path, monkeypatch, heartbeat: float = 0.2
) -> Iterator[RealHostedBoard]:
    with serve_board(tmp_path / "server", audit=False, heartbeat_seconds=heartbeat) as server:
        client = bind(tmp_path / "client", server.url, server.token, monkeypatch)
        outcome = cache.catch_up(client, bulk=True)
        assert outcome.kind == "applied", outcome
        yield RealHostedBoard(server, client)


def watch_while_the_server_changes(board: RealHostedBoard, *flags: str) -> tuple[str, list[dict]]:
    """Run ``lattice watch`` on the checkout while the server changes its task:
    (watch stdout, the events the server appended meanwhile)."""
    task_id = board.task["id"]
    before = len(board.server_events(task_id))

    failures: list[BaseException] = []

    def write() -> None:
        try:
            board.status(task_id, "in_planning")
            board.op("task.comment", {"task": task_id, "text": "hello from A"})
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            failures.append(exc)

    timer = threading.Timer(0.3, write)
    timer.start()
    try:
        result = board.cli("watch", "--timeout", "2", *flags)
    finally:
        timer.cancel()
    assert not failures, failures
    assert result.exit_code == 0, result.output
    return result.stdout, board.server_events(task_id)[before:]


def durable_files(root: Path) -> dict[str, bytes]:
    board = root / LATTICE_DIR
    out = {}
    for path in sorted(board.rglob("*")):
        rel = path.relative_to(board).as_posix()
        if path.is_file() and rel.split("/")[0] in {
            "tasks",
            "events",
            "archive",
            "plans",
            "notes",
            "artifacts",
            "resources",
            "sessions",
            "templates",
            "config.json",
            "ids.json",
            "context.md",
            ".gitignore",
        }:
            out[rel] = path.read_bytes()
    return out


class StubHostedBoard:
    """A real local board A published to the stream stub; checkout B bound to it
    (alias ``stub``), synced by the stub syncer patched in as ``cache.catch_up``."""

    def __init__(self, tmp_path: Path, stub: StubServer, monkeypatch) -> None:
        self.a = tmp_path / "a"
        self.b = tmp_path / "b"
        self.a.mkdir()
        self.b.mkdir()
        (self.b / LATTICE_DIR).mkdir()
        ensure_lattice_dirs(self.a)
        cfg = default_config()
        cfg["auto_code_review_on_transition"] = False
        cfg["auto_plan_review_on_transition"] = False
        atomic_write(self.a / LATTICE_DIR / "config.json", serialize_config(cfg))
        (self.a / LATTICE_DIR / "events" / "_lifecycle.jsonl").touch()
        self.stub = stub
        self.runner = CliRunner()
        self._published = durable_files(self.a)
        stub.files = dict(self._published)
        self.syncer = StubSyncer(stub.url)
        monkeypatch.setenv("LATTICE_REMOTE_STUB_URL", stub.url)
        monkeypatch.setenv("LATTICE_REMOTE_STUB_TOKEN", TOKEN)
        monkeypatch.setattr(cache, "catch_up", self.syncer)
        monkeypatch.setattr(
            cache, "read_lock", lambda root: contextlib.nullcontext(root / LATTICE_DIR)
        )

    def a_cli(self, *args: str) -> str:
        result = self.runner.invoke(cli, list(args), env={"LATTICE_ROOT": str(self.a)})
        assert result.exit_code == 0, result.output
        return result.output

    def publish(self) -> int:
        """Send A's changed files to the stub as one committed operation."""
        current = durable_files(self.a)
        changed = {p: c for p, c in current.items() if self._published.get(p) != c}
        self._published = current
        return self.stub.write(changed, [])

    def create(self, title: str) -> dict:
        out = self.a_cli("create", title, "--actor", "human:test", "--json")
        self.publish()
        return json.loads(out)["data"]

    def new_events(self, before: dict[str, bytes]) -> list[dict]:
        """The events A appended since *before*, as local watch would see them."""
        events = []
        for path, content in durable_files(self.a).items():
            if path.startswith("events/") and path.endswith(".jsonl"):
                old = before.get(path, b"")
                for line in content[len(old) :].decode().splitlines():
                    if line.strip():
                        event = json.loads(line)
                        event["task_id"] = Path(path).stem
                        events.append(event)
        return events
