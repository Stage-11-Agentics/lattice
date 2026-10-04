"""Tests for the Lattice dashboard HTTP server and API endpoints."""

from __future__ import annotations

import json
from urllib.request import Request, urlopen

import pytest

from lattice.core.events import create_event, serialize_event
from lattice.core.ids import generate_artifact_id, generate_event_id, generate_task_id
from lattice.core.tasks import apply_event_to_snapshot, serialize_snapshot


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
        headers={"Content-Type": "application/json", "Origin": base_url},
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
        headers={"Content-Type": content_type, "Origin": base_url},
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
        assert "renderAcceptanceCriteria" in body
        assert "linked evidence" in body
        assert "acceptance_criterion_retired" in body


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
        task_id = ids["in_progress"]
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
        task_id = ids["in_progress"]
        status, body = _get(base_url, f"/api/tasks/{task_id}")
        arts = body["data"]["artifacts"]
        assert len(arts) == 1
        assert arts[0]["title"] == "dep-report.txt"
        assert arts[0]["type"] == "text/plain"

    def test_wrong_only_unarchive_is_visible_in_list_detail_events_and_activity(
        self, dashboard_server
    ):
        base_url, lattice_dir, ids = dashboard_server
        task_id = ids["backlog"]
        active_event = lattice_dir / "events" / f"{task_id}.jsonl"
        archived_event = lattice_dir / "archive" / "events" / f"{task_id}.jsonl"
        archived_event.parent.mkdir(parents=True, exist_ok=True)
        # Force the worst case of a same-second pair: timestamps tie, and the
        # earlier event carries the larger ULID (ULIDs are not guaranteed
        # monotonic). The log order must still decide which is newest.
        tied_ts = "2025-02-01T00:00:00Z"
        low_id, high_id = sorted((generate_event_id(), generate_event_id()))
        archived_event.write_bytes(
            active_event.read_bytes()
            + serialize_event(
                create_event(
                    "task_archived", task_id, "human:test", {}, event_id=high_id, ts=tied_ts
                )
            ).encode()
            + serialize_event(
                create_event(
                    "task_unarchived", task_id, "human:test", {}, event_id=low_id, ts=tied_ts
                )
            ).encode()
        )
        active_event.unlink()
        active_notes = lattice_dir / "notes" / f"{task_id}.md"
        archived_notes = lattice_dir / "archive" / "notes" / f"{task_id}.md"
        archived_notes.parent.mkdir(parents=True, exist_ok=True)
        archived_notes.write_bytes(active_notes.read_bytes())
        active_notes.unlink()
        archived_task_id = ids["done"]
        archived_active_event = lattice_dir / "events" / f"{archived_task_id}.jsonl"
        split_archived_event = lattice_dir / "archive" / "events" / f"{archived_task_id}.jsonl"
        split_archived_event.write_bytes(
            archived_active_event.read_bytes()
            + serialize_event(
                create_event("task_archived", archived_task_id, "human:test", {})
            ).encode()
        )

        status, tasks = _get(base_url, "/api/tasks")
        assert status == 200
        assert task_id in {task["id"] for task in tasks["data"]}
        assert archived_task_id not in {task["id"] for task in tasks["data"]}
        status, archived_tasks = _get(base_url, "/api/archived")
        assert status == 200
        assert archived_task_id in {task["id"] for task in archived_tasks["data"]}
        status, detail = _get(base_url, f"/api/tasks/{task_id}")
        assert status == 200
        assert detail["data"]["notes_exists"] is True
        status, events = _get(base_url, f"/api/tasks/{task_id}/events")
        assert status == 200
        assert events["data"][0]["type"] == "task_unarchived"
        status, activity = _get(base_url, f"/api/activity?task={task_id}")
        assert status == 200
        assert activity["data"]["events"][0]["type"] == "task_unarchived"
        assert task_id in {task["id"] for task in activity["data"]["facets"]["tasks"]}

    def test_task_detail_preserves_criteria_history_and_full_evidence_refs(self, dashboard_server):
        base_url, lattice_dir, ids = dashboard_server
        task_id = ids["in_progress"]
        snapshot_path = lattice_dir / "tasks" / f"{task_id}.json"
        event_path = lattice_dir / "events" / f"{task_id}.jsonl"
        snapshot = json.loads(snapshot_path.read_text())
        artifact_id = generate_artifact_id()
        events = [
            create_event(
                "acceptance_criterion_added",
                task_id,
                "human:test",
                {
                    "criterion_id": "AC-1",
                    "outcome": "Dependencies update.",
                    "revision": 1,
                },
                ts="2025-01-10T12:20:00Z",
            ),
            create_event(
                "acceptance_criterion_edited",
                task_id,
                "human:test",
                {
                    "criterion_id": "AC-1",
                    "from_outcome": "Dependencies update.",
                    "outcome": "Dependencies update without regressions.",
                    "revision": 2,
                },
                ts="2025-01-10T12:21:00Z",
            ),
            create_event(
                "comment_added",
                task_id,
                "human:test",
                {"body": "Observed.", "criterion_ids": ["AC-1"]},
                ts="2025-01-10T12:22:00Z",
            ),
            create_event(
                "artifact_attached",
                task_id,
                "human:test",
                {"artifact_id": artifact_id, "criterion_ids": ["AC-1"]},
                ts="2025-01-10T12:23:00Z",
            ),
        ]
        with event_path.open("a", encoding="utf-8") as handle:
            for event in events:
                snapshot = apply_event_to_snapshot(snapshot, event)
                handle.write(serialize_event(event))
        snapshot_path.write_text(serialize_snapshot(snapshot))
        (lattice_dir / "artifacts" / "meta" / f"{artifact_id}.json").write_text(
            json.dumps(
                {"id": artifact_id, "title": "criteria.txt", "type": "file"},
                sort_keys=True,
                indent=2,
            )
            + "\n"
        )

        status, body = _get(base_url, f"/api/tasks/{task_id}")
        assert status == 200
        criterion = body["data"]["acceptance_criteria"][0]
        assert criterion["revision"] == 2
        assert len(criterion["revisions"]) == 2
        artifact = next(item for item in body["data"]["artifacts"] if item["id"] == artifact_id)
        assert artifact["role"] is None
        assert artifact["criterion_ids"] == ["AC-1"]

        status, comments = _get(base_url, f"/api/tasks/{task_id}/comments")
        linked_comment = next(item for item in comments["data"] if item["body"] == "Observed.")
        assert linked_comment["criterion_ids"] == ["AC-1"]

    def test_comment_edit_api_clears_role_without_losing_criterion_link(self, dashboard_server):
        base_url, lattice_dir, ids = dashboard_server
        task_id = ids["in_progress"]
        snapshot_path = lattice_dir / "tasks" / f"{task_id}.json"
        event_path = lattice_dir / "events" / f"{task_id}.jsonl"
        snapshot = json.loads(snapshot_path.read_text())
        criterion = create_event(
            "acceptance_criterion_added",
            task_id,
            "human:test",
            {"criterion_id": "AC-1", "outcome": "Observable.", "revision": 1},
        )
        comment = create_event(
            "comment_added",
            task_id,
            "human:test",
            {"body": "Observed.", "role": "review", "criterion_ids": ["AC-1"]},
        )
        with event_path.open("a", encoding="utf-8") as handle:
            for event in (criterion, comment):
                snapshot = apply_event_to_snapshot(snapshot, event)
                handle.write(serialize_event(event))
        snapshot_path.write_text(serialize_snapshot(snapshot))

        status, omitted = _post(
            base_url,
            f"/api/tasks/{task_id}/comment-edit",
            {
                "comment_id": comment["id"],
                "body": "Observed again.",
                "actor": "human:test",
            },
        )
        assert status == 200
        omitted_ref = next(
            ref for ref in omitted["data"]["evidence_refs"] if ref["source_type"] == "comment"
        )
        assert omitted_ref["role"] == "review"
        assert omitted_ref["criterion_ids"] == ["AC-1"]

        before_conflict = event_path.read_bytes()
        status, conflict = _post(
            base_url,
            f"/api/tasks/{task_id}/comment-edit",
            {
                "comment_id": comment["id"],
                "body": "Observed again.",
                "role": "review",
                "clear_role": True,
                "actor": "human:test",
            },
        )
        assert status == 400
        assert conflict["error"]["code"] == "VALIDATION_ERROR"
        assert event_path.read_bytes() == before_conflict

        status, cleared = _post(
            base_url,
            f"/api/tasks/{task_id}/comment-edit",
            {
                "comment_id": comment["id"],
                "body": "Observed again.",
                "clear_role": True,
                "actor": "human:test",
            },
        )
        assert status == 200
        cleared_ref = next(
            ref for ref in cleared["data"]["evidence_refs"] if ref["source_type"] == "comment"
        )
        assert cleared_ref == {
            "id": comment["id"],
            "role": None,
            "source_type": "comment",
            "criterion_ids": ["AC-1"],
        }
        clear_event = json.loads(event_path.read_text().splitlines()[-1])
        assert clear_event["type"] == "comment_edited"
        assert clear_event["data"]["role"] is None
        assert clear_event["data"]["previous_role"] == "review"

        after_clear = event_path.read_bytes()
        status, repeated = _post(
            base_url,
            f"/api/tasks/{task_id}/comment-edit",
            {
                "comment_id": comment["id"],
                "body": "Observed again.",
                "clear_role": True,
                "actor": "human:test",
            },
        )
        assert status == 200
        assert repeated["data"]["evidence_refs"] == cleared["data"]["evidence_refs"]
        assert event_path.read_bytes() == after_clear

    def test_archived_task_fallthrough(self, dashboard_server):
        base_url, _ld, ids = dashboard_server
        task_id = ids["archived"]
        status, body = _get(base_url, f"/api/tasks/{task_id}")
        assert status == 200
        assert body["ok"] is True
        assert body["data"]["archived"] is True
        assert body["data"]["title"] == "Old spike task"
        assert body["data"]["type"] == "spike"
        config = json.loads((_ld / "config.json").read_text())
        assert config["task_types"] == ["task", "bug", "chore"]

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
        task_id = ids["in_progress"]
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
        data = body["data"]
        # Response is now an envelope with events, total, offset, limit, has_more, facets
        assert "events" in data
        assert "total" in data
        assert "offset" in data
        assert "limit" in data
        assert "has_more" in data
        assert "facets" in data
        events = data["events"]
        assert isinstance(events, list)
        assert len(events) > 0

        # Should include non-lifecycle events (comment, relationship, etc.)
        event_types = {e["type"] for e in events}
        assert "comment_added" in event_types or "status_changed" in event_types

        # Newest first
        timestamps = [e["ts"] for e in events]
        assert timestamps == sorted(timestamps, reverse=True)

        # Facets should have types, actors, tasks
        facets = data["facets"]
        assert isinstance(facets["types"], list)
        assert isinstance(facets["actors"], list)
        assert isinstance(facets["tasks"], list)
        assert len(facets["types"]) > 0
        assert len(facets["actors"]) > 0

    def test_activity_same_second_ties_follow_log_within_task_and_id_across_tasks(self):
        """Same-second events of one task sort by log position, not by ULID."""
        from lattice.dashboard.api import sort_activity_newest_first as _sort_activity_newest_first

        tied = "2025-02-01T00:00:00Z"
        # Task A's log: archived then unarchived, but archived holds the larger ID.
        a_archived = {"id": "ev_9", "task_id": "task_a", "ts": tied, "type": "task_archived"}
        a_unarchived = {"id": "ev_1", "task_id": "task_a", "ts": tied, "type": "task_unarchived"}
        # Task B ties the same second; cross-task ties keep falling to the ID.
        b_comment = {"id": "ev_5", "task_id": "task_b", "ts": tied, "type": "comment_added"}
        older = {"id": "ev_0", "task_id": "task_b", "ts": "2025-01-31T23:59:59Z", "type": "x"}

        ordered = _sort_activity_newest_first([a_archived, a_unarchived, older, b_comment])

        assert [e["type"] for e in ordered] == [
            "task_unarchived",
            "comment_added",
            "task_archived",
            "x",
        ]

    def test_activity_pagination(self, dashboard_server):
        """Limit and offset should control the returned page."""
        base_url, _ld, _ids = dashboard_server
        # Get all events first
        status, body = _get(base_url, "/api/activity?limit=200")
        total = body["data"]["total"]
        all_events = body["data"]["events"]
        assert total > 0

        # Now paginate with limit=2
        status, body = _get(base_url, "/api/activity?limit=2&offset=0")
        assert status == 200
        data = body["data"]
        assert len(data["events"]) == min(2, total)
        assert data["offset"] == 0
        assert data["limit"] == 2
        if total > 2:
            assert data["has_more"] is True

        # Second page
        status, body = _get(base_url, "/api/activity?limit=2&offset=2")
        data = body["data"]
        assert data["offset"] == 2
        # Events should be different from first page
        if total > 2:
            assert data["events"][0]["id"] != all_events[0]["id"]

    def test_activity_type_filter(self, dashboard_server):
        """Filtering by event type should return only matching events."""
        base_url, _ld, _ids = dashboard_server
        status, body = _get(base_url, "/api/activity?type=comment_added&limit=200")
        assert status == 200
        events = body["data"]["events"]
        for ev in events:
            assert ev["type"] == "comment_added"

    def test_activity_multi_type_filter(self, dashboard_server):
        """Comma-separated type filter should match any listed type."""
        base_url, _ld, _ids = dashboard_server
        status, body = _get(base_url, "/api/activity?type=comment_added,status_changed&limit=200")
        assert status == 200
        events = body["data"]["events"]
        for ev in events:
            assert ev["type"] in ("comment_added", "status_changed")

    def test_activity_task_filter_ulid(self, dashboard_server):
        """Filtering by task ULID should return only that task's events."""
        base_url, _ld, ids = dashboard_server
        task_id = ids["in_progress"]
        status, body = _get(base_url, f"/api/activity?task={task_id}&limit=200")
        assert status == 200
        events = body["data"]["events"]
        assert len(events) > 0
        for ev in events:
            assert ev["task_id"] == task_id

    def test_activity_actor_filter(self, dashboard_server):
        """Filtering by actor should return only matching events."""
        base_url, _ld, _ids = dashboard_server
        status, body = _get(base_url, "/api/activity?actor=agent:claude&limit=200")
        assert status == 200
        events = body["data"]["events"]
        for ev in events:
            assert ev["actor"] == "agent:claude"

    def test_activity_date_range(self, dashboard_server):
        """After/before filters should constrain timestamps."""
        base_url, _ld, _ids = dashboard_server
        # Events in the fixture are from 2025-01-10
        status, body = _get(
            base_url,
            "/api/activity?after=2025-01-10T12:00:00Z&limit=200",
        )
        assert status == 200
        events = body["data"]["events"]
        for ev in events:
            assert ev["ts"] > "2025-01-10T12:00:00Z"

    def test_activity_search(self, dashboard_server):
        """Search filter should match text in event data."""
        base_url, _ld, _ids = dashboard_server
        status, body = _get(base_url, "/api/activity?search=dependency%20audit&limit=200")
        assert status == 200
        events = body["data"]["events"]
        # Should find the comment "Starting dependency audit."
        assert len(events) >= 1
        found = any(
            "dependency audit" in (e.get("data", {}).get("body") or "").lower() for e in events
        )
        assert found

    def test_activity_invalid_task_filter(self, dashboard_server):
        """Invalid task ID format should return 400."""
        base_url, _ld, _ids = dashboard_server
        status, body = _get(base_url, "/api/activity?task=not-valid-not-short")
        assert status == 400
        assert body["ok"] is False
        assert body["error"]["code"] == "VALIDATION_ERROR"

    def test_activity_facets_completeness(self, dashboard_server):
        """Facets should list all event types and actors present."""
        base_url, _ld, _ids = dashboard_server
        status, body = _get(base_url, "/api/activity?limit=200")
        facets = body["data"]["facets"]
        # We know there are human:atin and agent:claude actors in the fixture
        assert "human:atin" in facets["actors"]
        # Types should include task_created at minimum
        assert "task_created" in facets["types"]

    def test_activity_default_backward_compat(self, dashboard_server):
        """Default request (no params) should return events in envelope."""
        base_url, _ld, _ids = dashboard_server
        status, body = _get(base_url, "/api/activity")
        assert status == 200
        data = body["data"]
        assert data["limit"] == 50
        assert data["offset"] == 0
        assert isinstance(data["events"], list)


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
        assert status_dict.get("in_progress") == 1
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
        """A corrupted authoritative event log is reported, never partially read."""
        base_url, ld, ids = dashboard_server
        task_id = ids["backlog"]
        event_path = ld / "events" / f"{task_id}.jsonl"

        # Append a corrupted line
        with open(event_path, "a") as fh:
            fh.write("{truncated json\n")

        status, body = _get(base_url, f"/api/tasks/{task_id}/events")
        assert status == 409
        assert body["error"]["code"] == "INTEGRITY_ERROR"
        assert str(event_path) in body["error"]["message"]


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
        """Port-in-use should produce a JSON error envelope with PORT_IN_USE code."""
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
            parsed = json.loads(result.stdout)
            assert parsed["ok"] is False
            assert parsed["error"]["code"] == "PORT_IN_USE"
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
        """The move writes the CLI's events: entering active work unassigned
        auto-assigns the mover first, then the status_changed event."""
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
        assert len(lines_after) == len(lines_before) + 2
        assert json.loads(lines_after[-2])["type"] == "assignment_changed"

        new_event = json.loads(lines_after[-1])
        assert new_event["type"] == "status_changed"
        assert new_event["data"]["from"] == "backlog"
        assert new_event["data"]["to"] == "in_planning"
        assert new_event["actor"] == "dashboard:web"

    def test_invalid_transition(self, dashboard_server):
        """An invalid transition is the CLI's 422 INVALID_TRANSITION, naming the
        CLI override (the dashboard has no force control)."""
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
        assert status == 422
        assert body["ok"] is False
        assert body["error"]["code"] == "INVALID_TRANSITION"
        assert "--force --reason" in body["error"]["message"]

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
        assert body["error"]["code"] == "INVALID_ACTOR"

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

        # The plan gate applies, as in the CLI: write a plan first.
        (ld / "plans" / f"{task_id}.md").write_text("# Plan\n\nFix the redirect.\n")

        # planned -> in_progress
        status, body = _post(
            base_url,
            f"/api/tasks/{task_id}/status",
            {
                "status": "in_progress",
            },
        )
        assert status == 200
        assert body["data"]["status"] == "in_progress"

        # in_progress -> review
        status, body = _post(
            base_url,
            f"/api/tasks/{task_id}/status",
            {
                "status": "review",
            },
        )
        assert status == 200
        assert body["data"]["status"] == "review"

        # review -> done: the completion policy applies, as in the CLI (no
        # review artifact yet), and the refusal names the CLI override.
        status, body = _post(
            base_url,
            f"/api/tasks/{task_id}/status",
            {
                "status": "done",
            },
        )
        assert status == 422
        assert body["error"]["code"] == "COMPLETION_BLOCKED"
        assert "lattice status" in body["error"]["message"]


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

    def test_set_lane_sort(self, dashboard_server):
        """POST /api/config/dashboard should set per-lane sort modes."""
        base_url, ld, _ids = dashboard_server

        lane_sort = {
            "backlog": "priority",
            "review": "status_age_desc",
        }
        status, body = _post(
            base_url,
            "/api/config/dashboard",
            {
                "lane_sort": lane_sort,
            },
        )
        assert status == 200
        assert body["ok"] is True
        assert body["data"]["lane_sort"] == lane_sort

        # Verify config on disk
        cfg = json.loads((ld / "config.json").read_text())
        assert cfg["dashboard"]["lane_sort"]["backlog"] == "priority"
        assert cfg["dashboard"]["lane_sort"]["review"] == "status_age_desc"

    def test_set_lane_sort_rejects_non_object(self, dashboard_server):
        """lane_sort must be an object."""
        base_url, _ld, _ids = dashboard_server

        status, body = _post(base_url, "/api/config/dashboard", {"lane_sort": "priority"})
        assert status == 400
        assert body["ok"] is False
        assert body["error"]["code"] == "VALIDATION_ERROR"

    def test_set_lane_sort_rejects_non_string_values(self, dashboard_server):
        """lane_sort values must be strings."""
        base_url, _ld, _ids = dashboard_server

        status, body = _post(base_url, "/api/config/dashboard", {"lane_sort": {"backlog": 3}})
        assert status == 400
        assert body["ok"] is False
        assert body["error"]["code"] == "VALIDATION_ERROR"

    def test_lane_sort_and_done_display_persist_together(self, dashboard_server):
        """The done lane writes both keys — they must not clobber each other."""
        base_url, ld, _ids = dashboard_server

        status, body = _post(
            base_url,
            "/api/config/dashboard",
            {
                "lane_sort": {"done": "group:day"},
                "done_display": "grouped",
            },
        )
        assert status == 200
        cfg = json.loads((ld / "config.json").read_text())
        assert cfg["dashboard"]["lane_sort"]["done"] == "group:day"
        assert cfg["dashboard"]["done_display"] == "grouped"

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
    def test_negative_content_length_is_rejected_before_reading_issue_body(self, dashboard_server):
        """Negative Content-Length must not reach read(-1) and bypass the cap."""
        import http.client

        from lattice.core.config import serialize_config
        from lattice.dashboard import api
        from lattice.storage.fs import atomic_write

        base_url, ld, _ids = dashboard_server
        config = api.get_config(ld)
        config["issues"] = {"enabled": True}
        atomic_write(ld / "config.json", serialize_config(config))
        host = base_url.replace("http://", "")

        conn = http.client.HTTPConnection(host)
        conn.putrequest("POST", "/api/issues")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Origin", base_url)
        conn.putheader("Content-Length", "-1")
        conn.endheaders()
        response = conn.getresponse()
        assert response.status == 400
        error = json.loads(response.read())["error"]
        assert error["code"] == "BAD_REQUEST"
        assert "Content-Length" in error["message"]
        conn.close()

    def test_issue_file_has_a_larger_hard_bounded_route_limit(self, dashboard_server):
        """Quick-file JSON may exceed 1 MiB, while its route still has a hard ceiling."""
        import http.client

        from lattice.core.config import serialize_config
        from lattice.dashboard import api
        from lattice.dashboard.server import (
            MAX_ISSUE_FILE_BODY_BYTES,
            MAX_REQUEST_BODY_BYTES,
            issue_file_body_limit,
        )
        from lattice.storage.fs import atomic_write

        base_url, ld, _ids = dashboard_server
        config = api.get_config(ld)
        config["issues"] = {"enabled": True}
        atomic_write(ld / "config.json", serialize_config(config))
        host = base_url.replace("http://", "")

        # A valid JSON body just over the ordinary cap reaches issue.file.
        prefix = b'{"title":"x","padding":"'
        suffix = b'"}'
        body = prefix + b"x" * (MAX_REQUEST_BODY_BYTES + 1 - len(prefix) - len(suffix)) + suffix
        conn = http.client.HTTPConnection(host)
        conn.request(
            "POST",
            "/api/issues",
            body=body,
            headers={"Content-Type": "application/json", "Origin": base_url},
        )
        response = conn.getresponse()
        assert response.status == 201
        assert json.loads(response.read())["data"]["title"] == "x"
        conn.close()

        # The issue route's configured allowance is still capped, and checks
        # Content-Length before attempting to read the declared body.
        route_limit = issue_file_body_limit(ld)
        assert MAX_REQUEST_BODY_BYTES < route_limit <= MAX_ISSUE_FILE_BODY_BYTES
        conn = http.client.HTTPConnection(host)
        conn.putrequest("POST", "/api/issues")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Origin", base_url)
        conn.putheader("Content-Length", str(route_limit + 1))
        conn.endheaders()
        response = conn.getresponse()
        assert response.status == 413
        error = json.loads(response.read())["error"]
        assert error["code"] == "PAYLOAD_TOO_LARGE"
        assert str(route_limit) in error["message"]
        conn.close()

    def test_stalled_upload_does_not_block_other_requests(self, dashboard_server, monkeypatch):
        """A client that declares a body and then stalls holds only its own connection."""
        import http.client
        import socket

        from lattice.core.config import serialize_config
        from lattice.dashboard import api, media
        from lattice.storage.fs import atomic_write

        monkeypatch.setattr(media, "SOCKET_TIMEOUT", 1)
        base_url, ld, _ids = dashboard_server
        config = api.get_config(ld)
        config["issues"] = {"enabled": True}
        atomic_write(ld / "config.json", serialize_config(config))
        host, port = base_url.replace("http://", "").split(":")

        stalled = socket.create_connection((host, int(port)))
        try:
            stalled.sendall(
                (
                    f"POST /api/issues HTTP/1.1\r\nHost: {host}:{port}\r\n"
                    f"Origin: {base_url}\r\nContent-Type: application/json\r\n"
                    'Content-Length: 1000\r\n\r\n{"title"'
                ).encode()
            )
            conn = http.client.HTTPConnection(host, int(port), timeout=0.5)
            conn.request("GET", "/api/tasks")
            assert conn.getresponse().status == 200
            conn.close()
            # The stalled body times out with an error instead of hanging forever.
            stalled.settimeout(5)
            assert stalled.recv(64).startswith(b"HTTP/1.0 408")
        finally:
            stalled.close()

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
        conn.putheader("Origin", base_url)
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
        thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
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


