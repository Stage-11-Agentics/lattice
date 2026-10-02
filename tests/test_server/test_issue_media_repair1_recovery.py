"""LAT-368 repair 1: reload reconciliation of hosted issue media (SPEC §8.12).

A commit whose finalize never ran must be published on the next load even when
offline maintenance rotated the epoch in between (the committing journal line
then sits in a kept journal); an uncommitted manifest is still aborted; every
reconcile action is logged; reload never deletes bytes a snapshot needs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lattice.core.errors import OpError
from lattice.core.ids import generate_issue_id, generate_media_id, generate_op_id
from lattice.server import admin, recovery
from lattice.server.log import ServerLog
from lattice.server.testing import running_server
from tests.test_server.conftest import mint
from tests.test_server.faults import Injector, install, load_project, request, run
from tests.test_server.test_issue_media_routes import (
    SLUG,
    blob,
    board_of,
    call,
    filed,
    item,
    manifest_dir,
    names,
    sha,
    stage_dir,
    stage_ok,
)


@pytest.fixture()
def root(root: Path) -> Path:
    admin.set_project_config(root, SLUG, {"issues.enabled": True})
    return root


def _stage(project, data: bytes) -> None:
    upload = project.issue_media.begin_upload(sha(data), len(data))
    upload.write(data)
    upload.finish()


def _crash_after_commit(root: Path, data: bytes, monkeypatch: pytest.MonkeyPatch) -> None:
    """``issue.file`` commits (the journal line is fsynced), then finish fails before
    the post-commit finalize, as a power loss there would leave it: a pending
    manifest, the only copy of the bytes in staging, and the op's undo log."""
    project = load_project(root, SLUG)
    try:
        _stage(project, data)
        write = request("issue.file", {"title": "t", "media": [item(data)]})
        with monkeypatch.context() as m:
            install(m, Injector("finish.accept", sticky=True))
            with pytest.raises(OpError) as caught:
                run(project, write)
        assert caught.value.code == "BOARD_UNAVAILABLE"
    finally:
        project.release()
    assert len(names(manifest_dir(root))) == 1
    assert f"{sha(data)}.blob" in names(stage_dir(root))
    assert recovery.undo_log_paths(board_of(root))


def _snapshot(root: Path) -> dict:
    (path,) = list((board_of(root) / "issues").glob("iss_*.json"))
    return json.loads(path.read_text())


@pytest.mark.parametrize("maintenance", ["recover", "config"])
def test_committed_media_survives_offline_maintenance_after_a_crash_before_finalize(
    root: Path, monkeypatch: pytest.MonkeyPatch, maintenance: str
) -> None:
    data = blob(5000, b"rotation")
    _crash_after_commit(root, data, monkeypatch)
    if maintenance == "recover":
        result = admin.recover_project(root, SLUG, None)
        assert result["committed"] and result["maintenance"]
    else:
        admin.set_project_config(root, SLUG, {"review_mode": "single"})
    assert (board_of(root) / "hosted" / "maintenance.json").exists()  # the load rotates

    snapshot = _snapshot(root)
    entry = snapshot["media"][0]
    token = mint(root, projects=[SLUG])
    with running_server(root) as server:
        target = board_of(root) / "issues" / "media" / snapshot["id"] / f"{entry['id']}.png"
        assert target.read_bytes() == data
        assert names(manifest_dir(root)) == [] and names(stage_dir(root)) == []
        path = f"/v1/projects/{SLUG}/issues/media/{snapshot['id']}/{entry['id']}"
        status, _, body = call(server, "GET", path, token=token)
        assert (status, body) == (200, data)
        events = [line["event"] for line in server.log_lines]
        assert "maintenance_rotation" in events
        assert "issue_media_reconcile_finalize" in events
        assert "issue_media_reconcile_abort" not in events


def test_an_uncommitted_manifest_is_still_aborted_after_a_rotation_and_the_abort_is_logged(
    root: Path,
) -> None:
    lost = blob(600, b"never-committed")
    stream_log = []

    class Capture(ServerLog):
        def emit(self, level: str, event: str, **fields) -> None:  # noqa: ANN003
            stream_log.append((event, fields))

    project = load_project(root, SLUG)
    try:
        _stage(project, lost)
        op_id, issue_id, media_id = generate_op_id(), generate_issue_id(), generate_media_id()
        with project.locked():
            project.issue_media.add_manifest(
                op_id,
                issue_id,
                [
                    {
                        "media_id": media_id,
                        "t_ms": None,
                        "sha256": sha(lost),
                        "size_bytes": len(lost),
                        "target": f"issues/media/{issue_id}/{media_id}.png",
                    }
                ],
            )
    finally:
        project.release()
    admin.set_project_config(root, SLUG, {"review_mode": "single"})  # a rotation too

    project = load_project(root, SLUG, Capture("debug"))
    try:
        assert names(manifest_dir(root)) == []
        assert f"{sha(lost)}.blob" not in names(stage_dir(root))
        aborted = [f for e, f in stream_log if e == "issue_media_reconcile_abort"]
        assert aborted == [{"project": SLUG, "op_id": op_id}]
    finally:
        project.release()


def test_reload_keeps_a_stage_whose_bytes_a_snapshot_still_needs(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even when a manifest is aborted (here: its issue ID does not match the
    snapshot that holds the media), a staged blob that is the only copy of bytes
    a snapshot lists is not deleted."""
    data = blob(700, b"only-copy")
    _crash_after_commit(root, data, monkeypatch)
    (manifest,) = manifest_dir(root).glob("op_*.json")
    body = json.loads(manifest.read_text())
    body["issue_id"] = generate_issue_id()  # no longer recognisably this issue's op
    for obj in body["objects"]:
        obj["target"] = obj["target"].replace(obj["target"].split("/")[2], body["issue_id"])
    manifest.write_text(json.dumps(body))
    admin.recover_project(root, SLUG, None)

    project = load_project(root, SLUG)
    try:
        assert names(manifest_dir(root)) == []  # aborted
        assert f"{sha(data)}.blob" in names(stage_dir(root))  # but the bytes stay
    finally:
        project.release()


def test_reload_releases_reservations_of_crashed_uploads(root: Path) -> None:
    data = blob(4000, b"phantom")
    project = load_project(root, SLUG)
    try:
        upload = project.issue_media.begin_upload(sha(data), len(data))
        upload.write(data[:100])  # the process dies here: .part and .reserve remain
        assert {p.suffix for p in stage_dir(root).iterdir()} >= {".part", ".reserve"}
    finally:
        project.release()

    project = load_project(root, SLUG)  # a fresh process: nothing is uploading
    try:
        assert names(stage_dir(root)) == []
        assert project.issue_media._staged_unique_bytes() == 0
    finally:
        project.release()


def test_reload_keeps_media_of_an_issue_that_has_a_log_but_no_snapshot(
    root: Path,
) -> None:
    token = mint(root, projects=[SLUG])
    data = blob(900, b"log-only")
    with running_server(root) as server:
        stage_ok(server, token, data)
        issue = filed(server, token, [item(data)])
    target = board_of(root) / "issues" / "media" / issue["id"] / f"{issue['media'][0]['id']}.png"
    (board_of(root) / "issues" / f"{issue['id']}.json").unlink()
    with running_server(root):
        assert target.read_bytes() == data
