"""Tests for the Lattice dashboard HTTP server and API endpoints."""

from __future__ import annotations

import json
from urllib.request import Request, urlopen

import pytest

from lattice.core.ids import generate_task_id


def _get(base_url: str, path: str) -> tuple[int, dict | str]:
    """Make a GET request and return (status_code, parsed_body)."""
    req = Request(f"{base_url}{path}")
    try:
        with urlopen(req) as resp:
            body = resp.read().decode("utf-8")
            content_type = resp.headers.get("Content-Type", "")
            if "application/json" in content_type:
                return resp.status, json.loads(body)
            return resp.status, body
    except Exception as exc:
        # urllib raises on non-2xx; extract status from the error
        if hasattr(exc, "code"):
            body = exc.read().decode("utf-8")  # type: ignore[union-attr]
            content_type = exc.headers.get("Content-Type", "")  # type: ignore[union-attr]
            if "application/json" in content_type:
                return exc.code, json.loads(body)  # type: ignore[union-attr]
            return exc.code, body  # type: ignore[union-attr]
        raise


def _post(base_url: str, path: str, data: dict) -> tuple[int, dict | str]:
    """Make a POST request with JSON body and return (status_code, parsed_body)."""
    payload = json.dumps(data).encode("utf-8")
    req = Request(
        f"{base_url}{path}",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req) as resp:
            body = resp.read().decode("utf-8")
            content_type = resp.headers.get("Content-Type", "")
            if "application/json" in content_type:
                return resp.status, json.loads(body)
            return resp.status, body
    except Exception as exc:
        if hasattr(exc, "code"):
            body = exc.read().decode("utf-8")  # type: ignore[union-attr]
            content_type = exc.headers.get("Content-Type", "")  # type: ignore[union-attr]
            if "application/json" in content_type:
                return exc.code, json.loads(body)  # type: ignore[union-attr]
            return exc.code, body  # type: ignore[union-attr]
        raise


def _post_raw(
    base_url: str, path: str, raw_bytes: bytes, content_type: str = "application/json"
) -> tuple[int, dict | str]:
    """Make a POST request with raw bytes and return (status_code, parsed_body)."""
    req = Request(
        f"{base_url}{path}",
        data=raw_bytes,
        headers={"Content-Type": content_type},
        method="POST",
    )
    try:
        with urlopen(req) as resp:
            body = resp.read().decode("utf-8")
            ct = resp.headers.get("Content-Type", "")
            if "application/json" in ct:
                return resp.status, json.loads(body)
            return resp.status, body
    except Exception as exc:
        if hasattr(exc, "code"):
            body = exc.read().decode("utf-8")  # type: ignore[union-attr]
            ct = exc.headers.get("Content-Type", "")  # type: ignore[union-attr]
            if "application/json" in ct:
                return exc.code, json.loads(body)  # type: ignore[union-attr]
            return exc.code, body  # type: ignore[union-attr]
        raise


# ---------------------------------------------------------------------------
# Core endpoint tests
# ---------------------------------------------------------------------------


class TestRootEndpoint:
    def test_get_root_returns_html(self, dashboard_server):
        base_url, _ld, _ids = dashboard_server
        status, body = _get(base_url, "/")
        assert status == 200
        assert "<html" in body


class TestConfigEndpoint:
    def test_get_config(self, dashboard_server):
        base_url, _ld, _ids = dashboard_server
        status, body = _get(base_url, "/api/config")
        assert status == 200
        assert body["ok"] is True
        assert "workflow" in body["data"]
        assert "statuses" in body["data"]["workflow"]


class TestTasksEndpoint:
    def test_get_tasks(self, dashboard_server):
        base_url, _ld, _ids = dashboard_server
        status, body = _get(base_url, "/api/tasks")
        assert status == 200
        assert body["ok"] is True
        data = body["data"]
        assert isinstance(data, list)
        assert len(data) == 3  # 3 active tasks

        # Check compact snapshot fields are present
        t = data[0]
        assert "id" in t
        assert "title" in t
        assert "status" in t
        assert "updated_at" in t
        assert "created_at" in t

        # Sorted by ID
        ids = [task["id"] for task in data]
        assert ids == sorted(ids)

    def test_tasks_exclude_archived(self, dashboard_server):
        base_url, _ld, ids = dashboard_server
        archived_id = ids["archived"]
        status, body = _get(base_url, "/api/tasks")
        task_ids = [t["id"] for t in body["data"]]
        assert archived_id not in task_ids


