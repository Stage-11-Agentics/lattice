"""``lattice server project import`` and issue media (LAT-368, SPEC §11).

Import copies referenced issue media by default, after a full per-file
preflight, and leaves it out with ``--omit-media``. One source board with real
issues and real media (a PNG, a JPEG, a video with frame sidecars made by the
fake ffmpeg) is built per module and copied per test, so every test can damage
its own copy.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from click.testing import CliRunner

from lattice.cli.main import cli
from lattice.core.errors import OpError
from lattice.server import admin, importer
from lattice.server.issue_media import available_media, read_media
from lattice.server.testing import write_config
from lattice.storage.issue_media import frames_dir, media_path
from tests.issue_media_helpers import HTML_AS_PNG, jpeg, mov, png, use_fake_ffmpeg

SLUG = "imp"
OMIT = "--omit-media"


def _cli(*args: str, root: Path | None = None):
    env = {"LATTICE_ROOT": str(root)} if root is not None else None
    return CliRunner().invoke(cli, list(args), env=env, catch_exceptions=False)


def _ok(*args: str, root: Path) -> dict:
    result = _cli(*args, "--json", root=root)
    assert result.exit_code == 0, result.output
    return json.loads(result.output)["data"]


def _import_cmd(
    root: Path,
    source: Path,
    *,
    omit_media: bool = False,
    keep_photo_metadata: bool = False,
    as_json: bool = True,
    slug: str = SLUG,
):
    args = ["server", "project", "import", slug, "--from", str(source), "--root", str(root)]
    if omit_media:
        args.append(OMIT)
    if keep_photo_metadata:
        args.append("--keep-photo-metadata")
    if as_json:
        args.append("--json")
    return _cli(*args)


def _tree(base: Path) -> dict[str, tuple]:
    """Every path under *base* (links not followed): kind, bytes, and mtime."""
    out: dict[str, tuple] = {}
    for dirpath, dirs, files in os.walk(base):
        for name in dirs + files:
            path = Path(dirpath) / name
            st = path.lstat()
            data = path.read_bytes() if stat.S_ISREG(st.st_mode) else None
            out[str(path.relative_to(base))] = (stat.S_IFMT(st.st_mode), data, st.st_mtime_ns)
    return out


def _assert_nothing_created(root: Path, slug: str = SLUG) -> None:
    assert not (root / "projects" / slug).exists()
    assert sorted(p.name for p in (root / "projects").iterdir()) == []


# ---------------------------------------------------------------------------
# The source board
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Media:
    issue_id: str
    entry: dict
    original: Path
    frames: tuple[Path, ...]

    @property
    def objects(self) -> list[Path]:
        return [self.original, *self.frames]


def _build_source(base: Path) -> Path:
    """Issue 1: a PNG and a JPEG. Issue 2: a video (frames) and the same PNG."""
    src = base / "checkout"
    src.mkdir()
    result = _cli(
        "init",
        "--path",
        str(src),
        "--project-code",
        "IMP",
        "--actor",
        "human:t",
        "--no-setup-claude",
        "--no-setup-agents",
    )
    assert result.exit_code == 0, result.output
    board = src / ".lattice"
    config = json.loads((board / "config.json").read_text())
    config["issues"] = {"enabled": True}
    config["auto_code_review_on_transition"] = False
    config["auto_plan_review_on_transition"] = False
    (board / "config.json").write_text(json.dumps(config, sort_keys=True, indent=2) + "\n")

    inputs = base / "in"
    inputs.mkdir()
    (inputs / "shot.png").write_bytes(png(3, 2))
    (inputs / "after.jpg").write_bytes(jpeg())
    (inputs / "repro.mov").write_bytes(mov(b"repro"))
    with pytest.MonkeyPatch.context() as patch:
        use_fake_ffmpeg(patch, base / "bin", width=640, height=480, duration=3.0)
        _ok(
            "issue",
            "file",
            "Screens",
            "--evidence",
            str(inputs / "shot.png"),
            "--evidence",
            str(inputs / "after.jpg"),
            "--actor",
            "human:t",
            root=src,
        )
        _ok(
            "issue",
            "file",
            "Repro",
            "--evidence",
            str(inputs / "repro.mov"),
            "--evidence",
            str(inputs / "shot.png"),
            "--actor",
            "human:t",
            root=src,
        )
    return src


@pytest.fixture(scope="module")
def pristine(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _build_source(tmp_path_factory.mktemp("import-media-source"))


@pytest.fixture()
def source(pristine: Path, tmp_path: Path) -> Path:
    copy = tmp_path / "checkout"
    shutil.copytree(pristine, copy, symlinks=True)
    return copy


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    root = tmp_path / "server-root"
    admin.init_root(root)
    return root


def _snapshots(board: Path) -> list[dict]:
    issues = board / "issues"
    return [
        json.loads(path.read_text())
        for path in sorted(issues.glob("iss_*.json"))
        if path.is_file()
    ]


def _media(board: Path) -> list[Media]:
    rows = []
    for snapshot in _snapshots(board):
        for entry in snapshot["media"]:
            original = media_path(board, snapshot["id"], entry)
            directory = frames_dir(board, snapshot["id"], entry)
            frames = tuple(sorted(directory.iterdir())) if directory.is_dir() else ()
            rows.append(Media(snapshot["id"], entry, original, frames))
    return rows


@pytest.fixture()
def board(source: Path) -> Path:
    return source / ".lattice"


def _objects(board: Path) -> list[Path]:
    return [path for media in _media(board) for path in media.objects]


def _sizes(board: Path) -> dict[str, int]:
    return {path.relative_to(board).as_posix(): path.stat().st_size for path in _objects(board)}


def _by_title(board: Path, title: str) -> dict:
    return next(s for s in _snapshots(board) if s["title"] == title)


def _edit_log(board: Path, issue_id: str, change: Callable[[dict], None]) -> None:
    """Rewrite one issue's log event by event; *change* edits each event in place."""
    log = board / "issues" / "events" / f"{issue_id}.jsonl"
    lines = []
    for line in log.read_text().splitlines():
        event = json.loads(line)
        change(event)
        lines.append(json.dumps(event, sort_keys=True, separators=(",", ":")))
    log.write_text("\n".join(lines) + "\n")


