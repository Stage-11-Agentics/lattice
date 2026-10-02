"""Tests for the GET /api/graph endpoint."""

from __future__ import annotations

import hashlib
import json
import http.client
import re
import socket
import threading
from pathlib import Path
from urllib.request import Request, urlopen

from lattice.core.config import default_config, serialize_config
from lattice.core.events import create_event, serialize_event
from lattice.core.ids import generate_task_id
from lattice.core.tasks import apply_event_to_snapshot, serialize_snapshot
from lattice.dashboard.api import get_graph
from lattice.dashboard.server import create_server
from lattice.storage.fs import atomic_write, ensure_lattice_dirs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get(
    base_url: str, path: str, *, headers: dict[str, str] | None = None
) -> tuple[int, dict | str, dict[str, str]]:
    """Make a GET request and return (status_code, parsed_body, response_headers).

    Extended version of the test_server._get helper that also returns
    response headers and accepts optional request headers — needed for
    ETag / If-None-Match tests.
    """
    req = Request(f"{base_url}{path}")
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    try:
        with urlopen(req) as resp:
            body = resp.read().decode("utf-8")
            content_type = resp.headers.get("Content-Type", "")
            resp_headers = {k: v for k, v in resp.getheaders()}
            if "application/json" in content_type:
                return resp.status, json.loads(body), resp_headers
            return resp.status, body, resp_headers
    except Exception as exc:
        if hasattr(exc, "code"):
            body = exc.read().decode("utf-8")  # type: ignore[union-attr]
            content_type = exc.headers.get("Content-Type", "")  # type: ignore[union-attr]
            resp_headers = {k: v for k, v in exc.headers.items()}  # type: ignore[union-attr]
            if "application/json" in content_type:
                return exc.code, json.loads(body), resp_headers  # type: ignore[union-attr]
            return exc.code, body, resp_headers  # type: ignore[union-attr]
        raise


def _write_task(lattice_dir: Path, events: list[dict]) -> dict:
    """Apply a sequence of events, write snapshot + event log, return snapshot."""
    snapshot = None
    for ev in events:
        snapshot = apply_event_to_snapshot(snapshot, ev)

    task_id = snapshot["id"]
    atomic_write(lattice_dir / "tasks" / f"{task_id}.json", serialize_snapshot(snapshot))

    event_path = lattice_dir / "events" / f"{task_id}.jsonl"
    with open(event_path, "w", encoding="utf-8") as fh:
        for ev in events:
            fh.write(serialize_event(ev))

    return snapshot