class TestTaskDetailEndpoint:
    def test_get_task_detail(self, dashboard_server):
        base_url, _ld, ids = dashboard_server
        task_id = ids["in_implementation"]
        status, body = _get(base_url, f"/api/tasks/{task_id}")
        assert status == 200
        assert body["ok"] is True
        data = body["data"]
        assert data["id"] == task_id
        assert "notes_exists" in data
        assert isinstance(data["artifacts"], list)

    def test_task_detail_with_notes(self, dashboard_server):
        base_url, _ld, ids = dashboard_server
        task_id = ids["backlog"]
        status, body = _get(base_url, f"/api/tasks/{task_id}")
        assert body["data"]["notes_exists"] is True

    def test_task_detail_without_notes(self, dashboard_server):
        base_url, _ld, ids = dashboard_server
        task_id = ids["done"]
        status, body = _get(base_url, f"/api/tasks/{task_id}")
        assert body["data"]["notes_exists"] is False

    def test_task_detail_with_artifacts(self, dashboard_server):
        base_url, _ld, ids = dashboard_server
        task_id = ids["in_implementation"]
        status, body = _get(base_url, f"/api/tasks/{task_id}")
        arts = body["data"]["artifacts"]
        assert len(arts) == 1
        assert arts[0]["title"] == "dep-report.txt"
        assert arts[0]["type"] == "text/plain"

    def test_archived_task_fallthrough(self, dashboard_server):
        base_url, _ld, ids = dashboard_server
        task_id = ids["archived"]
        status, body = _get(base_url, f"/api/tasks/{task_id}")
        assert status == 200
        assert body["ok"] is True
        assert body["data"]["archived"] is True
        assert body["data"]["title"] == "Old spike task"

    def test_invalid_id_format(self, dashboard_server):
        base_url, _ld, _ids = dashboard_server
        status, body = _get(base_url, "/api/tasks/not-a-valid-id")
        assert status == 400
        assert body["ok"] is False
        assert body["error"]["code"] == "INVALID_ID"

    def test_valid_but_nonexistent(self, dashboard_server):
        base_url, _ld, _ids = dashboard_server
        fake_id = generate_task_id()
        status, body = _get(base_url, f"/api/tasks/{fake_id}")
        assert status == 404
        assert body["ok"] is False
        assert body["error"]["code"] == "NOT_FOUND"


class TestTaskEventsEndpoint:
    def test_get_events(self, dashboard_server):
        base_url, _ld, ids = dashboard_server
        task_id = ids["in_implementation"]
        status, body = _get(base_url, f"/api/tasks/{task_id}/events")
        assert status == 200
        assert body["ok"] is True
        events = body["data"]
        assert isinstance(events, list)
        assert len(events) == 5  # task_created + status_changed + rel + comment + artifact

        # Newest first
        timestamps = [e["ts"] for e in events]
        assert timestamps == sorted(timestamps, reverse=True)

    def test_events_for_archived_task(self, dashboard_server):
        base_url, _ld, ids = dashboard_server
        task_id = ids["archived"]
        status, body = _get(base_url, f"/api/tasks/{task_id}/events")
        assert status == 200
        assert len(body["data"]) == 2  # task_created + task_archived


class TestActivityEndpoint:
    def test_get_activity(self, dashboard_server):
        base_url, _ld, _ids = dashboard_server
        status, body = _get(base_url, "/api/activity")
        assert status == 200
        assert body["ok"] is True
        events = body["data"]
        assert isinstance(events, list)
        assert len(events) > 0

        # Should include non-lifecycle events (comment, relationship, etc.)
        event_types = {e["type"] for e in events}
        assert "comment_added" in event_types or "status_changed" in event_types

        # Newest first
        timestamps = [e["ts"] for e in events]
        assert timestamps == sorted(timestamps, reverse=True)


