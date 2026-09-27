"""AC-16 (API part): /v1/projects and /v1/info list exactly the caller's projects."""

from __future__ import annotations

from pathlib import Path

from lattice.server import admin
from lattice.server.testing import ServerHandle
from tests.test_server.conftest import create_task, mint


def test_projects_lists_exactly_the_callers_projects(server: ServerHandle, root: Path) -> None:
    admin.create_project(root, "gamma")
    narrow = mint(root, projects=["alpha", "gamma"])
    wide = mint(root)
    none = mint(root, projects=[])
    create_task(server, wide, "alpha")
    _, _, body = server.request("GET", "/v1/projects", token=narrow)
    rows = body["data"]["projects"]
    assert [r["slug"] for r in rows] == ["alpha", "gamma"]
    assert rows[0]["project_code"] == "ALP" and rows[0]["head_seq"] == 1
    _, _, body = server.request("GET", "/v1/projects", token=wide)
    assert [r["slug"] for r in body["data"]["projects"]] == ["alpha", "beta", "gamma"]
    _, _, body = server.request("GET", "/v1/projects", token=none)
    assert body["data"]["projects"] == []
    _, _, info = server.request("GET", "/v1/info", token=narrow)
    assert info["data"]["projects"] == ["alpha", "gamma"]


def test_info_describes_the_server_and_the_caller(server: ServerHandle, root: Path) -> None:
    token = mint(root, projects=["alpha"])
    status, _, body = server.request("GET", "/v1/info", token=token)
    assert status == 200
    data = body["data"]
    assert data["protocol"] == 1
    assert data["min_client_version"] and data["version"]
    assert data["stream_heartbeat_seconds"] == 2
    assert data["identity"]["user"] == "human:alice"
    assert data["identity"]["machine"] == "laptop"
    assert data["identity"]["actors"] == ["human:alice", "agent:*"]
    assert "sha256" not in data["identity"]
    assert "title" in data["ops"]["task.create"]
    assert "task_created" in data["event_types"]
    assert data["audit"]["active"] is False


def test_read_endpoints(server: ServerHandle, root: Path) -> None:
    token = mint(root, projects=["alpha"])
    task = create_task(server, token, "alpha", title="read me")
    status, _, body = server.request("GET", "/v1/projects/alpha/tasks/ALP-1", token=token)
    assert status == 200
    assert body["data"]["snapshot"]["id"] == task["id"]
    assert body["data"]["events"][0]["type"] == "task_created"
    assert body["data"]["plan"] is not None
    assert server.request("GET", f"/v1/projects/alpha/tasks/{task['id']}", token=token)[0] == 200
    status, _, body = server.request("GET", "/v1/projects/alpha/tasks/ALP-9", token=token)
    assert status == 404
    status, _, body = server.request("GET", "/v1/projects/alpha/tasks/bogus", token=token)
    assert status == 400 and body["error"]["code"] == "INVALID_ID"
    _, _, listing = server.request("GET", "/v1/projects/alpha/tasks?status=backlog", token=token)
    assert [t["id"] for t in listing["data"]["tasks"]] == [task["id"]]
    _, _, listing = server.request("GET", "/v1/projects/alpha/tasks?status=done", token=token)
    assert listing["data"]["tasks"] == []


def test_a_project_created_after_startup_is_listed_loaded(
    server: ServerHandle, root: Path
) -> None:
    """A5: rows are read under the project's locks, loading it lazily."""
    token = mint(root)
    admin.create_project(root, "late", code="LAT")
    _, _, body = server.request("GET", "/v1/projects", token=token)
    late = next(r for r in body["data"]["projects"] if r["slug"] == "late")
    assert late == {"slug": "late", "project_code": "LAT", "head_seq": 0, "state": "loaded"}


def test_listing_waits_for_a_write_in_progress(server: ServerHandle, root: Path) -> None:
    """A5: a row is never read while a write holds the project (here: a slow op and a
    project-code change queued behind it), so the listing shows the settled state."""
    import threading
    import time

    from lattice.server.testing import wait_for

    token = mint(root)
    slow = threading.Thread(
        target=server.op, args=("alpha", "xtest.sleep", {"ms": 600}), kwargs={"token": token}
    )
    slow.start()
    assert wait_for(lambda: server.project("alpha").work.locked())
    change = threading.Thread(
        target=server.op,
        args=("alpha", "board.set_project_code", {"code": "NEW", "force": True}),
        kwargs={"token": token},
    )
    change.start()
    time.sleep(0.05)
    started = time.monotonic()
    _, _, body = server.request("GET", "/v1/projects", token=token)
    waited = time.monotonic() - started
    slow.join()
    change.join()
    alpha = next(r for r in body["data"]["projects"] if r["slug"] == "alpha")
    assert waited > 0.3
    # whichever request admission let in first, the row is a settled state
    assert (alpha["project_code"], alpha["head_seq"]) in {("ALP", 1), ("NEW", 2)}
