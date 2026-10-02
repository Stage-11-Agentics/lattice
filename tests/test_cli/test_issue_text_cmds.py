"""Title, description and comment CLI scenarios (LAT-371)."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from lattice.cli.main import cli
from lattice.core.events import create_issue_event, serialize_event
from lattice.core.issues import apply_issue_event, serialize_issue_snapshot

ACTOR = ("--actor", "agent:qa")


def set_config(root: Path, **changes: object) -> None:
    path = root / ".lattice" / "config.json"
    config = json.loads(path.read_text())
    for key, value in changes.items():
        if value is None:
            config.pop(key, None)
        else:
            config[key] = value
    path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n")


def _board(root: Path) -> Path:
    set_config(root, project_code="LAT", issues={"enabled": True})
    return root


class BoardRunner:
    def __init__(self, root: Path) -> None:
        self.root = root

    def invoke(self, command, args, **kwargs):  # noqa: ANN001, ANN003, ANN201
        env = dict(kwargs.pop("env", {}))
        env["LATTICE_ROOT"] = str(self.root)
        return CliRunner().invoke(command, args, env=env, **kwargs)


def test_file_description_file_stdin_and_shortened_title(initialized_root: Path) -> None:
    root = _board(initialized_root)
    runner = BoardRunner(initialized_root)
    detail = "A detail with `backticks` and $(literal)."
    path = root / "description.txt"
    path.write_text(detail)
    filed = runner.invoke(
        cli,
        ["issue", "file", "Footer overlaps", "--description-file", str(path), *ACTOR, "--json"],
    )
    assert filed.exit_code == 0, filed.output
    data = json.loads(filed.output)["data"]
    assert data["title"] == "Footer overlaps" and data["description"] == detail
    assert "text" not in data

    piped = runner.invoke(
        cli,
        ["issue", "file", "-", *ACTOR, "--json"],
        input="Line one\n\nMore detail\n",
    )
    assert piped.exit_code == 0, piped.output
    piped_data = json.loads(piped.output)["data"]
    assert piped_data["title"] == "Line one"
    assert piped_data["description"] == "More detail"
    assert piped_data["notes"] == []

    long = "This is a long issue title " * 8
    shortened = runner.invoke(cli, ["issue", "file", long, *ACTOR, "--json"])
    assert shortened.exit_code == 0, shortened.output
    shortened_data = json.loads(shortened.output)["data"]
    assert shortened_data["notes"] == [{"reason": "title_shortened", "limit": 120}]
    assert len(shortened_data["title"]) <= 120
    assert long.strip() in shortened_data["description"]
    human = runner.invoke(cli, ["issue", "file", long, *ACTOR])
    assert "title shortened to 120 characters" in human.output


def test_file_validation_writes_nothing(initialized_root: Path) -> None:
    root = _board(initialized_root)
    description = root / "description.txt"
    description.write_text("description")
    commands = [
        ["issue", "file", "  ", *ACTOR, "--json"],
        [
            "issue",
            "file",
            "Title",
            "--description",
            "D",
            "--description-file",
            str(description),
            *ACTOR,
            "--json",
        ],
        ["issue", "file", "-", *ACTOR, "--description", "-", "--json"],
    ]
    for command in commands:
        result = BoardRunner(root).invoke(cli, command, input="stdin text\n")
        assert result.exit_code == 1, result.output
        assert json.loads(result.output)["error"]["code"] == "VALIDATION_ERROR"
    assert not (root / ".lattice" / "issues").exists()


def test_edit_comments_quiet_reply_and_json(initialized_root: Path) -> None:
    root = _board(initialized_root)
    runner = BoardRunner(root)
    filed = runner.invoke(
        cli, ["issue", "file", "Title", "--description", "Detail", *ACTOR, "--json"]
    )
    assert filed.exit_code == 0, filed.output

    edited = runner.invoke(cli, ["issue", "edit", "LAT-I1", "--title", "Corrected", *ACTOR])
    assert edited.exit_code == 0 and edited.output == "Edited LAT-I1: title\n"
    unchanged = runner.invoke(cli, ["issue", "edit", "LAT-I1", "--title", "Corrected", *ACTOR])
    assert unchanged.output == "LAT-I1 is unchanged\n"

    first = runner.invoke(cli, ["issue", "comment", "LAT-I1", "Reproduced.", *ACTOR, "--quiet"])
    assert (
        first.exit_code == 0
        and first.output.startswith("ev_")
        and "\n" not in first.output.rstrip("\n")
    )
    first_id = first.output.strip()
    reply = runner.invoke(
        cli,
        ["issue", "comment", "LAT-I1", "Confirmed.", "--reply-to", first_id, *ACTOR, "--json"],
    )
    assert reply.exit_code == 0, reply.output
    data = json.loads(reply.output)["data"]
    assert data["comment"]["parent_id"] == first_id
    assert data["comment"]["id"].startswith("ev_")
    assert data["comment"]["origin"] is None or isinstance(data["comment"]["origin"], dict)
    assert data["comment_count"] == 2
    assert data["title"] == "Corrected" and data["description"] == "Detail"

    description_file = root / "edited-description.txt"
    description_file.write_text("Updated from file  \n")
    edited_description = runner.invoke(
        cli,
        ["issue", "edit", "LAT-I1", "--description-file", str(description_file), *ACTOR, "--json"],
    )
    assert edited_description.exit_code == 0, edited_description.output
    assert json.loads(edited_description.output)["data"]["description"] == "Updated from file"


def test_comment_file_stdin_validation_and_closed_issue(initialized_root: Path) -> None:
    root = _board(initialized_root)
    runner = BoardRunner(root)
    assert runner.invoke(cli, ["issue", "file", "Closed", *ACTOR]).exit_code == 0
    assert (
        runner.invoke(
            cli, ["issue", "dismiss", "LAT-I1", "--reason", "not actionable", *ACTOR]
        ).exit_code
        == 0
    )

    path = root / "comment.txt"
    path.write_text("Long `comment` with $(literal).\n")
    from_file = runner.invoke(
        cli, ["issue", "comment", "LAT-I1", "--file", str(path), *ACTOR, "--json"]
    )
    assert from_file.exit_code == 0, from_file.output
    assert (
        json.loads(from_file.output)["data"]["comment"]["body"]
        == "Long `comment` with $(literal)."
    )

    piped = runner.invoke(cli, ["issue", "comment", "LAT-I1", "-", *ACTOR], input="From stdin\n")
    assert piped.exit_code == 0 and piped.output.startswith("Comment added to LAT-I1 (ev_")

    for command in (
        ["issue", "comment", "LAT-I1", "", *ACTOR, "--json"],
        ["issue", "comment", "LAT-I1", "one", "--file", str(path), *ACTOR, "--json"],
        ["issue", "comment", "LAT-I1", "reply", "--reply-to", "ev_unknown", *ACTOR, "--json"],
    ):
        result = runner.invoke(cli, command)
        assert result.exit_code == 1, result.output
        assert json.loads(result.output)["error"]["code"] == "VALIDATION_ERROR"


def test_show_json_and_human_render_thread_origin_and_description(initialized_root: Path) -> None:
    _board(initialized_root)
    runner = BoardRunner(initialized_root)
    runner.invoke(cli, ["issue", "file", "Footer overlap", "--description", "At 400px.", *ACTOR])
    first = runner.invoke(cli, ["issue", "comment", "LAT-I1", "Reproduced.", *ACTOR, "--quiet"])
    first_id = first.output.strip()
    runner.invoke(
        cli,
        [
            "issue",
            "comment",
            "LAT-I1",
            "Confirmed.",
            "--reply-to",
            first_id,
            "--actor",
            "human:atin",
        ],
    )

    shown = runner.invoke(cli, ["issue", "show", "LAT-I1", "--json"])
    assert shown.exit_code == 0, shown.output
    data = json.loads(shown.output)["data"]
    assert data["title"] == "Footer overlap" and data["description"] == "At 400px."
    assert data["comment_count"] == 2 and len(data["comments"]) == 1
    assert data["comments"][0]["id"] == first_id
    assert data["comments"][0]["replies"][0]["body"] == "Confirmed."
    assert "text" not in data
    human = runner.invoke(cli, ["issue", "show", "LAT-I1"])
    assert human.exit_code == 0, human.output
    assert "LAT-I1 (iss_" in human.output and '"Footer overlap"' in human.output
    assert "State: open" in human.output
    assert "Description:\n  At 400px." in human.output
    assert "Comments (2):" in human.output
    assert "Reproduced." in human.output and "Confirmed." in human.output


def test_list_by_linked_task_and_promote_description(initialized_root: Path) -> None:
    root = _board(initialized_root)
    runner = BoardRunner(root)
    runner.invoke(cli, ["issue", "file", "First title", "--description", "First detail", *ACTOR])
    runner.invoke(cli, ["issue", "comment", "LAT-I1", "A note", "--actor", "agent:other"])
    runner.invoke(cli, ["issue", "file", "Second title", *ACTOR])
    runner.invoke(cli, ["issue", "dismiss", "LAT-I2", "--reason", "outdated", *ACTOR])
    runner.invoke(cli, ["issue", "comment", "LAT-I2", "Still relevant", *ACTOR])

    found = runner.invoke(cli, ["issue", "list", "--by", "agent:qa", "--json"])
    assert found.exit_code == 0, found.output
    rows = json.loads(found.output)["data"]
    assert [(row["title"], row["activity"]) for row in rows] == [
        ("First title", "filed"),
        ("Second title", "filed"),
    ]
    commented = runner.invoke(cli, ["issue", "list", "--by", "agent:other", "--json"])
    assert json.loads(commented.output)["data"][0]["activity"] == "commented"
    commented_human = runner.invoke(cli, ["issue", "list", "--by", "agent:other"]).output
    assert "commented" in commented_human
    assert "--all to show" not in commented_human

    runner.invoke(cli, ["create", "Target task", *ACTOR])
    runner.invoke(cli, ["issue", "link", "LAT-I1", "LAT-1", *ACTOR])
    task = runner.invoke(cli, ["show", "LAT-1", "--json"])
    linked = json.loads(task.output)["data"]["linked_issues"]
    assert linked == [
        {"id": linked[0]["id"], "short_id": "LAT-I1", "state": "linked", "title": "First title"}
    ]

    promoted = runner.invoke(cli, ["issue", "promote", "LAT-I1", *ACTOR, "--json"])
    assert promoted.exit_code == 0, promoted.output
    task = json.loads(promoted.output)["data"]["task"]
    assert task["title"] == "First title"
    assert "First detail" in task["description"]
    assert "Comments: 1 (lattice issue show LAT-I1)" in task["description"]


def test_raw_old_text_log_reads_and_rebuilds_byte_identically(initialized_root: Path) -> None:
    root = _board(initialized_root)
    issue_id = "iss_01K00000000000000000000000"
    event = create_issue_event(
        "issue_filed",
        issue_id,
        "agent:qa",
        {"seq": 1, "short_id": "LAT-I1", "text": "Legacy title\nLegacy detail"},
        event_id="ev_01K00000000000000000000001",
        ts="2026-10-01T10:00:00Z",
    )
    issues = root / ".lattice" / "issues"
    (issues / "events").mkdir(parents=True)
    (issues / "ids.json").write_text(
        json.dumps({"schema_version": 1, "next_seq": 2, "map": {"1": issue_id}})
    )
    snapshot = apply_issue_event(None, event)
    snapshot_path = issues / f"{issue_id}.json"
    snapshot_path.write_text(serialize_issue_snapshot(snapshot))
    (issues / "events" / f"{issue_id}.jsonl").write_text(serialize_event(event))
    before = snapshot_path.read_bytes()

    runner = BoardRunner(root)
    shown = runner.invoke(cli, ["issue", "show", "LAT-I1", "--json"])
    assert shown.exit_code == 0, shown.output
    data = json.loads(shown.output)["data"]
    assert data["title"] == "Legacy title" and data["description"] == "Legacy detail"
    assert "text" not in data
    rebuilt = runner.invoke(cli, ["rebuild", "--all", "--json"])
    assert rebuilt.exit_code == 0, rebuilt.output
    assert snapshot_path.read_bytes() == before