class TestStatsEndpoint:
    def test_get_stats(self, dashboard_server):
        base_url, _ld, _ids = dashboard_server
        status, body = _get(base_url, "/api/stats")
        assert status == 200
        assert body["ok"] is True
        data = body["data"]
        assert data["summary"]["active_tasks"] == 3
        assert data["summary"]["archived_tasks"] == 1
        assert isinstance(data["by_status"], list)
        assert isinstance(data["by_type"], list)
        assert isinstance(data["by_priority"], list)

    def test_stats_are_dynamic(self, dashboard_server):
        base_url, _ld, _ids = dashboard_server
        status, body = _get(base_url, "/api/stats")
        data = body["data"]
        # by_status is a list of [status, count] pairs
        status_dict = {s: c for s, c in data["by_status"]}
        # Counts should match actual task data
        total_from_status = sum(status_dict.values())
        assert total_from_status == data["summary"]["active_tasks"]
        # Verify specific counts
        assert status_dict.get("backlog") == 1
        assert status_dict.get("in_implementation") == 1
        assert status_dict.get("done") == 1


class TestArchivedEndpoint:
    def test_get_archived(self, dashboard_server):
        base_url, _ld, ids = dashboard_server
        status, body = _get(base_url, "/api/archived")
        assert status == 200
        assert body["ok"] is True
        data = body["data"]
        assert len(data) == 1
        assert data[0]["id"] == ids["archived"]
        assert data[0]["archived"] is True


class TestNotFoundRoutes:
    def test_unknown_api_route(self, dashboard_server):
        base_url, _ld, _ids = dashboard_server
        status, body = _get(base_url, "/api/nonexistent")
        assert status == 404
        assert body["ok"] is False

    def test_random_path(self, dashboard_server):
        base_url, _ld, _ids = dashboard_server
        status, body = _get(base_url, "/random/path")
        assert status == 404

    def test_unknown_task_sub_route(self, dashboard_server):
        base_url, _ld, ids = dashboard_server
        task_id = ids["backlog"]
        status, body = _get(base_url, f"/api/tasks/{task_id}/unknown")
        assert status == 404


# ---------------------------------------------------------------------------
# Edge case tests
# ---------------------------------------------------------------------------


class TestCorruptedFiles:
    def test_corrupted_task_snapshot_skipped(self, dashboard_server):
        """A corrupted task file should be silently skipped in list."""
        base_url, ld, _ids = dashboard_server

        # Write a corrupted file
        bad_path = ld / "tasks" / "task_AAAAAAAAAAAAAAAAAAAAAAAAAA.json"
        bad_path.write_text("{invalid json")

        status, body = _get(base_url, "/api/tasks")
        assert status == 200
        # Should still have the 3 valid tasks
        assert len(body["data"]) == 3

    def test_corrupted_event_line_skipped(self, dashboard_server):
        """A corrupted event line should be skipped in event list."""
        base_url, ld, ids = dashboard_server
        task_id = ids["backlog"]
        event_path = ld / "events" / f"{task_id}.jsonl"

        # Append a corrupted line
        with open(event_path, "a") as fh:
            fh.write("{truncated json\n")

        status, body = _get(base_url, f"/api/tasks/{task_id}/events")
        assert status == 200
        # Should still have the 1 valid event
        assert len(body["data"]) == 1


class TestNonLoopbackWarning:
    def test_dashboard_cmd_warns_non_loopback(self):
        """The CLI command should warn when binding to a non-loopback address."""
        from lattice.cli.dashboard_cmd import _LOOPBACK_HOSTS

        assert "127.0.0.1" in _LOOPBACK_HOSTS
        assert "::1" in _LOOPBACK_HOSTS
        assert "localhost" in _LOOPBACK_HOSTS
        assert "0.0.0.0" not in _LOOPBACK_HOSTS


class TestBindError:
    def test_bind_error_json_envelope(self, populated_lattice_dir):
        """Port-in-use should produce a JSON error envelope with BIND_ERROR code."""
        import socket

        from click.testing import CliRunner

        from lattice.cli.main import cli

        ld, _ids = populated_lattice_dir

        # Occupy a port
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.listen(1)

        try:
            runner = CliRunner()
            env = {"LATTICE_ROOT": str(ld.parent)}
            result = runner.invoke(
                cli,
                ["dashboard", "--port", str(port), "--json"],
                env=env,
            )
            assert result.exit_code != 0
            parsed = json.loads(result.output)
            assert parsed["ok"] is False
            assert parsed["error"]["code"] == "BIND_ERROR"
        finally:
            sock.close()


