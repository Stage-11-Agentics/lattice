"""SPEC §4 on the hosted paths that route on their own (``lattice sync``,
``lattice remote status`` / ``op-status``, ``lattice cache clear``) and on a
routing error that quotes the committed binding: plain output shows other
people's control characters as U+FFFD on stdout and stderr; ``--json`` is
unchanged (post-merge review round 1, finding 2). Local output is unchanged
(``tests/test_cli/test_show_origin.py::test_local_plain_output_keeps_control_characters``)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lattice.core.errors import OpError
from lattice.core.ids import generate_op_id
from tests.test_remote.hosted import HostedEnv, make_repo, run_cli

HOSTILE = "evil \x1b]0;pwned\x07 text \x9b2J end"
SCRUBBED = "evil �]0;pwned� text �2J end"
RAW = ("\x1b", "\x07", "\x9b")


@pytest.fixture()
def repo(hosted_env: HostedEnv, tmp_path: Path) -> Path:
    repo = make_repo(tmp_path / "repo")
    assert run_cli(repo, "remote", "attach", "team", "demo").exit_code == 0
    return repo


def _hostile(*args: object, **kwargs: object) -> object:
    raise OpError("FORBIDDEN", HOSTILE)


def _assert_scrubbed(text: str) -> None:
    assert SCRUBBED in text, text
    for raw in RAW:
        assert raw not in text


def test_sync_scrubs_a_server_supplied_error(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lattice.server import app as server_app

    monkeypatch.setattr(server_app, "fast_path_body", _hostile)
    plain = run_cli(repo, "sync", color=True)
    assert plain.exit_code == 1, plain.output
    _assert_scrubbed(plain.stderr)
    as_json = run_cli(repo, "sync", "--json", color=True)
    assert json.loads(as_json.stdout)["error"]["message"] == HOSTILE


def test_remote_status_scrubs_a_server_supplied_error(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lattice.server import app as server_app

    monkeypatch.setattr(server_app, "_registered_operations", _hostile)
    plain = run_cli(repo, "remote", "status", color=True)
    assert plain.exit_code == 0, plain.output
    _assert_scrubbed(plain.stdout)
    as_json = run_cli(repo, "remote", "status", "--json", color=True)
    assert json.loads(as_json.stdout)["data"]["error"] == f"FORBIDDEN: {HOSTILE}"


def test_remote_op_status_scrubs_a_server_supplied_error(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lattice.server.project import Project

    monkeypatch.setattr(Project, "op_status", _hostile)
    op_id = generate_op_id()
    plain = run_cli(repo, "remote", "op-status", op_id, color=True)
    assert plain.exit_code == 1, plain.output
    _assert_scrubbed(plain.stderr)
    as_json = run_cli(repo, "remote", "op-status", op_id, "--json", color=True)
    assert json.loads(as_json.stdout)["error"]["message"] == HOSTILE


def test_a_routing_error_quoting_a_hostile_binding_is_scrubbed(
    hosted_env: HostedEnv, tmp_path: Path
) -> None:
    repo = make_repo(tmp_path / "hostile-binding")
    alias = "te\x1b]0;pwned\x07am"
    (repo / ".lattice-remote.json").write_text(json.dumps({"remote": alias, "project": "demo"}))
    for argv in (
        ("list",),
        ("create", "x", "--actor", "agent:a"),
        ("sync",),
        ("remote", "status"),
    ):
        plain = run_cli(repo, *argv, color=True)
        assert plain.exit_code == 1, (argv, plain.output)
        assert "REMOTE_NOT_CONFIGURED" not in plain.stderr  # plain mode prints the message
        assert "te�]0;pwned�am" in plain.stderr, (argv, plain.stderr)
        for raw in RAW:
            assert raw not in plain.stderr
    as_json = run_cli(repo, "list", "--json", color=True)
    error = json.loads(as_json.stdout)["error"]
    assert error["code"] == "REMOTE_NOT_CONFIGURED" and alias in error["message"]


def test_cache_clear_scrubs_the_names_it_prints(repo: Path) -> None:
    state = repo / ".lattice" / "cache" / "state.json"
    data = json.loads(state.read_text())
    (repo / ".lattice-remote.json").unlink()  # route by the marker alone
    state.chmod(0o600)
    state.write_text(json.dumps({**data, "project": "de\x1b[2Jmo"}))
    plain = run_cli(repo, "cache", "clear", color=True)
    assert plain.exit_code == 0, plain.output
    assert "de�[2Jmo" in plain.stdout
    assert "\x1b" not in plain.stdout