@pytest.fixture()
def no_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test the moment the importer writes anything at all."""

    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the importer wrote before its preflight finished")

    for name in ("ensure_dir", "atomic_write", "store_media", "_create_audit_repo"):
        monkeypatch.setattr(importer, name, boom)


def _set_limits(root: Path, **limits: int) -> None:
    write_config(root, {"limits": limits})


# ---------------------------------------------------------------------------
# 1. Default import copies the media
# ---------------------------------------------------------------------------


def test_default_import_copies_every_original_and_frame(root: Path, source: Path) -> None:
    board = source / ".lattice"
    media = _media(board)
    assert len(media) == 4  # png, jpeg, video, png again
    video = next(m for m in media if m.entry["content_type"] == "video/mp4")
    assert len(video.frames) == 3
    before = _tree(source)
    expected_bytes = sum(p.stat().st_size for p in _objects(board))

    result = importer.import_project(root, SLUG, source)

    imported = root / "projects" / SLUG / ".lattice"
    assert result["media_omitted"] is False
    assert result["media_count"] == len(media) == 4
    assert result["media_object_count"] == len(_objects(board)) == 4 + 3
    assert result["media_bytes"] == expected_bytes
    assert result["media_known_bytes"] == expected_bytes
    assert result["media_unknown_size_count"] == 0
    assert result["media_inventory_complete"] is True
    for path in _objects(board):
        copy = imported / path.relative_to(board)
        assert copy.read_bytes() == path.read_bytes(), path
        assert hashlib.sha256(copy.read_bytes()).hexdigest() == (
            hashlib.sha256(path.read_bytes()).hexdigest()
        )
        assert stat.S_IMODE(copy.stat().st_mode) == stat.S_IMODE(path.stat().st_mode), path
    # Exactly the referenced objects, nothing else, under issues/media.
    copied = {
        p.relative_to(imported).as_posix()
        for p in (imported / "issues" / "media").rglob("*")
        if p.is_file()
    }
    assert copied == {p.relative_to(board).as_posix() for p in _objects(board)}
    assert not [row for row in result["not_copied"] if "issues/media" in row["path"]]
    assert result["copied"] > result["media_count"]
    assert _tree(source) == before
    assert not list((root / "projects").glob(".importing-*"))


def test_import_sanitizes_photo_append_only_and_leaves_source_unchanged(
    root: Path, source: Path
) -> None:
    from lattice.core.issues import replay_issue
    from lattice.storage.issues import rebuild_issue_snapshots
    from tests.photo_metadata_helpers import assert_no_identifying_metadata, jpeg_with_gps

    board = source / ".lattice"
    original_snapshot = _by_title(board, "Screens")
    old_entry = next(m for m in original_snapshot["media"] if m["content_type"] == "image/jpeg")
    old_path = media_path(board, original_snapshot["id"], old_entry)
    raw = jpeg_with_gps()
    old_path.write_bytes(raw)
    repro_snapshot = _by_title(board, "Repro")
    video = next(m for m in _media(board) if m.entry["content_type"] == "video/mp4")
    raw_frame = jpeg_with_gps(gps_value=2, orientation=3)
    video.frames[0].write_bytes(raw_frame)
    _edit_log(
        board,
        original_snapshot["id"],
        lambda event: (
            event["data"].update(sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw))
            if event.get("type") == "issue_media_added"
            and event.get("data", {}).get("media_id") == old_entry["id"]
            else None
        ),
    )
    rebuild_issue_snapshots(board)
    source_before = _tree(source)
    original_log = (board / "issues" / "events" / f"{original_snapshot['id']}.jsonl").read_text()

    result = importer.import_project(root, SLUG, source)

    imported = root / "projects" / SLUG / ".lattice"
    snapshot = _by_title(imported, "Screens")
    removed = next(m for m in snapshot["media"] if m["id"] == old_entry["id"])
    replacement = next(
        m
        for m in snapshot["media"]
        if m.get("content_type") == "image/jpeg" and not m.get("removed")
    )
    assert removed["removed"]["reason"] == "photo_metadata_removed_on_import"
    assert replacement["id"] != old_entry["id"] and replacement["n"] > old_entry["n"]
    stored_path = media_path(imported, snapshot["id"], replacement)
    assert_no_identifying_metadata(stored_path.read_bytes(), "image/jpeg")
    assert not media_path(imported, snapshot["id"], old_entry).exists()
    imported_video = next(
        m for m in _media(imported) if m.issue_id == repro_snapshot["id"] and m.frames
    )
    imported_frame = next(p for p in imported_video.frames if p.name == video.frames[0].name)
    assert_no_identifying_metadata(imported_frame.read_bytes(), "image/jpeg")
    assert imported_frame.read_bytes() != raw_frame
    assert video.frames[0].read_bytes() == raw_frame
    assert old_path.read_bytes() == raw
    assert _tree(source) == source_before
    assert result["photos_sanitized"] == 1
    assert result["frames_sanitized"] == 1
    assert result["media_count"] == 4
    assert result["media_object_count"] == 7

    events = [
        json.loads(line)
        for line in (imported / "issues" / "events" / f"{snapshot['id']}.jsonl")
        .read_text()
        .splitlines()
    ]
    source_events = original_log.splitlines()
    assert [
        json.dumps(e, sort_keys=True, separators=(",", ":")) for e in events[: len(source_events)]
    ] == source_events
    appended = events[len(source_events) :]
    assert [event["type"] for event in appended] == ["issue_media_removed", "issue_media_added"]
    assert all(event["actor"] == "system:import" and event["origin"] for event in appended)
    assert replay_issue(events) == snapshot


def test_import_keep_photo_metadata_cli_fallback_preserves_heic(root: Path, source: Path) -> None:
    from lattice.storage.issues import rebuild_issue_snapshots
    from tests.issue_media_helpers import heic

    board = source / ".lattice"
    snapshot = _by_title(board, "Screens")
    old_entry = next(m for m in snapshot["media"] if m["content_type"] == "image/jpeg")
    old_path = media_path(board, snapshot["id"], old_entry)
    assert old_path is not None
    raw = heic()
    new_entry = {**old_entry, "content_type": "image/heic", "original_name": "kept.heic"}
    new_path = media_path(board, snapshot["id"], new_entry)
    assert new_path is not None
    old_path.unlink()
    new_path.write_bytes(raw)

    def edit(event: dict) -> None:
        if (
            event.get("type") == "issue_media_added"
            and event.get("data", {}).get("media_id") == old_entry["id"]
        ):
            event["data"].update(
                content_type="image/heic",
                original_name="kept.heic",
                sha256=hashlib.sha256(raw).hexdigest(),
                size_bytes=len(raw),
            )

    _edit_log(board, snapshot["id"], edit)
    rebuild_issue_snapshots(board)
    source_before = _tree(source)

    refused = _import_cmd(root, source)
    assert refused.exit_code != 0
    assert "--keep-photo-metadata" in refused.output
    _assert_nothing_created(root)

    result = _import_cmd(root, source, keep_photo_metadata=True)

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    imported = root / "projects" / SLUG / ".lattice"
    imported_snapshot = _by_title(imported, "Screens")
    kept = next(m for m in imported_snapshot["media"] if m["id"] == old_entry["id"])
    kept_path = media_path(imported, imported_snapshot["id"], kept)
    assert kept_path is not None and kept_path.read_bytes() == raw
    assert data["photos_sanitized"] == data["frames_sanitized"] == 0
    assert data["media_count"] == 4 and data["media_object_count"] == 7
    assert _tree(source) == source_before


def test_import_help_documents_keep_photo_metadata_fallback() -> None:
    result = _cli("server", "project", "import", "--help")
    assert result.exit_code == 0, result.output
    assert "--keep-photo-metadata" in result.output


def test_imported_issue_serves_its_media_and_availability(root: Path, source: Path) -> None:
    board = source / ".lattice"
    importer.import_project(root, SLUG, source)
    imported = root / "projects" / SLUG / ".lattice"

    for media in _media(board):
        entry = media.entry
        got = read_media(imported, media.issue_id, entry["id"])
        assert got.body == media.original.read_bytes()
        assert got.content_type == entry["content_type"]
        assert got.sha256 == entry["sha256"] and got.size_bytes == entry["size_bytes"]
        for frame in media.frames:
            served = read_media(imported, media.issue_id, entry["id"], frame_name_value=frame.name)
            assert served.body == frame.read_bytes() and served.content_type == "image/jpeg"

    repro = _by_title(board, "Repro")
    available = available_media(imported, [repro["id"]])["issues"][repro["id"]]
    assert {row["media_id"] for row in available} == {m["id"] for m in repro["media"]}
    video = next(row for row in available if row["content_type"] == "video/mp4")
    assert len(video["frames"]) == 3
    snapshot = json.loads((imported / "issues" / f"{repro['id']}.json").read_text())
    assert [m["sha256"] for m in snapshot["media"]] == [m["sha256"] for m in repro["media"]]


# ---------------------------------------------------------------------------
# 2. --omit-media
# ---------------------------------------------------------------------------


def _forbid_media_content_access(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Fail on any media read, hash comparison or preflight; return the paths read."""
    reads: list[str] = []
    original = importer._read_file

    def read(lattice_fd: int, path: str, scan: object) -> bytes:
        reads.append(path)
        assert not path.startswith("issues/media"), f"omit-media read {path}"
        return original(lattice_fd, path, scan)

    class _NoHash:
        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"omit-media used hashlib.{name}")

    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("omit-media ran the copy preflight")

    monkeypatch.setattr(importer, "_read_file", read)
    monkeypatch.setattr(importer, "hashlib", _NoHash())
    monkeypatch.setattr(importer, "_preflight_media_copy", boom)
    monkeypatch.setattr(importer, "_copy_preflighted_media", boom)
    return reads