# ---------------------------------------------------------------------------
# POST endpoint tests — Status change
# ---------------------------------------------------------------------------


class TestPostTaskStatus:
    def test_valid_transition(self, dashboard_server):
        """POST /api/tasks/<id>/status with a valid transition should succeed."""
        base_url, ld, ids = dashboard_server
        task_id = ids["backlog"]  # backlog -> in_planning is valid

        status, body = _post(
            base_url,
            f"/api/tasks/{task_id}/status",
            {
                "status": "in_planning",
                "actor": "dashboard:web",
            },
        )
        assert status == 200
        assert body["ok"] is True
        data = body["data"]
        assert data["status"] == "in_planning"
        assert data["id"] == task_id

        # Verify the snapshot on disk was updated
        snap = json.loads((ld / "tasks" / f"{task_id}.json").read_text())
        assert snap["status"] == "in_planning"

    def test_valid_transition_default_actor(self, dashboard_server):
        """Actor should default to dashboard:web when not provided."""
        base_url, ld, ids = dashboard_server
        task_id = ids["backlog"]

        status, body = _post(
            base_url,
            f"/api/tasks/{task_id}/status",
            {
                "status": "in_planning",
            },
        )
        assert status == 200
        assert body["ok"] is True

        # Check the event was written with the default actor
        events_path = ld / "events" / f"{task_id}.jsonl"
        lines = events_path.read_text().strip().split("\n")
        last_event = json.loads(lines[-1])
        assert last_event["actor"] == "dashboard:web"

    def test_event_written_on_transition(self, dashboard_server):
        """A status_changed event should be appended to the event log."""
        base_url, ld, ids = dashboard_server
        task_id = ids["backlog"]

        # Count events before
        events_path = ld / "events" / f"{task_id}.jsonl"
        lines_before = events_path.read_text().strip().split("\n")

        _post(
            base_url,
            f"/api/tasks/{task_id}/status",
            {
                "status": "in_planning",
                "actor": "dashboard:web",
            },
        )

        lines_after = events_path.read_text().strip().split("\n")
        assert len(lines_after) == len(lines_before) + 1

        new_event = json.loads(lines_after[-1])
        assert new_event["type"] == "status_changed"
        assert new_event["data"]["from"] == "backlog"
        assert new_event["data"]["to"] == "in_planning"
        assert new_event["actor"] == "dashboard:web"

    def test_invalid_transition(self, dashboard_server):
        """An invalid transition should return 400 with INVALID_TRANSITION."""
        base_url, _ld, ids = dashboard_server
        task_id = ids["backlog"]  # backlog -> done is NOT valid

        status, body = _post(
            base_url,
            f"/api/tasks/{task_id}/status",
            {
                "status": "done",
                "actor": "dashboard:web",
            },
        )
        assert status == 400
        assert body["ok"] is False
        assert body["error"]["code"] == "INVALID_TRANSITION"

    def test_force_transition_succeeds(self, dashboard_server):
        """Force=true with reason should bypass invalid transition."""
        base_url, ld, ids = dashboard_server
        task_id = ids["backlog"]

        status, body = _post(
            base_url,
            f"/api/tasks/{task_id}/status",
            {
                "status": "done",
                "actor": "dashboard:web",
                "force": True,
                "reason": "Hotfix already deployed",
            },
        )
        assert status == 200
        assert body["ok"] is True
        assert body["data"]["status"] == "done"

        # Verify event includes force + reason
        events_path = ld / "events" / f"{task_id}.jsonl"
        lines = events_path.read_text().strip().split("\n")
        last_event = json.loads(lines[-1])
        assert last_event["data"]["force"] is True
        assert last_event["data"]["reason"] == "Hotfix already deployed"

    def test_force_without_reason_rejected(self, dashboard_server):
        """Force=true without reason should be rejected."""
        base_url, _ld, ids = dashboard_server
        task_id = ids["backlog"]

        status, body = _post(
            base_url,
            f"/api/tasks/{task_id}/status",
            {
                "status": "done",
                "actor": "dashboard:web",
                "force": True,
            },
        )
        assert status == 400
        assert body["error"]["code"] == "VALIDATION_ERROR"
        assert "reason" in body["error"]["message"].lower()

    def test_same_status_noop(self, dashboard_server):
        """Transition to the same status should return 200 with a message."""
        base_url, _ld, ids = dashboard_server
        task_id = ids["backlog"]

        status, body = _post(
            base_url,
            f"/api/tasks/{task_id}/status",
            {
                "status": "backlog",
                "actor": "dashboard:web",
            },
        )
        assert status == 200
        assert body["ok"] is True
        assert "Already" in body["data"]["message"]

    def test_missing_status_field(self, dashboard_server):
        """Missing 'status' field should return 400."""
        base_url, _ld, ids = dashboard_server
        task_id = ids["backlog"]

        status, body = _post(
            base_url,
            f"/api/tasks/{task_id}/status",
            {
                "actor": "dashboard:web",
            },
        )
        assert status == 400
        assert body["ok"] is False
        assert body["error"]["code"] == "VALIDATION_ERROR"

    def test_invalid_task_id(self, dashboard_server):
        """Invalid task ID format should return 400."""
        base_url, _ld, _ids = dashboard_server

        status, body = _post(
            base_url,
            "/api/tasks/not-valid/status",
            {
                "status": "in_planning",
            },
        )
        assert status == 400
        assert body["error"]["code"] == "INVALID_ID"

    def test_nonexistent_task(self, dashboard_server):
        """Valid but nonexistent task ID should return 404."""
        base_url, _ld, _ids = dashboard_server
        fake_id = generate_task_id()

        status, body = _post(
            base_url,
            f"/api/tasks/{fake_id}/status",
            {
                "status": "in_planning",
                "actor": "dashboard:web",
            },
        )
        assert status == 404
        assert body["error"]["code"] == "NOT_FOUND"

    def test_unknown_status(self, dashboard_server):
        """An unknown target status should return 400 VALIDATION_ERROR."""
        base_url, _ld, ids = dashboard_server
        task_id = ids["backlog"]

        status, body = _post(
            base_url,
            f"/api/tasks/{task_id}/status",
            {
                "status": "nonexistent_status",
                "actor": "dashboard:web",
            },
        )
        assert status == 400
        assert body["error"]["code"] == "VALIDATION_ERROR"

    def test_invalid_actor_format(self, dashboard_server):
        """An actor that doesn't match prefix:id format should return 400."""
        base_url, _ld, ids = dashboard_server
        task_id = ids["backlog"]

        status, body = _post(
            base_url,
            f"/api/tasks/{task_id}/status",
            {
                "status": "in_planning",
                "actor": "bad-actor",
            },
        )
        assert status == 400
        assert body["error"]["code"] == "VALIDATION_ERROR"

    def test_invalid_json_body(self, dashboard_server):
        """Malformed JSON body should return 400."""
        base_url, _ld, ids = dashboard_server
        task_id = ids["backlog"]

        status, body = _post_raw(
            base_url,
            f"/api/tasks/{task_id}/status",
            b"{invalid json",
        )
        assert status == 400
        assert body["error"]["code"] == "BAD_REQUEST"

    def test_chained_transitions(self, dashboard_server):
        """Multiple valid transitions should work in sequence."""
        base_url, ld, ids = dashboard_server
        task_id = ids["backlog"]

        # backlog -> in_planning
        status, body = _post(
            base_url,
            f"/api/tasks/{task_id}/status",
            {
                "status": "in_planning",
            },
        )
        assert status == 200
        assert body["data"]["status"] == "in_planning"

        # in_planning -> planned
        status, body = _post(
            base_url,
            f"/api/tasks/{task_id}/status",
            {
                "status": "planned",
            },
        )
        assert status == 200
        assert body["data"]["status"] == "planned"

        # planned -> in_implementation
        status, body = _post(
            base_url,
            f"/api/tasks/{task_id}/status",
            {
                "status": "in_implementation",
            },
        )
        assert status == 200
        assert body["data"]["status"] == "in_implementation"

        # in_implementation -> implemented
        status, body = _post(
            base_url,
            f"/api/tasks/{task_id}/status",
            {
                "status": "implemented",
            },
        )
        assert status == 200
        assert body["data"]["status"] == "implemented"

        # implemented -> in_review
        status, body = _post(
            base_url,
            f"/api/tasks/{task_id}/status",
            {
                "status": "in_review",
            },
        )
        assert status == 200
        assert body["data"]["status"] == "in_review"

        # in_review -> done
        status, body = _post(
            base_url,
            f"/api/tasks/{task_id}/status",
            {
                "status": "done",
            },
        )
        assert status == 200
        assert body["data"]["status"] == "done"


