"""A real board on the stub server, and a hosted checkout routed to it.

``A`` is a local board the tests write with the real CLI; after each write
:func:`publish` sends the files that changed to the stub as one journal entry,
as the server would after running the operation. ``B`` is the hosted
checkout: its cache is synced by :class:`StubSyncer`, and the H-11 routing
stubs (``hosted_root``, ``endpoint_for``) and H-10b stubs (``catch_up``,
``read_lock``) are patched to point at it.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

from click.testing import CliRunner

from lattice.cli.main import cli
from lattice.core.config import default_config, serialize_config
from lattice.remote import cache
from lattice.remote import endpoint as remote_endpoint
from lattice.storage.fs import LATTICE_DIR, atomic_write, ensure_lattice_dirs
from tests.test_remote.stream_stub import StubServer, StubSyncer, endpoint


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


class HostedBoard:
    def __init__(self, tmp_path: Path, stream_stub: StubServer, monkeypatch) -> None:
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
        self.stream_stub = stream_stub
        self.runner = CliRunner()
        self._published = durable_files(self.a)
        stream_stub.files = dict(self._published)
        self.syncer = StubSyncer(stream_stub.url)
        b = self.b.resolve()
        monkeypatch.setattr(
            remote_endpoint,
            "hosted_root",
            lambda start: b if Path(start).resolve() == b else None,
        )
        monkeypatch.setattr(
            remote_endpoint, "endpoint_for", lambda root: endpoint(stream_stub.url)
        )
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
        return self.stream_stub.write(changed, [])

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
