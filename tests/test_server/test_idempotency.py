"""AC-46 (server level): a retried write applies once; a reused op_id is refused (SPEC §8.6).

Over raw HTTP against a server on ``127.0.0.1:0``. A lost response is a
request whose connection the client closes before reading the answer; the
server applies it anyway.
"""

from __future__ import annotations

import http.client
import json
from pathlib import Path

import pytest

from lattice.core.ids import generate_op_id
from lattice.server.testing import ServerHandle, running_server, wait_for
from tests.test_server.conftest import mint

SLUG = "alpha"


def _journal(root: Path) -> list[dict]:
    raw = (root / "projects" / SLUG / ".lattice" / "hosted" / "journal.jsonl").read_bytes()
    return [json.loads(x) for x in raw.splitlines()]


def drop_response(server: ServerHandle, op: str, body: dict, token: str) -> None:
    """Send an op request and hang up without reading the answer (a lost response)."""
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    payload = json.dumps(body).encode()
    conn.request(
        "POST",
        f"/v1/projects/{SLUG}/ops/{op}",
        body=payload,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    conn.close()


def op_status(server: ServerHandle, token: str, op_id: str) -> tuple[int, dict]:
    status, _, body = server.request("GET", f"/v1/projects/{SLUG}/ops/{op_id}", token=token)
    return status, body


def test_a_lost_response_retried_with_its_op_id_replays_verbatim(root: Path) -> None:
    alice = mint(root)
    bob = mint(root, user="human:bob")
    with running_server(root) as server:
        create_id, comment_id = generate_op_id(), generate_op_id()
        create_body = {"op_id": create_id, "params": {"title": "lost"}}
        drop_response(server, "task.create", create_body, alice)
        assert wait_for(lambda: any(x["op_id"] == create_id for x in _journal(root)))
        # An intervening write by another token.
        status, _, _ = server.op(SLUG, "task.create", {"title": "bob's"}, token=bob)
        assert status == 200

        status, _, body = server.op(
            SLUG, "task.create", {"title": "lost"}, token=alice, op_id=create_id
        )
        assert status == 200, body
        replay = body["data"]
        assert replay["op_id"] == create_id and replay["result"]["replayed"] is True
        _, committed = op_status(server, alice, create_id)
        stored = committed["data"]
        assert stored["state"] == "committed" and stored["seq"] == replay["seq"] == 1
        assert replay["result"] == {**stored["result"], "replayed": True}
        task = replay["result"]["task"]["id"]

        comment_body = {"op_id": comment_id, "params": {"task": task, "text": "once"}}
        drop_response(server, "task.comment", comment_body, alice)
        assert wait_for(lambda: any(x["op_id"] == comment_id for x in _journal(root)))
        server.op(SLUG, "task.create", {"title": "bob again"}, token=bob)
        status, _, body = server.op(
            SLUG, "task.comment", {"task": task, "text": "once"}, token=alice, op_id=comment_id
        )
        assert status == 200 and body["data"]["result"]["replayed"] is True

        # Applied exactly once each.
        lines = _journal(root)
        assert [x["op_id"] for x in lines].count(create_id) == 1
        assert [x["op_id"] for x in lines].count(comment_id) == 1
        _, _, shown = server.request("GET", f"/v1/projects/{SLUG}/tasks/{task}", token=alice)
        comments = [e for e in shown["data"]["events"] if e["type"] == "comment_added"]
        assert len(comments) == 1
        _, _, listed = server.request("GET", f"/v1/projects/{SLUG}/tasks", token=alice)
        assert [t["title"] for t in listed["data"]["tasks"]].count("lost") == 1


def test_an_op_id_reused_with_different_arguments_is_refused_exactly(root: Path) -> None:
    alice = mint(root)
    with running_server(root) as server:
        op_id = generate_op_id()
        status, _, _ = server.op(SLUG, "task.create", {"title": "a"}, token=alice, op_id=op_id)
        assert status == 200
        before = _journal(root)
        status, _, body = server.op(SLUG, "task.create", {"title": "b"}, token=alice, op_id=op_id)
        assert status == 409
        assert body == {
            "ok": False,
            "error": {
                "code": "CONFLICT",
                "message": f"operation id {op_id} was already used with different arguments",
                "details": {"reason": "OP_ID_REUSED", "seq": 1},
            },
        }
        # A different actor is a different request too.
        status, _, body = server.op(
            SLUG, "task.create", {"title": "a"}, token=alice, op_id=op_id, actor="agent:x"
        )
        assert status == 409 and body["error"]["details"]["reason"] == "OP_ID_REUSED"
        assert _journal(root) == before


def test_the_same_op_id_from_another_token_is_a_separate_operation(root: Path) -> None:
    alice = mint(root)
    bob = mint(root, user="human:bob")
    with running_server(root) as server:
        op_id = generate_op_id()
        _, _, first = server.op(SLUG, "task.create", {"title": "same"}, token=alice, op_id=op_id)
        _, _, second = server.op(SLUG, "task.create", {"title": "same"}, token=bob, op_id=op_id)
        a, b = first["data"], second["data"]
        assert a["result"]["replayed"] is False and b["result"]["replayed"] is False
        assert a["result"]["task"]["id"] != b["result"]["task"]["id"]
        assert b["result"]["task"]["created_by"] == "human:bob"
        assert (a["seq"], b["seq"]) == (1, 2)
        # Each token sees only its own operation under that op_id.
        assert op_status(server, alice, op_id)[1]["data"]["seq"] == 1
        assert op_status(server, bob, op_id)[1]["data"]["seq"] == 2


def test_a_request_without_an_op_id_is_minted_one_and_never_deduplicated(root: Path) -> None:
    alice = mint(root)
    with running_server(root) as server:
        _, _, first = server.op(SLUG, "task.create", {"title": "again"}, token=alice)
        _, _, second = server.op(SLUG, "task.create", {"title": "again"}, token=alice)
        a, b = first["data"], second["data"]
        assert a["op_id"].startswith("op_") and b["op_id"].startswith("op_")
        assert a["op_id"] != b["op_id"]
        assert a["result"]["task"]["id"] != b["result"]["task"]["id"]
        assert not a["result"]["replayed"] and not b["result"]["replayed"]
        assert [x["op_id"] for x in _journal(root)] == [a["op_id"], b["op_id"]]
        assert op_status(server, alice, a["op_id"])[1]["data"]["seq"] == 1


def test_op_status_is_scoped_to_the_callers_token(root: Path) -> None:
    alice = mint(root)
    bob = mint(root, user="human:bob")
    with running_server(root) as server:
        op_id = generate_op_id()
        _, _, body = server.op(SLUG, "task.create", {"title": "mine"}, token=alice, op_id=op_id)
        status, mine = op_status(server, alice, op_id)
        assert status == 200
        assert mine["data"] == {
            "state": "committed",
            "epoch": _journal_epoch(root),
            "seq": 1,
            "result": body["data"]["result"],
        }
        assert op_status(server, bob, op_id) == (200, {"ok": True, "data": {"state": "not_found"}})
        never = generate_op_id()
        assert op_status(server, alice, never)[1]["data"] == {"state": "not_found"}
        status, _, bad = server.request("GET", f"/v1/projects/{SLUG}/ops/op_../../x", token=alice)
        assert status in (400, 404) and bad["ok"] is False
        status, _, bad = server.request("GET", f"/v1/projects/{SLUG}/ops/not-an-op", token=alice)
        assert status == 400 and bad["error"]["code"] == "VALIDATION_ERROR"
        status, _, _ = server.request("GET", f"/v1/projects/{SLUG}/ops/{op_id}")
        assert status == 401


def test_op_status_finds_an_operation_from_a_retained_epoch(root: Path) -> None:
    alice = mint(root)
    op_id = generate_op_id()
    with running_server(root) as server:
        server.op(SLUG, "task.create", {"title": "old epoch"}, token=alice, op_id=op_id)
    old_epoch = _journal_epoch(root)
    hosted = root / "projects" / SLUG / ".lattice" / "hosted"
    # An offline maintenance run rotates the epoch at the next load (SPEC §8.7 step 5).
    (hosted / "maintenance.json").write_text('{"command": "doctor --fix"}\n')
    with running_server(root) as server:
        assert _journal_epoch(root) != old_epoch
        status, body = op_status(server, alice, op_id)
        assert status == 200
        data = body["data"]
        # The index is rebuilt from the retained receipt at load (H-22), so the
        # result comes along (SPEC §8.6 "Op status").
        assert data.pop("result")["task"]["title"] == "old epoch"
        assert data == {"state": "committed", "epoch": old_epoch, "seq": 1}


def _journal_epoch(root: Path) -> str:
    meta = root / "projects" / SLUG / ".lattice" / "hosted" / "journal_meta.json"
    return json.loads(meta.read_text())["epoch"]


def test_op_status_sees_a_commit_before_the_finish_step_completes(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SPEC §8.6: a complete, fsynced journal line is committed. Op status reads
    without the work lock, so it must see the commit while finish is still running."""
    import threading

    import lattice.server.transactions as transactions

    alice = mint(root)
    bob = mint(root, user="human:bob")
    reached, release = threading.Event(), threading.Event()

    def pause(point: str, **_ctx: object) -> None:
        if point == "finish.index" and not reached.is_set():
            reached.set()
            assert release.wait(10)

    monkeypatch.setattr(transactions, "_fault", pause)
    op_id = generate_op_id()
    with running_server(root) as server:
        answers: dict = {}
        writer = threading.Thread(
            target=lambda: answers.update(
                write=server.op(SLUG, "task.create", {"title": "slow"}, token=alice, op_id=op_id)
            )
        )
        writer.start()
        try:
            assert reached.wait(10)
            mine = op_status(server, alice, op_id)
            theirs = op_status(server, bob, op_id)
        finally:
            release.set()
            writer.join(10)
        assert mine[0] == 200 and mine[1]["data"]["state"] == "committed"
        assert mine[1]["data"]["seq"] == 1
        assert theirs[1]["data"] == {"state": "not_found"}
        assert answers["write"][0] == 200
        assert op_status(server, alice, op_id)[1]["data"]["result"]["task"]["title"] == "slow"