# ---------------------------------------------------------------------------
# POST endpoint tests — Dashboard config
# ---------------------------------------------------------------------------


class TestPostDashboardConfig:
    def test_set_background_image(self, dashboard_server):
        """POST /api/config/dashboard should set background_image."""
        base_url, ld, _ids = dashboard_server

        status, body = _post(
            base_url,
            "/api/config/dashboard",
            {
                "background_image": "https://example.com/bg.jpg",
            },
        )
        assert status == 200
        assert body["ok"] is True
        data = body["data"]
        assert data["background_image"] == "https://example.com/bg.jpg"

        # Verify config on disk
        cfg = json.loads((ld / "config.json").read_text())
        assert cfg["dashboard"]["background_image"] == "https://example.com/bg.jpg"

    def test_clear_background_image(self, dashboard_server):
        """Setting background_image to null should remove it."""
        base_url, ld, _ids = dashboard_server

        # Set it first
        _post(
            base_url,
            "/api/config/dashboard",
            {
                "background_image": "https://example.com/bg.jpg",
            },
        )

        # Clear it
        status, body = _post(
            base_url,
            "/api/config/dashboard",
            {
                "background_image": None,
            },
        )
        assert status == 200
        assert body["ok"] is True
        # background_image should not be present
        assert "background_image" not in body["data"]

    def test_clear_background_image_empty_string(self, dashboard_server):
        """Setting background_image to empty string should remove it."""
        base_url, _ld, _ids = dashboard_server

        # Set then clear with empty string
        _post(
            base_url,
            "/api/config/dashboard",
            {
                "background_image": "https://example.com/bg.jpg",
            },
        )
        status, body = _post(
            base_url,
            "/api/config/dashboard",
            {
                "background_image": "",
            },
        )
        assert status == 200
        assert "background_image" not in body["data"]

    def test_reject_javascript_background_image(self, dashboard_server):
        """background_image with javascript: scheme must be rejected."""
        base_url, _ld, _ids = dashboard_server

        status, body = _post(
            base_url,
            "/api/config/dashboard",
            {
                "background_image": "javascript:alert(1)",
            },
        )
        assert status == 400
        assert body["ok"] is False
        assert "http or https URL" in body["error"]["message"]

    def test_reject_data_uri_background_image(self, dashboard_server):
        """background_image with data: scheme must be rejected."""
        base_url, _ld, _ids = dashboard_server

        status, body = _post(
            base_url,
            "/api/config/dashboard",
            {
                "background_image": "data:text/html,<script>alert(1)</script>",
            },
        )
        assert status == 400
        assert body["ok"] is False
        assert "http or https URL" in body["error"]["message"]

    def test_reject_bare_string_background_image(self, dashboard_server):
        """background_image with no scheme must be rejected."""
        base_url, _ld, _ids = dashboard_server

        status, body = _post(
            base_url,
            "/api/config/dashboard",
            {
                "background_image": "not-a-url",
            },
        )
        assert status == 400
        assert body["ok"] is False
        assert "http or https URL" in body["error"]["message"]

    def test_accept_http_background_image(self, dashboard_server):
        """background_image with http:// scheme should be accepted."""
        base_url, _ld, _ids = dashboard_server

        status, body = _post(
            base_url,
            "/api/config/dashboard",
            {
                "background_image": "http://example.com/bg.jpg",
            },
        )
        assert status == 200
        assert body["ok"] is True
        assert body["data"]["background_image"] == "http://example.com/bg.jpg"

    def test_set_lane_colors(self, dashboard_server):
        """POST /api/config/dashboard should set lane_colors."""
        base_url, ld, _ids = dashboard_server

        colors = {
            "backlog": "#ff0000",
            "done": "#00ff00",
        }
        status, body = _post(
            base_url,
            "/api/config/dashboard",
            {
                "lane_colors": colors,
            },
        )
        assert status == 200
        assert body["ok"] is True
        assert body["data"]["lane_colors"] == colors

        # Verify config on disk
        cfg = json.loads((ld / "config.json").read_text())
        assert cfg["dashboard"]["lane_colors"]["backlog"] == "#ff0000"
        assert cfg["dashboard"]["lane_colors"]["done"] == "#00ff00"

    def test_set_both_settings(self, dashboard_server):
        """Setting background_image and lane_colors in one request."""
        base_url, _ld, _ids = dashboard_server

        status, body = _post(
            base_url,
            "/api/config/dashboard",
            {
                "background_image": "https://example.com/bg.jpg",
                "lane_colors": {"backlog": "#aaa"},
            },
        )
        assert status == 200
        assert body["data"]["background_image"] == "https://example.com/bg.jpg"
        assert body["data"]["lane_colors"]["backlog"] == "#aaa"

    def test_unknown_keys_rejected(self, dashboard_server):
        """Unknown keys in the body should be rejected."""
        base_url, _ld, _ids = dashboard_server

        status, body = _post(
            base_url,
            "/api/config/dashboard",
            {
                "unknown_key": "value",
            },
        )
        assert status == 400
        assert body["error"]["code"] == "VALIDATION_ERROR"

    def test_lane_colors_must_be_object(self, dashboard_server):
        """lane_colors must be an object, not a string."""
        base_url, _ld, _ids = dashboard_server

        status, body = _post(
            base_url,
            "/api/config/dashboard",
            {
                "lane_colors": "not-an-object",
            },
        )
        assert status == 400
        assert body["error"]["code"] == "VALIDATION_ERROR"

    def test_invalid_json_body(self, dashboard_server):
        """Malformed JSON body should return 400."""
        base_url, _ld, _ids = dashboard_server

        status, body = _post_raw(
            base_url,
            "/api/config/dashboard",
            b"not json",
        )
        assert status == 400
        assert body["error"]["code"] == "BAD_REQUEST"

    def test_config_preserved_after_dashboard_save(self, dashboard_server):
        """Saving dashboard config should not corrupt existing config keys."""
        base_url, ld, _ids = dashboard_server

        # Save dashboard config
        _post(
            base_url,
            "/api/config/dashboard",
            {
                "background_image": "https://example.com/bg.jpg",
            },
        )

        # Verify existing config keys are intact
        cfg = json.loads((ld / "config.json").read_text())
        assert "workflow" in cfg
        assert "statuses" in cfg["workflow"]
        assert cfg["schema_version"] == 1
        assert cfg["dashboard"]["background_image"] == "https://example.com/bg.jpg"

    def test_config_returned_via_get_after_save(self, dashboard_server):
        """GET /api/config should include dashboard settings after save."""
        base_url, _ld, _ids = dashboard_server

        # Save dashboard config
        _post(
            base_url,
            "/api/config/dashboard",
            {
                "background_image": "https://example.com/bg.jpg",
                "lane_colors": {"backlog": "#ff0000"},
            },
        )

        # Read back via GET
        status, body = _get(base_url, "/api/config")
        assert status == 200
        assert body["data"]["dashboard"]["background_image"] == "https://example.com/bg.jpg"
        assert body["data"]["dashboard"]["lane_colors"]["backlog"] == "#ff0000"


