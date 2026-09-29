"""LAT-357: hosted first-contact polish.

- Hints name the program as invoked (``lattice-v2`` under that alias).
- ``plan show <task> [--json]`` is ``plan <task>``.
- ``plan write`` / ``notes write`` with neither ``--file`` nor ``--stdin``
  read standard input only when it is a pipe or a regular file.
- ``show`` on a bound checkout names the plan only once one is written.
- The origin line labels a token user who is not the actor ``via token``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from lattice.cli.main import cli
from lattice.core.origin import format_origin_line
from lattice.storage.board_init import create_board
from lattice.storage.fs import LATTICE_DIR
from tests.test_remote.hosted import HostedEnv, make_repo, run_cli
from tests.test_remote.hosted import hosted_env as hosted_env  # noqa: F401 - fixture

A = ("--actor", "agent:t")


def _lattice_script() -> str:
    """The installed ``lattice`` console script (the real entry point)."""
    beside = Path(sys.executable).with_name("lattice")
    found = str(beside) if beside.exists() else shutil.which("lattice")
    if found is None:
        pytest.skip("no lattice console script installed")
    return found


@pytest.fixture()
def board(tmp_path: Path) -> Path:
    """A local board with task LOC-1 whose plan is the scaffold."""
    root = tmp_path / "board"
    root.mkdir()
    create_board(root, project_code="LOC", actor="human:a")
    assert run_cli(root, "create", "One", *A).exit_code == 0
    return root


def _plan(root: Path) -> Path:
    (plan,) = (root / LATTICE_DIR / "plans").glob("task_*.md")
    return plan


def _event_types(root: Path) -> list[str]:
    (log,) = (root / LATTICE_DIR / "events").glob("task_*.jsonl")
    return [json.loads(line)["type"] for line in log.read_text().splitlines()]


def _run(root: Path, *args: str, prog: str | None = None, **kwargs) -> subprocess.CompletedProcess:
    """The real program in a child process, so fd 0 is what *kwargs* make it."""
    script = _lattice_script()
    if prog is not None:
        alias = root.parent / prog
        if not alias.exists():
            alias.symlink_to(script)
        script = str(alias)
    if "input" not in kwargs:
        kwargs.setdefault("stdin", subprocess.DEVNULL)
    env = {k: v for k, v in os.environ.items() if k != "LATTICE_ROOT"}
    return subprocess.run(
        [script, *args], cwd=root, capture_output=True, env=env, timeout=60, **kwargs
    )


# ---------------------------------------------------------------------------
# Item 5: implicit stdin for plan write / notes write
# ---------------------------------------------------------------------------


@pytest.mark.timeout(120)
@pytest.mark.skipif(sys.platform == "win32", reason="fd 0 kinds are POSIX here")
class TestImplicitStdin:
    def test_dev_null_keeps_the_error_and_names_the_command(self, board: Path) -> None:
        before = _plan(board).read_bytes()
        plain = _run(board, "plan", "write", "LOC-1", *A)  # stdin is /dev/null
        assert plain.returncode == 1
        assert plain.stderr.decode() == (
            "Error: Provide the plan as --file PATH or --stdin, for example: "
            "lattice plan write LOC-1 --stdin < plan.md\n"
        )
        as_json = _run(board, "plan", "write", "LOC-1", "--json", *A)
        assert as_json.returncode == 1
        assert json.loads(as_json.stdout)["error"]["code"] == "VALIDATION_ERROR"
        assert _plan(board).read_bytes() == before
        assert "plan_written" not in _event_types(board)

    def test_a_terminal_keeps_the_error(self, board: Path) -> None:
        import pty

        before = _plan(board).read_bytes()
        leader, follower = pty.openpty()
        try:
            result = _run(board, "plan", "write", "LOC-1", "--json", *A, stdin=follower)
        finally:
            os.close(follower)
            os.close(leader)
        assert result.returncode == 1
        error = json.loads(result.stdout)["error"]
        assert error["code"] == "VALIDATION_ERROR"
        assert "lattice plan write LOC-1 --stdin" in error["message"]
        assert _plan(board).read_bytes() == before
        assert "plan_written" not in _event_types(board)

    def test_empty_pipe_is_refused_and_the_plan_is_untouched(self, board: Path) -> None:
        assert _run(board, "plan", "write", "LOC-1", "--stdin", *A, input=b"old\n").returncode == 0
        plain = _run(board, "plan", "write", "LOC-1", *A, input=b"")
        assert plain.returncode == 1
        assert plain.stderr.decode() == ("Error: Standard input was empty; nothing was written.\n")
        as_json = _run(board, "plan", "write", "LOC-1", "--json", *A, input=b"")
        assert json.loads(as_json.stdout)["error"] == {
            "code": "VALIDATION_ERROR",
            "message": "Standard input was empty; nothing was written.",
        }
        assert _plan(board).read_bytes() == b"old\n"
        assert _event_types(board).count("plan_written") == 1

    def test_a_pipe_is_read(self, board: Path) -> None:
        result = _run(board, "plan", "write", "LOC-1", *A, input=b"plan\n")
        assert result.returncode == 0, result.stderr
        assert _plan(board).read_bytes() == b"plan\n"
        assert _event_types(board)[-1] == "plan_written"

    def test_a_regular_file_is_read(self, board: Path, tmp_path: Path) -> None:
        src = tmp_path / "plan.md"
        src.write_bytes(b"# From a file\n")
        with src.open("rb") as handle:
            result = _run(board, "plan", "write", "LOC-1", "--json", *A, stdin=handle)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["data"]["bytes"] == len(b"# From a file\n")
        assert _plan(board).read_bytes() == b"# From a file\n"

    def test_notes_write_shares_the_path(self, board: Path) -> None:
        refused = _run(board, "notes", "write", "LOC-1", *A)
        assert refused.returncode == 1
        assert "lattice notes write LOC-1 --stdin < notes.md" in refused.stderr.decode()
        empty = _run(board, "notes", "write", "LOC-1", *A, input=b"")
        assert empty.returncode == 1
        assert empty.stderr.decode() == "Error: Standard input was empty; nothing was written.\n"
        written = _run(board, "notes", "write", "LOC-1", *A, input=b"notes\n")
        assert written.returncode == 0, written.stderr
        (notes,) = (board / LATTICE_DIR / "notes").glob("task_*.md")
        assert notes.read_bytes() == b"notes\n"

    def test_explicit_sources_are_unchanged(self, board: Path, tmp_path: Path) -> None:
        src = tmp_path / "p.md"
        src.write_bytes(b"from file\n")
        # Both given: refused, even with a pipe on stdin.
        both = _run(board, "plan", "write", "LOC-1", "--file", str(src), "--stdin", *A, input=b"x")
        assert both.returncode == 1
        assert both.stderr.decode() == "Error: Provide either --file or --stdin, not both.\n"
        # --file ignores whatever is on stdin.
        by_file = _run(board, "plan", "write", "LOC-1", "--file", str(src), *A, input=b"piped\n")
        assert by_file.returncode == 0, by_file.stderr
        assert _plan(board).read_bytes() == b"from file\n"
        # --stdin reads /dev/null as empty content, as before (explicit, so it is written).
        explicit_empty = _run(board, "plan", "write", "LOC-1", "--stdin", *A)
        assert explicit_empty.returncode == 0, explicit_empty.stderr
        assert _plan(board).read_bytes() == b""


def test_the_operation_still_takes_exactly_one_source() -> None:
    """MCP and the server call the operation directly: no implicit source there."""
    from lattice.core.errors import OpError
    from lattice.ops.prose_common import check_content_sources

    with pytest.raises(OpError) as neither:
        check_content_sources(False, False, "the plan")
    assert (neither.value.code, neither.value.message) == (
        "VALIDATION_ERROR",
        "Provide the plan as --file PATH or --stdin.",
    )


# ---------------------------------------------------------------------------
# Item 4: plan show
# ---------------------------------------------------------------------------


class TestPlanShow:
    def test_same_as_the_read(self, board: Path) -> None:
        for flags in ((), ("--json",)):
            shown = run_cli(board, "plan", "show", "LOC-1", *flags)
            read = run_cli(board, "plan", "LOC-1", *flags)
            assert shown.exit_code == read.exit_code == 0
            assert shown.output == read.output

    def test_missing_plan_and_task_errors_match(self, board: Path) -> None:
        _plan(board).unlink()
        for task in ("LOC-1", "LOC-9"):
            shown = run_cli(board, "plan", "show", task, "--json")
            read = run_cli(board, "plan", task, "--json")
            assert shown.exit_code == read.exit_code == 1
            assert shown.output == read.output

    def test_needs_a_task(self, board: Path) -> None:
        result = run_cli(board, "plan", "show")
        assert result.exit_code == 2
        assert "Missing argument 'TASK_ID'" in result.output


# ---------------------------------------------------------------------------
# Item 1: hints name the invoked program
# ---------------------------------------------------------------------------


def _status(root: Path, prog: str | None, *args: str):  # noqa: ANN202
    kwargs = {"prog_name": prog} if prog else {}
    previous = Path.cwd()
    os.chdir(root)
    try:
        return CliRunner().invoke(cli, ["status", "LOC-1", *args, *A], **kwargs)
    finally:
        os.chdir(previous)


def test_hints_name_the_alias(board: Path) -> None:
    assert _status(board, "lattice-v2", "in_planning").exit_code == 0
    planned = _status(board, "lattice-v2", "planned", "--no-auto-review")
    assert "Next: run 'lattice-v2 plan-review LOC-1'" in planned.output
    assert "'lattice plan-review" not in planned.output
    reviewed = _status(board, "lattice-v2", "review", "--force", "--reason", "r", "--json")
    steps = json.loads(reviewed.output)["data"]["next_steps"]
    assert steps["command"] == "lattice-v2 code-review LOC-1"
    validated = _status(board, "lattice-v2", "in_validation", "--force", "--reason", "r", "--json")
    steps = json.loads(validated.output)["data"]["next_steps"]
    assert steps["evidence"] == "lattice-v2 attach LOC-1 --role validation"


def test_under_the_name_lattice_nothing_changes(tmp_path: Path) -> None:
    outputs = []
    for prog in (None, "lattice"):
        root = tmp_path / (prog or "default")
        root.mkdir()
        create_board(root, project_code="LOC", actor="human:a")
        assert run_cli(root, "create", "One", *A).exit_code == 0
        assert _status(root, prog, "in_planning").exit_code == 0
        planned = _status(root, prog, "planned", "--no-auto-review")
        assert "Next: run 'lattice plan-review LOC-1'" in planned.output
        outputs.append(planned.output)
    assert outputs[0] == outputs[1]


@pytest.mark.timeout(120)
@pytest.mark.skipif(sys.platform == "win32", reason="symlinked console script")
def test_a_symlinked_alias_names_itself(board: Path) -> None:
    assert _run(board, "status", "LOC-1", "in_planning", *A, prog="lattice-v2").returncode == 0
    planned = _run(board, "status", "LOC-1", "planned", "--no-auto-review", *A, prog="lattice-v2")
    assert planned.returncode == 0, planned.stderr
    assert "Next: run 'lattice-v2 plan-review LOC-1'" in planned.stdout.decode()
    refused = _run(board, "plan", "write", "LOC-1", *A, prog="lattice-v2")
    assert "lattice-v2 plan write LOC-1 --stdin" in refused.stderr.decode()


@pytest.mark.parametrize(
    ("info_name", "expected"),
    [
        ("lattice", "lattice"),
        ("lattice-v2", "lattice-v2"),
        ("lattice.exe", "lattice"),
        ("cli", "lattice"),
        ("-c", "lattice"),
        (None, "lattice"),
    ],
)
def test_program_name(info_name: str | None, expected: str) -> None:
    import click

    from lattice.cli.helpers import program_name

    with click.Context(cli, info_name=info_name):
        assert program_name() == expected


def test_program_name_outside_a_command() -> None:
    from lattice.cli.helpers import program_name

    assert program_name() == "lattice"


# ---------------------------------------------------------------------------
# Item 3: show's plan line on a bound checkout
# ---------------------------------------------------------------------------


def test_local_show_still_names_the_scaffold(board: Path) -> None:
    lines = run_cli(board, "show", "LOC-1").output.splitlines()
    assert any(line.startswith("Plan: plans/task_") for line in lines)
    assert not any("none yet" in line for line in lines)


def test_hosted_show_names_the_plan_once_written(
    hosted_env: HostedEnv,  # noqa: F811 - the fixture imported above
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path / "repo")
    assert run_cli(repo, "remote", "attach", "team", "demo").exit_code == 0
    created = run_cli(repo, "create", "Hosted", "--description", "Why.", "--json", *A)
    task_id = json.loads(created.stdout)["data"]["id"]

    before = run_cli(repo, "show", "DEM-1").stdout.splitlines()
    assert "Plan: none yet (lattice plan write DEM-1 --stdin)" in before
    assert not any(line.startswith("Plan: plans/") for line in before)
    previous = Path.cwd()
    os.chdir(repo)
    try:
        aliased = CliRunner().invoke(cli, ["show", "DEM-1"], prog_name="lattice-v2")
    finally:
        os.chdir(previous)
    assert "Plan: none yet (lattice-v2 plan write DEM-1 --stdin)" in aliased.stdout
    # --json is unchanged: plan_path names the file that exists.
    as_json = json.loads(run_cli(repo, "show", "DEM-1", "--json").stdout)["data"]
    assert as_json["plan_path"] == f"plans/{task_id}.md"

    # A heading alone is scaffold-shaped, but it was written: the task has a plan.
    assert run_cli(repo, "plan", "write", "DEM-1", "--stdin", *A, input="# Plan\n").exit_code == 0
    after = run_cli(repo, "show", "DEM-1").stdout.splitlines()
    assert f"Plan: plans/{task_id}.md" in after
    assert not any("none yet" in line for line in after)


def test_plan_written_without_an_event(tmp_path: Path) -> None:
    """A plan that arrived without a ``plan_written`` event (a board moved to a
    server with its files) counts once it is more than the scaffold."""
    from lattice.cli.query_cmds import _plan_written

    plan = tmp_path / "p.md"
    snapshot = {"description": "Why."}
    assert not _plan_written(plan, snapshot, [])
    plan.write_text("# DEM-1: T\n\nWhy.\n")
    assert not _plan_written(plan, snapshot, [])
    assert _plan_written(plan, snapshot, [{"type": "plan_written"}])
    plan.write_text("# DEM-1: T\n\n- step one\n")
    assert _plan_written(plan, snapshot, [])


# ---------------------------------------------------------------------------
# Item 7: the origin line labels the token's user
# ---------------------------------------------------------------------------


def _origin_event(actor: str, origin: dict) -> dict:
    return {"type": "comment_added", "actor": actor, "data": {}, "origin": origin}


REPORTED = {"os_user": "alice", "host": "lap", "worktree": "/w", "branch": "b"}
TOKEN = {"user": "human:alice", "machine": "laptop"}


@pytest.mark.parametrize(
    ("actor", "origin", "expected"),
    [
        (
            "agent:x",
            {"reported": REPORTED, "authenticated": TOKEN},
            "agent:x · via token human:alice@laptop · /w (b)",
        ),
        (
            "human:alice",
            {"reported": REPORTED, "authenticated": TOKEN},
            "human:alice · human:alice@laptop · /w (b)",
        ),
        (
            "human:alice",
            {"reported": {"source": "browser"}, "authenticated": TOKEN},
            "human:alice · human:alice@laptop · browser",
        ),
        ("agent:x", {"reported": REPORTED}, "agent:x · alice@lap · /w (b)"),
    ],
)
def test_origin_line(actor: str, origin: dict, expected: str) -> None:
    assert format_origin_line(_origin_event(actor, origin)) == expected


def test_hosted_show_labels_the_token_user(
    hosted_env: HostedEnv,  # noqa: F811 - the fixture imported above
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path / "repo")
    assert run_cli(repo, "remote", "attach", "team", "demo").exit_code == 0
    assert run_cli(repo, "create", "Two actors", *A).exit_code == 0
    assert run_cli(repo, "comment", "DEM-1", "mine", "--actor", "human:alice").exit_code == 0
    lines = run_cli(repo, "show", "DEM-1").stdout.splitlines()
    worktree = f"{repo.resolve()} (main)"
    created = lines.index(next(line for line in lines if "task_created" in line))
    commented = lines.index(next(line for line in lines if "comment_added" in line))
    machine = lines[created + 1].split("@", 1)[1].split(" · ", 1)[0]
    assert lines[created + 1] == f"    agent:t · via token human:alice@{machine} · {worktree}"
    assert lines[commented + 1] == f"    human:alice · human:alice@{machine} · {worktree}"
