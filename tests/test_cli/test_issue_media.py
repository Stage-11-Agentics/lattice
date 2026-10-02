"""``lattice issue file --evidence`` / ``attach`` / ``detach`` / ``media`` (LAT-366,
acceptance criteria 1-9) through the CLI, with fake ffmpeg where a video needs it."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from lattice.cli.main import cli
from tests.issue_media_helpers import (
    HTML_AS_PNG,
    SVG,
    fake_sips,
    heic,
    jpeg,
    mov,
    png,
    use_fake_ffmpeg,
    webm,
)
from tests.test_cli.test_issue_cmds import A, _set_config, _tree


@pytest.fixture()
def root(initialized_root: Path) -> Path:
    _set_config(initialized_root, project_code="LAT", issues={"enabled": True})
    return initialized_root


@pytest.fixture()
def files(tmp_path: Path) -> Path:
    directory = tmp_path / "in"
    directory.mkdir()
    for name, data in {
        "shot.png": png(1440, 900),
        "shot.txt": png(1440, 900),
        "fake.png": HTML_AS_PNG,
        "logo.svg": SVG,
        "IMG_1.HEIC": heic(),
        "after.jpg": jpeg(),
        "repro.mov": mov(b"repro"),
        "run.webm": webm(b"run"),
        "build.log": b"ok\n",
    }.items():
        (directory / name).write_bytes(data)
    (directory / "adir").mkdir()
    return directory


def ok(invoke, *args: str) -> dict:  # noqa: ANN001
    result = invoke(*args, "--json")
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)["data"]


def err(invoke, *args: str) -> dict:  # noqa: ANN001
    result = invoke(*args, "--json")
    assert result.exit_code == 1, result.output
    return json.loads(result.stdout)["error"]


def events(root: Path, issue: dict) -> list[dict]:
    log = root / ".lattice" / "issues" / "events" / f"{issue['id']}.jsonl"
    return [json.loads(line) for line in log.read_text().splitlines()]


def strings(value: object) -> list[str]:
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in strings(k) + strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in strings(v)]
    return [value] if isinstance(value, str) else []


# ---------------------------------------------------------------------------
# AC-1: off, and local only
# ---------------------------------------------------------------------------

NEW_COMMANDS = [
    ("issue", "file", "t", "--evidence", "x.png", *A),
    ("issue", "attach", "LAT-I1", "x.png", *A),
    ("issue", "detach", "LAT-I1", "1", "--reason", "r", *A),
    ("issue", "media", "LAT-I1"),
]


def test_new_commands_refuse_when_off_and_on_a_bound_checkout(
    initialized_root: Path, invoke, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for argv in NEW_COMMANDS:
        assert err(invoke, *argv)["code"] == "ISSUES_DISABLED"
    assert not (initialized_root / ".lattice" / "issues").exists()
    bound = tmp_path / "bound"
    bound.mkdir()
    (bound / ".lattice-remote.json").write_text(json.dumps({"remote": "h", "project": "p"}))
    monkeypatch.chdir(bound)
    for argv in NEW_COMMANDS:
        result = CliRunner().invoke(cli, [*argv, "--json"])
        assert json.loads(result.output)["error"]["code"] == "LOCAL_ONLY", argv


# ---------------------------------------------------------------------------
# AC-2, AC-3: what --evidence copies and what it keeps as text
# ---------------------------------------------------------------------------


def test_a_screenshot_is_copied_into_the_issue(root: Path, invoke, files: Path) -> None:
    result = invoke("issue", "file", "t", "--evidence", str(files / "shot.png"), *A)
    assert result.exit_code == 0, result.output
    assert result.stdout == "Filed LAT-I1: t (1 photo)\n"
    view = ok(invoke, "issue", "show", "LAT-I1")
    assert view["evidence"] == []
    snapshot = json.loads((root / ".lattice" / "issues" / f"{view['id']}.json").read_text())
    assert "evidence" not in snapshot
    (entry,) = view["media"]
    stored = Path(entry["path"])
    assert stored.parent.parent == root / ".lattice" / "issues" / "media"
    assert stored.name == f"{entry['id']}.png"
    assert (
        hashlib.sha256(stored.read_bytes()).hexdigest()
        == hashlib.sha256((files / "shot.png").read_bytes()).hexdigest()
    )
    data = events(root, view)[1]["data"]
    assert data == {
        "media_id": entry["id"],
        "n": 1,
        "kind": "photo",
        "content_type": "image/png",
        "original_name": "shot.png",
        "size_bytes": len(png(1440, 900)),
        "sha256": entry["sha256"],
        "width": 1440,
        "height": 900,
    }


def test_type_comes_from_content_and_the_rest_stays_text(root: Path, invoke, files: Path) -> None:
    kept = ["fake.png", "logo.svg", "IMG_1.HEIC", "adir", "missing.png", "build.log"]
    argv = ["--evidence", str(files / "shot.txt")]
    for name in kept:
        argv += ["--evidence", str(files / name)]
    argv += ["--evidence", "https://ci.example/run/1"]
    result = invoke("issue", "file", "t", *argv, *A)
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0] == "Filed LAT-I1: t (1 photo)"
    assert f"  kept as text: {files / 'fake.png'} (not a photo or video by its content)" in lines
    assert f"  kept as text: {files / 'adir'} (a directory)" in lines
    assert f"  kept as text: {files / 'missing.png'} (no such file)" in lines
    assert any(
        "IMG_1.HEIC (a HEIC photo" in line and "sips -s format jpeg" in line for line in lines
    )
    view = ok(invoke, "issue", "show", "LAT-I1")
    assert view["media"][0]["content_type"] == "image/png"
    assert view["evidence"] == [str(files / n) for n in kept] + ["https://ci.example/run/1"]

    data = ok(invoke, "issue", "file", "u", "--evidence", str(files / "fake.png"), *A)
    assert data["notes"] == [
        {"evidence": str(files / "fake.png"), "kept_as": "text", "reason": "not_media"}
    ]


def test_heic_is_converted_to_jpeg(
    root: Path, invoke, files: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LATTICE_SIPS", str(fake_sips(tmp_path / "sips")))
    data = ok(invoke, "issue", "file", "t", "--evidence", str(files / "IMG_1.HEIC"), *A)
    (entry,) = data["media"]
    assert (entry["content_type"], entry["original_name"]) == ("image/jpeg", "IMG_1.HEIC")
    assert entry["converted_from"]["content_type"] == "image/heic"
    assert data["notes"][0]["reason"] == "converted"
    assert data["evidence"] == []


# ---------------------------------------------------------------------------
# AC-4: limits
# ---------------------------------------------------------------------------


def test_over_the_file_limit_files_nothing(root: Path, invoke, files: Path) -> None:
    config = json.loads((root / ".lattice" / "config.json").read_text())
    _set_config(root, issues={**config["issues"], "max_media_mb": 1})
    big = files / "big.mov"
    big.write_bytes(mov() + b"\x00" * (1024 * 1024))
    error = err(invoke, "issue", "file", "t", "--evidence", str(big), *A)
    assert error["code"] == "PAYLOAD_TOO_LARGE"
    assert error["message"].startswith("big.mov is 1.0 MB; the limit is 1 MB per file")
    assert "Nothing was filed." in error["message"] and "ffmpeg -i big.mov" in error["message"]
    assert not (root / ".lattice" / "issues").exists()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO input requires POSIX")
def test_fifo_evidence_is_refused_without_blocking(root: Path, tmp_path: Path) -> None:
    fifo = tmp_path / "waiting.png"
    os.mkfifo(fifo)
    lattice_script = Path(sys.executable).with_name("lattice")
    assert lattice_script.exists(), "the test environment must install the lattice entry point"
    result = subprocess.run(
        [
            str(lattice_script),
            "issue",
            "file",
            "fifo evidence",
            "--evidence",
            str(fifo),
            *A,
            "--json",
        ],
        cwd=root,
        env={**os.environ, "LATTICE_ROOT": str(root)},
        capture_output=True,
        text=True,
        timeout=3,
    )
    assert result.returncode == 0, result.stderr
    view = json.loads(result.stdout)["data"]
    assert view["media"] == []
    assert view["evidence"] == [str(fifo)]


def test_oversized_media_is_refused_before_payload_open(
    root: Path, invoke, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = json.loads((root / ".lattice" / "config.json").read_text())
    _set_config(root, issues={**config["issues"], "max_media_mb": 1})
    big = tmp_path / "large.png"
    with big.open("wb") as fh:
        fh.truncate(2 * 1024 * 1024)

    monkeypatch.setattr(
        "lattice.cli.issue_cmds._classify",
        lambda _arg: ("media", big, "image/png"),
    )
    real_open = open
    payload_opens: list[Path] = []

    def observe_open(file, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        if Path(file) == big:
            payload_opens.append(big)
            raise AssertionError("oversized media payload was opened")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", observe_open)
    error = err(
        invoke,
        "issue",
        "file",
        "large media",
        "--evidence",
        str(big),
        *A,
    )
    assert error["code"] == "PAYLOAD_TOO_LARGE"
    assert payload_opens == []
    assert not (root / ".lattice" / "issues").exists()


# ---------------------------------------------------------------------------
# AC-5: video frames, with fake tools and without any
# ---------------------------------------------------------------------------


def test_video_is_transcoded_and_gets_frames(
    root: Path, invoke, files: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_fake_ffmpeg(monkeypatch, tmp_path / "bin", width=2880, height=1800, duration=14.2)
    result = invoke("issue", "file", "t", "--evidence", str(files / "repro.mov"), *A)
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[0] == "Filed LAT-I1: t (1 video, 8 frames)"
    assert "  repro.mov: " in result.stdout and "(H.264)" in result.stdout
    (entry,) = ok(invoke, "issue", "show", "LAT-I1")["media"]
    assert (entry["content_type"], entry["width"], entry["height"]) == ("video/mp4", 2880, 1800)
    assert entry["duration_ms"] == 14200
    assert entry["converted_from"]["content_type"] == "video/quicktime"
    names = [Path(f["path"]).name for f in entry["frames"]]
    assert names[0] == "t0000.000s.jpg" and names[-1] == "t0014.100s.jpg" and len(names) == 8
    assert names == sorted(names)


def test_video_without_ffmpeg_is_stored_as_is(root: Path, invoke, files: Path) -> None:
    result = invoke("issue", "file", "t", "--evidence", str(files / "run.webm"), *A)
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        "Filed LAT-I1: t (1 video)",
        "  no frames for run.webm: ffmpeg is off (LATTICE_FFMPEG=off)",
    ]
    (entry,) = ok(invoke, "issue", "show", "LAT-I1")["media"]
    assert entry["content_type"] == "video/webm" and entry["frames"] == []
    assert not {"width", "height", "duration_ms"} & set(entry)
    quiet = invoke("issue", "file", "u", "--evidence", str(files / "repro.mov"), *A, "--quiet")
    assert quiet.stdout == "LAT-I2\n"
    assert "no frames for repro.mov" in quiet.stderr


# ---------------------------------------------------------------------------
# AC-6: reading it back
# ---------------------------------------------------------------------------


def test_media_paths_json_and_no_content_in_json(
    root: Path, invoke, files: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_fake_ffmpeg(monkeypatch, tmp_path / "bin", duration=3.0)
    filed = ok(
        invoke,
        "issue",
        "file",
        "t",
        "--evidence",
        str(files / "shot.png"),
        "--evidence",
        str(files / "repro.mov"),
        *A,
    )
    monkeypatch.setenv("LATTICE_FFMPEG", "off")
    ok(invoke, "issue", "attach", "LAT-I1", str(files / "run.webm"), *A)
    photo, video, bare = ok(invoke, "issue", "media", "LAT-I1")["media"]
    result = invoke("issue", "media", "LAT-I1", "--paths")
    assert result.stdout.splitlines() == [photo["path"]] + [f["path"] for f in video["frames"]]
    assert result.stderr.startswith(f"{bare['path']}: no frames")
    assert len(video["frames"]) == 3 and photo["missing"] is False
    human = invoke("issue", "media", "LAT-I1").stdout
    assert human.startswith("LAT-I1 media (3)\n")
    assert "     frames: " in human and bare["path"] in human
    # no command's JSON carries file content
    everything = strings(
        [filed, ok(invoke, "issue", "list"), ok(invoke, "issue", "show", "LAT-I1")]
    )
    assert max(len(s) for s in everything) < 400 and "content_b64" not in everything

    Path(photo["path"]).unlink()
    assert ok(invoke, "issue", "media", "LAT-I1")["media"][0]["missing"] is True
    assert f"(missing: {photo['path']})" in invoke("issue", "media", "LAT-I1").stdout


# ---------------------------------------------------------------------------
# AC-7, AC-8: attach, show, detach
# ---------------------------------------------------------------------------


def test_attach_is_all_or_nothing_and_idempotent(root: Path, invoke, files: Path) -> None:
    ok(invoke, "issue", "file", "t", *A)
    ok(invoke, "issue", "dismiss", "LAT-I1", "--reason", "late evidence still lands", *A)
    before = _tree(root / ".lattice" / "issues")
    for bad, words in (
        ("build.log", "is not a photo or video by its content. Accepted: PNG"),
        ("nope.png", "No such file"),
        ("adir", "is a directory"),
    ):
        error = err(
            invoke, "issue", "attach", "LAT-I1", str(files / "shot.png"), str(files / bad), *A
        )
        assert error["code"] == "VALIDATION_ERROR" and words in error["message"]
    assert _tree(root / ".lattice" / "issues") == before
    heic_error = err(invoke, "issue", "attach", "LAT-I1", str(files / "IMG_1.HEIC"), *A)
    assert "sips -s format jpeg in.heic --out out.jpg" in heic_error["message"]

    result = invoke("issue", "attach", "LAT-I1", str(files / "after.jpg"), *A)
    assert result.stdout == "Attached to LAT-I1: 1 photo\n"
    again = invoke(
        "issue", "attach", "LAT-I1", str(files / "after.jpg"), str(files / "shot.png"), *A
    )
    assert again.stdout.splitlines() == [
        "Attached to LAT-I1: 1 photo",
        "  LAT-I1 already has after.jpg (media 1)",
    ]
    same = invoke("issue", "attach", "LAT-I1", str(files / "after.jpg"), *A, "--json")
    assert json.loads(same.stdout)["data"]["notes"][0]["reason"] == "duplicate"
    assert len(events(root, {"id": ok(invoke, "issue", "show", "LAT-I1")["id"]})) == 4


def test_detach_removes_the_file_and_the_name(root: Path, invoke, files: Path) -> None:
    ok(invoke, "issue", "file", "t", "--evidence", str(files / "shot.png"), *A)
    ok(invoke, "issue", "attach", "LAT-I1", str(files / "after.jpg"), *A)
    (_, entry) = ok(invoke, "issue", "media", "LAT-I1")["media"]
    assert err(invoke, "issue", "detach", "LAT-I1", "2", *A)["code"] == "VALIDATION_ERROR"
    result = invoke(
        "issue", "detach", "LAT-I1", "2", "--reason", "shows an API key", "--actor", "human:atin"
    )
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[0] == (
        "Removed media 2 (photo, after.jpg) from LAT-I1; its file is deleted."
    )
    assert "still in git history" in result.stdout
    assert not Path(entry["path"]).exists()
    shown = invoke("issue", "show", "LAT-I1").stdout
    assert "2  removed " in shown and "by human:atin: shows an API key" in shown
    assert "after.jpg" not in shown
    for view in (ok(invoke, "issue", "show", "LAT-I1"), ok(invoke, "issue", "list")):
        assert "after.jpg" not in json.dumps(view)  # events included
    log = (
        root
        / ".lattice"
        / "issues"
        / "events"
        / f"{ok(invoke, 'issue', 'show', 'LAT-I1')['id']}.jsonl"
    )
    assert "after.jpg" in log.read_text()  # the append-only log keeps it
    assert invoke("issue", "media", "LAT-I1", "--paths").stdout.count("\n") == 1
    again = invoke("issue", "detach", "LAT-I1", "2", "--reason", "r", *A)
    assert again.stdout == "Media 2 of LAT-I1 was already removed\n"


# ---------------------------------------------------------------------------
# AC-9: the rest of the board does not move; rebuild reproduces media snapshots
# ---------------------------------------------------------------------------


def test_board_unchanged_and_rebuild_reproduces(root: Path, invoke, files: Path) -> None:
    ok(invoke, "create", "A task", *A)
    board = root / ".lattice"
    watched = {name: _tree(board / name) for name in ("tasks", "events")} | {
        "ids": (board / "ids.json").read_bytes()
    }
    ok(invoke, "issue", "file", "t", "--evidence", str(files / "shot.png"), *A)
    ok(invoke, "issue", "attach", "LAT-I1", str(files / "after.jpg"), *A)
    ok(invoke, "issue", "detach", "LAT-I1", "1", "--reason", "r", *A)
    assert {name: _tree(board / name) for name in ("tasks", "events")} | {
        "ids": (board / "ids.json").read_bytes()
    } == watched
    issues = _tree(board / "issues")
    ok(invoke, "rebuild", "--all")
    assert _tree(board / "issues") == issues