# ---------------------------------------------------------------------------
# POST routing edge cases
# ---------------------------------------------------------------------------


class TestPostRouting:
    def test_post_unknown_api_route(self, dashboard_server):
        """POST to unknown API route should return 404."""
        base_url, _ld, _ids = dashboard_server

        status, body = _post(base_url, "/api/nonexistent", {"data": "test"})
        assert status == 404
        assert body["ok"] is False

    def test_post_to_non_api_path(self, dashboard_server):
        """POST to a non-API path should return 404."""
        base_url, _ld, _ids = dashboard_server

        status, body = _post(base_url, "/random/path", {"data": "test"})
        assert status == 404

    def test_post_to_task_without_sub_route(self, dashboard_server):
        """POST /api/tasks/<id> (without /status) should return 404."""
        base_url, _ld, ids = dashboard_server
        task_id = ids["backlog"]

        status, body = _post(base_url, f"/api/tasks/{task_id}", {"status": "in_planning"})
        assert status == 404


# ---------------------------------------------------------------------------
# POST body size limit (DoS prevention)
# ---------------------------------------------------------------------------


class TestPayloadSizeLimit:
    def test_oversized_content_length_rejected_with_413(self, dashboard_server):
        """A Content-Length exceeding MAX_REQUEST_BODY_BYTES should return 413."""
        import http.client

        from lattice.dashboard.server import MAX_REQUEST_BODY_BYTES

        base_url, _ld, ids = dashboard_server
        task_id = ids["backlog"]

        # Use http.client directly to send a request with a spoofed
        # Content-Length that is too large, without actually sending that much
        # data.  The server checks the header before reading.
        url = f"/api/tasks/{task_id}/status"
        host = base_url.replace("http://", "")
        conn = http.client.HTTPConnection(host)
        small_body = b'{"status":"in_planning"}'
        conn.putrequest("POST", url)
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(MAX_REQUEST_BODY_BYTES + 1))
        conn.endheaders(small_body)

        resp = conn.getresponse()
        assert resp.status == 413
        body = json.loads(resp.read().decode("utf-8"))
        assert body["ok"] is False
        assert body["error"]["code"] == "PAYLOAD_TOO_LARGE"
        conn.close()

    def test_normal_body_accepted(self, dashboard_server):
        """A normal-sized body should work fine."""
        base_url, _ld, ids = dashboard_server
        task_id = ids["backlog"]

        status, body = _post(
            base_url,
            f"/api/tasks/{task_id}/status",
            {"status": "in_planning", "actor": "dashboard:web"},
        )
        assert status == 200


