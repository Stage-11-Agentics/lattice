"""The 0.2.1 version gate keys on any synced file under ``issues/`` (not ``issues/media``)."""

from __future__ import annotations

import json
from pathlib import Path

from lattice.storage.issues import has_issue_metadata, has_synced_issue_files


def test_synced_issue_files_include_an_empty_id_map(tmp_path: Path) -> None:
    board = tmp_path / ".lattice"
    issues = board / "issues"
    assert not has_synced_issue_files(board)
    (issues / "events").mkdir(parents=True)
    (issues / "media" / "iss_X").mkdir(parents=True)
    assert not has_synced_issue_files(board)  # directories alone are no synced path
    (issues / "media" / "iss_X" / "med_Y.png").write_bytes(b"x")
    assert not has_synced_issue_files(board)  # media is not synced
    (issues / "ids.json").write_text(json.dumps({"schema_version": 1, "next_seq": 1, "map": {}}))
    assert has_synced_issue_files(board)
    # The disabled-guidance helper still reads real issue data only.
    assert not has_issue_metadata(board)


def test_synced_issue_files_see_logs_and_snapshots(tmp_path: Path) -> None:
    board = tmp_path / ".lattice"
    events = board / "issues" / "events"
    events.mkdir(parents=True)
    (events / "iss_X.jsonl").write_text("{}\n")
    assert has_synced_issue_files(board)