class TestIssueHostGuard:
    """Issue routes follow the dashboard's one Host rule: loopback hosts only on a
    loopback bind, any Host on a network bind (as ``origin_allowed`` for POSTs)."""

    @staticmethod
    def _serve(tmp_path, bind, *, advertised_host=None):
        import threading

        from lattice.core.config import default_config, serialize_config
        from lattice.dashboard.server import create_server
        from lattice.storage.fs import atomic_write, ensure_lattice_dirs

        ensure_lattice_dirs(tmp_path)
        lattice_dir = tmp_path / ".lattice"
        atomic_write(lattice_dir / "config.json", serialize_config(default_config()))
        server = create_server(lattice_dir, bind, 0)
        if advertised_host is not None:
            server.server_address = (advertised_host, server.server_address[1])
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        return server, worker

    @staticmethod
    def _request(port, method, path, host, body=None):
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        headers = {"Host": host}
        payload = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            headers["Origin"] = f"http://{host}"
            payload = json.dumps(body)
        conn.request(method, path, payload, headers)
        response = conn.getresponse()
        status = response.status
        envelope = json.loads(response.read())
        conn.close()
        return status, envelope

    def test_loopback_bind_refuses_hostile_hosts_on_issue_routes(self, tmp_path):
        server, worker = self._serve(tmp_path, "127.0.0.1")
        port = server.server_address[1]
        try:
            hostile = f"evil.example:{port}"
            assert self._request(port, "GET", "/api/issues", hostile)[0] == 403
            assert self._request(port, "POST", "/api/issues", hostile, {"title": "x"})[0] == 403
            media_path = (
                "/api/issues/iss_01ARZ3NDEKTSV4RRFFQ69G5FAV/media/med_01ARZ3NDEKTSV4RRFFQ69G5FAV"
            )
            assert self._request(port, "GET", media_path, hostile)[0] == 403
            assert self._request(port, "GET", "/api/issues", f"localhost:{port}")[0] == 409
            assert self._request(port, "GET", "/api/issues", f"127.0.0.1:{port}")[0] == 409
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)

    def test_network_bind_serves_issue_routes_to_any_host(self, tmp_path):
        network_host = "0.0.0.0"
        server, worker = self._serve(tmp_path, "127.0.0.1", advertised_host=network_host)
        port = server.server_address[1]
        try:
            assert server.server_address == ("0.0.0.0", port)
            for host in (f"box.lan:{port}", f"198.51.100.9:{port}", f"0.0.0.0:{port}"):
                # 409: issues are off on this board, which means the Host was accepted.
                assert self._request(port, "GET", "/api/issues", host)[0] == 409
                assert self._request(
                    port, "GET", "/api/issues/iss_01ARZ3NDEKTSV4RRFFQ69G5FAV", host
                )[0] in (404, 409)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)


