"""Regression tests for the independent review of LAT-368: an uncertain commit
keeps its media, media reads and availability never hold the project lock or
re-hash whole files, quota counts every stored object, and the staging manager's
abort, cleanup and sweep edge cases."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from lattice.core.ids import generate_op_id
from lattice.server import admin
from lattice.server import issue_media as media_module
from lattice.server.issue_media import available_media, plan_media_read, serve_media
from lattice.server.testing import running_server
from tests.test_server.conftest import mint
from tests.test_server.faults import Injector, install
from tests.test_server.test_issue_media_routes import (  # noqa: F401 - the fixtures
    SLUG,
    blob,
    board_of,
    file_issue,
    filed,
    item,
    manifest_dir,
    media_server,
    names,
    sha,
    stage_dir,
    stage_ok,
)
from tests.test_server.test_issue_media_transactions import journal


@pytest.fixture()
def root(root: Path) -> Path:
    for slug in (SLUG, "beta"):
        admin.set_project_config(root, slug, {"issues.enabled": True})
    return root


@pytest.fixture()
def token(root: Path) -> str:
    return mint(root, projects=[SLUG])


def _media_files(root: Path) -> list[Path]:
    base = board_of(root) / "issues" / "media"
    return [p for p in base.rglob("*") if p.is_file()] if base.exists() else []


def test_a_commit_of_unknown_outcome_keeps_its_media_for_reload(
    root: Path, token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The journal line landed but its fsync failed: whether it committed is
    unknown. The manifest and staged bytes must survive so reload can publish them
    (deleting them left a committed issue pointing at bytes that never existed)."""
    data = blob(600, b"fsync")
    op_id = generate_op_id()
    with running_server(root) as server:
        stage_ok(server, token, data)
        with monkeypatch.context() as patched:
            install(patched, Injector("journal.fsync"))
            status, _, _ = file_issue(server, token, [item(data)], op_id=op_id)
        assert status == 503
        assert names(manifest_dir(root)) == [f"{op_id}.json"]
        assert names(stage_dir(root))
    assert [e for e in journal(root) if e.get("op_id") == op_id], "the line is in the journal"
    with running_server(root):
        assert [p.read_bytes() for p in _media_files(root)] == [data]
        assert names(manifest_dir(root)) == []


def test_serving_media_takes_no_project_lock_and_hashes_each_file_once(
    root: Path, token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = blob(3000, b"serve")
    with media_server(root) as server:
        stage_ok(server, token, data)
        issue = filed(server, token, [item(data)])
        project = server.project(SLUG)
        entry = issue["media"][0]
        plan = plan_media_read(project.board, issue["id"], entry["id"])

        result: dict = {}

        def read_while_the_lock_is_held() -> None:
            result["first"] = serve_media(plan, "bytes=0-9")

        with project.locked():
            worker = threading.Thread(target=read_while_the_lock_is_held)
            worker.start()
            worker.join(timeout=10)
            assert not worker.is_alive(), "serve_media waited for the project lock"
        assert result["first"].body == data[:10] and result["first"].status == 206

        # The identity is verified: a second read must not hash again.
        monkeypatch.setattr(media_module.hashlib, "sha256", _forbidden)
        again = serve_media(plan, "bytes=10-19")
        assert again.body == data[10:20]


def _forbidden(*_args, **_kwargs):
    raise AssertionError("the file was hashed again")


def test_availability_and_the_published_scan_do_not_hash_originals(
    root: Path, token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = blob(3000, b"avail")
    with media_server(root) as server:
        stage_ok(server, token, data)
        issue = filed(server, token, [item(data)])
        project = server.project(SLUG)
        monkeypatch.setattr(media_module, "_verified_digest", _forbidden)
        rows = available_media(project.board, [issue["id"]])["issues"][issue["id"]]
        assert rows[0]["size_bytes"] == 3000 and rows[0]["sha256"] == sha(data)
        assert project.issue_media.scan_published() == 3000


def test_an_object_changed_in_place_is_not_served_from_the_hash_cache(
    root: Path, token: str
) -> None:
    data = blob(3000, b"flip")
    with media_server(root) as server:
        stage_ok(server, token, data)
        issue = filed(server, token, [item(data)])
        project = server.project(SLUG)
        entry = issue["media"][0]
        plan = plan_media_read(project.board, issue["id"], entry["id"])
        assert serve_media(plan).sha256 == sha(data)
        path = plan.path
        changed = bytearray(path.read_bytes())
        changed[-1] ^= 1
        path.write_bytes(bytes(changed))
        with pytest.raises(Exception) as caught:
            serve_media(plan)
        assert getattr(caught.value, "code", "") == "INTEGRITY_ERROR"


def test_the_project_quota_counts_each_stored_copy_of_the_same_file(
    root: Path, token: str
) -> None:
    """Storage is not deduplicated, so re-attaching one file cannot bypass the
    quota: the third copy of a 200-byte file does not fit under 500 bytes."""
    data = blob(200, b"a")
    with media_server(root, max_issue_media_project_bytes=500) as server:
        for _ in range(2):
            stage_ok(server, token, data)
            filed(server, token, [item(data)])
        assert len(_media_files(root)) == 2
        status, _, body = _put(server, token, data)
        assert status == 413, body


def _put(server, token, data):
    from tests.test_server.test_issue_media_routes import put_stage

    return put_stage(server, token, data)


def test_a_stale_abort_never_removes_a_newer_uploads_reservation(root: Path, token: str) -> None:
    data = blob(400, b"abort")
    with media_server(root) as server:
        manager = server.project(SLUG).issue_media
        first = manager.begin_upload(sha(data), len(data))
        first.abort()
        second = manager.begin_upload(sha(data), len(data))
        reserve = stage_dir(root) / f"{sha(data)}.reserve"
        assert reserve.exists()
        first.abort()  # a stale second abort of the first upload
        assert reserve.exists()
        second.abort()
        assert not reserve.exists()


def test_a_rollback_cleanup_keeps_a_stage_another_upload_is_using(root: Path, token: str) -> None:
    data = blob(400, b"shared")
    with media_server(root) as server:
        manager = server.project(SLUG).issue_media
        stage_ok(server, token, data)
        manager._inflight.add(sha(data))
        try:
            manager._remove_unreferenced_stage({sha(data)})
            assert (stage_dir(root) / f"{sha(data)}.blob").exists()
        finally:
            manager._inflight.discard(sha(data))
        manager._remove_unreferenced_stage({sha(data)})
        assert not (stage_dir(root) / f"{sha(data)}.blob").exists()


def test_reload_removes_crash_leftovers_from_staging(root: Path, token: str) -> None:
    with media_server(root):
        pass
    leftovers = [
        stage_dir(root) / f".{'a' * 64}.1.2.part",
        stage_dir(root) / f".{'b' * 64}.json.1.2.tmp",
        manifest_dir(root) / ".op_x.json.1.2.tmp",
    ]
    for path in leftovers:
        path.write_bytes(b"x")
    with media_server(root):
        assert not any(path.exists() for path in leftovers)


def test_an_unreadable_issue_does_not_stop_the_project_loading_or_lose_its_media(
    root: Path, token: str
) -> None:
    data = blob(600, b"keep")
    with media_server(root) as server:
        stage_ok(server, token, data)
        issue = filed(server, token, [item(data)])
    base = board_of(root) / "issues"
    (base / f"{issue['id']}.json").write_text("{not json")
    (base / "events" / f"{issue['id']}.jsonl").write_text("{not json\n")
    with media_server(root):
        pass  # the project loads
    assert [p.read_bytes() for p in _media_files(root)] == [data]