def test_omit_media_imports_metadata_only_and_reports_the_inventory(
    root: Path, source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = source / ".lattice"
    expected_bytes = sum(p.stat().st_size for p in _objects(board))
    before = _tree(source)
    reads = _forbid_media_content_access(monkeypatch)

    result = importer.import_project(root, SLUG, source, omit_media=True)

    imported = root / "projects" / SLUG / ".lattice"
    assert reads  # logs and the rest were read, media was not
    assert result["media_omitted"] is True
    assert result["media_count"] == 4
    assert result["media_object_count"] == 7
    assert result["photos_unchecked"] == 3
    assert result["media_bytes"] == result["media_known_bytes"] == expected_bytes
    assert result["media_unknown_size_count"] == 0
    assert result["media_inventory_complete"] is True
    assert not (imported / "issues" / "media").exists()
    not_copied = {row["path"]: row["class"] for row in result["not_copied"]}
    assert {c for p, c in not_copied.items() if "issues/media" in p} == {"issue_media"}
    assert any(p.endswith(media.original.name) for p in not_copied for media in _media(board))
    # Metadata, hash fields included, is imported and the snapshots validate.
    for snapshot in _snapshots(board):
        copy = json.loads((imported / "issues" / f"{snapshot['id']}.json").read_text())
        assert copy["title"] == snapshot["title"]
        assert [m["sha256"] for m in copy["media"]] == [m["sha256"] for m in snapshot["media"]]
        assert [m["size_bytes"] for m in copy["media"]] == [
            m["size_bytes"] for m in snapshot["media"]
        ]
        assert (imported / "issues" / "events" / f"{snapshot['id']}.jsonl").read_bytes() == (
            board / "issues" / "events" / f"{snapshot['id']}.jsonl"
        ).read_bytes()
    assert _tree(source) == before


def _break_missing(board: Path) -> None:
    _media(board)[0].original.unlink()


def _break_corrupt(board: Path) -> None:
    original = _media(board)[1].original
    data = bytearray(original.read_bytes())
    data[-1] ^= 0xFF  # same size, different hash
    original.write_bytes(bytes(data))


def _break_truncated_frame(board: Path) -> None:
    video = next(m for m in _media(board) if m.frames)
    video.frames[0].write_bytes(b"not a jpeg")


@pytest.mark.parametrize("damage", [_break_missing, _break_corrupt, _break_truncated_frame])
def test_omit_media_ignores_a_missing_or_corrupt_blob_and_every_quota(
    root: Path, source: Path, monkeypatch: pytest.MonkeyPatch, damage: Callable[[Path], None]
) -> None:
    board = source / ".lattice"
    damage(board)
    _set_limits(
        root,
        max_issue_media_file_bytes=1,
        max_issue_media_issue_bytes=1,
        max_issue_media_project_bytes=1,
    )
    before = _tree(source)
    _forbid_media_content_access(monkeypatch)

    result = importer.import_project(root, SLUG, source, omit_media=True)

    assert (root / "projects" / SLUG / ".lattice" / "issues").is_dir()
    assert not (root / "projects" / SLUG / ".lattice" / "issues" / "media").exists()
    assert result["media_count"] == 4
    assert result["media_object_count"] == 7
    assert _tree(source) == before


def test_omit_media_reports_unknown_sizes_for_a_missing_original(root: Path, source: Path) -> None:
    board = source / ".lattice"
    _break_missing(board)
    result = importer.import_project(root, SLUG, source, omit_media=True)
    assert result["media_unknown_size_count"] == 1
    assert result["media_bytes"] is None
    assert result["media_known_bytes"] > 0
    assert result["media_count"] == 4
    assert result["media_object_count"] == 7
    assert result["media_inventory_complete"] is True


def test_default_import_of_the_same_damage_is_refused(root: Path, source: Path) -> None:
    _break_corrupt(source / ".lattice")
    with pytest.raises(OpError) as exc:
        importer.import_project(root, SLUG, source)
    assert exc.value.code == "INTEGRITY_ERROR"
    _assert_nothing_created(root)


def test_omit_media_still_refuses_a_malformed_log_and_hash_field(root: Path, source: Path) -> None:
    board = source / ".lattice"
    issue = _by_title(board, "Screens")

    def upper(event: dict) -> None:
        if event["type"] == "issue_media_added":
            event["data"]["sha256"] = event["data"]["sha256"].upper()

    _edit_log(board, issue["id"], upper)
    with pytest.raises(OpError) as exc:
        importer.import_project(root, SLUG, source, omit_media=True)
    assert exc.value.code == "INTEGRITY_ERROR"
    assert "lowercase" in exc.value.message
    _assert_nothing_created(root)


# ---------------------------------------------------------------------------
# 3. Default import refuses before anything is written
# ---------------------------------------------------------------------------


def _png_media(board: Path) -> Media:
    return next(m for m in _media(board) if m.entry["content_type"] == "image/png")


def _video_media(board: Path) -> Media:
    return next(m for m in _media(board) if m.frames)


def _missing(board: Path) -> None:
    _png_media(board).original.unlink()


def _size_mismatch(board: Path) -> None:
    with _png_media(board).original.open("ab") as handle:
        handle.write(b"\x00")


def _hash_mismatch(board: Path) -> None:
    original = _png_media(board).original
    data = bytearray(original.read_bytes())
    data[-1] ^= 0x01
    original.write_bytes(bytes(data))


def _wrong_type(board: Path) -> None:
    """Bytes, recorded size and recorded hash agree, but they are not a PNG."""
    media = _png_media(board)
    media.original.write_bytes(HTML_AS_PNG)
    digest = hashlib.sha256(HTML_AS_PNG).hexdigest()

    def rewrite(event: dict) -> None:
        if event["type"] == "issue_media_added" and event["data"]["media_id"] == media.entry["id"]:
            event["data"]["sha256"] = digest
            event["data"]["size_bytes"] = len(HTML_AS_PNG)

    _edit_log(board, media.issue_id, rewrite)


def _symlinked_file(board: Path) -> None:
    original = _png_media(board).original
    target = board.parent / "elsewhere.png"
    target.write_bytes(original.read_bytes())
    original.unlink()
    original.symlink_to(target)


def _symlinked_issue_dir(board: Path) -> None:
    directory = _png_media(board).original.parent
    moved = board.parent / "moved-media"
    shutil.move(str(directory), str(moved))
    directory.symlink_to(moved, target_is_directory=True)


def _symlinked_media_root(board: Path) -> None:
    root = board / "issues" / "media"
    moved = board.parent / "moved-media-root"
    shutil.move(str(root), str(moved))
    root.symlink_to(moved, target_is_directory=True)


def _symlinked_frames_dir(board: Path) -> None:
    frames = _video_media(board).frames[0].parent
    moved = board.parent / "moved-frames"
    shutil.move(str(frames), str(moved))
    frames.symlink_to(moved, target_is_directory=True)


def _fifo_original(board: Path) -> None:
    original = _png_media(board).original
    original.unlink()
    os.mkfifo(original)


def _fifo_frame(board: Path) -> None:
    frame = _video_media(board).frames[0]
    frame.unlink()
    os.mkfifo(frame)


def _symlinked_frame(board: Path) -> None:
    frame = _video_media(board).frames[0]
    target = board.parent / "elsewhere.jpg"
    target.write_bytes(frame.read_bytes())
    frame.unlink()
    frame.symlink_to(target)


def _bad_frame_name(board: Path) -> None:
    (_video_media(board).frames[0].parent / "frame-1.jpg").write_bytes(jpeg())


def _frame_wrong_type(board: Path) -> None:
    frames = _video_media(board).frames[0].parent
    (frames / "t0099.000s.jpg").write_bytes(png())


INTEGRITY_CASES = [
    pytest.param(_missing, "is missing", id="missing-file"),
    pytest.param(_size_mismatch, "size recorded", id="size-mismatch"),
    pytest.param(_hash_mismatch, "SHA-256 recorded", id="hash-mismatch"),
    pytest.param(_wrong_type, "image/png content type", id="wrong-content-type"),
    pytest.param(_symlinked_file, "symbolic link", id="symlinked-file"),
    pytest.param(_symlinked_issue_dir, "is missing", id="symlinked-issue-dir"),
    pytest.param(_symlinked_media_root, "is missing", id="symlinked-media-root"),
    pytest.param(_symlinked_frames_dir, "frame-sidecar directory", id="symlinked-frames-dir"),
    pytest.param(_fifo_original, "not a regular file", id="fifo-original"),
    pytest.param(_fifo_frame, "not a regular file", id="fifo-frame"),
    pytest.param(_symlinked_frame, "not a regular file", id="symlinked-frame"),
    pytest.param(_bad_frame_name, "invalid frame-sidecar name", id="bad-frame-name"),
    pytest.param(_frame_wrong_type, "image/jpeg content type", id="frame-wrong-type"),
]


@pytest.mark.parametrize(("damage", "fragment"), INTEGRITY_CASES)
def test_unsafe_or_damaged_media_refuses_before_any_write(
    root: Path,
    source: Path,
    no_writes: None,
    damage: Callable[[Path], None],
    fragment: str,
) -> None:
    damage(source / ".lattice")
    before = _tree(source)

    with pytest.raises(OpError) as exc:
        importer.import_project(root, SLUG, source)

    assert exc.value.code == "INTEGRITY_ERROR"
    assert fragment in exc.value.message, exc.value.message
    assert exc.value.message.startswith("Import refused")
    _assert_nothing_created(root)
    assert _tree(source) == before


@pytest.mark.parametrize(
    "damage",
    [
        _missing,
        _hash_mismatch,
        _wrong_type,
        _symlinked_file,
        _symlinked_issue_dir,
        _fifo_original,
        _bad_frame_name,
    ],
)
def test_the_same_damage_does_not_block_omit_media(
    root: Path, source: Path, damage: Callable[[Path], None]
) -> None:
    damage(source / ".lattice")
    before = _tree(source)
    result = importer.import_project(root, SLUG, source, omit_media=True)
    assert result["media_omitted"] is True
    assert (root / "projects" / SLUG / ".lattice" / "issues").is_dir()
    assert not (root / "projects" / SLUG / ".lattice" / "issues" / "media").exists()
    assert _tree(source) == before


def _issue_total(board: Path, title: str) -> int:
    snapshot = _by_title(board, title)
    total = 0
    for media in _media(board):
        if media.issue_id == snapshot["id"]:
            total += sum(p.stat().st_size for p in media.objects)
    return total


def test_per_file_limit_refuses_naming_the_quota_and_omit_media(
    root: Path, source: Path, no_writes: None
) -> None:
    board = source / ".lattice"
    largest = max(p.stat().st_size for p in _objects(board))
    _set_limits(root, max_issue_media_file_bytes=largest - 1)
    before = _tree(source)

    with pytest.raises(OpError) as exc:
        importer.import_project(root, SLUG, source)

    assert exc.value.code == "PAYLOAD_TOO_LARGE"
    assert "max_issue_media_file_bytes" in exc.value.message
    assert OMIT in exc.value.message
    _assert_nothing_created(root)
    assert _tree(source) == before


def test_per_file_limit_at_the_exact_size_passes(root: Path, source: Path) -> None:
    board = source / ".lattice"
    _set_limits(root, max_issue_media_file_bytes=max(p.stat().st_size for p in _objects(board)))
    result = importer.import_project(root, SLUG, source)
    assert result["media_count"] == 4
    assert result["media_object_count"] == 7


def test_per_issue_limit_refuses_naming_the_quota_and_omit_media(
    root: Path, source: Path, no_writes: None
) -> None:
    board = source / ".lattice"
    repro = _by_title(board, "Repro")["id"]
    total = _issue_total(board, "Repro")
    _set_limits(root, max_issue_media_issue_bytes=total - 1)
    before = _tree(source)

    with pytest.raises(OpError) as exc:
        importer.import_project(root, SLUG, source)

    assert exc.value.code == "PAYLOAD_TOO_LARGE"
    assert "max_issue_media_issue_bytes" in exc.value.message
    assert OMIT in exc.value.message
    assert repro in exc.value.message
    _assert_nothing_created(root)
    assert _tree(source) == before


def test_per_issue_limit_at_the_exact_total_passes(root: Path, source: Path) -> None:
    board = source / ".lattice"
    biggest = max(_issue_total(board, "Repro"), _issue_total(board, "Screens"))
    _set_limits(root, max_issue_media_issue_bytes=biggest)
    result = importer.import_project(root, SLUG, source)
    assert result["media_count"] == 4
    assert result["media_object_count"] == 7


def _stored_bytes(board: Path) -> int:
    """Storage is not deduplicated: the project quota counts every stored object."""
    return sum(path.stat().st_size for path in _objects(board))


def test_project_limit_refuses_naming_the_quota_and_omit_media(
    root: Path, source: Path, no_writes: None
) -> None:
    board = source / ".lattice"
    _set_limits(root, max_issue_media_project_bytes=_stored_bytes(board) - 1)
    before = _tree(source)

    with pytest.raises(OpError) as exc:
        importer.import_project(root, SLUG, source)

    assert exc.value.code == "MEDIA_QUOTA_EXCEEDED"
    assert "max_issue_media_project_bytes" in exc.value.message
    assert OMIT in exc.value.message
    _assert_nothing_created(root)
    assert _tree(source) == before


def test_project_quota_counts_every_stored_object_even_identical_content(
    root: Path, source: Path
) -> None:
    """The PNG is in two issues and every video frame is the same JPEG: each copy
    is stored, so each counts; a limit equal to the stored total passes."""
    board = source / ".lattice"
    every_object = _stored_bytes(board)
    _set_limits(root, max_issue_media_project_bytes=every_object)

    result = importer.import_project(root, SLUG, source)

    assert result["media_count"] == 4
    assert result["media_object_count"] == 7
    assert result["media_bytes"] == every_object


# ---------------------------------------------------------------------------
# 4. A source that changes between preflight and copy
# ---------------------------------------------------------------------------


def _hook_copy(monkeypatch: pytest.MonkeyPatch, action: Callable[[], None]) -> None:
    original = importer._copy_preflighted_media

    def copy(lattice_fd: int, scan: object, board: Path, inventory: object):
        action()
        return original(lattice_fd, scan, board, inventory)

    monkeypatch.setattr(importer, "_copy_preflighted_media", copy)


def _flip_in_place(path: Path) -> None:
    """Same inode, size and mtime, different bytes: only the hash can tell."""
    st = path.stat()
    data = bytearray(path.read_bytes())
    data[-1] ^= 0x01
    with path.open("r+b") as handle:
        handle.write(bytes(data))
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))