class TestIssueWritesOverHttp:
    """What the dashboard does to an issue filing or comment between the socket and the board."""

    @staticmethod
    def _board(tmp_path, *, default_actor=None):
        import threading

        from lattice.core.config import default_config, serialize_config
        from lattice.dashboard.server import create_server
        from lattice.storage.fs import atomic_write, ensure_lattice_dirs

        ensure_lattice_dirs(tmp_path)
        lattice_dir = tmp_path / ".lattice"
        config = default_config()
        config["issues"] = {"enabled": True}
        if default_actor is None:
            config.pop("default_actor", None)
        else:
            config["default_actor"] = default_actor
        atomic_write(lattice_dir / "config.json", serialize_config(config))
        server = create_server(lattice_dir, "127.0.0.1", 0)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        return server, worker, lattice_dir

    @staticmethod
    def _post(server, path, body):
        import http.client

        host = f"127.0.0.1:{server.server_address[1]}"
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=30)
        conn.request(
            "POST",
            path,
            json.dumps(body),
            {"Content-Type": "application/json", "Origin": f"http://{host}", "Host": host},
        )
        response = conn.getresponse()
        status, envelope = response.status, json.loads(response.read())
        conn.close()
        return status, envelope

    @staticmethod
    def _stop(server, worker):
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)

    def test_a_geo_tagged_video_filed_over_http_is_stored_without_its_location(
        self, tmp_path, monkeypatch
    ):
        """The media step is wired into POST /api/issues: delete the call and this goes red."""
        import shutil
        import subprocess

        import pytest

        from lattice.ops.task_attach import encode_payload

        if shutil.which("ffmpeg") is None:
            pytest.skip("needs ffmpeg")
        monkeypatch.delenv("LATTICE_FFMPEG")  # the suite turns ffmpeg off; this test needs it
        src = tmp_path / "geo.mp4"
        subprocess.run(
            [
                "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=1:size=64x48:rate=5",
                "-pix_fmt", "yuv420p", "-metadata", "location=+37.7749-122.4194/",
                "-metadata", "title=home", str(src),
            ],
            check=True,
        )  # fmt: skip
        server, worker, lattice_dir = self._board(tmp_path / "board")
        try:
            status, envelope = self._post(
                server,
                "/api/issues",
                {
                    "title": "geo",
                    "media": [{"payload": encode_payload("geo.mp4", src.read_bytes())}],
                },
            )
            assert status == 201, envelope
            stored = next((lattice_dir / "issues" / "media").rglob("*.mp4"))
            tags = subprocess.run(
                ["ffprobe", "-v", "error", "-show_format", str(stored)],
                capture_output=True,
                text=True,
            ).stdout.lower()
            assert "location" not in tags and "37.7749" not in tags
            assert envelope["data"]["media"][0]["frames"], "ffmpeg's frames are stored"
        finally:
            self._stop(server, worker)

    def test_geo_tagged_photos_filed_over_http_are_stored_without_location_metadata(
        self, tmp_path
    ):
        import hashlib

        from tests.photo_metadata_helpers import (
            assert_no_identifying_metadata,
            jpeg_with_gps,
            png_with_gps,
        )
        from lattice.ops.task_attach import encode_payload

        server, worker, lattice_dir = self._board(tmp_path / "board")
        try:
            for name, content, content_type, suffix in (
                ("gps.jpg", jpeg_with_gps(), "image/jpeg", ".jpg"),
                ("gps.png", png_with_gps(), "image/png", ".png"),
            ):
                status, envelope = self._post(
                    server,
                    "/api/issues",
                    {
                        "title": name,
                        "media": [{"payload": encode_payload(name, content)}],
                    },
                )
                assert status == 201, envelope
                issue = envelope["data"]
                entry = issue["media"][0]
                stored = lattice_dir / "issues" / "media" / issue["id"] / f"{entry['id']}{suffix}"
                clean = stored.read_bytes()
                assert_no_identifying_metadata(clean, content_type)
                assert entry["sha256"] == hashlib.sha256(clean).hexdigest()
                assert entry["size_bytes"] == len(clean)
        finally:
            self._stop(server, worker)

    def test_heic_conversion_output_is_sanitized_before_dashboard_filing(
        self, tmp_path, monkeypatch
    ):
        import hashlib

        from lattice.integrations import ffmpeg
        from lattice.ops.task_attach import decode_payload, encode_payload
        from lattice.dashboard import media_prep
        from tests.issue_media_helpers import heic
        from tests.photo_metadata_helpers import assert_no_identifying_metadata, jpeg_with_gps

        converted = jpeg_with_gps()
        monkeypatch.setattr(ffmpeg, "convert_heic", lambda _path: converted)
        original_payload = encode_payload("IMG_1.HEIC", heic())
        prepared = media_prep.prepare_issue_media([{"payload": original_payload}])[0]
        _prepared_name, prepared_bytes = decode_payload(prepared["payload"])
        assert_no_identifying_metadata(prepared_bytes, "image/jpeg")
        assert prepared["payload"]["sha256"] == hashlib.sha256(prepared_bytes).hexdigest()
        server, worker, lattice_dir = self._board(tmp_path / "board")
        try:
            status, envelope = self._post(
                server,
                "/api/issues",
                {
                    "title": "converted HEIC",
                    "media": [{"payload": original_payload}],
                },
            )
            assert status == 201, envelope
            issue = envelope["data"]
            entry = issue["media"][0]
            assert entry["content_type"] == "image/jpeg"
            assert entry["converted_from"]["content_type"] == "image/heic"
            stored_path = next(
                (lattice_dir / "issues" / "media" / issue["id"]).glob(f"{entry['id']}.*")
            )
            clean = stored_path.read_bytes()
            assert_no_identifying_metadata(clean, "image/jpeg")
            assert entry["sha256"] == hashlib.sha256(clean).hexdigest()
            assert entry["size_bytes"] == len(clean)
        finally:
            self._stop(server, worker)

    def test_dashboard_refusal_names_the_full_cli_photo_metadata_escape(self, tmp_path):
        from lattice.ops.task_attach import encode_payload

        malformed = b"\xff\xd8\xff\xe1\x00\x20Exif"
        server, worker, _lattice_dir = self._board(tmp_path / "board")
        try:
            status, envelope = self._post(
                server,
                "/api/issues",
                {
                    "title": "malformed photo",
                    "media": [{"payload": encode_payload("bad.jpg", malformed)}],
                },
            )
            assert status == 400
            assert envelope["error"]["code"] == "VALIDATION_ERROR"
            message = envelope["error"]["message"]
            assert "lattice issue file --evidence <photo> --keep-photo-metadata" in message
            assert "lattice issue attach <issue> <photo> --keep-photo-metadata" in message
        finally:
            self._stop(server, worker)

    def test_a_failing_media_step_answers_an_envelope_not_a_reset(self, tmp_path, monkeypatch):
        from lattice.dashboard import media_prep
        from lattice.ops.task_attach import encode_payload

        def boom(items):  # noqa: ANN001, ANN202
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(media_prep, "prepare_issue_media", boom)
        server, worker, _ld = self._board(tmp_path)
        try:
            status, envelope = self._post(
                server,
                "/api/issues",
                {"title": "x", "media": [{"payload": encode_payload("a.png", b"png")}]},
            )
            assert status == 500
            assert envelope["error"]["code"] == "WRITE_ERROR"
        finally:
            self._stop(server, worker)

    def test_a_request_that_will_be_refused_never_reaches_the_media_step(
        self, tmp_path, monkeypatch
    ):
        from lattice.dashboard import media_prep
        from lattice.ops.task_attach import encode_payload

        calls = []
        monkeypatch.setattr(
            media_prep, "prepare_issue_media", lambda items: calls.append(items) or items
        )
        server, worker, _ld = self._board(tmp_path)
        try:
            media = [{"payload": encode_payload("a.png", b"png")}]
            assert self._post(server, "/api/issues", {"title": "  ", "media": media})[0] == 400
            too_many = media * 65
            assert self._post(server, "/api/issues", {"title": "x", "media": too_many})[0] == 400
            unknown = [{"payload": encode_payload("a.png", b"png"), "path": "/tmp/secret"}]
            refused = self._post(server, "/api/issues", {"title": "x", "media": unknown})
            assert refused[0] == 400
            assert refused[1]["error"]["code"] == "VALIDATION_ERROR"
            assert calls == []
        finally:
            self._stop(server, worker)

    def test_issues_and_comments_are_written_by_the_configured_human(self, tmp_path):
        server, worker, _ld = self._board(tmp_path, default_actor="human:atin")
        try:
            status, filed = self._post(server, "/api/issues", {"title": "From the dashboard"})
            assert status == 201 and filed["data"]["filed_by"] == "human:atin"
            status, commented = self._post(
                server, f"/api/issues/{filed['data']['id']}/comment", {"body": "A note"}
            )
            assert status == 200
            assert commented["ok"]
            import http.client

            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
            conn.request("GET", f"/api/issues/{filed['data']['id']}")
            detail = json.loads(conn.getresponse().read())["data"]
            conn.close()
            assert detail["comments"][0]["author"] == "human:atin"
            issue_path = f"/api/issues/{filed['data']['id']}"
            assert self._post(server, issue_path + "/dismiss", {"reason": "Not a bug"})[0] == 200
            assert self._post(server, issue_path + "/reopen", {})[0] == 200
            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
            conn.request("GET", issue_path)
            events = json.loads(conn.getresponse().read())["data"]["events"]
            conn.close()
            by_type = {event["type"]: event for event in events}
            for kind in ("issue_dismissed", "issue_reopened"):
                assert by_type[kind]["actor"] == "human:atin", kind
            # A local single-user dashboard honours its explicit actor; hosted
            # sessions instead attribute writes to their authenticated browser actor.
            _status, explicit = self._post(
                server, "/api/issues", {"title": "By an agent", "actor": "agent:qa"}
            )
            assert explicit["data"]["filed_by"] == "agent:qa"
        finally:
            self._stop(server, worker)

    def test_without_a_configured_human_the_dashboard_files_as_itself(self, tmp_path):
        for default_actor in (None, "agent:cairn"):
            server, worker, _ld = self._board(
                tmp_path / str(default_actor), default_actor=default_actor
            )
            try:
                _status, filed = self._post(server, "/api/issues", {"title": "x"})
                assert filed["data"]["filed_by"] == "dashboard:web"
            finally:
                self._stop(server, worker)

