"""The dashboard writes through operations (H-13a: AC-24 local, AC-36, AC-38).

Every POST runs the CLI's operation for the same change, so the CLI's rules
apply (plan gate, completion policy) and a refusal names the CLI's override;
POSTs must be same-origin JSON; every dashboard write carries a browser
origin; boards leave erased tasks out; event views carry the origin line
``show --events`` prints.
"""

from __future__ import annotations

import http.client
import json
import re
import threading
from pathlib import Path

import pytest

from lattice.boards import LocalBoard
from lattice.core.errors import OpError
from lattice.dashboard import api
from lattice.dashboard.server import create_server, origin_allowed
from lattice.ops import Caller

OP_ID = re.compile(r"^op_[0-9A-HJKMNP-TV-Z]{26}$")


@pytest.fixture()
def dash(populated_lattice_dir: tuple[Path, dict[str, str]]):
    """A dashboard on 127.0.0.1:0: ``(port, lattice_dir, task_ids)``."""
    ld, ids = populated_lattice_dir
    server = create_server(ld, "127.0.0.1", 0)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        yield server.server_address[1], ld, ids
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def request(
    port: int,
    method: str,
    path: str,
    body: object = None,
    *,
    origin: str | None = "same",
    host: str | None = None,
    content_type: str = "application/json",
) -> tuple[int, dict]:
    """One request; ``origin="same"`` sends the dashboard's own origin."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    host_header = host or f"127.0.0.1:{port}"
    headers = {"Host": host_header}
    if origin == "same":
        headers["Origin"] = f"http://{host_header}"
    elif origin is not None:
        headers["Origin"] = origin
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = content_type
    conn.request(method, path, body=data, headers=headers)
    resp = conn.getresponse()
    payload = json.loads(resp.read().decode() or "null")
    conn.close()
    return resp.status, payload


def post(port: int, path: str, body: object, **kwargs: object) -> tuple[int, dict]:
    return request(port, "POST", path, body, **kwargs)


def get(port: int, path: str) -> tuple[int, dict]:
    return request(port, "GET", path, origin=None)


def events_of(ld: Path, task_id: str) -> list[dict]:
    path = ld / "events" / f"{task_id}.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def board_bytes(ld: Path) -> dict[str, bytes]:
    return {
        p.relative_to(ld).as_posix(): p.read_bytes()
        for p in ld.rglob("*")
        if p.is_file() and "locks" not in p.parts
    }


# ---------------------------------------------------------------------------
# The CLI's rules, and the CLI's escape
# ---------------------------------------------------------------------------


class TestRules:
    def test_plan_gate_refuses_a_drag_and_names_the_cli_escape(self, dash):
        port, ld, ids = dash
        task = ids["backlog"]
        for step in ("in_planning", "planned"):
            status, _ = post(port, f"/api/tasks/{task}/status", {"status": step})
            assert status == 200
        before = board_bytes(ld)

        status, body = post(port, f"/api/tasks/{task}/status", {"status": "in_progress"})

        assert status == 422
        assert body["error"]["code"] == "PLAN_REQUIRED"
        message = body["error"]["message"]
        assert f'lattice status {task} in_progress --force --reason "..."' in message
        assert body["error"]["details"]["snapshot"]["status"] == "planned"
        assert board_bytes(ld) == before

    def test_permitted_move_after_the_plan_is_written(self, dash):
        port, ld, ids = dash
        task = ids["backlog"]
        for step in ("in_planning", "planned"):
            post(port, f"/api/tasks/{task}/status", {"status": step})
        (ld / "plans" / f"{task}.md").write_text("# Plan\n\nOne real line.\n")

        status, body = post(port, f"/api/tasks/{task}/status", {"status": "in_progress"})

        assert status == 200
        assert body["data"]["status"] == "in_progress"

    def test_completion_policy_applies(self, dash):
        port, _ld, ids = dash
        status, body = post(port, f"/api/tasks/{ids['in_progress']}/status", {"status": "done"})
        assert status == 422
        assert body["error"]["code"] in {"INVALID_TRANSITION", "COMPLETION_BLOCKED"}
        assert "--force --reason" in body["error"]["message"]

    def test_non_status_refusal_has_no_escape(self, dash):
        port, _ld, ids = dash
        status, body = post(port, f"/api/tasks/{ids['backlog']}/comment", {"body": "  "})
        assert status == 400
        assert "lattice status" not in body["error"]["message"]


# ---------------------------------------------------------------------------
# Same-origin JSON only
# ---------------------------------------------------------------------------


class TestRequestChecks:
    @pytest.mark.parametrize(
        "origin",
        [
            "http://evil.example",
            None,
            "null",
            "http://127.0.0.1:1",
            "https://127.0.0.1",
        ],
    )
    def test_foreign_or_missing_origin_is_refused(self, dash, origin):
        port, ld, ids = dash
        before = board_bytes(ld)
        status, body = post(
            port, f"/api/tasks/{ids['backlog']}/comment", {"body": "hi"}, origin=origin
        )
        assert status == 403
        assert body["error"]["code"] == "FORBIDDEN"
        assert board_bytes(ld) == before

    def test_rebound_hostname_is_refused(self, dash):
        """A hostile name pointed at 127.0.0.1 (DNS rebinding) is not this dashboard."""
        port, ld, ids = dash
        before = board_bytes(ld)
        status, body = post(
            port,
            f"/api/tasks/{ids['backlog']}/comment",
            {"body": "hi"},
            host=f"evil.example:{port}",
        )
        assert status == 403
        assert board_bytes(ld) == before

    def test_localhost_name_is_accepted(self, dash):
        port, _ld, ids = dash
        status, _ = post(
            port, f"/api/tasks/{ids['backlog']}/comment", {"body": "hi"}, host=f"localhost:{port}"
        )
        assert status == 200

    @pytest.mark.parametrize(
        "content_type", ["text/plain", "application/x-www-form-urlencoded", "multipart/form-data"]
    )
    def test_non_json_content_type_is_refused(self, dash, content_type):
        port, ld, ids = dash
        before = board_bytes(ld)
        status, body = post(
            port,
            f"/api/tasks/{ids['backlog']}/comment",
            {"body": "hi"},
            content_type=content_type,
        )
        assert status == 415
        assert body["error"]["code"] == "VALIDATION_ERROR"
        assert board_bytes(ld) == before

    def test_open_plans_is_guarded_too(self, dash):
        """open-notes / open-plans spawn a process, so a foreign page cannot trigger them."""
        port, _ld, ids = dash
        status, _ = post(
            port, f"/api/tasks/{ids['backlog']}/open-plans", {}, origin="http://evil.example"
        )
        assert status == 403

    def test_non_object_body_is_refused(self, dash):
        port, _ld, ids = dash
        status, body = post(port, f"/api/tasks/{ids['backlog']}/comment", ["x"])
        assert status == 400
        assert body["error"]["code"] == "VALIDATION_ERROR"

    @pytest.mark.parametrize(
        ("origin", "host", "bound", "allowed"),
        [
            ("http://127.0.0.1:8799", "127.0.0.1:8799", "127.0.0.1", True),
            ("http://localhost:8799", "localhost:8799", "127.0.0.1", True),
            ("http://[::1]:8799", "[::1]:8799", "::1", True),
            ("http://evil.example:8799", "evil.example:8799", "127.0.0.1", False),
            ("http://127.0.0.1:8799", "127.0.0.1:8800", "127.0.0.1", False),
            (None, "127.0.0.1:8799", "127.0.0.1", False),
            ("http://127.0.0.1:8799", None, "127.0.0.1", False),
            ("http://box.lan:8799", "box.lan:8799", "0.0.0.0", True),
        ],
    )
    def test_origin_allowed(self, origin, host, bound, allowed):
        assert origin_allowed(origin, host, bound) is allowed


# ---------------------------------------------------------------------------
# Every dashboard write is an operation with a browser origin (AC-36)
# ---------------------------------------------------------------------------


def _assert_browser_origin(event: dict, op: str) -> None:
    origin = event["origin"]
    assert origin["op"] == op
    assert OP_ID.match(origin["op_id"])
    reported = origin["reported"]
    assert reported["source"] == "browser"
    assert "worktree" not in reported and "branch" not in reported
    assert reported.get("host") and reported.get("os_user")


class TestBrowserWrites:
    def test_every_write_route_stamps_its_operation_and_a_browser_origin(self, dash):
        port, ld, ids = dash
        status, body = post(port, "/api/tasks", {"title": "From the page", "tags": ["a", "b"]})
        assert status == 201
        task = body["data"]["id"]
        assert body["data"]["tags"] == ["a", "b"]
        assert (ld / "plans" / f"{task}.md").is_file()  # scaffolded, as the CLI does

        steps = [
            ("status", {"status": "in_planning"}, "task.status"),
            ("assign", {"assigned_to": "human:bob"}, "task.assign"),
            ("assign", {"assigned_to": None}, "task.assign"),
            ("update", {"fields": {"priority": "high", "tags": ["c"]}}, "task.update"),
            ("comment", {"body": "first"}, "task.comment"),
        ]
        for sub, payload, op in steps:
            before = len(events_of(ld, task))
            status, body = post(port, f"/api/tasks/{task}/{sub}", payload)
            assert status == 200, body
            new = events_of(ld, task)[before:]
            assert new, sub
            for event in new:
                _assert_browser_origin(event, op)
                assert event["actor"] == "dashboard:web"

        comment_id = events_of(ld, task)[-1]["id"]
        for sub, payload, op in [
            ("comment", {"body": "reply", "parent_id": comment_id}, "task.comment"),
            ("comment-edit", {"comment_id": comment_id, "body": "edited"}, "task.comment_edit"),
            ("react", {"comment_id": comment_id, "emoji": "thumbsup"}, "task.react"),
            ("unreact", {"comment_id": comment_id, "emoji": "thumbsup"}, "task.unreact"),
            ("comment-delete", {"comment_id": comment_id}, "task.comment_delete"),
        ]:
            before = len(events_of(ld, task))
            status, body = post(port, f"/api/tasks/{task}/{sub}", payload)
            assert status == 200, body
            (event,) = events_of(ld, task)[before:]
            _assert_browser_origin(event, op)

        status, body = post(port, f"/api/tasks/{task}/archive", {})
        assert status == 200
        assert body["data"] == {"message": f"Task {task} archived"}
        archived = ld / "archive" / "events" / f"{task}.jsonl"
        last = json.loads(archived.read_text().splitlines()[-1])
        _assert_browser_origin(last, "task.archive")

    def test_create_event_carries_the_origin(self, dash):
        port, ld, _ids = dash
        _, body = post(port, "/api/tasks", {"title": "Origin on create"})
        (created,) = events_of(ld, body["data"]["id"])
        _assert_browser_origin(created, "task.create")

    def test_a_named_actor_is_used_locally(self, dash):
        port, ld, ids = dash
        post(port, f"/api/tasks/{ids['backlog']}/comment", {"body": "x", "actor": "human:alice"})
        assert events_of(ld, ids["backlog"])[-1]["actor"] == "human:alice"

    def test_unchanged_update_returns_the_snapshot(self, dash):
        port, _ld, ids = dash
        status, body = post(
            port, f"/api/tasks/{ids['backlog']}/update", {"fields": {"priority": "high"}}
        )
        assert status == 200
        assert body["data"]["id"] == ids["backlog"]

    def test_settings_post_runs_the_operation(self, dash):
        port, ld, _ids = dash
        status, body = post(port, "/api/config/dashboard", {"theme": "dark", "font_size": 15})
        assert status == 200
        assert body["data"]["theme"] == "dark"
        config = json.loads((ld / "config.json").read_text())
        assert config["dashboard"]["font_size"] == 15

    def test_settings_post_takes_an_actor_and_validates_it(self, dash):
        port, _ld, _ids = dash
        status, _ = post(port, "/api/config/dashboard", {"voice": "calm", "actor": "human:alice"})
        assert status == 200
        status, body = post(port, "/api/config/dashboard", {"voice": "calm", "actor": "nope"})
        assert status == 400
        assert body["error"]["code"] == "INVALID_ACTOR"

    def test_settings_post_cannot_touch_board_configuration(self, dash):
        port, ld, _ids = dash
        before = (ld / "config.json").read_bytes()
        status, body = post(port, "/api/config/dashboard", {"workflow": {"statuses": ["x"]}})
        assert status == 403
        assert body["error"]["code"] == "FORBIDDEN"
        assert (ld / "config.json").read_bytes() == before


# ---------------------------------------------------------------------------
# Erased tasks leave the boards (SPEC §7)
# ---------------------------------------------------------------------------


class TestTombstones:
    def test_erased_task_leaves_boards_but_detail_still_shows_it(self, dash):
        port, ld, ids = dash
        board = LocalBoard(root=ld.parent, start=ld.parent)
        erased = ids["backlog"]
        board.execute("task.erase", {"task": erased, "reason": "test"}, Caller(actor="human:a"))

        _, tasks = get(port, "/api/tasks")
        assert erased not in {t["id"] for t in tasks["data"]}
        _, graph = get(port, "/api/graph")
        assert erased not in {n["id"] for n in graph["data"]["nodes"]}
        _, stats = get(port, "/api/stats")
        assert stats["data"]["summary"]["active_tasks"] == len(tasks["data"])

        status, detail = get(port, f"/api/tasks/{erased}")
        assert status == 200
        assert detail["data"]["tombstoned"] is True
        assert detail["data"]["tombstone_reason"] == "test"

    def test_erased_archived_task_leaves_the_archive_list(self, dash):
        """A task erased, then archived: its history is rewritten here to hold
        the tombstone before the archive event."""
        from lattice.core.events import create_event, serialize_event
        from lattice.core.tasks import apply_event_to_snapshot, serialize_snapshot

        port, ld, ids = dash
        task = ids["archived"]
        _, archived = get(port, "/api/archived")
        assert task in {t["id"] for t in archived["data"]}

        log = ld / "archive" / "events" / f"{task}.jsonl"
        events = [json.loads(line) for line in log.read_text().splitlines()]
        tombstone = create_event("task_tombstoned", task, "human:a", {"reason": "old"})
        events.insert(len(events) - 1, tombstone)
        log.write_text("".join(serialize_event(e) for e in events))
        snapshot = None
        for event in events:
            snapshot = apply_event_to_snapshot(snapshot, event)
        (ld / "archive" / "tasks" / f"{task}.json").write_text(serialize_snapshot(snapshot))

        status, detail = get(port, f"/api/tasks/{task}")
        assert status == 200 and detail["data"]["tombstoned"] is True
        _, archived = get(port, "/api/archived")
        assert task not in {t["id"] for t in archived["data"]}


# ---------------------------------------------------------------------------
# The origin line in event views (AC-38)
# ---------------------------------------------------------------------------


class TestOriginLine:
    def test_event_views_carry_the_browser_origin_line(self, dash):
        port, _ld, ids = dash
        task = ids["backlog"]
        post(port, f"/api/tasks/{task}/comment", {"body": "hello"})

        _, events = get(port, f"/api/tasks/{task}/events")
        line = events["data"][0]["origin_line"]
        assert line.startswith("dashboard:web · ")
        assert line.endswith(" · browser")

        _, full = get(port, f"/api/tasks/{task}/full")
        assert full["data"]["recent_events"][0]["origin_line"] == line

        _, activity = get(port, f"/api/activity?task={task}")
        assert activity["data"]["events"][0]["origin_line"] == line

    def test_pre_origin_events_have_no_line(self, dash):
        port, _ld, ids = dash
        _, events = get(port, f"/api/tasks/{ids['backlog']}/events")
        assert all("origin_line" not in e for e in events["data"])

    def test_cli_origin_shows_worktree_and_branch(self):
        event = {
            "actor": "agent:x",
            "origin": {
                "reported": {"os_user": "alice", "host": "lap", "worktree": "/w", "branch": "b"}
            },
        }
        (out,) = api.with_origin_lines([event])
        assert out["origin_line"] == "agent:x · alice@lap · /w (b)"


# ---------------------------------------------------------------------------
# The browser actor on a bound checkout (SPEC §8.3)
# ---------------------------------------------------------------------------


def _identity(user: str, *actors: str) -> dict:
    from lattice.server.tokens import TokenRecord

    token = TokenRecord(
        id="tok_x",
        sha256="0",
        user=user,
        machine="m",
        actors=actors,
        projects=("*",),
        created_at="",
    )
    return {"user": token.user, "actors": list(token.actors), "browser_actor": token.browser_actor}


class TestBrowserActor:
    @pytest.mark.parametrize(
        "actors",
        [("human:alice",), ("human:alice", "agent:*")],
    )
    def test_person_tokens_write_as_the_user(self, actors):
        assert api.browser_actor(_identity("human:alice", *actors)) == "human:alice"

    def test_seat_token_writes_as_its_actor(self):
        assert api.browser_actor(_identity("human:alice", "agent:owner-3")) == "agent:owner-3"

    def test_no_browser_actor_is_missing_actor(self):
        with pytest.raises(OpError) as exc:
            api.browser_actor(_identity("human:alice", "agent:*"))
        assert exc.value.code == "MISSING_ACTOR"


# ---------------------------------------------------------------------------
# A bound checkout's dashboard writes as the browser actor (SPEC §8.3, §10)
# ---------------------------------------------------------------------------


class _RecordingBoard:
    """Stands in for a ``HostedBoard``: records each call, then runs it locally."""

    def __init__(self, lattice_dir: Path) -> None:
        self.local = LocalBoard(root=lattice_dir.parent, start=lattice_dir.parent)
        self.lattice_dir = lattice_dir
        self.calls: list[tuple[str, dict, Caller]] = []

    def execute(self, op_name: str, params: dict, caller: Caller, **kwargs: object):
        self.calls.append((op_name, params, caller))
        return self.local.execute(op_name, params, caller, **kwargs)


@pytest.fixture()
def bound_dash(populated_lattice_dir, request):
    from lattice.dashboard.server import DashboardBoard

    ld, ids = populated_lattice_dir
    board = _RecordingBoard(ld)
    identity = _identity("human:alice", *request.param)
    target = DashboardBoard(board, browser_actor=lambda: api.browser_actor(identity), hosted=True)
    server = create_server(ld, "127.0.0.1", 0, board=target)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        yield server.server_address[1], ld, ids, board
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class TestBoundCheckoutSeam:
    @pytest.mark.parametrize(
        "bound_dash", [("human:alice",), ("human:alice", "agent:*")], indirect=True
    )
    def test_drag_writes_as_the_user_whatever_the_body_says(self, bound_dash):
        port, ld, ids, board = bound_dash
        task = ids["backlog"]
        status, _ = post(
            port,
            f"/api/tasks/{task}/status",
            {"status": "in_planning", "actor": "human:mallory"},
        )
        assert status == 200
        ((op, params, caller),) = board.calls
        assert (op, params["new_status"], caller.actor) == (
            "task.status",
            "in_planning",
            "human:alice",
        )
        assert caller.origin["reported"]["source"] == "browser"
        assert events_of(ld, task)[-1]["actor"] == "human:alice"

    @pytest.mark.parametrize("bound_dash", [("human:alice",)], indirect=True)
    def test_open_plans_is_local_only(self, bound_dash):
        port, _ld, ids, board = bound_dash
        status, body = post(port, f"/api/tasks/{ids['backlog']}/open-plans", {})
        assert status == 400
        assert body["error"]["code"] == "LOCAL_ONLY"
        assert "lattice plan write" in body["error"]["message"]
        assert board.calls == []

    @pytest.mark.parametrize("bound_dash", [("agent:*",)], indirect=True)
    def test_no_browser_actor_refuses_the_write(self, bound_dash):
        port, ld, ids, board = bound_dash
        before = board_bytes(ld)
        status, body = post(port, f"/api/tasks/{ids['backlog']}/comment", {"body": "x"})
        assert status == 400
        assert body["error"]["code"] == "MISSING_ACTOR"
        assert board.calls == [] and board_bytes(ld) == before
