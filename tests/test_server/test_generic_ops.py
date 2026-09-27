"""G-11: an operation registered only in the test process, and one from a
``lattice.operations`` entry point of an installed distribution, run through the server
with no server change (H-9), and their events appear in sync and the stream (H-10a)."""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest

from lattice.ops import discovery
from lattice.server.testing import ServerHandle, open_stream
from tests.test_server.conftest import create_task, mint

PLUGIN_MODULE = """
from dataclasses import dataclass

from lattice.ops.base import CommonParams, OpContext, OpResult, operation
from lattice.storage.operations import TaskMutationDecision


@dataclass(frozen=True, kw_only=True)
class StampParams(CommonParams):
    task: str
    mark: str = "stamped"


@operation("xg11.stamp")
class Stamp:
    Params = StampParams

    def run(self, ctx: OpContext, p: StampParams) -> OpResult:
        task_id = ctx.resolve_task(p.task)
        ctx.require_active(task_id)

        def decide(_context):
            return TaskMutationDecision(
                events=[ctx.event("x_g11_stamp", task_id, {"mark": p.mark}, p)]
            )

        result = ctx.mutate(task_id, decide)
        return OpResult(task=result.snapshot, events=result.appended_events, value=p.mark)
"""


def _in_sync_and_stream(
    server: ServerHandle, token: str, op: str, params: dict, event_type: str
) -> None:
    """G-11 (H-10a part): the operation's events reach the stream and its log bytes
    reach a delta sync, with no server change."""
    _, _, before = server.request("GET", "/v1/projects/alpha/sync", token=token)
    head = before["data"]
    reader = open_stream(server.url, "alpha", token)
    try:
        assert reader.next().event == "heartbeat"
        status, _, body = server.op("alpha", op, params, token=token)
        assert status == 200, body
        event = body["data"]["result"]["events"][0]
        message = reader.next_of("journal")
    finally:
        reader.close()
    assert message.data["op"] == op
    assert [e["type"] for e in message.data["events"]] == [event_type]
    assert message.data["events"][0]["id"] == event["id"]
    query = f"since={head['head_seq']}&epoch={head['epoch']}&hash={head['head_hash']}"
    _, _, delta = server.request("GET", f"/v1/projects/alpha/sync?{query}", token=token)
    appended = [
        base64.b64decode(spec["content_b64"])
        for rel, spec in delta["data"]["files"].items()
        if rel.startswith("events/") and "append_from" in spec
    ]
    assert any(event["id"].encode() in chunk for chunk in appended)


def _journal(root: Path) -> list[dict]:
    path = root / "projects" / "alpha" / ".lattice" / "hosted" / "journal.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_a_runtime_registered_operation_runs_through_the_server(
    server: ServerHandle, root: Path
) -> None:
    token = mint(root)
    create_task(server, token)
    status, _, body = server.op("alpha", "xtest.note", {"task": "ALP-1", "note": "n"}, token=token)
    assert status == 200, body
    event = body["data"]["result"]["events"][0]
    assert event["type"] == "x_note" and event["origin"]["op"] == "xtest.note"
    assert _journal(root)[-1]["event_ids"] == [event["id"]]
    _in_sync_and_stream(server, token, "xtest.note", {"task": "ALP-1", "note": "m"}, "x_note")


@pytest.fixture()
def installed_plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    site = tmp_path / "site"
    site.mkdir()
    (site / "lattice_g11_plugin.py").write_text(PLUGIN_MODULE)
    dist = site / "lattice_g11_plugin-0.0.1.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: lattice-g11-plugin\nVersion: 0.0.1\n"
    )
    (dist / "entry_points.txt").write_text("[lattice.operations]\ng11 = lattice_g11_plugin\n")
    monkeypatch.syspath_prepend(str(site))
    monkeypatch.setattr(discovery, "_discovered", False)
    yield
    sys.modules.pop("lattice_g11_plugin", None)


def test_an_entry_point_operation_runs_through_the_server(
    installed_plugin: None, server: ServerHandle, root: Path
) -> None:
    token = mint(root)
    create_task(server, token)
    status, _, body = server.op("alpha", "xg11.stamp", {"task": "ALP-1"}, token=token)
    assert status == 200, body
    assert body["data"]["result"]["value"] == "stamped"
    assert body["data"]["result"]["events"][0]["type"] == "x_g11_stamp"
    _, _, info = server.request("GET", "/v1/info", token=token)
    assert info["data"]["ops"]["xg11.stamp"] == sorted(
        ["task", "mark", "model", "session", "triggered_by", "on_behalf_of", "reason"]
    )
    _in_sync_and_stream(server, token, "xg11.stamp", {"task": "ALP-1"}, "x_g11_stamp")
