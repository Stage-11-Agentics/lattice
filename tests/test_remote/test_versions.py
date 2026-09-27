"""AC-48 (client side): protocol and version skew between a hosted client and
its server (SPEC §15)."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from lattice import __version__
from lattice.boards import resolve_board
from lattice.core import tasks as core_tasks
from lattice.core.errors import OpError
from lattice.ops import get_operation
from lattice.remote import session
from tests.test_remote.hosted import HostedEnv, make_repo, run_cli


@pytest.fixture()
def repo(hosted_env: HostedEnv, tmp_path: Path) -> Path:
    repo = make_repo(tmp_path / "repo")
    assert run_cli(repo, "remote", "attach", "team", "demo").exit_code == 0
    assert run_cli(repo, "create", "Versioned", "--actor", "human:alice").exit_code == 0
    return repo


def _notices(stderr: str) -> list[str]:
    return [line for line in stderr.splitlines() if line.startswith("lattice: ")]


def _task_count(env: HostedEnv) -> int:
    return len(list((env.board / "tasks").glob("*.json")))


def test_client_refuses_to_write_to_another_protocol(
    hosted_env: HostedEnv, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lattice.server import app as server_app

    info_path = repo / ".lattice" / "cache" / "server_info.json"
    info = json.loads(info_path.read_text())
    assert info["protocol"] == 1
    monkeypatch.setattr(server_app, "PROTOCOL", 2)
    info_path.unlink()
    before = _task_count(hosted_env)
    result = run_cli(repo, "create", "Refused", "--actor", "human:alice", "--json")
    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["error"]["code"] == "PROTOCOL_MISMATCH"
    assert _task_count(hosted_env) == before


def test_default_parameters_are_omitted_and_a_new_one_is_unsupported(
    hosted_env: HostedEnv, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A newer client whose ``task.comment`` declares an extra option works
    against this server until the option is used."""
    base = get_operation("task.comment").Params

    @dataclasses.dataclass(frozen=True)
    class NewerParams(base):  # type: ignore[misc, valid-type]
        shout: bool = False

    monkeypatch.chdir(repo)
    board = resolve_board()
    fields = {f.name: getattr(f, "default", None) for f in dataclasses.fields(base) if f.init}
    params = {k: v for k, v in fields.items() if v is not dataclasses.MISSING}
    params.update(task="DEM-1", text="quietly")
    from lattice.ops import Caller

    result = board.execute("task.comment", NewerParams(**params), Caller(actor="human:alice"))
    assert result.events[-1]["type"] == "comment_added"
    with pytest.raises(OpError) as exc:
        board.execute(
            "task.comment",
            NewerParams(**{**params, "shout": True}),
            Caller(actor="human:alice"),
        )
    assert exc.value.code == "UNSUPPORTED_PARAM"
    assert "shout" in exc.value.message


def test_a_client_below_the_minimum_is_told_on_reads_and_refused_on_writes(
    hosted_env: HostedEnv, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lattice.server import app as server_app

    monkeypatch.setattr(server_app, "MIN_CLIENT_VERSION", "99.0.0")
    hosted_env.handle.state.version = "99.1.0"
    read = run_cli(repo, "show", "DEM-1")
    assert read.exit_code == 0, read.output
    notices = _notices(read.stderr)
    assert notices == [
        f"lattice: this client ({__version__}) is older than the server's minimum (99.0.0); "
        "upgrade Lattice"
    ]
    write = run_cli(repo, "comment", "DEM-1", "hello", "--actor", "human:alice", "--json")
    assert write.exit_code == 1
    assert json.loads(write.stdout)["error"]["code"] == "CLIENT_TOO_OLD"


def test_an_older_client_prints_one_upgrade_line_and_filters_known_types(
    hosted_env: HostedEnv,
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    hosted_env.handle.state.version = "99.1.0"
    from lattice.server import app as server_app

    registered = sorted(server_app.BUILTIN_EVENT_TYPES)
    monkeypatch.setattr(server_app, "BUILTIN_EVENT_TYPES", {*registered, "future_judgment"})
    read = run_cli(repo, "list")
    assert read.exit_code == 0, read.output
    assert _notices(read.stderr) == [
        f"lattice: server runs Lattice 99.1.0, this client {__version__}; "
        "upgrade to read every event type"
    ]
    info = json.loads((repo / ".lattice" / "cache" / "server_info.json").read_text())
    assert info["version"] == "99.1.0" and "future_judgment" in info["event_types"]

    # Within the command: a type the server registers is not warned about; a
    # type nobody registers still is.
    hosted = session.Hosted(repo, "team", "demo")
    session.announce_versions(hosted)
    capsys.readouterr()
    core_tasks._unknown_type_reporter("future_judgment")
    core_tasks._unknown_type_reporter("nobody_knows")
    err = capsys.readouterr().err
    assert "future_judgment" not in err
    assert "nobody_knows" in err
    session.reset_process_state()

    again = run_cli(repo, "list")
    assert len(_notices(again.stderr)) == 1  # one line per command


def test_same_version_prints_nothing(hosted_env: HostedEnv, repo: Path) -> None:
    read = run_cli(repo, "list")
    assert read.exit_code == 0
    assert _notices(read.stderr) == []
