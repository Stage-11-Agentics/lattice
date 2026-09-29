"""``lattice issue ...`` end to end through the CLI (LAT-361, acceptance criteria 1-10)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from lattice.cli.main import cli

A = ("--actor", "agent:qa")
DISABLED_COMMANDS = [
    ("issue", "file", "text", *A),
    ("issue", "list"),
    ("issue", "show", "LAT-I1"),
    ("issue", "promote", "LAT-I1", *A),
    ("issue", "link", "LAT-I1", "LAT-1", *A),
    ("issue", "unlink", "LAT-I1", "LAT-1", *A),
    ("issue", "dismiss", "LAT-I1", "--reason", "r", *A),
    ("issue", "duplicate", "LAT-I1", "--of", "LAT-I2", *A),
    ("issue", "reopen", "LAT-I1", *A),
]


def _set_config(root: Path, **changes: object) -> None:
    path = root / ".lattice" / "config.json"
    config = json.loads(path.read_text())
    for key, value in changes.items():
        if value is None:
            config.pop(key, None)
        else:
            config[key] = value
    path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n")


@pytest.fixture()
def root(initialized_root: Path) -> Path:
    _set_config(initialized_root, project_code="LAT")
    return initialized_root


@pytest.fixture()
def on(root: Path) -> Path:
    _set_config(root, issues={"enabled": True})
    return root


@pytest.fixture()
def ok(invoke):
    """Run a command with --json; assert success; return its data."""

    def _ok(*args: str, **kwargs):  # noqa: ANN202
        result = invoke(*args, "--json", **kwargs)
        assert result.exit_code == 0, result.output
        return json.loads(result.output)["data"]

    return _ok


@pytest.fixture()
def err(invoke):
    """Run a command with --json; assert failure; return the error code."""

    def _err(*args: str, **kwargs) -> str:
        result = invoke(*args, "--json", **kwargs)
        assert result.exit_code == 1, result.output
        return json.loads(result.output)["error"]["code"]

    return _err


def _tree(path: Path) -> dict[str, str]:
    return {
        str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(path.rglob("*"))
        if p.is_file()
    }


# ---------------------------------------------------------------------------
# AC-1: off by default, and off means off
# ---------------------------------------------------------------------------


def test_every_subcommand_refuses_when_off(root: Path, invoke, ok) -> None:
    ok("create", "A task", *A)
    for enabled in (None, {"enabled": False}):
        _set_config(root, issues=enabled)
        for argv in DISABLED_COMMANDS:
            result = invoke(*argv, "--json")
            assert result.exit_code == 1, argv
            error = json.loads(result.output)["error"]
            assert error["code"] == "ISSUES_DISABLED", argv
            assert '"issues": {"enabled": true}' in error["message"]
            assert "Existing issues" not in error["message"]
    assert not (root / ".lattice" / "issues").exists()
    assert invoke("issue", "--help").exit_code == 0
    assert invoke("issue", "file", "--help").exit_code == 0


def test_off_leaves_show_and_rebuild_as_they_were(root: Path, ok, invoke) -> None:
    ok("create", "A task", *A)
    shown = ok("show", "LAT-1")
    assert "linked_issues" not in shown
    assert "Issues:" not in invoke("show", "LAT-1").output
    rebuilt = ok("rebuild", "--all")
    assert set(rebuilt) == {"rebuilt_tasks", "rebuilt_resources", "global_log_rebuilt"}


def test_turning_it_off_keeps_the_data(on: Path, ok, invoke) -> None:
    ok("issue", "file", "Kept", *A)
    before = _tree(on / ".lattice" / "issues")
    _set_config(on, issues=None)
    result = invoke("issue", "list")
    assert result.exit_code == 1
    assert "Existing issues are kept and reappear when it is on." in result.output
    assert _tree(on / ".lattice" / "issues") == before
    _set_config(on, issues={"enabled": True})
    assert [v["short_id"] for v in ok("issue", "list")] == ["LAT-I1"]


# ---------------------------------------------------------------------------
# AC-2, AC-3: filing
# ---------------------------------------------------------------------------


def test_file_needs_only_the_text(on: Path, invoke, err) -> None:
    result = invoke("issue", "file", "Footer overlaps the CTA at 400px", *A)
    assert result.exit_code == 0, result.output
    assert result.output == "Filed LAT-I1: Footer overlaps the CTA at 400px\n"
    assert (on / ".lattice" / "issues" / "ids.json").exists()
    quiet = invoke("issue", "file", "Second", *A, "--quiet")
    assert quiet.output == "LAT-I2\n"
    assert err("issue", "file", "No actor") == "MISSING_ACTOR"
    assert err("issue", "file", "  ", *A) == "VALIDATION_ERROR"
    assert invoke("issue", "file", "x", "--confidence", "maybe", *A).exit_code == 2


def test_file_all_options_and_stdin(on: Path, ok) -> None:
    view = ok(
        "issue", "file", "Signup dead", *A, "--confidence", "definite",
        "--evidence", "screens/a.png", "--evidence", "https://ci.example/run/1",
        "--source", "tester-round-8",
    )  # fmt: skip
    assert view["confidence"] == "definite"
    assert view["evidence"] == ["screens/a.png", "https://ci.example/run/1"]
    assert view["source"] == "tester-round-8"
    assert view["state"] == "open" and view["tasks"] == []
    text = "The `make test` target prints $(HOME) and fails\nsecond line"
    piped = ok("issue", "file", "-", *A, input=text + "\n")
    assert piped["text"] == text


def test_file_by_session_name(on: Path, ok, invoke) -> None:
    session = ok("session", "start", "--name", "Argus", "--model", "m", "--framework", "f")
    name = session.get("name") or session.get("session_name")
    view = ok("issue", "file", "From a session", "--name", name)
    assert isinstance(view["filed_by"], dict)
    shown = invoke("issue", "show", "LAT-I1")
    assert f"by {name}" in shown.output


def test_issues_never_move_the_task_sequence(on: Path, ok) -> None:
    ok("create", "First", *A)
    task_index = on / ".lattice" / "ids.json"
    before = task_index.read_bytes()
    for i in range(3):
        ok("issue", "file", f"Issue {i}", *A)
    assert task_index.read_bytes() == before
    assert ok("create", "Second", *A)["short_id"] == "LAT-2"


def test_no_project_code_numbers_bare(initialized_root: Path, ok) -> None:
    _set_config(initialized_root, issues={"enabled": True})
    assert ok("issue", "file", "Bare", *A)["short_id"] == "I1"
    assert ok("issue", "show", "i1")["text"] == "Bare"


# ---------------------------------------------------------------------------
# AC-4, AC-9: nothing else sees the issue log
# ---------------------------------------------------------------------------


def test_task_views_and_logs_do_not_change(on: Path, ok, invoke) -> None:
    from lattice.dashboard import api

    ok("create", "Only task", *A)
    ld = on / ".lattice"

    def views() -> tuple:
        return (
            invoke("list").output,
            invoke("list", "--json").output,
            invoke("next", "--json").output,
            invoke("stats", "--json").output,
            json.dumps(api.get_tasks(ld), sort_keys=True),
        )

    before = views()
    events_before = _tree(ld / "events")
    ok("issue", "file", "One", *A)
    ok("issue", "file", "Two", *A)
    ok("issue", "link", "LAT-I1", "LAT-1", *A)
    ok("issue", "dismiss", "LAT-I2", "--reason", "noise", *A)
    ok("issue", "unlink", "LAT-I1", "LAT-1", *A)
    assert views() == before
    assert _tree(ld / "events") == events_before
    for log in (ld / "events").glob("task_*.jsonl"):
        assert "issue_" not in log.read_text()


# ---------------------------------------------------------------------------
# AC-5: promote
# ---------------------------------------------------------------------------


def test_promote_two_issues(on: Path, ok, invoke) -> None:
    ok("issue", "file", "Footer overlaps the CTA", *A, "--confidence", "definite")
    ok("issue", "file", "Footer hides the legal links", *A)
    human = invoke("issue", "promote", "LAT-I1", "LAT-I2", *A)
    assert human.exit_code == 0, human.output
    assert human.output == 'Created LAT-1 "Footer overlaps the CTA" from LAT-I1, LAT-I2\n'

    task = ok("show", "LAT-1")
    assert task["status"] == "backlog"
    assert "LAT-I1 (definite, filed by agent:qa" in task["description"]
    assert "LAT-I2 (filed by agent:qa" in task["description"]
    assert [i["short_id"] for i in task["linked_issues"]] == ["LAT-I1", "LAT-I2"]
    assert {i["state"] for i in task["linked_issues"]} == {"linked"}
    shown = invoke("show", "LAT-1").output
    assert "Issues:\n  LAT-I1  linked     Footer overlaps the CTA\n" in shown

    rows = invoke("issue", "list").output.splitlines()
    assert rows[0] == "LAT-I1  linked     definite  Footer overlaps the CTA -> LAT-1 (backlog)"
    view = ok("issue", "show", "LAT-I2")
    assert view["tasks"][0]["short_id"] == "LAT-1"
    assert view["tasks"][0]["status"] == "backlog"
    assert [e["type"] for e in view["events"]] == ["issue_filed", "issue_linked"]
    assert 'LAT-1  backlog  "Footer overlaps the CTA"' in invoke("issue", "show", "LAT-I2").output

    again = ok("issue", "promote", "LAT-I1", "--title", "Separate", "--priority", "high", *A)
    assert again["task"]["title"] == "Separate" and again["task"]["priority"] == "high"
    assert invoke("issue", "promote", "LAT-I1", *A, "--quiet").output == "LAT-3\n"


def test_promote_that_fails_to_link_names_the_task(
    on: Path, ok, invoke, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lattice.ops import issue_common
    from lattice.ops.base import OpError

    ok("issue", "file", "One", *A)
    ok("issue", "file", "Two", *A)
    real = issue_common.link_one

    def flaky(ctx, issue_id, task_id, p):  # noqa: ANN001, ANN202
        if issue_common.current_issue(ctx.lattice_dir, issue_id)["seq"] == 2:
            raise OpError("CONFLICT", "Issue LAT-I2 is dismissed.")
        return real(ctx, issue_id, task_id, p)

    monkeypatch.setattr(issue_common, "link_one", flaky)
    result = invoke("issue", "promote", "LAT-I1", "LAT-I2", *A)
    assert result.exit_code == 1
    assert "Created task LAT-1, but could not link LAT-I2" in result.output
    assert "lattice issue link <issue> LAT-1" in result.output
    monkeypatch.setattr(issue_common, "link_one", real)
    assert ok("issue", "show", "LAT-I1")["state"] == "linked"
    assert ok("issue", "link", "LAT-I2", "LAT-1", *A)["state"] == "linked"


# ---------------------------------------------------------------------------
# AC-6: derived state
# ---------------------------------------------------------------------------


def test_state_follows_the_linked_tasks(on: Path, ok) -> None:
    for text in ("A", "B", "C"):
        ok("issue", "file", text, *A)
    ok("issue", "promote", "LAT-I1", "LAT-I2", *A)
    ok("create", "Other", *A)
    ok("issue", "link", "LAT-I3", "LAT-2", *A)

    ok("status", "LAT-1", "done", "--force", "--reason", "test", *A)
    assert {v["short_id"]: v["state"] for v in ok("issue", "list", "--all")} == {
        "LAT-I1": "resolved",
        "LAT-I2": "resolved",
        "LAT-I3": "linked",
    }
    ok("status", "LAT-2", "cancelled", *A)
    assert ok("issue", "show", "LAT-I3")["state"] == "open"

    ok("create", "Third", *A)
    ok("issue", "link", "LAT-I3", "LAT-3", *A)
    assert ok("issue", "show", "LAT-I3")["state"] == "linked"
    ok("erase", "LAT-3", "--reason", "mistake", *A)
    view = ok("issue", "show", "LAT-I3")
    assert view["state"] == "open"
    assert view["tasks"][-1]["erased"] is True

    ok("issue", "dismiss", "LAT-I3", "--reason", "noise", *A)
    assert ok("issue", "show", "LAT-I3")["state"] == "dismissed"
    ok("issue", "reopen", "LAT-I3", *A)
    ok("issue", "duplicate", "LAT-I3", "--of", "LAT-I1", *A)
    assert ok("issue", "show", "LAT-I3")["state"] == "duplicate"
    ok("issue", "reopen", "LAT-I3", *A)
    assert ok("issue", "show", "LAT-I3")["state"] == "open"


def test_list_filters_and_order(on: Path, ok, invoke) -> None:
    for text in ("A", "B", "C", "D"):
        ok("issue", "file", text, *A)
    ok("create", "T", *A)
    ok("issue", "link", "LAT-I1", "LAT-1", *A)
    ok("issue", "dismiss", "LAT-I4", "--reason", "r", *A)
    assert [v["short_id"] for v in ok("issue", "list")] == ["LAT-I2", "LAT-I3", "LAT-I1"]
    assert [v["short_id"] for v in ok("issue", "list", "--state", "dismissed")] == ["LAT-I4"]
    assert len(ok("issue", "list", "--all")) == 4
    footer = invoke("issue", "list").output.splitlines()[-1]
    assert footer == "3 issues (2 open, 1 linked); 1 other hidden (--all to show)"


# ---------------------------------------------------------------------------
# AC-7: edge rules
# ---------------------------------------------------------------------------


def test_edge_rules(on: Path, ok, err) -> None:
    ok("issue", "file", "One", *A)
    ok("issue", "file", "Two", *A)
    ok("issue", "file", "Three", *A)
    ok("create", "Task", *A)
    ok("create", "Archived", *A)
    ok("create", "Erased", *A)
    ok("status", "LAT-2", "done", "--force", "--reason", "t", *A)
    ok("archive", "LAT-2", *A)
    ok("erase", "LAT-3", "--reason", "gone", *A)

    assert err("issue", "dismiss", "LAT-I1", *A) == "VALIDATION_ERROR"
    assert err("issue", "duplicate", "LAT-I1", "--of", "lat-i1", *A) == "VALIDATION_ERROR"
    assert err("issue", "duplicate", "LAT-I1", "--of", "LAT-I9", *A) == "NOT_FOUND"
    assert err("issue", "link", "LAT-I1", "LAT-99", *A) == "NOT_FOUND"
    assert err("issue", "link", "LAT-I1", "LAT-3", *A) == "TASK_ERASED"
    assert err("issue", "show", "LAT-3") == "INVALID_ID"
    assert err("issue", "show", "XYZ-I1") == "NOT_FOUND"
    assert err("issue", "reopen", "LAT-I1", *A) == "CONFLICT"

    first = ok("issue", "link", "LAT-I1", "LAT-1", *A)
    again = ok("issue", "link", "LAT-I1", "LAT-1", *A)
    assert first == again  # idempotent: nothing written
    archived = ok("issue", "link", "LAT-I1", "LAT-2", *A)
    assert archived["tasks"][1]["archived"] is True
    ok("issue", "unlink", "LAT-I2", "LAT-1", *A)  # not linked: nothing to do
    assert ok("issue", "show", "LAT-I2")["events"][-1]["type"] == "issue_filed"

    ok("issue", "dismiss", "LAT-I1", "--reason", "noise", *A)  # live links: allowed
    assert err("issue", "dismiss", "LAT-I1", "--reason", "again", *A) == "CONFLICT"
    assert err("issue", "duplicate", "LAT-I1", "--of", "LAT-I2", *A) == "CONFLICT"
    assert err("issue", "link", "LAT-I1", "LAT-1", *A) == "CONFLICT"
    assert err("issue", "promote", "LAT-I1", *A) == "CONFLICT"

    ok("issue", "duplicate", "LAT-I3", "--of", "LAT-I2", *A)
    result_code = err("issue", "duplicate", "LAT-I2", "--of", "LAT-I3", *A)
    assert result_code == "VALIDATION_ERROR"  # LAT-I3 is itself a duplicate of LAT-I2


def test_human_messages(on: Path, invoke, ok) -> None:
    ok("issue", "file", "One", *A)
    ok("issue", "file", "Two", *A)
    ok("create", "Task", *A)

    def out(*argv: str) -> str:
        result = invoke(*argv, *A)
        assert result.exit_code == 0, result.output
        return result.output

    assert out("issue", "link", "LAT-I1", "LAT-1") == "Linked LAT-I1 to LAT-1 (backlog)\n"
    assert (
        out("issue", "link", "LAT-I1", "lat-1") == "LAT-I1 is already linked to LAT-1 (backlog)\n"
    )
    assert out("issue", "unlink", "LAT-I1", "LAT-1") == "Unlinked LAT-I1 from LAT-1\n"
    assert out("issue", "dismiss", "LAT-I1", "--reason", "no repro") == (
        "Dismissed LAT-I1: no repro\n"
    )
    assert out("issue", "reopen", "LAT-I1") == "Reopened LAT-I1\n"
    assert out("issue", "duplicate", "LAT-I2", "--of", "LAT-I1") == (
        "Marked LAT-I2 as a duplicate of LAT-I1\n"
    )
    assert "Closed: duplicate of LAT-I1" in invoke("issue", "show", "LAT-I2").output


# ---------------------------------------------------------------------------
# AC-8: local boards only
# ---------------------------------------------------------------------------


def test_bound_checkout_refuses_before_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".lattice-remote.json").write_text(
        json.dumps({"remote": "home", "project": "proj"})
    )
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    for argv in DISABLED_COMMANDS:
        result = runner.invoke(cli, [*argv, "--json"])
        assert result.exit_code == 1, argv
        error = json.loads(result.output)["error"]
        assert error["code"] == "LOCAL_ONLY", argv
        assert "'home/proj'" in error["message"]
    assert not (tmp_path / ".lattice").exists()


def test_operations_refuse_a_server_owned_board(on: Path) -> None:
    from lattice.ops import Caller, OpError, execute
    from lattice.storage.ownership import owning_board

    ld = (on / ".lattice").resolve()
    (ld / "hosted").mkdir()
    (ld / "hosted" / "owner.json").write_text("{}")
    caller = Caller(actor="agent:qa", origin={"op_id": "op_01K00000000000000000000000"})
    with owning_board(ld):
        for op, params in (
            ("issue.file", {"text": "x"}),
            ("issue.link", {"issue": "LAT-I1", "task": "LAT-1"}),
            ("issue.promote", {"issues": ["LAT-I1"]}),
            ("issue.reopen", {"issue": "LAT-I1"}),
        ):
            with pytest.raises(OpError) as exc:
                execute(ld, op, params, caller, run_hooks=False)
            assert exc.value.code == "LOCAL_ONLY", op
    assert not (ld / "issues").exists()


# ---------------------------------------------------------------------------
# AC-10: rebuild
# ---------------------------------------------------------------------------


def test_rebuild_all_reproduces_the_issue_files(on: Path, ok, invoke) -> None:
    ok("issue", "file", "One", *A, "--evidence", "a.png")
    ok("issue", "file", "Two", *A)
    ok("issue", "promote", "LAT-I1", *A)
    ok("issue", "dismiss", "LAT-I2", "--reason", "noise", *A)
    issues = on / ".lattice" / "issues"
    before = _tree(issues)
    (issues / "ids.json").unlink()
    data = ok("rebuild", "--all")
    assert len(data["rebuilt_issues"]) == 2
    assert "issue_seq_collisions" not in data
    assert _tree(issues) == before
    assert "2 issues" in invoke("rebuild", "--all").output


def test_show_compact_and_empty(on: Path, ok) -> None:
    ok("create", "Task", *A)
    assert ok("show", "LAT-1")["linked_issues"] == []  # on, but no issues/ yet
    assert "linked_issues" not in ok("show", "LAT-1", "--compact")


# ---------------------------------------------------------------------------
# Review fixes: an unreadable issue file never breaks a read
# ---------------------------------------------------------------------------


def test_corrupt_snapshot_does_not_break_show_or_list(on: Path, ok, invoke) -> None:
    ok("create", "Unrelated", *A)
    ok("issue", "file", "Linked one", *A)
    ok("issue", "file", "Corrupt one", *A)
    ok("issue", "link", "LAT-I1", "LAT-1", *A)
    corrupt = on / ".lattice" / "issues" / f"{ok('issue', 'show', 'LAT-I2')['id']}.json"
    corrupt.write_text("{not json")

    plain = invoke("show", "LAT-1")
    assert plain.exit_code == 0, plain.output
    assert "LAT-I1  linked" in plain.stdout
    assert f"skipped unreadable issue file {corrupt}" in plain.stderr
    assert "lattice rebuild --all" in plain.stderr

    as_json = invoke("show", "LAT-1", "--json")
    assert as_json.exit_code == 0
    data = json.loads(as_json.stdout)["data"]  # stdout stays valid JSON
    assert [i["short_id"] for i in data["linked_issues"]] == ["LAT-I1"]
    assert "skipped unreadable issue file" in as_json.stderr

    listed = invoke("issue", "list")
    assert listed.exit_code == 0
    assert listed.stdout.startswith("LAT-I1  linked")
    assert "skipped unreadable issue file" in listed.stderr
    # The unreadable issue itself still shows, replayed from its log.
    assert ok("issue", "show", "LAT-I2")["text"] == "Corrupt one"
    assert corrupt.read_text() == "{not json"  # reads wrote nothing
