"""Privacy by default: a video whose metadata cannot be stripped is refused unless
``--keep-video-metadata`` is passed, on ``issue file --evidence`` and ``issue attach``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from lattice.cli.main import cli
from tests.issue_media_helpers import mov, png, use_fake_ffmpeg, webm
from tests.test_cli.test_issue_cmds import A, _set_config, _tree

FLAG = "--keep-video-metadata"


@pytest.fixture()
def root(initialized_root: Path) -> Path:
    _set_config(initialized_root, project_code="LAT", issues={"enabled": True})
    return initialized_root


@pytest.fixture()
def files(tmp_path: Path) -> Path:
    directory = tmp_path / "in"
    directory.mkdir()
    (directory / "shot.png").write_bytes(png(800, 600))
    (directory / "repro.mov").write_bytes(mov(b"gps 48.8584"))
    (directory / "run.webm").write_bytes(webm(b"run"))
    return directory


def _ffmpeg_modes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Each way the strip cannot run: off, missing, failing."""

    def off() -> None:
        monkeypatch.setenv("LATTICE_FFMPEG", "off")

    def missing() -> None:
        monkeypatch.setenv("LATTICE_FFMPEG", str(tmp_path / "nowhere" / "ffmpeg"))

    def failing() -> None:
        use_fake_ffmpeg(monkeypatch, tmp_path / "bin", duration=3.0)
        monkeypatch.setenv("FAKE_TRANSCODE_FAIL", "1")

    return {"off": off, "missing": missing, "failing": failing}


@pytest.mark.parametrize("mode", ["off", "missing", "failing"])
def test_file_and_attach_refuse_a_video_that_cannot_be_stripped(
    mode: str, root: Path, invoke, files: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ffmpeg_modes(monkeypatch, tmp_path)[mode]()
    issues = root / ".lattice" / "issues"

    # file: the photo beside it does not get filed either; nothing is written
    result = invoke(
        "issue", "file", "t", "--evidence", str(files / "shot.png"),
        "--evidence", str(files / "repro.mov"), *A, "--json",
    )  # fmt: skip
    assert result.exit_code == 1, result.output
    error = json.loads(result.stdout)["error"]
    assert "repro.mov" in error["message"] and FLAG in error["message"]
    assert (
        "Nothing was filed." in error["message"] and "install ffmpeg" in error["message"].lower()
    )
    assert not issues.exists() or not any(issues.rglob("*.json*"))

    # attach: refused, and the issue is untouched
    assert invoke("issue", "file", "plain", *A).exit_code == 0
    before = _tree(issues)
    result = invoke("issue", "attach", "LAT-I1", str(files / "run.webm"), *A, "--json")
    assert result.exit_code == 1, result.output
    error = json.loads(result.stdout)["error"]
    assert FLAG in error["message"] and "Nothing was attached." in error["message"]
    assert _tree(issues) == before


def test_the_flag_files_the_original_as_it_is_and_says_so(
    root: Path, invoke, files: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LATTICE_FFMPEG", "off")
    original = (files / "repro.mov").read_bytes()
    result = invoke("issue", "file", "t", "--evidence", str(files / "repro.mov"), FLAG, *A)
    assert result.exit_code == 0, result.output
    assert any(
        "with its metadata" in line and "recorded" in line for line in result.stdout.splitlines()
    )
    stored = list((root / ".lattice" / "issues" / "media").rglob("*.mov"))
    assert [p.read_bytes() for p in stored] == [original]

    result = invoke("issue", "attach", "LAT-I1", str(files / "run.webm"), FLAG, *A, "--json")
    assert result.exit_code == 0, result.output
    reasons = [n["reason"] for n in json.loads(result.stdout)["data"]["notes"]]
    assert "metadata_kept" in reasons


def test_a_stripped_video_needs_no_flag_and_reports_no_kept_metadata(
    root: Path, invoke, files: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_fake_ffmpeg(monkeypatch, tmp_path / "bin", duration=3.0)
    result = invoke("issue", "file", "t", "--evidence", str(files / "repro.mov"), *A)
    assert result.exit_code == 0, result.output
    assert "metadata" not in result.stdout
    assert invoke("issue", "attach", "LAT-I1", str(files / "run.webm"), FLAG, *A).exit_code == 0
    assert "kept" not in invoke("issue", "show", "LAT-I1").stdout


@pytest.mark.parametrize("command", ["file", "attach"])
def test_help_names_the_flag(command: str) -> None:
    out = " ".join(CliRunner().invoke(cli, ["issue", command, "--help"]).output.split())
    assert FLAG in out and "where it was recorded" in out