def test_an_original_changed_after_preflight_is_detected_and_nothing_published(
    root: Path, source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = source / ".lattice"
    last = _media(board)[-1]
    _hook_copy(monkeypatch, lambda: _flip_in_place(last.original))

    with pytest.raises(OpError) as exc:
        importer.import_project(root, SLUG, source)

    assert exc.value.code == "CONFLICT"
    assert exc.value.details["reason"] == "SOURCE_CHANGED"
    assert exc.value.details["path"] == last.original.relative_to(board).as_posix()
    _assert_nothing_created(root)  # staging, with earlier media already in it, is removed


def test_a_frame_changed_after_preflight_is_detected(
    root: Path, source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = source / ".lattice"
    frame = _video_media(board).frames[-1]
    _hook_copy(monkeypatch, lambda: _flip_in_place(frame))

    with pytest.raises(OpError) as exc:
        importer.import_project(root, SLUG, source)

    assert exc.value.code == "CONFLICT"
    assert exc.value.details["path"] == frame.relative_to(board).as_posix()
    _assert_nothing_created(root)


def test_an_original_grown_after_preflight_is_detected(
    root: Path, source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = _media(source / ".lattice")[0].original

    def grow() -> None:
        with original.open("ab") as handle:
            handle.write(b"\x00")

    _hook_copy(monkeypatch, grow)
    with pytest.raises(OpError) as exc:
        importer.import_project(root, SLUG, source)
    assert exc.value.code == "CONFLICT"
    _assert_nothing_created(root)


def test_a_media_file_changed_before_the_first_rescan_is_a_conflict(
    root: Path, source: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = source / ".lattice"
    original = _media(board)[0].original
    real = importer._preflight_media_copy

    def preflight(*args: object, **kwargs: object):
        inventory = real(*args, **kwargs)
        original.write_bytes(original.read_bytes() + b"\x00")
        return inventory

    monkeypatch.setattr(importer, "_preflight_media_copy", preflight)
    with pytest.raises(OpError) as exc:
        importer.import_project(root, SLUG, source)
    assert exc.value.code == "CONFLICT"
    assert exc.value.details["reason"] == "SOURCE_CHANGED"
    _assert_nothing_created(root)


# ---------------------------------------------------------------------------
# 5. Replay, rebuilt snapshots, unreferenced files, hash syntax
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("omit", [False, True])
def test_a_stale_or_corrupt_snapshot_is_rebuilt_from_its_log(
    root: Path, source: Path, omit: bool
) -> None:
    board = source / ".lattice"
    screens = _by_title(board, "Screens")
    repro = _by_title(board, "Repro")
    (board / "issues" / f"{screens['id']}.json").write_text('{"text": "stale snapshot"}\n')
    (board / "issues" / f"{repro['id']}.json").write_text("{<<<<<<< conflict marker\n")
    before = _tree(source)

    importer.import_project(root, SLUG, source, omit_media=omit)

    imported = root / "projects" / SLUG / ".lattice"
    for expected in (screens, repro):
        rebuilt = json.loads((imported / "issues" / f"{expected['id']}.json").read_text())
        assert rebuilt["title"] == expected["title"]
        assert [m["sha256"] for m in rebuilt["media"]] == [m["sha256"] for m in expected["media"]]
    assert _tree(source) == before  # the source's damaged snapshots are left as found
    if not omit:
        served = read_media(imported, repro["id"], repro["media"][0]["id"])
        assert served.sha256 == repro["media"][0]["sha256"]


@pytest.mark.parametrize("omit", [False, True])
def test_a_corrupt_log_is_an_integrity_error(root: Path, source: Path, omit: bool) -> None:
    board = source / ".lattice"
    issue = _by_title(board, "Screens")
    log = board / "issues" / "events" / f"{issue['id']}.jsonl"
    log.write_text(log.read_text() + "{broken\n")

    with pytest.raises(OpError) as exc:
        importer.import_project(root, SLUG, source, omit_media=omit)

    assert exc.value.code == "INTEGRITY_ERROR"
    assert str(log) in exc.value.message
    _assert_nothing_created(root)


def _unreferenced(board: Path) -> list[str]:
    """Files under issues/media that no snapshot references; their relative paths."""
    media = _png_media(board)
    stray = media.original.parent / "med_01ARZ3NDEKTSV4RRFFQ69G5FAV.png"
    stray.write_bytes(png(5, 5))
    orphan_dir = board / "issues" / "media" / "iss_01ARZ3NDEKTSV4RRFFQ69G5FAV"
    orphan_dir.mkdir()
    (orphan_dir / "med_01ARZ3NDEKTSV4RRFFQ69G5FAW.jpg").write_bytes(jpeg())
    frames = media.original.parent / "med_01ARZ3NDEKTSV4RRFFQ69G5FAV.frames"
    frames.mkdir()
    (frames / "t0000.000s.jpg").write_bytes(jpeg())
    return [
        p.relative_to(board).as_posix()
        for p in (
            stray,
            orphan_dir / "med_01ARZ3NDEKTSV4RRFFQ69G5FAW.jpg",
            frames / "t0000.000s.jpg",
        )
    ]


def test_unreferenced_media_files_are_not_copied_and_are_listed(root: Path, source: Path) -> None:
    board = source / ".lattice"
    strays = _unreferenced(board)
    before = _tree(source)

    result = importer.import_project(root, SLUG, source)

    imported = root / "projects" / SLUG / ".lattice"
    for path in strays:
        assert not (imported / path).exists(), path
    assert not (imported / "issues" / "media" / "iss_01ARZ3NDEKTSV4RRFFQ69G5FAV").exists()
    listed = {row["path"]: row["class"] for row in result["not_copied"]}
    for path in strays:
        assert listed[path] == "issue_media"
    # Referenced objects, and the directories that hold them, are not "not copied".
    for path in _objects(board):
        assert path.relative_to(board).as_posix() not in listed
    assert "issues/media/" not in listed
    assert result["media_count"] == 4  # strays are not counted
    assert result["media_object_count"] == 7
    assert _tree(source) == before


def test_unreferenced_files_are_unaffected_by_quotas_and_damage(root: Path, source: Path) -> None:
    """A stray is never read, so a symlink or fifo there cannot block a default import."""
    board = source / ".lattice"
    media = _png_media(board)
    os.mkfifo(media.original.parent / "med_01ARZ3NDEKTSV4RRFFQ69G5FAV.png")
    (media.original.parent / "med_01ARZ3NDEKTSV4RRFFQ69G5FAW.png").symlink_to("/etc/hosts")
    result = importer.import_project(root, SLUG, source)
    assert result["media_count"] == 4
    assert result["media_object_count"] == 7
    imported = root / "projects" / SLUG / ".lattice" / "issues" / "media"
    assert sorted(p.name for p in imported.rglob("*") if p.is_file()) == sorted(
        p.name for p in _objects(board)
    )


def test_omit_media_lists_every_media_path_as_not_copied(root: Path, source: Path) -> None:
    board = source / ".lattice"
    strays = _unreferenced(board)
    result = importer.import_project(root, SLUG, source, omit_media=True)
    listed = {row["path"] for row in result["not_copied"]}
    for path in [*strays, *(p.relative_to(board).as_posix() for p in _objects(board))]:
        assert path in listed, path
    assert result["media_count"] == 4  # strays are not referenced, so not counted
    assert result["media_object_count"] == 7


BAD_HASHES = [
    pytest.param(lambda h: h.upper(), id="uppercase"),
    pytest.param(lambda h: h[:-1], id="63-chars"),
    pytest.param(lambda h: h + "0", id="65-chars"),
    pytest.param(lambda h: h + "\n", id="trailing-newline"),
    pytest.param(lambda h: "0x" + h[2:], id="non-hex"),
    pytest.param(lambda h: " " + h[1:], id="leading-space"),
    pytest.param(lambda h: "", id="empty"),
]


@pytest.mark.parametrize("omit", [False, True])
@pytest.mark.parametrize("mangle", BAD_HASHES)
def test_a_sha256_that_is_not_64_lowercase_hex_is_refused_at_replay(
    root: Path, source: Path, mangle: Callable[[str], str], omit: bool
) -> None:
    board = source / ".lattice"
    issue = _by_title(board, "Screens")

    def change(event: dict) -> None:
        if event["type"] == "issue_media_added":
            event["data"]["sha256"] = mangle(event["data"]["sha256"])

    _edit_log(board, issue["id"], change)
    before = _tree(source)

    with pytest.raises(OpError) as exc:
        importer.import_project(root, SLUG, source, omit_media=omit)

    assert exc.value.code == "INTEGRITY_ERROR"
    assert "sha256 must be 64 lowercase hexadecimal characters" in exc.value.message
    _assert_nothing_created(root)
    assert _tree(source) == before


def test_a_malformed_converted_from_hash_is_refused_at_replay(root: Path, source: Path) -> None:
    board = source / ".lattice"
    repro = _by_title(board, "Repro")

    def change(event: dict) -> None:
        converted = (
            event["data"].get("converted_from") if event["type"] == "issue_media_added" else None
        )
        if converted is not None:
            converted["sha256"] = converted["sha256"].upper()

    _edit_log(board, repro["id"], change)
    assert any("converted_from" in json.dumps(e) for e in (repro["media"]))
    with pytest.raises(OpError) as exc:
        importer.import_project(root, SLUG, source, omit_media=True)
    assert exc.value.code == "INTEGRITY_ERROR"
    _assert_nothing_created(root)


def test_a_removed_media_entry_is_not_read_or_copied(root: Path, source: Path) -> None:
    """A detached entry is metadata only; its bytes may be gone and import still works."""
    board = source / ".lattice"
    screens = _by_title(board, "Screens")
    detached = screens["media"][0]
    _ok(
        "issue",
        "detach",
        screens["id"],
        "1",
        "--reason",
        "wrong shot",
        "--actor",
        "human:t",
        root=source,
    )
    leftover = media_path(board, screens["id"], detached)
    if leftover.exists():
        leftover.unlink()

    result = importer.import_project(root, SLUG, source)

    assert result["media_count"] == 3
    assert result["media_object_count"] == 6
    imported = root / "projects" / SLUG / ".lattice"
    assert not media_path(imported, screens["id"], detached).exists()


# ---------------------------------------------------------------------------
# 6. The CLI
# ---------------------------------------------------------------------------


def test_cli_default_import_report(root: Path, source: Path) -> None:
    board = source / ".lattice"
    total = sum(p.stat().st_size for p in _objects(board))

    json_root = root
    result = _import_cmd(json_root, source)
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["slug"] == SLUG
    assert data["media_omitted"] is False
    assert data["media_count"] == 4
    assert data["media_object_count"] == 7
    assert data["media_bytes"] == data["media_known_bytes"] == total
    assert data["media_unknown_size_count"] == 0
    assert data["media_inventory_complete"] is True
    assert (root / "projects" / SLUG / ".lattice" / "issues" / "media").is_dir()

    human_root = root.parent / "server-root-2"
    admin.init_root(human_root)
    human = _import_cmd(human_root, source, as_json=False)
    assert human.exit_code == 0, human.output
    assert "Media copied: 7 objects" in human.output
    assert "Media omitted" not in human.output


def test_cli_omit_media_report(root: Path, source: Path) -> None:
    board = source / ".lattice"
    total = sum(p.stat().st_size for p in _objects(board))

    result = _import_cmd(root, source, omit_media=True)
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["media_omitted"] is True
    assert data["media_count"] == 4
    assert data["media_object_count"] == 7
    assert data["photos_unchecked"] == 3
    assert data["media_bytes"] == data["media_known_bytes"] == total
    assert data["media_unknown_size_count"] == 0
    assert data["media_inventory_complete"] is True
    assert not (root / "projects" / SLUG / ".lattice" / "issues" / "media").exists()

    human_root = root.parent / "server-root-2"
    admin.init_root(human_root)
    human = _import_cmd(human_root, source, omit_media=True, as_json=False)
    assert human.exit_code == 0, human.output
    assert "Media omitted (--omit-media): 7 objects" in human.output
    assert "Photo metadata unchecked (--omit-media): 3 photos." in human.output
    assert "0 unknown-size objects" in human.output
    assert "Media copied" not in human.output


def test_cli_omit_media_reports_unknown_sizes(root: Path, source: Path) -> None:
    _break_missing(source / ".lattice")
    result = _import_cmd(root, source, omit_media=True)
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["media_bytes"] is None
    assert data["media_unknown_size_count"] == 1
    assert data["media_known_bytes"] > 0

    other = root.parent / "server-root-2"
    admin.init_root(other)
    human = _import_cmd(other, source, omit_media=True, as_json=False)
    assert human.exit_code == 0, human.output
    assert "1 unknown-size objects" in human.output


def test_cli_a_board_without_media_says_so(root: Path, tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert (
        _cli(
            "init",
            "--path",
            str(plain),
            "--project-code",
            "PLN",
            "--actor",
            "human:t",
            "--no-setup-claude",
            "--no-setup-agents",
        ).exit_code
        == 0
    )
    result = _import_cmd(root, plain, as_json=False)
    assert result.exit_code == 0, result.output
    assert "Media copied: no referenced objects." in result.output


@pytest.mark.parametrize(
    ("limit", "code", "quota"),
    [
        ("max_issue_media_file_bytes", "PAYLOAD_TOO_LARGE", "max_issue_media_file_bytes"),
        ("max_issue_media_issue_bytes", "PAYLOAD_TOO_LARGE", "max_issue_media_issue_bytes"),
        ("max_issue_media_project_bytes", "MEDIA_QUOTA_EXCEEDED", "max_issue_media_project_bytes"),
    ],
)
def test_cli_over_quota_refuses_then_omit_media_succeeds(
    root: Path, source: Path, limit: str, code: str, quota: str
) -> None:
    _set_limits(root, **{limit: 1})
    before = _tree(source)

    refused = _import_cmd(root, source)
    assert refused.exit_code == 1, refused.output
    error = json.loads(refused.output)["error"]
    assert error["code"] == code
    assert quota in error["message"] and OMIT in error["message"]
    _assert_nothing_created(root)

    human = _import_cmd(root, source, as_json=False)
    assert human.exit_code == 1
    assert quota in human.output and OMIT in human.output
    _assert_nothing_created(root)

    allowed = _import_cmd(root, source, omit_media=True)
    assert allowed.exit_code == 0, allowed.output
    assert json.loads(allowed.output)["data"]["media_omitted"] is True
    assert _tree(source) == before


def test_cli_damaged_media_is_an_integrity_error(root: Path, source: Path) -> None:
    _break_corrupt(source / ".lattice")
    result = _import_cmd(root, source)
    assert result.exit_code == 1, result.output
    assert json.loads(result.output)["error"]["code"] == "INTEGRITY_ERROR"
    _assert_nothing_created(root)
    assert _import_cmd(root, source, omit_media=True).exit_code == 0


def test_cli_help_documents_omit_media() -> None:
    result = _cli("server", "project", "import", "--help")
    assert result.exit_code == 0
    assert OMIT in result.output
