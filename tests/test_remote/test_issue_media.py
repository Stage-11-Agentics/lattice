"""Hosted issue-media availability and the private per-checkout cache."""

from __future__ import annotations

import hashlib
import os
import stat
from urllib.parse import parse_qs, urlsplit

import pytest

from lattice.core.errors import OpError
from lattice.core.issue_media import frame_name
from lattice.remote import http, issue_media
from tests.issue_media_helpers import jpeg, png


def _id(kind: str, value: int) -> str:
    return f"{kind}_{value:026d}"


def test_availability_batches_issue_ids_at_server_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    remote = http.Remote(alias="test", url="https://lattice.invalid", token="secret")
    issue_ids = [_id("iss", value) for value in range(205)]
    calls: list[list[str]] = []

    def fake_get_json(_remote: http.Remote, path: str, **_kwargs: object) -> dict:
        batch = parse_qs(urlsplit(path).query).get("issue", [])
        calls.append(batch)
        return {"issues": {issue_id: [] for issue_id in batch}}

    monkeypatch.setattr(issue_media, "get_json", fake_get_json)

    result = issue_media.availability(remote, "demo", issue_ids)

    assert [len(batch) for batch in calls] == [100, 100, 5]
    assert [issue_id for batch in calls for issue_id in batch] == issue_ids
    assert result == {issue_id: [] for issue_id in issue_ids}


def test_offline_annotations_use_only_hash_verified_private_cache(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "checkout"
    (root / ".lattice").mkdir(parents=True)
    remote = http.Remote(alias="test", url="https://lattice.invalid", token="secret")
    issue_id = _id("iss", 1)
    media_id = _id("med", 1)
    content = png()
    digest = hashlib.sha256(content).hexdigest()
    original_path = issue_media._safe_write(
        root, "demo", issue_id, media_id, f"{media_id}.png", content
    )
    frame = jpeg()
    frame_digest = hashlib.sha256(frame).hexdigest()
    frame_filename = frame_name(1500)
    frame_path = issue_media._safe_write(
        root, "demo", issue_id, media_id, frame_filename, frame, frame=True
    )
    issue_media._write_frame_metadata(
        root, "demo", issue_id, media_id, frame_filename, 1500, frame_digest, len(frame)
    )

    def unreachable(*_args, **_kwargs):
        raise OpError("SERVER_UNREACHABLE", "offline")

    monkeypatch.setattr(issue_media, "availability", unreachable)
    views = [
        {
            "id": issue_id,
            "media": [
                {
                    "id": media_id,
                    "content_type": "image/png",
                    "sha256": digest,
                    "size_bytes": len(content),
                    "removed": False,
                    "frames": [],
                }
            ],
        }
    ]

    annotated = issue_media.annotate_views(root, remote, "demo", views)[0]["media"][0]

    assert annotated["available"] == "local" and annotated["missing"] is False
    assert annotated["path"] == str(original_path)
    assert annotated["frames"] == [
        {
            "t_ms": 1500,
            "sha256": frame_digest,
            "size_bytes": len(frame),
            "path": str(frame_path),
            "available": "local",
            "missing": False,
        }
    ]
    assert stat.S_IMODE(original_path.parent.parent.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(original_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(frame_path.stat().st_mode) == 0o600


def test_on_demand_fetch_verifies_and_evicts_oldest_private_cache_entry(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "checkout"
    (root / ".lattice").mkdir(parents=True)
    remote = http.Remote(alias="test", url="https://lattice.invalid", token="secret")
    issue_id = _id("iss", 2)
    media_id = _id("med", 2)
    content = png(1, 1)
    digest = hashlib.sha256(content).hexdigest()
    monkeypatch.setattr(
        issue_media,
        "availability",
        lambda *_args: {
            issue_id: [
                {
                    "media_id": media_id,
                    "sha256": digest,
                    "size_bytes": len(content),
                    "content_type": "image/png",
                    "frames": [],
                }
            ]
        },
    )
    monkeypatch.setattr(
        issue_media.http,
        "request",
        lambda *_args, **_kwargs: http.Response(200, {}, content),
    )
    monkeypatch.setattr(issue_media, "MAX_CACHE_BYTES", len(content))
    old_id = _id("med", 3)
    old_path = issue_media._safe_write(root, "demo", issue_id, old_id, f"{old_id}.png", b"old")
    os.utime(old_path, ns=(1_000_000_000, 1_000_000_000))

    view = {
        "id": issue_id,
        "media": [
            {
                "id": media_id,
                "content_type": "image/png",
                "sha256": digest,
                "size_bytes": len(content),
                "removed": False,
                "frames": [],
            }
        ],
    }
    fetched = issue_media.fetch_view_media(root, remote, "demo", view)["media"][0]

    assert fetched["available"] == "local" and fetched["missing"] is False
    assert (
        fetched["path"] is not None
        and (root / fetched["path"][len(str(root)) + 1 :]).read_bytes() == content
    )
    assert not old_path.exists()


def test_symlinked_cache_object_is_never_read(tmp_path) -> None:
    root = tmp_path / "checkout"
    (root / ".lattice").mkdir(parents=True)
    issue_id = _id("iss", 4)
    media_id = _id("med", 4)
    outside = tmp_path / "outside.png"
    outside.write_bytes(png())
    directory = root / ".lattice" / "cache" / "issue-media" / "demo" / issue_id / media_id
    directory.mkdir(parents=True)
    (directory / f"{media_id}.png").symlink_to(outside)

    with pytest.raises(OSError):
        issue_media._safe_read(root, "demo", issue_id, media_id, f"{media_id}.png")
