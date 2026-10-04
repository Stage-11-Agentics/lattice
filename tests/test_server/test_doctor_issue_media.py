"""``lattice server project doctor``: the media pass (SPEC §8.2, §8.12).

Plain ``lattice doctor`` never looks under ``issues/media``; the server doctor
does. Existence, type and size are always checked; sha256 only with
``--verify-media``. Each test files real media through a server, stops it, then
damages the files and runs the doctor offline (a live-server run is last).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from lattice.cli.main import cli
from lattice.core.issue_media import frame_name
from lattice.server import admin
from lattice.server.testing import running_server
from tests.issue_media_helpers import png
from tests.test_server.test_issue_media_routes import (
    CLIP,
    FRAME,
    SLUG,
    board_of,
    filed,
    item,
    payload,
    stage_dir,
    stage_ok,
)
from tests.test_server.conftest import mint

PHOTO = png()


@pytest.fixture()
def root(root: Path) -> Path:
    for slug in (SLUG, "beta"):
        admin.set_project_config(root, slug, {"issues.enabled": True})
    return root


@pytest.fixture()
def filed_issue(root: Path) -> dict:
    """An issue with a photo and a video with one frame, filed through a real
    server that is then stopped, so the files can be damaged offline."""
    token = mint(root, projects=[SLUG])
    with running_server(root) as server:
        for data in (PHOTO, CLIP, FRAME):
            stage_ok(server, token, data)
        clip = item(
            CLIP,
            "clip.mp4",
            video={"width": 64, "height": 48, "duration_ms": 1500},
            frames=[{"t_ms": 500, "payload": payload(FRAME, "f.jpg")}],
        )
        return filed(server, token, [item(PHOTO), clip])


def media_file(root: Path, issue: dict, index: int) -> Path:
    entry = issue["media"][index]
    matches = list((board_of(root) / "issues" / "media" / issue["id"]).glob(f"{entry['id']}.*"))
    return next(p for p in matches if p.is_file())


def frame_file(root: Path, issue: dict) -> Path:
    directory = (
        board_of(root) / "issues" / "media" / issue["id"] / f"{issue['media'][1]['id']}.frames"
    )
    return directory / frame_name(500)


def doctor(root: Path, **kwargs) -> dict:
    return admin.project_doctor(root, SLUG, **kwargs)


def checks(report: dict) -> list[str]:
    return sorted(f["check"] for f in report["findings"])


def test_a_clean_project_passes_with_every_file_counted(root: Path, filed_issue: dict) -> None:
    report = doctor(root, verify_media=True)
    assert report["findings"] == []
    assert report["summary"]["errors"] == 0
    assert {
        k: report["summary"][k] for k in report["summary"] if k.startswith(("media", "staged"))
    } == {
        "media_checked": 3,  # the photo, the video, its frame
        "media_missing": 0,
        "media_corrupt": 0,
        "media_orphans": 0,
        "staged_objects": 0,
        "media_hash_verified": True,
    }


def test_a_moved_away_original_is_missing_with_or_without_the_hash_pass(
    root: Path, filed_issue: dict
) -> None:
    media_file(root, filed_issue, 0).rename(root / "moved-away.png")
    for verify in (False, True):
        report = doctor(root, verify_media=verify)
        assert checks(report) == ["issue_media_missing"]
        assert report["findings"][0]["level"] == "error"
        assert report["summary"]["media_missing"] == 1
        assert report["summary"]["errors"] == 1


def test_a_flipped_byte_needs_verify_media_and_the_flag_is_reported(
    root: Path, filed_issue: dict
) -> None:
    path = media_file(root, filed_issue, 0)
    data = bytearray(path.read_bytes())
    data[-1] ^= 0xFF  # same size, same magic bytes
    path.write_bytes(bytes(data))
    skipped = doctor(root)
    assert skipped["findings"] == [] and skipped["summary"]["media_hash_verified"] is False
    verified = doctor(root, verify_media=True)
    assert checks(verified) == ["issue_media_corrupt"]
    assert "corrupt" in verified["findings"][0]["message"]
    assert verified["summary"]["media_corrupt"] == 1


def test_a_truncated_original_is_wrong_size_without_the_flag(
    root: Path, filed_issue: dict
) -> None:
    path = media_file(root, filed_issue, 1)
    path.write_bytes(path.read_bytes()[:-10])
    report = doctor(root)
    assert checks(report) == ["issue_media_corrupt"]
    assert "wrong size" in report["findings"][0]["message"]
    assert report["summary"]["media_corrupt"] == 1


def test_an_orphan_file_and_a_stray_frame_are_warnings(root: Path, filed_issue: dict) -> None:
    directory = board_of(root) / "issues" / "media" / filed_issue["id"]
    (directory / "med_01ARZ3NDEKTSV4RRFFQ69G5FAV.png").write_bytes(PHOTO)
    stray = frame_file(root, filed_issue).parent / "notes.txt"
    stray.write_text("left behind")
    report = doctor(root)
    assert checks(report) == ["issue_media_orphan", "issue_media_orphan"]
    assert {f["level"] for f in report["findings"]} == {"warning"}
    assert report["summary"]["media_orphans"] == 2
    assert report["summary"]["errors"] == 0


def test_a_missing_frame_sidecar_and_a_damaged_frame(root: Path, filed_issue: dict) -> None:
    frame = frame_file(root, filed_issue)
    frame.write_bytes(b"not a jpeg")
    assert checks(doctor(root)) == ["issue_media_corrupt"]
    frame.unlink()
    frame.parent.rmdir()
    report = doctor(root)
    assert checks(report) == ["issue_media_frames"]
    assert report["findings"][0]["level"] == "warning"
    assert "no frames" in report["findings"][0]["message"]


def test_a_stale_staged_object_is_reported_and_counted(root: Path, filed_issue: dict) -> None:
    token = mint(root, projects=[SLUG])
    with running_server(root) as server:
        stage_ok(server, token, png(4, 2))  # staged, never filed
    meta = next(stage_dir(root).glob("*.json"))
    raw = json.loads(meta.read_text())
    assert doctor(root)["summary"]["staged_objects"] == 1
    assert doctor(root)["findings"] == []  # fresh: not stale
    raw["created_at"] = time.time() - 48 * 3600
    meta.write_text(json.dumps(raw))
    report = doctor(root)
    assert checks(report) == ["issue_media_staged"]
    assert report["summary"]["staged_objects"] == 1


def test_the_running_server_runs_the_pass_and_the_cli_prints_it(
    root: Path, filed_issue: dict
) -> None:
    media_file(root, filed_issue, 0).unlink()
    with running_server(root):
        report = doctor(root, verify_media=True)
        assert report["via"] == "server"
        assert checks(report) == ["issue_media_missing"]
        assert report["summary"]["media_hash_verified"] is True
        plain = CliRunner().invoke(
            cli, ["server", "project", "doctor", SLUG, "--root", str(root)], catch_exceptions=False
        )
        assert plain.exit_code == 1  # an error finding
        assert "hash check skipped (use --verify-media)" in plain.output
        full = CliRunner().invoke(
            cli,
            ["server", "project", "doctor", SLUG, "--root", str(root), "--verify-media", "--json"],
            catch_exceptions=False,
        )
        data = json.loads(full.output)["data"]
        assert data["summary"]["media_missing"] == 1 and data["summary"]["media_hash_verified"]


def test_the_hash_pass_does_not_hold_the_project_while_it_runs(
    root: Path, filed_issue: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--verify-media`` hashes outside the project's admission: reads and writes
    keep working (before, they waited and then failed BOARD_BUSY)."""
    import threading

    from lattice.server import doctor_media
    from tests.test_server.test_issue_media_routes import call

    token = mint(root, projects=[SLUG])
    hashing = threading.Event()
    release = threading.Event()
    original = doctor_media._hash

    def slow_hash(path):
        hashing.set()
        assert release.wait(timeout=15)
        return original(path)

    monkeypatch.setattr(doctor_media, "_hash", slow_hash)
    entry = filed_issue["media"][0]
    path = f"/v1/projects/{SLUG}/issues/media/{filed_issue['id']}/{entry['id']}"
    with running_server(root, config={"limits": {"lock_timeout_seconds": 1}}) as server:
        report: dict = {}
        worker = threading.Thread(
            target=lambda: report.update(doctor(root, verify_media=True)), daemon=True
        )
        worker.start()
        try:
            assert hashing.wait(timeout=10), "the hash pass never started"
            started = time.monotonic()
            status, _, _ = call(server, "GET", path, token=token)
            assert status == 200
            availability = (
                f"/v1/projects/{SLUG}/issues/media/availability?issue={filed_issue['id']}"
            )
            assert call(server, "GET", availability, token=token)[0] == 200
            assert time.monotonic() - started < 1.0, "reads waited for the doctor"
        finally:
            release.set()
        worker.join(timeout=15)
        assert report["summary"]["media_hash_verified"] is True
