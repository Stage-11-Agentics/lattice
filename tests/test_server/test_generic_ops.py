"""G-11 (server part): an operation registered only in the test process, and one from a
``lattice.operations`` entry point of an installed distribution, run through the server
with no server change."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from lattice.ops import discovery
from lattice.server.testing import ServerHandle
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