def _get_free_port() -> int:
    """Find an available TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_server(lattice_dir: Path) -> tuple[str, object]:
    """Start a dashboard server on a random port and return (base_url, server)."""
    port = _get_free_port()
    host = "127.0.0.1"
    server = create_server(lattice_dir, host, port)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    return f"http://{host}:{port}", server


# ---------------------------------------------------------------------------
# Graph endpoint tests — populated lattice
# ---------------------------------------------------------------------------


class TestGraphBasic:
    def test_graph_basic(self, dashboard_server):
        """GET /api/graph returns 200 with nodes, links, and revision."""
        base_url, _ld, ids = dashboard_server

        status, body, _hdrs = _get(base_url, "/api/graph")
        assert status == 200
        assert body["ok"] is True

        data = body["data"]

        # 3 active tasks — task 4 is archived and must be excluded
        nodes = data["nodes"]
        assert len(nodes) == 3

        node_ids = {n["id"] for n in nodes}
        assert ids["backlog"] in node_ids
        assert ids["in_progress"] in node_ids
        assert ids["done"] in node_ids

        # Each node must have the expected fields
        for node in nodes:
            assert "id" in node
            assert "title" in node
            assert "status" in node
            assert "priority" in node
            assert "type" in node

        # 1 link: task 2 blocks task 1
        links = data["links"]
        assert len(links) == 1
        link = links[0]
        assert link["source"] == ids["in_progress"]
        assert link["target"] == ids["backlog"]
        assert link["type"] == "blocks"

        # Revision must exist and be non-empty
        assert isinstance(data["revision"], str)
        assert len(data["revision"]) > 0


class TestGraphExcludesArchived:
    def test_graph_excludes_archived(self, dashboard_server):
        """Archived task must NOT appear in nodes or links."""
        base_url, _ld, ids = dashboard_server
        archived_id = ids["archived"]

        status, body, _hdrs = _get(base_url, "/api/graph")
        assert status == 200

        data = body["data"]
        node_ids = {n["id"] for n in data["nodes"]}
        assert archived_id not in node_ids

        # Also must not appear as a link source or target
        for link in data["links"]:
            assert link["source"] != archived_id
            assert link["target"] != archived_id


class TestGraphLinksOnlyExistingTargets:
    def test_graph_links_only_existing_targets(self, tmp_path):
        """A relationship pointing to a non-existent task ID must be filtered out."""
        root = tmp_path
        ensure_lattice_dirs(root)
        ld = root / ".lattice"
        atomic_write(ld / "config.json", serialize_config(default_config()))

        # Create task A with a relationship to a non-existent task
        fake_target = generate_task_id()
        ta_id = generate_task_id()
        ta_events = [
            create_event(
                type="task_created",
                task_id=ta_id,
                actor="human:atin",
                data={
                    "title": "Task A",
                    "status": "backlog",
                    "priority": "medium",
                    "urgency": "normal",
                    "type": "task",
                    "description": None,
                    "tags": [],
                    "assigned_to": None,
                    "custom_fields": {},
                },
            ),
            create_event(
                type="relationship_added",
                task_id=ta_id,
                actor="human:atin",
                data={"type": "blocks", "target_task_id": fake_target, "note": None},
            ),
        ]
        _write_task(ld, ta_events)

        base_url, server = _start_server(ld)
        try:
            status, body, _hdrs = _get(base_url, "/api/graph")
            assert status == 200
            assert body["ok"] is True

            data = body["data"]
            assert len(data["nodes"]) == 1
            # The link to the non-existent target must be filtered out
            assert len(data["links"]) == 0
        finally:
            server.shutdown()
            server.server_close()


class TestGraphNodeFields:
    def test_graph_node_fields(self, dashboard_server):
        """Nodes must contain only the expected lightweight fields."""
        base_url, _ld, _ids = dashboard_server

        status, body, _hdrs = _get(base_url, "/api/graph")
        assert status == 200

        expected_fields = {
            "id",
            "short_id",
            "title",
            "status",
            "priority",
            "type",
            "assigned_to",
        }

        # Fields that must NOT be on graph nodes (heavyweight / detail fields)
        forbidden_fields = {
            "description",
            "custom_fields",
            "comments",
            "evidence_refs",
            "relationships_out",
            "created_by",
            "last_event_id",
            "schema_version",
        }

        for node in body["data"]["nodes"]:
            # Every expected field should be present
            for field in expected_fields:
                # short_id is optional (only present when project_code configured)
                if field == "short_id":
                    continue
                assert field in node, f"Expected field '{field}' missing from node"

            # No forbidden field should be present
            for field in forbidden_fields:
                assert field not in node, f"Forbidden field '{field}' found on node"


class TestGraphEmpty:
    def test_graph_empty(self, tmp_path):
        """An empty lattice dir should return nodes=[], links=[], and a revision."""
        root = tmp_path
        ensure_lattice_dirs(root)
        ld = root / ".lattice"
        atomic_write(ld / "config.json", serialize_config(default_config()))

        base_url, server = _start_server(ld)
        try:
            status, body, _hdrs = _get(base_url, "/api/graph")
            assert status == 200
            assert body["ok"] is True

            data = body["data"]
            assert data["nodes"] == []
            assert data["links"] == []
            assert isinstance(data["revision"], str)
            assert len(data["revision"]) > 0
        finally:
            server.shutdown()
            server.server_close()


# ---------------------------------------------------------------------------
# ETag / conditional request tests
# ---------------------------------------------------------------------------


class TestGraphETag304:
    def test_graph_etag_304(self, dashboard_server):
        """Second request with matching ETag should return 304 Not Modified."""
        base_url, _ld, _ids = dashboard_server

        # First request — get the ETag
        status1, body1, hdrs1 = _get(base_url, "/api/graph")
        assert status1 == 200
        etag = hdrs1.get("ETag")
        assert etag is not None, "Response must include an ETag header"

        # ETag must be quoted per RFC 7232
        assert etag.startswith('"') and etag.endswith('"'), "ETag must be quoted"

        # Second request with If-None-Match — should get 304
        status2, body2, hdrs2 = _get(base_url, "/api/graph", headers={"If-None-Match": etag})
        assert status2 == 304

        # 304 must include ETag header (RFC 7232 §4.1)
        assert hdrs2.get("ETag") == etag, "304 response must include ETag"


class TestGraphETagMismatch:
    def test_graph_etag_mismatch(self, dashboard_server):
        """Request with non-matching ETag should return 200 with full data."""
        base_url, _ld, _ids = dashboard_server

        status, body, hdrs = _get(base_url, "/api/graph", headers={"If-None-Match": '"wrong"'})
        assert status == 200
        assert body["ok"] is True
        assert len(body["data"]["nodes"]) == 3
        assert "ETag" in hdrs


def test_graph_etag_hashes_crlf_bearing_event_timestamp(dashboard_server) -> None:  # noqa: ANN001
    """A task event timestamp cannot split response headers through the graph ETag."""
    base_url, ld, ids = dashboard_server
    task_id = ids["backlog"]
    event_path = ld / "events" / f"{task_id}.jsonl"
    events = [json.loads(line) for line in event_path.read_text().splitlines()]
    injected_ts = '9999"\r\nX-Injected: yes\r\n\r\n<script>bad()</script>'
    events[-1]["ts"] = injected_ts
    event_path.write_text(
        "".join(
            json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n" for event in events
        ),
        encoding="utf-8",
    )

    host = base_url.removeprefix("http://").split(":", 1)[0]
    port = int(base_url.rsplit(":", 1)[1])
    conn = http.client.HTTPConnection(host, port, timeout=5)
    conn.request("GET", "/api/graph")
    response = conn.getresponse()
    raw_headers = response.getheaders()
    raw_body = response.read()
    conn.close()

    assert response.status == 200
    header_text = "\n".join(f"{name}: {value}" for name, value in raw_headers).lower()
    assert "x-injected" not in header_text
    assert "<script>" not in header_text
    etag = dict(raw_headers)["ETag"]
    body = json.loads(raw_body)
    revision = body["data"]["revision"]
    assert re.fullmatch(r'"[0-9a-f]{64}"', etag)
    assert revision == _graph_revision(body["data"])
    assert etag == _graph_etag(revision)


def test_graph_etag_changes_when_relationship_changes_with_same_timestamp(dashboard_server):
    """A graph projection change invalidates its ETag even within one timestamp."""
    _base_url, lattice_dir, ids = dashboard_server
    source_id = ids["in_progress"]
    target_id = ids["done"]
    first = get_graph(lattice_dir)
    etag = first.headers["ETag"]
    snapshot_path = lattice_dir / "tasks" / f"{source_id}.json"
    snapshot = json.loads(snapshot_path.read_text())
    event = create_event(
        "relationship_added",
        source_id,
        "human:test",
        {"type": "relates_to", "target_task_id": target_id},
        ts=snapshot["updated_at"],
    )
    with (lattice_dir / "events" / f"{source_id}.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(serialize_event(event))
    snapshot_path.write_text(
        serialize_snapshot(apply_event_to_snapshot(snapshot, event)), encoding="utf-8"
    )

    changed = get_graph(lattice_dir, if_none_match=etag)
    assert changed.status == 200
    assert changed.headers["ETag"] != etag
    assert changed.envelope["data"]["revision"] != first.envelope["data"]["revision"]
    assert changed.headers["ETag"] == _graph_etag(changed.envelope["data"]["revision"])
    assert {link["type"] for link in changed.envelope["data"]["links"]} == {
        "blocks",
        "relates_to",
    }


def test_graph_revision_and_etag_change_for_two_same_second_edits(dashboard_server):
    """The response revision follows content even when edit timestamps are equal."""
    _base_url, lattice_dir, ids = dashboard_server
    task_id = ids["in_progress"]
    snapshot_path = lattice_dir / "tasks" / f"{task_id}.json"
    event_path = lattice_dir / "events" / f"{task_id}.jsonl"
    snapshot = json.loads(snapshot_path.read_text())
    same_timestamp = snapshot["updated_at"]

    first = get_graph(lattice_dir)
    previous_revision = first.envelope["data"]["revision"]
    previous_etag = first.headers["ETag"]
    assert previous_revision == _graph_revision(first.envelope["data"])
    assert previous_etag == _graph_etag(previous_revision)

    for title in ("First same-second edit", "Second same-second edit"):
        event = create_event(
            "field_updated",
            task_id,
            "human:test",
            {"field": "title", "from": snapshot["title"], "to": title},
            ts=same_timestamp,
        )
        with event_path.open("a", encoding="utf-8") as handle:
            handle.write(serialize_event(event))
        snapshot = apply_event_to_snapshot(snapshot, event)
        snapshot_path.write_text(serialize_snapshot(snapshot), encoding="utf-8")

        changed = get_graph(lattice_dir, if_none_match=previous_etag)
        assert changed.status == 200
        assert changed.envelope["data"]["nodes"]
        revision = changed.envelope["data"]["revision"]
        etag = changed.headers["ETag"]
        assert revision != previous_revision
        assert etag != previous_etag
        assert revision == _graph_revision(changed.envelope["data"])
        assert etag == _graph_etag(revision)
        previous_revision, previous_etag = revision, etag


def _graph_revision(data: dict) -> str:
    projection = json.dumps(
        {"nodes": data["nodes"], "links": data["links"]},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(projection.encode("utf-8")).hexdigest()


def _graph_etag(revision: str) -> str:
    digest = hashlib.sha256(revision.encode("utf-8", errors="surrogatepass")).hexdigest()
    return f'"{digest}"'