def _start_restart_test_server(lattice_dir, monkeypatch, post_handler):  # noqa: ANN001
    import threading

    from lattice.dashboard.server import create_server

    server = create_server(lattice_dir, "127.0.0.1", 0)
    monkeypatch.setattr(server.RequestHandlerClass, "_do_post", post_handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    return server, worker


def _restart_test_post(server, path="/api/test"):
    import http.client

    host = f"127.0.0.1:{server.server_address[1]}"
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    conn.request(
        "POST",
        path,
        json.dumps({"value": "test"}),
        {"Content-Type": "application/json", "Origin": f"http://{host}", "Host": host},
    )
    response = conn.getresponse()
    result = response.status, response.read()
    conn.close()
    return result


def test_boot_identity_is_stable_and_bypasses_board_lock(dashboard_server):
    import os
    import threading

    from lattice.dashboard.server import _BOARD_LOCK

    base_url, _ld, _ids = dashboard_server
    result = []
    finished = threading.Event()

    def get_boot():
        result.append(_get(base_url, "/api/boot"))
        finished.set()

    worker = threading.Thread(target=get_boot, daemon=True)
    try:
        with _BOARD_LOCK:
            worker.start()
            assert finished.wait(1), "/api/boot waited for the board lock"
    finally:
        worker.join(timeout=2)

    status, first = result[0]
    assert status == 200
    assert first["ok"] is True
    assert first["data"]["pid"] == os.getpid()
    assert first["data"]["boot_id"]
    assert _get(base_url, "/api/boot")[1]["data"] == first["data"]


def test_write_drain_waits_for_response_and_refuses_later_write(
    populated_lattice_dir, monkeypatch
):
    import threading

    from lattice.dashboard import api

    lattice_dir, _ids = populated_lattice_dir
    entered = threading.Event()
    release = threading.Event()
    post_result = []
    drain_result = []

    def slow_post(handler, _path, _body):  # noqa: ANN001
        entered.set()
        assert release.wait(4)
        handler._send(api.ok({"saved": True}))

    server, worker = _start_restart_test_server(lattice_dir, monkeypatch, slow_post)
    writer = threading.Thread(target=lambda: post_result.append(_restart_test_post(server)))
    drain = threading.Thread(
        target=lambda: drain_result.append(server.begin_write_drain(timeout=4))
    )
    try:
        writer.start()
        assert entered.wait(2)
        drain.start()
        with server._restart_condition:
            assert server._restart_condition.wait_for(lambda: server._draining, timeout=2)

        status, body = _restart_test_post(server, "/api/rejected-during-drain")
        assert status == 503
        assert b"RESTARTING" in body
        release.set()
        writer.join(timeout=3)
        drain.join(timeout=3)
        assert post_result and post_result[0][0] == 200
        assert drain_result == [(True, 0)]
    finally:
        release.set()
        if drain.is_alive():
            drain.join(timeout=2)
        if writer.is_alive():
            writer.join(timeout=2)
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_write_parsed_at_drain_edge_gets_explicit_refusal(populated_lattice_dir, monkeypatch):
    import threading

    from lattice.dashboard import api

    lattice_dir, _ids = populated_lattice_dir
    admission_entered = threading.Event()
    allow_admission = threading.Event()
    post_called = threading.Event()
    post_result = []
    drain_result = []

    def should_not_run(handler, _path, _body):  # noqa: ANN001
        post_called.set()
        handler._send(api.ok({"saved": True}))

    server, worker = _start_restart_test_server(lattice_dir, monkeypatch, should_not_run)
    original_admit = server.admit_write

    def paused_admission(request):  # noqa: ANN001
        admission_entered.set()
        assert allow_admission.wait(3)
        return original_admit(request)

    monkeypatch.setattr(server, "admit_write", paused_admission)
    writer = threading.Thread(target=lambda: post_result.append(_restart_test_post(server)))
    drain = threading.Thread(
        target=lambda: drain_result.append(server.begin_write_drain(timeout=3))
    )
    try:
        writer.start()
        assert admission_entered.wait(2)
        drain.start()
        with server._restart_condition:
            assert server._restart_condition.wait_for(lambda: server._draining, timeout=2)
        allow_admission.set()
        writer.join(timeout=3)
        drain.join(timeout=3)
        assert post_result and post_result[0][0] == 503
        assert b"RESTARTING" in post_result[0][1]
        assert not post_called.is_set()
        assert drain_result == [(True, 0)]
    finally:
        allow_admission.set()
        if writer.is_alive():
            writer.join(timeout=2)
        if drain.is_alive():
            drain.join(timeout=2)
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_drain_timeout_resumes_the_same_listener(populated_lattice_dir, monkeypatch):
    import threading

    from lattice.dashboard import api

    lattice_dir, _ids = populated_lattice_dir
    entered = threading.Event()
    release = threading.Event()
    post_result = []

    def slow_post(handler, _path, _body):  # noqa: ANN001
        entered.set()
        assert release.wait(4)
        handler._send(api.ok({"saved": True}))

    server, worker = _start_restart_test_server(lattice_dir, monkeypatch, slow_post)
    writer = threading.Thread(target=lambda: post_result.append(_restart_test_post(server)))
    try:
        writer.start()
        assert entered.wait(2)
        assert server.begin_write_drain(timeout=0.05) == (False, 1)
        assert server._draining is False

        release.set()
        writer.join(timeout=3)
        assert post_result and post_result[0][0] == 200
        status, boot = _get(f"http://127.0.0.1:{server.server_address[1]}", "/api/boot")
        assert status == 200
        assert boot["data"]["pid"] == server.pid
        assert boot["data"]["boot_id"] == server.boot_id
        assert _restart_test_post(server)[0] == 200
    finally:
        release.set()
        writer.join(timeout=2)
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_unparsed_and_media_read_connections_do_not_hold_drain(populated_lattice_dir, monkeypatch):
    import socket
    import threading
    import time

    from lattice.dashboard import api, media

    lattice_dir, _ids = populated_lattice_dir
    media_entered = threading.Event()
    media_release = threading.Event()

    def post_unused(handler, _path, _body):  # noqa: ANN001
        handler._send(api.ok({}))

    def paused_media(handler, target, path):  # noqa: ANN001
        media_entered.set()
        media_release.wait(4)

    server, worker = _start_restart_test_server(lattice_dir, monkeypatch, post_unused)
    monkeypatch.setattr(server.RequestHandlerClass, "protocol_version", "HTTP/1.1")
    monkeypatch.setattr(media, "serve_issue_media", paused_media)
    port = server.server_address[1]
    partial = socket.create_connection(("127.0.0.1", port), timeout=2)
    media_conn = socket.create_connection(("127.0.0.1", port), timeout=2)
    from http.client import HTTPConnection

    keepalive = HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        keepalive.request("GET", "/api/boot")
        assert keepalive.getresponse().status == 200
        partial.sendall(b"GET /api/boot HTTP/1.1\r\nHost:")
        media_conn.sendall(
            (
                "GET /api/issues/iss_01ARZ3NDEKTSV4RRFFQ69G5FAV/media/"
                "med_01ARZ3NDEKTSV4RRFFQ69G5FAV HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\n\r\n"
            ).encode()
        )
        assert media_entered.wait(2)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with server._restart_condition:
                states = set(server._request_states.values())
            if "unparsed" in states and "read" in states:
                break
            time.sleep(0.01)
        else:
            pytest.fail(f"connections were not tracked before drain: {states}")

        assert server.begin_write_drain(timeout=0.5) == (True, 0)
        assert server._draining is True
    finally:
        partial.close()
        media_conn.close()
        keepalive.close()
        media_release.set()
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