# ---------------------------------------------------------------------------
# Read-only mode
# ---------------------------------------------------------------------------


class TestReadonlyMode:
    @pytest.fixture()
    def readonly_server(self, populated_lattice_dir):
        """Start a dashboard server in readonly mode."""
        import socket
        import threading

        from lattice.dashboard.server import create_server

        ld, task_ids = populated_lattice_dir
        # Find free port
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]

        server = create_server(ld, "127.0.0.1", port, readonly=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        base_url = f"http://127.0.0.1:{port}"
        yield base_url, ld, task_ids

        server.shutdown()
        server.server_close()

    def test_post_returns_403_in_readonly(self, readonly_server):
        """All POST requests should return 403 FORBIDDEN in readonly mode."""
        base_url, _ld, ids = readonly_server
        task_id = ids["backlog"]

        status, body = _post(
            base_url,
            f"/api/tasks/{task_id}/status",
            {"status": "in_planning"},
        )
        assert status == 403
        assert body["ok"] is False
        assert body["error"]["code"] == "FORBIDDEN"

    def test_post_create_returns_403_in_readonly(self, readonly_server):
        """POST /api/tasks should also return 403 in readonly mode."""
        base_url, _ld, _ids = readonly_server

        status, body = _post(
            base_url,
            "/api/tasks",
            {"title": "New task", "actor": "dashboard:web"},
        )
        assert status == 403
        assert body["error"]["code"] == "FORBIDDEN"

    def test_get_still_works_in_readonly(self, readonly_server):
        """GET requests should still work in readonly mode."""
        base_url, _ld, _ids = readonly_server

        status, body = _get(base_url, "/api/tasks")
        assert status == 200
        assert body["ok"] is True

    def test_get_config_still_works_in_readonly(self, readonly_server):
        """GET /api/config should still work in readonly mode."""
        base_url, _ld, _ids = readonly_server

        status, body = _get(base_url, "/api/config")
        assert status == 200
        assert body["ok"] is True
